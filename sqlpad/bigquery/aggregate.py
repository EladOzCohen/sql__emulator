"""BigQuery aggregate / window functions and operators on DuckDB.

* aggregate modifiers (DISTINCT, IGNORE NULLS, WHERE, HAVING MAX/MIN, ORDER BY,
  LIMIT) are parsed from the call arguments by HANDLERS and emitted as DuckDB
  aggregate syntax (FILTER, ordered aggregates, list slicing);
* operators with no DuckDB spelling (IS [NOT] DISTINCT FROM kept away from the
  column-naming pass, LIKE ANY/SOME/ALL, IN UNNEST, ^ as XOR, COLLATE) are
  rewritten by token rules into TEMP macros / UDFs.

Nothing here defines a macro named like a DuckDB aggregate / window function.
"""

from __future__ import annotations

import re
import unicodedata

from .. import translate as tr
from . import helpers as H

# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO _bq_isdf(a, b) AS (a IS DISTINCT FROM b)",
    "CREATE OR REPLACE TEMP MACRO _bq_isndf(a, b) AS (a IS NOT DISTINCT FROM b)",
    # three-valued ANY / ALL over a list of booleans
    "CREATE OR REPLACE TEMP MACRO _bq_any3(t) AS CASE "
    "WHEN t IS NULL OR len(t) = 0 THEN FALSE "
    "WHEN list_contains(t, TRUE) THEN TRUE "
    "WHEN list_contains(list_transform(t, _v -> _v IS NULL), TRUE) THEN NULL ELSE FALSE END",
    "CREATE OR REPLACE TEMP MACRO _bq_all3(t) AS CASE "
    "WHEN t IS NULL OR len(t) = 0 THEN TRUE "
    "WHEN list_contains(t, FALSE) THEN FALSE "
    "WHEN list_contains(list_transform(t, _v -> _v IS NULL), TRUE) THEN NULL ELSE TRUE END",
    "CREATE OR REPLACE TEMP MACRO _bq_like_any_arr(x, arr) AS "
    "_bq_any3(list_transform(arr, _p -> x LIKE _p))",
    "CREATE OR REPLACE TEMP MACRO _bq_like_all_arr(x, arr) AS "
    "_bq_all3(list_transform(arr, _p -> x LIKE _p))",
    "CREATE OR REPLACE TEMP MACRO _bq_nlike_any_arr(x, arr) AS "
    "_bq_any3(list_transform(arr, _p -> x NOT LIKE _p))",
    "CREATE OR REPLACE TEMP MACRO _bq_nlike_all_arr(x, arr) AS "
    "_bq_all3(list_transform(arr, _p -> x NOT LIKE _p))",
    # x IN UNNEST(arr)
    "CREATE OR REPLACE TEMP MACRO _bq_in_arr(x, arr) AS CASE "
    "WHEN arr IS NULL OR len(arr) = 0 THEN FALSE "
    "WHEN x IS NULL THEN NULL "
    "WHEN list_contains(arr, x) THEN TRUE "
    "WHEN list_contains(list_transform(arr, _e -> _e IS NULL), TRUE) THEN NULL ELSE FALSE END",
]



# --------------------------------------------------------------------------- #
#  Python helpers
# --------------------------------------------------------------------------- #
def _ci_fold(s: str) -> str:
    """Case-insensitive collation key ('und:ci'): no ignorable controls, NFC, lower, kana-insensitive."""
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf"))
    s = unicodedata.normalize("NFC", s).lower()
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)


def _like_regex(pattern: str) -> "re.Pattern[str]":
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.S)


def _eq_ci(a, b):
    if a is None or b is None:
        return None
    return _ci_fold(a) == _ci_fold(b)


def _like_ci(s, p):
    if s is None or p is None:
        return None
    return _like_regex(_ci_fold(p)).match(_ci_fold(s)) is not None


def _pc_resp(values, p, discrete):
    """PERCENTILE_CONT / _DISC with RESPECT NULLS (NULL sorts lowest)."""
    if not values or p is None:
        return None
    nulls = [v for v in values if v is None]
    vals = sorted(v for v in values if v is not None)
    s = nulls + vals
    n = len(s)
    if discrete:
        import math
        return s[max(math.ceil(p * n), 1) - 1]
    pos = p * (n - 1)
    lo, hi = int(pos), min(int(pos) + (pos > int(pos)), n - 1)
    a, b = s[lo], s[hi]
    if lo == hi:
        return None if a is None else float(a)
    if a is None:
        return None if b is None else float(b)
    if b is None:
        return float(a)
    return a + (b - a) * (pos - lo)


