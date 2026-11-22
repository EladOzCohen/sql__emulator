"""BigQuery (GoogleSQL) arrays, STRUCT, UNNEST and GENERATE_*_ARRAY on DuckDB.

* TEMP macros (PRELUDE): ARRAY_FIRST/LAST/SLICE/TO_STRING, GENERATE_ARRAY and
  the `_bqa_*` helpers behind subscripts and date/timestamp generators.
* HANDLERS: ARRAY(SELECT ...) (keeps the row order of UNION ALL branches),
  STRUCT(1 AS a, ...) literals, GENERATE_DATE_ARRAY / GENERATE_TIMESTAMP_ARRAY
  (INTERVAL argument).
* PRE_RULES: `SELECT AS STRUCT ...` / `SELECT AS VALUE ...`,
  `arr[OFFSET(i)]` / ORDINAL / SAFE_OFFSET / SAFE_ORDINAL subscripts,
  `UNNEST(arr) AS x [WITH OFFSET [AS off]]` aliasing.
"""

from __future__ import annotations

import re

from .. import translate as tr
from . import helpers as H  # noqa: F401


def _macro(name: str, *defs: str) -> str:
    return f"CREATE OR REPLACE TEMP MACRO {name}" + ", ".join(defs)


_NN = "a IS NULL OR b IS NULL"

PRELUDE: list[str] = [
    # --- simple array functions ---------------------------------------------
    _macro("array_reverse", "(a) AS list_transform(range(len(a), 0, -1), _i -> list_extract(a, _i))"),
    _macro("array_first", "(a) AS list_extract(a, 1)"),
    _macro("array_last", "(a) AS list_extract(a, -1)"),
    _macro("_bqa_lo", "(a, s) AS greatest(CASE WHEN s < 0 THEN len(a) + s ELSE s END, 0)"),
    _macro("_bqa_hi", "(a, e) AS least(CASE WHEN e < 0 THEN len(a) + e ELSE e END, len(a) - 1)"),
    _macro("array_slice",
           "(a, s, e) AS CASE WHEN a IS NULL OR s IS NULL OR e IS NULL THEN NULL "
           "WHEN _bqa_hi(a, e) < 0 OR _bqa_lo(a, s) > _bqa_hi(a, e) THEN list_slice(a, 1, 0) "
           "ELSE list_slice(a, _bqa_lo(a, s) + 1, _bqa_hi(a, e) + 1) END"),
    _macro("array_to_string",
           "(a, d) AS CASE WHEN a IS NULL OR d IS NULL THEN NULL ELSE coalesce("
           "list_aggregate(list_transform(list_filter(a, x -> x IS NOT NULL), x -> CAST(x AS VARCHAR)), "
           "'string_agg', CAST(d AS VARCHAR)), '') END",
           "(a, d, n) AS CASE WHEN a IS NULL OR d IS NULL THEN NULL ELSE coalesce("
           "list_aggregate(list_transform(a, x -> coalesce(CAST(x AS VARCHAR), CAST(n AS VARCHAR))), "
           "'string_agg', CAST(d AS VARCHAR)), '') END"),
    # --- generators -----------------------------------------------------------
    _macro("_bqa_gen",
           "(a, b, s) AS CASE WHEN a IS NULL OR b IS NULL OR s IS NULL THEN NULL ELSE "
           "list_transform(range(0, CAST(floor((b - a) / s) AS BIGINT) + 1), _i -> a + _i * s) END"),
    _macro("generate_array", "(a, b) AS _bqa_gen(a, b, 1)", "(a, b, s) AS _bqa_gen(a, b, s)"),
    _macro("_bqa_gen_date",
           "(a, b, iv) AS CASE WHEN a IS NULL OR b IS NULL THEN NULL ELSE "
           "list_transform(generate_series(CAST(a AS TIMESTAMP), CAST(b AS TIMESTAMP), iv), "
           "_t -> CAST(_t AS DATE)) END"),
    _macro("_bqa_gen_ts",
           "(a, b, iv) AS CASE WHEN a IS NULL OR b IS NULL THEN NULL ELSE "
           "list_transform(generate_series(CAST(a AS TIMESTAMP), CAST(b AS TIMESTAMP), iv), "
           "_t -> CAST(_t AS TIMESTAMPTZ)) END"),
    # --- subscripts: OFFSET / ORDINAL are strict, SAFE_* give NULL ------------
    _macro("_bqa_off",
           "(a, i) AS CASE WHEN i < 0 OR i >= len(a) THEN error('Array index ' || i || ' is out of bounds') "
           "ELSE list_extract(a, i + 1) END"),
    _macro("_bqa_ord",
           "(a, i) AS CASE WHEN i < 1 OR i > len(a) THEN error('Array index ' || i || ' is out of bounds') "
           "ELSE list_extract(a, i) END"),
    _macro("_bqa_soff", "(a, i) AS CASE WHEN i < 0 OR i >= len(a) THEN NULL ELSE list_extract(a, i + 1) END"),
    _macro("_bqa_sord", "(a, i) AS CASE WHEN i < 1 OR i > len(a) THEN NULL ELSE list_extract(a, i) END"),
]