# --------------------------------------------------------------------------- #
#  Token helpers
# --------------------------------------------------------------------------- #
COMMA = tr.Tok(tr.PUNCT, ",")


def _t(text: str) -> list[tr.Tok]:
    return tr.tokenize_plain(text)


def _text(toks: list[tr.Tok]) -> str:
    """Tokens as text without comments (a '-- ...' comment would swallow what follows)."""
    return " ".join(tr.detokenize([t for t in toks if t.kind != tr.COMMENT]).split())


def _join_args(args: list[tr.Arg]) -> list[tr.Tok]:
    out: list[tr.Tok] = []
    for i, a in enumerate(args):
        if i:
            out.append(COMMA)
        out.extend(a.toks)
    return out


_KEYWORDS = ("HAVING", "IGNORE", "RESPECT", "WHERE", "ORDER", "LIMIT")


class Mods:
    def __init__(self):
        self.args: list[str] = []
        self.distinct = False
        self.ignore = False
        self.respect = False
        self.where = ""
        self.order = ""
        self.limit = ""
        self.having = ""        # "MAX expr" / "MIN expr"

    @property
    def has_any(self) -> bool:
        return bool(self.distinct or self.ignore or self.respect or self.where or self.order
                    or self.limit or self.having)


def parse_mods(args: list[tr.Arg]) -> Mods:
    toks = [t for t in _join_args(args) if t.kind != tr.COMMENT]
    toks = tr.strip(toks)
    m = Mods()
    if toks and toks[0].is_word("DISTINCT"):
        m.distinct = True
        toks = tr.strip(toks[1:])
    # find top-level modifier keywords
    marks: list[tuple[int, str, int]] = []      # (index, keyword, length)
    depth = 0
    for i, t in enumerate(toks):
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.kind == tr.WORD:
            nx = tr.next_sig(toks, i)
            n2 = toks[nx] if nx < len(toks) else None
            u = t.upper
            if u in ("IGNORE", "RESPECT") and n2 is not None and n2.is_word("NULLS"):
                marks.append((i, u, nx + 1 - i))
            elif u == "ORDER" and n2 is not None and n2.is_word("BY"):
                marks.append((i, u, nx + 1 - i))
            elif u == "HAVING" and n2 is not None and n2.is_word("MAX", "MIN"):
                marks.append((i, u, nx + 1 - i))
            elif u in ("WHERE", "LIMIT"):
                marks.append((i, u, 1))
    main_end = marks[0][0] if marks else len(toks)
    main = toks[:main_end]
    m.args = [_text(a) for a in tr.split_args(main, 0, len(main))]
    for k, (i, kw, ln) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(toks)
        body = _text(toks[i + ln:end])
        if kw == "IGNORE":
            m.ignore = True
        elif kw == "RESPECT":
            m.respect = True
        elif kw == "ORDER":
            m.order = body
        elif kw == "WHERE":
            m.where = body
        elif kw == "LIMIT":
            m.limit = body
        elif kw == "HAVING":
            m.having = _text(toks[i + 1:end])
    return m


def _filter(m: Mods, first: str | None, ignore_nulls: bool) -> str:
    conds = []
    if m.where:
        conds.append(f"({m.where})")
    if ignore_nulls and first is not None:
        conds.append(f"({first}) IS NOT NULL")
    return f" FILTER (WHERE {' AND '.join(conds)})" if conds else ""


def _plain(name: str):
    """Handler for an ordinary aggregate that may carry modifiers: same name, DuckDB syntax."""
    def handler(args):
        m = parse_mods(args)
        if not m.has_any:
            return None
        inner = ", ".join(m.args) or "*"
        if m.distinct:
            inner = "DISTINCT " + inner
        if m.order:
            inner += " ORDER BY " + m.order
        return f"{name}({inner})" + _filter(m, m.args[0] if m.args else None, m.ignore)
    return handler


def _renamed(name: str):
    def handler(args):
        m = parse_mods(args)
        inner = ", ".join(m.args)
        if m.distinct:
            inner = "DISTINCT " + inner
        if m.order:
            inner += " ORDER BY " + m.order
        return f"{name}({inner})" + _filter(m, m.args[0] if m.args else None, m.ignore)
    return handler


def _list_expr(m: Mods, x: str, dedupe_first_seen: bool = True) -> str:
    inner = x
    if m.distinct:
        inner = "DISTINCT " + inner
    if m.order:
        inner += " ORDER BY " + m.order
    if m.distinct and not m.order and dedupe_first_seen:
        inner = x
    expr = f"list({inner})" + _filter(m, x, m.ignore)
    if m.distinct and not m.order and dedupe_first_seen:
        # docs show DISTINCT values in order of first appearance
        expr = f"list_filter({expr}, (_v, _i) -> list_position({expr}, _v) = _i)"
    if m.limit:
        expr = f"list_slice({expr}, 1, {m.limit})"
    return expr


def array_agg(args):
    m = parse_mods(args)
    if not m.args:
        return None
    if m.distinct and not m.order:
        # also valid inside a window: no first-appearance trick there
        pass
    return _list_expr(m, m.args[0])


def array_concat_agg(args):
    m = parse_mods(args)
    m.ignore = False
    x = m.args[0]
    inner = x
    if m.order:
        inner += " ORDER BY " + m.order
    expr = f"list({inner}) FILTER (WHERE ({x}) IS NOT NULL" + (f" AND ({m.where})" if m.where else "") + ")"
    if m.limit:
        expr = f"list_slice({expr}, 1, {m.limit})"
    return f"flatten({expr})"


def string_agg(args):
    m = parse_mods(args)
    x = m.args[0]
    delim = m.args[1] if len(m.args) > 1 else "','"
    if not m.limit and not (m.distinct and not m.order):
        inner = ("DISTINCT " if m.distinct else "") + f"{x}, {delim}"
        if m.order:
            inner += " ORDER BY " + m.order
        return f"string_agg({inner})" + _filter(m, x, True)
    m.ignore = True
    return f"array_to_string({_list_expr(m, x)}, {delim})"


def any_value(args):
    m = parse_mods(args)
    x = m.args[0]
    if m.having:
        kind, _, expr = m.having.partition(" ")
        fn = "arg_max" if kind.upper() == "MAX" else "arg_min"
        return f"{fn}({x}, {expr.strip()})" + _filter(m, None, False)
    if not m.has_any:
        return None
    return f"any_value({x})" + _filter(m, x, m.ignore)


def countif(args):
    m = parse_mods(args)
    cond = m.args[0]
    inner = f"CASE WHEN ({cond}) THEN 1 END"
    if m.distinct:
        inner = "DISTINCT " + inner
    return f"count({inner})" + _filter(m, None, False)


def percentile(discrete: bool):
    fn = "quantile_disc" if discrete else "quantile_cont"

    def handler(args):
        m = parse_mods(args)
        x, p = m.args[0], m.args[1]
        if m.respect:
            return f"_bq_pc_marker_{'d' if discrete else 'c'}(list({x}), {p})"
        return f"{fn}({x}, {p})" + _filter(m, None, False)
    return handler


def approx_quantiles(args):
    m = parse_mods(args)
    x, n = m.args[0], m.args[1]
    try:
        k = int(n)
    except ValueError:
        return None
    qs = ", ".join(repr(i / k) for i in range(k + 1))
    return f"approx_quantile({x}, [{qs}])" + _filter(m, x, True)


def collate(args):
    return args[0].text if args else None


def with_expr(args):
    """WITH(a AS e1, b AS e2, result): inline the variables."""
    if len(args) < 2:
        return None
    subs: dict[str, str] = {}
    for a in args[:-1]:
        toks = [t for t in a.toks if t.kind != tr.COMMENT]
        sig = [t for t in toks if t.kind != tr.WS]
        if len(sig) < 3 or sig[0].kind != tr.WORD or not sig[1].is_word("AS"):
            return None
        subs[sig[0].text.upper()] = "(" + _subst(sig[2:], subs) + ")"
    return "(" + _subst([t for t in args[-1].toks if t.kind != tr.COMMENT], subs) + ")"


def _subst(toks: list[tr.Tok], subs: dict[str, str]) -> str:
    out = []
    for i, t in enumerate(toks):
        if t.kind == tr.WORD and t.upper in subs:
            p = tr.prev_sig(toks, i)
            if not (p >= 0 and toks[p].is_punct(".")):
                out.append(subs[t.upper])
                continue
        out.append(t.text)
    return _text(tr.tokenize_plain("".join(out)))


HANDLERS: dict = {
    "ARRAY_AGG": array_agg,
    "ARRAY_CONCAT_AGG": array_concat_agg,
    "STRING_AGG": string_agg,
    "ANY_VALUE": any_value,
    "COUNTIF": countif,
    "LOGICAL_AND": _renamed("bool_and"),
    "LOGICAL_OR": _renamed("bool_or"),
    "PERCENTILE_CONT": percentile(False),
    "PERCENTILE_DISC": percentile(True),
    "APPROX_QUANTILES": approx_quantiles,
    "COLLATE": collate,
    "WITH": with_expr,
}
for _n in ("COUNT", "SUM", "AVG", "MIN", "MAX", "BIT_AND", "BIT_OR", "BIT_XOR", "CORR",
           "COVAR_POP", "COVAR_SAMP", "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "VARIANCE",
           "VAR_POP", "VAR_SAMP", "APPROX_COUNT_DISTINCT", "MAX_BY", "MIN_BY"):
    HANDLERS[_n] = _plain(_n.lower())