# --------------------------------------------------------------------------- #
#  Handlers
# --------------------------------------------------------------------------- #
_INTERVAL = re.compile(r"(?is)^INTERVAL\s+(.+?)\s+([A-Za-z_]+)$")


def _interval(text: str) -> str:
    m = _INTERVAL.match(text.strip())
    if not m:
        return text
    return f"INTERVAL ({m.group(1)}) {m.group(2).upper()}"


def h_gen_date(args):
    if len(args) not in (2, 3):
        return None
    iv = _interval(args[2].text) if len(args) == 3 else "INTERVAL 1 DAY"
    return f"_bqa_gen_date({args[0].text}, {args[1].text}, {iv})"


def h_gen_ts(args):
    if len(args) != 3:
        return None
    return f"_bqa_gen_ts({args[0].text}, {args[1].text}, {_interval(args[2].text)})"


def _split_alias(arg_toks: list[tr.Tok]):
    """(expr tokens, alias or None) for `expr AS name`."""
    s = tr.strip(arg_toks)
    sig = [k for k, t in enumerate(s) if not t.kind in (tr.WS, tr.COMMENT)]
    if len(sig) >= 3 and s[sig[-2]].is_word("AS") and s[sig[-1]].kind == tr.WORD:
        return s[:sig[-2]], s[sig[-1]].value or s[sig[-1]].text
    return s, None


def _implicit_name(expr: list[tr.Tok]):
    sig = [t for t in expr if t.kind not in (tr.WS, tr.COMMENT)]
    if sig and len(sig) % 2 == 1 and all(
            (t.kind == tr.WORD) if i % 2 == 0 else t.is_punct(".") for i, t in enumerate(sig)):
        last = sig[-1]
        return (last.value or last.text).strip('"')
    return None


def _struct_text(parts: list[tuple[list[tr.Tok], str | None]]) -> str:
    named = [(e, a or _implicit_name(e)) for e, a in parts]
    if all(a is None for _, a in named):
        return "row(" + ", ".join(tr.text_of(e) for e, _ in named) + ")"
    fields = []
    for i, (e, a) in enumerate(named, 1):
        fields.append(f"{tr.quote_ident(a or f'_field_{i}')} := {tr.text_of(e)}")
    return "struct_pack(" + ", ".join(fields) + ")"


def h_struct(args):
    if not args:
        return None
    return _struct_text([_split_alias(a.toks) for a in args])


def _split_union_all(toks: list[tr.Tok]):
    """Branches of a top-level UNION ALL chain, or None if other set operators occur."""
    branches, cur, depth = [], [], 0
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        if depth == 0 and t.is_word("UNION"):
            n = tr.next_sig(toks, i)
            if n < len(toks) and toks[n].is_word("ALL"):
                branches.append(cur)
                cur = []
                i = n + 1
                continue
            return None
        if depth == 0 and t.is_word("INTERSECT", "EXCEPT"):
            return None
        cur.append(t)
        i += 1
    branches.append(cur)
    return branches


def h_array(args):
    """ARRAY(SELECT ...): collect in UNION ALL branch order, [] (not NULL) when empty."""
    if len(args) != 1:
        return None
    body = tr.strip(args[0].toks)
    if not body or not body[0].is_word("SELECT", "WITH"):
        return None
    branches = _split_union_all(body)
    if branches is None or any(not tr.strip(b) for b in branches):
        return f"(SELECT coalesce(list(_v), []) FROM ({tr.text_of(body)}) AS _q(_v))"
    parts = []
    for k, b in enumerate(branches):
        parts.append(f"SELECT _v, {k} AS _b, row_number() OVER () AS _r "
                     f"FROM ({tr.text_of(b)}) AS _q{k}(_v)")
    return ("(SELECT coalesce(list(_v ORDER BY _b, _r), []) FROM ("
            + " UNION ALL ".join(parts) + ") AS _qq)")


HANDLERS: dict = {
    "ARRAY": h_array,
    "STRUCT": h_struct,
    "GENERATE_DATE_ARRAY": h_gen_date,
    "GENERATE_TIMESTAMP_ARRAY": h_gen_ts,
}