# --------------------------------------------------------------------------- #
#  Token rules
# --------------------------------------------------------------------------- #
def hex_literals(toks):
    out = []
    for t in toks:
        if t.kind == tr.NUM and t.text[:2].lower() == "0x":
            out.append(tr.Tok(tr.NUM, str(int(t.text, 16))))
        else:
            out.append(t)
    return out


def _find_seq(toks, i, words):
    """Index after the words sequence starting at toks[i], or -1."""
    j = i
    for w in words:
        while j < len(toks) and toks[j].kind in (tr.WS, tr.COMMENT):
            j += 1
        if j >= len(toks) or not toks[j].is_word(w):
            return -1
        j += 1
    return j


def is_distinct_from(toks):
    for _ in range(200):
        for i, t in enumerate(toks):
            if not t.is_word("IS"):
                continue
            neg = _find_seq(toks, i, ["IS", "NOT", "DISTINCT", "FROM"])
            pos = _find_seq(toks, i, ["IS", "DISTINCT", "FROM"]) if neg < 0 else -1
            end_kw = neg if neg >= 0 else pos
            if end_kw < 0:
                continue
            s = tr.primary_start(toks, i)
            e = tr.primary_end(toks, end_kw)
            if s < 0 or e < 0:
                continue
            left, right = _text(toks[s:i]), _text(toks[end_kw:e])
            fn = "_bq_isndf" if neg >= 0 else "_bq_isdf"
            toks = toks[:s] + _t(f"{fn}({left}, {right})") + toks[e:]
            break
        else:
            return toks
    return toks


def xor_operator(toks):
    """a ^ b  ->  xor(a, b)  (GoogleSQL ^ is bitwise XOR; DuckDB's is power)."""
    for _ in range(100):
        for i, t in enumerate(toks):
            if t.is_punct("^"):
                s = tr.primary_start(toks, i)
                e = tr.primary_end(toks, i + 1)
                if s < 0 or e < 0:
                    continue
                toks = toks[:s] + _t(f"xor({_text(toks[s:i])}, {_text(toks[i + 1:e])})") + toks[e:]
                break
        else:
            return toks
    return toks


def quantified_like(toks):
    """x [NOT] LIKE ANY|SOME|ALL (p, ...) / UNNEST(arr)."""
    for _ in range(200):
        for i, t in enumerate(toks):
            if not t.is_word("LIKE"):
                continue
            q = tr.next_sig(toks, i)
            if q >= len(toks) or not toks[q].is_word("ANY", "SOME", "ALL"):
                continue
            allq = toks[q].is_word("ALL")
            p = tr.prev_sig(toks, i)
            neg = p >= 0 and toks[p].is_word("NOT")
            left_end = p if neg else i
            s = tr.primary_start(toks, left_end)
            o = tr.next_sig(toks, q)
            if s < 0 or o >= len(toks):
                continue
            left = _text(toks[s:left_end])
            if toks[o].is_punct("("):
                c = tr.match_paren(toks, o)
                if c < 0:
                    continue
                pats = [_text(a) for a in tr.split_args(toks, o + 1, c)]
                op = "NOT LIKE" if neg else "LIKE"
                ci = _is_collate_ci(toks[s:left_end]) or any(
                    _is_collate_ci(tr.tokenize_plain(pat)) for pat in pats)
                if ci:
                    parts = [("NOT " if neg else "") + f"_bq_cf_like({left}, {pat})" for pat in pats]
                else:
                    parts = [f"{left} {op} {pat}" for pat in pats]
                new = "(" + (" AND " if allq else " OR ").join(parts) + ")"
                end = c + 1
            elif toks[o].is_word("UNNEST"):
                e = tr.primary_end(toks, o)
                if e < 0:
                    continue
                arr = _text(toks[o:e])
                inner = arr[arr.index("(") + 1:arr.rindex(")")]
                name = ("_bq_nlike_" if neg else "_bq_like_") + ("all" if allq else "any") + "_arr"
                new = f"{name}({left}, {inner})"
                end = e
            else:
                continue
            toks = toks[:s] + _t(new) + toks[end:]
            break
        else:
            return toks
    return toks


def in_unnest(toks):
    """x [NOT] IN UNNEST(arr)."""
    for _ in range(100):
        for i, t in enumerate(toks):
            if not t.is_word("IN"):
                continue
            o = tr.next_sig(toks, i)
            if o >= len(toks) or not toks[o].is_word("UNNEST"):
                continue
            n = tr.next_sig(toks, o)
            if n >= len(toks) or not toks[n].is_punct("("):
                continue
            p = tr.prev_sig(toks, i)
            neg = p >= 0 and toks[p].is_word("NOT")
            left_end = p if neg else i
            s = tr.primary_start(toks, left_end)
            c = tr.match_paren(toks, n)
            if s < 0 or c < 0:
                continue
            new = f"_bq_in_arr({_text(toks[s:left_end])}, {_text(toks[n + 1:c])})"
            if neg:
                new = f"(NOT {new})"
            toks = toks[:s] + _t(new) + toks[c + 1:]
            break
        else:
            return toks
    return toks


def _is_collate_ci(operand: list[tr.Tok]) -> bool:
    s = tr.strip(operand)
    return bool(s) and s[0].is_word("COLLATE") and ":ci" in _text(s)


def collated_compare(toks):
    """COLLATE(x, 'und:ci') = / LIKE / NOT LIKE y  -> case-insensitive UDF comparison."""
    for _ in range(100):
        for i, t in enumerate(toks):
            kind = None
            start_op = i
            if t.is_punct("="):
                p = tr.prev_sig(toks, i)
                if p >= 0 and toks[p].kind == tr.PUNCT and toks[p].text in "<>!:":
                    continue
                kind = "eq"
            elif t.is_word("LIKE"):
                kind = "like"
                p = tr.prev_sig(toks, i)
                if p >= 0 and toks[p].is_word("NOT"):
                    kind, start_op = "nlike", p
            if kind is None:
                continue
            s = tr.primary_start(toks, start_op)
            e = tr.primary_end(toks, i + 1)
            if s < 0 or e < 0:
                continue
            L, R = toks[s:start_op], toks[i + 1:e]
            if not (_is_collate_ci(L) or _is_collate_ci(R)):
                continue
            fn = "_bq_cf_eq" if kind == "eq" else "_bq_cf_like"
            new = f"{fn}({_text(L)}, {_text(R)})"
            if kind == "nlike":
                new = f"(NOT {new})"
            toks = toks[:s] + _t(new) + toks[e:]
            break
        else:
            return toks
    return toks


def pc_respect(toks):
    """_bq_pc_marker(list(x), p, disc) [OVER w]  ->  _bq_pc_resp(list(x) [OVER w], p, disc)."""
    for _ in range(50):
        for i, t in enumerate(toks):
            if not (t.kind == tr.WORD and t.text.startswith("_bq_pc_marker_")):
                continue
            o = tr.next_sig(toks, i)
            c = tr.match_paren(toks, o)
            if c < 0:
                continue
            a = tr.split_args(toks, o + 1, c)
            end = c + 1
            over = ""
            n = tr.next_sig(toks, c)
            if n < len(toks) and toks[n].is_word("OVER"):
                m = tr.next_sig(toks, n)
                if m < len(toks) and toks[m].is_punct("("):
                    e = tr.match_paren(toks, m) + 1
                else:
                    e = m + 1
                over = " " + _text(toks[n:e])
                end = e
            new = f"_bq_pc_resp_{t.text[-1]}({_text(a[0])}{over}, {_text(a[1])})"
            toks = toks[:i] + _t(new) + toks[end:]
            break
        else:
            return toks
    return toks


PRE_RULES: list = [hex_literals, quantified_like, in_unnest, collated_compare,
                   is_distinct_from, xor_operator]
POST_RULES: list = [pc_respect]


def _register_all(con) -> None:
    H.udf(con, "_bq_cf_eq", _eq_ci, [H.VARCHAR, H.VARCHAR], H.BOOLEAN)
    H.udf(con, "_bq_cf_like", _like_ci, [H.VARCHAR, H.VARCHAR], H.BOOLEAN)
    H.udf(con, "_bq_pc_resp_c", lambda l, p: None if l is None else _pc_resp(l, p, False),
          [H.T.DuckDBPyType(list[float]) if False else "DOUBLE[]", H.DOUBLE], H.DOUBLE)
    H.udf(con, "_bq_pc_resp_d", lambda l, p: None if l is None else _pc_resp(l, p, True),
          ["VARCHAR[]", H.DOUBLE], H.VARCHAR)


register = _register_all  # noqa: F811