# --------------------------------------------------------------------------- #
#  Rules
# --------------------------------------------------------------------------- #
_SELECT_END = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION", "INTERSECT",
               "EXCEPT", "WINDOW", "QUALIFY"}


def select_as(toks):
    """SELECT AS STRUCT a, b AS c  ->  SELECT struct_pack(..);  SELECT AS VALUE x -> SELECT x."""
    for _ in range(200):
        for i, t in enumerate(toks):
            if not t.is_word("SELECT"):
                continue
            a = tr.next_sig(toks, i)
            b = tr.next_sig(toks, a) if a < len(toks) else len(toks)
            if not (a < len(toks) and b < len(toks) and toks[a].is_word("AS")
                    and toks[b].is_word("STRUCT", "VALUE")):
                continue
            start = b + 1
            depth, end, items, cur = 0, len(toks), [], []
            for k in range(start, len(toks)):
                x = toks[k]
                if x.kind == tr.PUNCT and x.text in "([{":
                    depth += 1
                elif x.kind == tr.PUNCT and x.text in ")]}":
                    depth -= 1
                    if depth < 0:
                        end = k
                        break
                if depth == 0 and (x.is_punct(";") or (x.kind == tr.WORD and x.upper in _SELECT_END)):
                    end = k
                    break
                if depth == 0 and x.is_punct(","):
                    items.append(cur)
                    cur = []
                else:
                    cur.append(x)
            items.append(cur)
            if toks[b].is_word("VALUE"):
                new = tr.text_of(items[0]) if len(items) == 1 else None
            else:
                new = _struct_text([_split_alias(it) for it in items])
            if new is None:
                continue
            toks = (toks[:i + 1] + tr.tokenize_plain(" " + new + " ") + toks[end:])
            break
        else:
            return toks
    return toks


_SUBSCRIPT = {"OFFSET": "_bqa_off", "ORDINAL": "_bqa_ord",
              "SAFE_OFFSET": "_bqa_soff", "SAFE_ORDINAL": "_bqa_sord"}


_KEYWORD_BEFORE = tr._NOT_FUNCTIONS | {"ARRAY", "CASE", "RETURN", "DEFAULT", "VALUES", "OVER", "BY"}


def _is_operand_end(toks, i) -> bool:
    p = tr.prev_sig(toks, i)
    if p < 0:
        return False
    t = toks[p]
    if t.kind == tr.PUNCT:
        return t.text in ")]}"
    return t.kind == tr.WORD and t.upper not in _KEYWORD_BEFORE


def plain_index(toks):
    """expr[i] (0-based, GoogleSQL shorthand for OFFSET) -> _bqa_off(expr, i)."""
    for _ in range(500):
        for i, t in enumerate(toks):
            if not (t.is_punct("[") and _is_operand_end(toks, i)):
                continue
            close = tr.match_paren(toks, i)
            if close < 0:
                continue
            inner = toks[i + 1:close]
            sig = tr.strip(inner)
            if (not sig or sig[0].kind == tr.STR or any(x.is_punct(":", ",") for x in sig)
                    or sig[0].is_word("OFFSET", "ORDINAL", "SAFE_OFFSET", "SAFE_ORDINAL")):
                continue
            n = tr.next_sig(toks, close)
            n2 = tr.next_sig(toks, n) if n < len(toks) else len(toks)
            if n2 < len(toks) and toks[n].is_punct("[") and toks[n2].kind == tr.STR:
                continue                        # JSON path: j[0]['key'] stays DuckDB's
            st = tr.primary_start(toks, i)
            if st < 0:
                continue
            call = f"_bqa_off({tr.text_of(toks[st:i])}, {tr.text_of(inner)})"
            toks = toks[:st] + tr.tokenize_plain(call) + toks[close + 1:]
            break
        else:
            return toks
    return toks


def subscripts(toks):
    """expr[OFFSET(i)] -> _bqa_off(expr, i) (and ORDINAL / SAFE_OFFSET / SAFE_ORDINAL)."""
    for _ in range(500):
        for i, t in enumerate(toks):
            if not t.is_punct("["):
                continue
            w = tr.next_sig(toks, i)
            if not (w < len(toks) and toks[w].kind == tr.WORD and toks[w].upper in _SUBSCRIPT):
                continue
            p = tr.next_sig(toks, w)
            if p >= len(toks) or not toks[p].is_punct("("):
                continue
            close = tr.match_paren(toks, p)
            rb = tr.next_sig(toks, close) if close > 0 else len(toks)
            if close < 0 or rb >= len(toks) or not toks[rb].is_punct("]"):
                continue
            st = tr.primary_start(toks, i)
            if st < 0:
                continue
            operand = tr.text_of(toks[st:i])
            idx = tr.text_of(toks[p + 1:close])
            call = f"{_SUBSCRIPT[toks[w].upper]}({operand}, {idx})"
            toks = toks[:st] + tr.tokenize_plain(call) + toks[rb + 1:]
            break
        else:
            return toks
    return toks


_NOT_ALIAS = {"WHERE", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "CROSS", "FULL", "OUTER", "GROUP",
              "ORDER", "LIMIT", "HAVING", "UNION", "INTERSECT", "EXCEPT", "QUALIFY", "WINDOW",
              "USING", "SELECT", "FROM", "WITH", "NATURAL", "LATERAL", "TABLESAMPLE", "OFFSET"}


def _table_context(toks, i) -> bool:
    depth = 0
    for k in range(i - 1, -1, -1):
        t = toks[k]
        if t.kind == tr.PUNCT and t.text in ")]}":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in "([{":
            if depth == 0:
                return False
            depth -= 1
        elif depth == 0 and t.kind == tr.WORD:
            u = t.upper
            if u in ("FROM", "JOIN"):
                return True
            if u in ("SELECT", "WHERE", "GROUP", "ORDER", "HAVING", "QUALIFY", "LIMIT", "WINDOW"):
                return False
    return False


def _alias_after(toks, j):
    """(alias token index or None) for an optional `[AS] name` at toks[j]."""
    if j < len(toks) and toks[j].is_word("AS"):
        k = tr.next_sig(toks, j)
        if k < len(toks) and toks[k].kind == tr.WORD:
            return k
        return None
    if j < len(toks) and toks[j].kind == tr.WORD and toks[j].upper not in _NOT_ALIAS:
        return j
    return None


def unnest_alias(toks):
    """FROM UNNEST(e) AS x [WITH OFFSET [AS o]] -> a table alias DuckDB understands."""
    out: list[tr.Tok] = []
    i, n, counter = 0, len(toks), 0
    while i < n:
        t = toks[i]
        if t.is_word("UNNEST"):
            p = tr.next_sig(toks, i)
            if p < n and toks[p].is_punct("(") and _table_context(toks, i):
                close = tr.match_paren(toks, p)
                if close > 0:
                    j = tr.next_sig(toks, close)
                    a = _alias_after(toks, j)
                    end = a if a is not None else close
                    elem = toks[a].text if a is not None else None
                    w = tr.next_sig(toks, end)
                    off_alias, with_off = None, False
                    if w < n and toks[w].is_word("WITH"):
                        o = tr.next_sig(toks, w)
                        if o < n and toks[o].is_word("OFFSET"):
                            with_off = True
                            end = o
                            q = tr.next_sig(toks, o)
                            oa = _alias_after(toks, q)
                            if oa is not None:
                                off_alias, end = toks[oa].text, oa
                    inner = tr.text_of(toks[p + 1:close])
                    counter += 1
                    if with_off:
                        text = (f"(SELECT _v AS {elem or '_bq_value'}, _o - 1 AS {off_alias or chr(34) + 'offset' + chr(34)} "
                                f"FROM UNNEST({inner}) WITH ORDINALITY AS _w(_v, _o)) AS _bqu{counter}")
                    elif elem is not None:
                        text = f"UNNEST({inner}) AS _bqu{counter}({elem})"
                    else:
                        out.append(t)
                        i += 1
                        continue
                    out.extend(tr.tokenize_plain(text))
                    i = end + 1
                    continue
        out.append(t)
        i += 1
    return out


def offset_column(toks):
    """A bare `OFFSET` used as the default WITH OFFSET column (ORDER BY OFFSET, ...)."""
    if not any(t.is_word("OFFSET") for t in toks) or '"offset"' not in "".join(t.text for t in toks):
        return toks
    out = list(toks)
    for i, t in enumerate(out):
        if not t.is_word("OFFSET"):
            continue
        p, n = tr.prev_sig(out, i), tr.next_sig(out, i)
        prev_ok = p >= 0 and (out[p].is_word("BY") or out[p].is_punct(",", "(", "+", "-", "*", "/", "=", "<", ">"))
        next_ok = n >= len(out) or out[n].is_punct(")", ",", ";", "+", "-", "*", "/", "=", "<", ">")             or out[n].is_word("ASC", "DESC", "AS", "FROM")
        if prev_ok and (n >= len(out) or not out[n].kind == tr.NUM):
            out[i] = tr.Tok(tr.WORD, '"offset"')
        elif next_ok and p >= 0 and out[p].is_word("BY", "SELECT"):
            out[i] = tr.Tok(tr.WORD, '"offset"')
    return out


PRE_RULES: list = [select_as, subscripts, plain_index, unnest_alias, offset_column]
POST_RULES: list = []


def register(con) -> None:
    pass
