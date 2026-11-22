"""MySQL aggregate, window and miscellaneous functions on DuckDB.

Contents
  * GROUP_CONCAT([DISTINCT] expr[, expr ...] [ORDER BY ...] [SEPARATOR 'x'])
    -> string_agg (NULL rows skipped, several expressions concatenated per row,
    MySQL NULL ordering inside ORDER BY)
  * STD / STDDEV / VARIANCE are the POPULATION versions in MySQL (DuckDB's
    stddev / variance are the sample versions); BIT_AND / BIT_OR / BIT_XOR work
    on unsigned 64-bit values with MySQL's neutral element for empty sets;
    COUNT(DISTINCT a, b); AVG gives DECIMAL for integer / decimal input;
    JSON_ARRAYAGG / JSON_OBJECTAGG; ANY_VALUE (native)
  * window functions are native; `NTH_VALUE(...) FROM FIRST` / `RESPECT NULLS`
    are accepted (they are the defaults)
  * GROUP BY ... WITH ROLLUP -> GROUP BY ROLLUP(...) plus the implicit sort
    MySQL applies (NULL first, super-aggregate rows after their group)
  * logical operators: XOR, and `!` with MySQL's (high) precedence
  * INET_ATON / INET_NTOA / INET6_* / IS_IPV4* / IS_IPV6 / IS_UUID /
    UUID_TO_BIN / BIN_TO_UUID / UUID / UUID_SHORT / SLEEP / NAME_CONST

Session behaviour set in register(): NULLs sort first for ASC and last for DESC
(MySQL), and DuckDB runs single-threaded so results without ORDER BY come out
in a stable order (window partitions, GROUP BY) instead of changing run to run.
"""

from __future__ import annotations

import ipaddress
import re
import threading
import time

from .. import translate as tr
from . import helpers as H

Tok = tr.Tok

# --------------------------------------------------------------------------- #
#  Small token helpers
# --------------------------------------------------------------------------- #
_OPEN, _CLOSE = "([{", ")]}"


def _plain(text: str) -> list[Tok]:
    return tr.tokenize_plain(text)


def _join(*parts) -> list[Tok]:
    """Concatenate str (tokenized as plain SQL) and token-list parts."""
    out: list[Tok] = []
    for p in parts:
        out.extend(_plain(p) if isinstance(p, str) else p)
    return out


def _word(t: Tok) -> str:
    """Upper-cased text of a word token ('' for anything else); the `!` / `||`
    rewrites of rules.py leave padded words such as ' NOT ' which are kept apart."""
    return t.text.upper() if t.kind == tr.WORD else ""


def _first_sig(toks: list[Tok], start: int = 0) -> int:
    j = start
    while j < len(toks) and toks[j].kind in (tr.WS, tr.COMMENT):
        j += 1
    return j


def _depth0_words(toks: list[Tok], stop_at=None):
    """Yield (index, token) of tokens at nesting depth 0."""
    depth = 0
    for j, t in enumerate(toks):
        if t.kind == tr.PUNCT and t.text in _OPEN:
            if depth == 0:
                yield j, t
            depth += 1
        elif t.kind == tr.PUNCT and t.text in _CLOSE:
            depth -= 1
            if depth == 0:
                yield j, t
        elif depth == 0:
            yield j, t


# --------------------------------------------------------------------------- #
#  GROUP_CONCAT
# --------------------------------------------------------------------------- #
def _split_order_item(item: list[Tok]) -> tuple[str, bool]:
    """('expr', is_desc) for one ORDER BY item."""
    s = tr.strip(item)
    desc = False
    if s and s[-1].is_word("ASC", "DESC"):
        desc = s[-1].upper == "DESC"
        s = tr.strip(s[:-1])
    return tr.text_of(s), desc


def h_group_concat(args: list[tr.Arg]) -> str | None:
    toks: list[Tok] = []
    for i, a in enumerate(args):
        if i:
            toks.append(Tok(tr.PUNCT, ","))
        toks.extend(a.toks)
    order_at = sep_at = None
    sig = [(j, t) for j, t in _depth0_words(toks) if t.kind not in (tr.WS, tr.COMMENT)]
    for n, (j, t) in enumerate(sig):
        if t.is_word("ORDER") and order_at is None and n + 1 < len(sig) and sig[n + 1][1].is_word("BY"):
            order_at = (j, sig[n + 1][0])
        elif t.is_word("SEPARATOR") and sep_at is None and n + 1 < len(sig) and sig[n + 1][1].kind == tr.STR:
            sep_at = (j, sig[n + 1][0])
    sep = "','"
    if sep_at is not None:
        sep = toks[sep_at[1]].text
    expr_end = min([x[0] for x in (order_at, sep_at) if x is not None] or [len(toks)])
    expr_toks = toks[:expr_end]
    distinct = False
    k = _first_sig(expr_toks)
    if k < len(expr_toks) and expr_toks[k].is_word("DISTINCT"):
        distinct = True
        expr_toks = expr_toks[k + 1:]
    elif k < len(expr_toks) and expr_toks[k].is_word("ALL"):
        expr_toks = expr_toks[k + 1:]
    exprs = [tr.text_of(a) for a in tr.split_args(expr_toks, 0, len(expr_toks))]
    if not exprs:
        return None
    if len(exprs) == 1:
        e = exprs[0]
    else:   # several expressions are concatenated; one NULL makes the whole row NULL
        e = "(" + " || ".join(f"CAST({x} AS VARCHAR)" for x in exprs) + ")"
    order_sql = ""
    if order_at is not None:
        o_end = sep_at[0] if sep_at is not None and sep_at[0] > order_at[1] else len(toks)
        items = tr.split_args(toks[order_at[1] + 1:o_end], 0, o_end - order_at[1] - 1)
        keys = []
        for item in items:
            text, desc = _split_order_item(item)
            keys.append(f"{text} DESC NULLS LAST" if desc else f"{text} ASC NULLS FIRST")
        if keys:
            order_sql = " ORDER BY " + ", ".join(keys)
    d = "DISTINCT " if distinct else ""
    return f"string_agg({d}{e}, {sep}{order_sql})"


# --------------------------------------------------------------------------- #
#  COUNT / STD family / NAME_CONST
# --------------------------------------------------------------------------- #
def h_count(args: list[tr.Arg]) -> str | None:
    toks: list[Tok] = []
    for i, a in enumerate(args):
        if i:
            toks.append(Tok(tr.PUNCT, ","))
        toks.extend(a.toks)
    k = _first_sig(toks)
    if k >= len(toks) or not toks[k].is_word("DISTINCT"):
        return None
    exprs = [tr.text_of(a) for a in tr.split_args(toks[k + 1:], 0, len(toks) - k - 1)]
    if not exprs:
        return None
    ci = [f"_my_ci({x})" for x in exprs]
    if len(exprs) == 1:
        return f"count(DISTINCT {ci[0]})"
    nulls = " OR ".join(f"({x}) IS NULL" for x in exprs)      # COUNT skips tuples with a NULL
    return f"count(DISTINCT CASE WHEN {nulls} THEN NULL ELSE ({', '.join(ci)}) END)"


def _rename(duck_name: str):
    def handler(args: list[tr.Arg]) -> str:
        return f"{duck_name}(" + ", ".join(a.text for a in args) + ")"
    return handler


def h_name_const(args: list[tr.Arg]) -> str | None:
    return f"({args[1].text})" if len(args) == 2 else None


# --------------------------------------------------------------------------- #
#  Aggregates that need to wrap the call *and* its OVER clause (token rule)
# --------------------------------------------------------------------------- #
def _over_end(toks: list[Tok], close: int) -> int:
    """Index just past `OVER (...)` / `OVER name` after the call closing at `close`."""
    n = len(toks)
    k = tr.next_sig(toks, close)
    if k < n and toks[k].is_word("OVER"):
        m = tr.next_sig(toks, k)
        if m < n and toks[m].is_punct("("):
            e = tr.match_paren(toks, m)
            return e + 1 if e > 0 else close + 1
        if m < n and toks[m].kind == tr.WORD:
            return m + 1
    return close + 1


def _starts_distinct(inner: list[Tok]) -> bool:
    k = _first_sig(inner)
    return k < len(inner) and inner[k].is_word("DISTINCT")


def _b_bit(op: str, neutral: str):
    def build(inner: list[Tok], over: list[Tok]):
        return _join("COALESCE(_my_u64(system.main.", op, "(", _cast_arg(inner), ")", over, "), ", neutral, ")")
    return build


def _cast_arg(inner: list[Tok]) -> list[Tok]:
    """`DISTINCT x` -> `DISTINCT CAST(x AS BIGINT)`; `x` -> `CAST(x AS BIGINT)`."""
    k = _first_sig(inner)
    if k < len(inner) and inner[k].is_word("DISTINCT"):
        return _join("DISTINCT CAST(", inner[k + 1:], " AS BIGINT)")
    return _join("CAST(", inner, " AS BIGINT)")


def _b_json_array(inner, over):
    return _join("_my_jfmt(system.main.json_group_array(", inner, ")", over, ")")


def _b_json_object(inner, over):
    parts = tr.split_args(inner, 0, len(inner))
    if len(parts) != 2:
        return None
    return _join("_my_jfmt(system.main.json_group_object(CAST(", parts[0], " AS VARCHAR), ",
                 parts[1], ")", over, ")")


def _b_avg(inner, over):
    if over or _starts_distinct(inner):
        return None
    return _join("_my_avg(", inner, ")")


_AGG_BUILDERS = {
    "BIT_AND": _b_bit("bit_and", "18446744073709551615"),
    "BIT_OR": _b_bit("bit_or", "0"),
    "BIT_XOR": _b_bit("bit_xor", "0"),
    "JSON_ARRAYAGG": _b_json_array,
    "JSON_OBJECTAGG": _b_json_object,
    "AVG": _b_avg,
}


def rule_aggregates(toks: list[Tok]) -> list[Tok]:
    out: list[Tok] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind == tr.WORD and t.upper in _AGG_BUILDERS:
            prev = tr.prev_sig(toks, i)
            k = tr.next_sig(toks, i)
            if not (prev >= 0 and toks[prev].is_punct(".")) and k < n and toks[k].is_punct("("):
                close = tr.match_paren(toks, k)
                if close > 0:
                    end = _over_end(toks, close)
                    inner = rule_aggregates(toks[k + 1:close])
                    over = rule_aggregates(toks[close + 1:end])
                    built = _AGG_BUILDERS[t.upper](inner, over)
                    if built is None:
                        out.extend(toks[i:k + 1])
                        out.extend(inner)
                        out.append(toks[close])
                        out.extend(over)
                    else:
                        out.extend(built)
                    i = end
                    continue
        out.append(t)
        i += 1
    return out


# --------------------------------------------------------------------------- #
#  Logical operators: `!` (high precedence NOT) and XOR
# --------------------------------------------------------------------------- #
def _is_bang(t: Tok) -> bool:
    return t.kind == tr.WORD and t.text == " NOT "      # what rules.operators makes of `!`


def _operand(toks: list[Tok], i: int):
    """(end, rewritten tokens) of the unary-level operand starting at toks[i]."""
    k = _first_sig(toks, i)
    if k < len(toks) and _is_bang(toks[k]):
        end, inner = _operand(toks, k + 1)
        if end < 0:
            return -1, []
        return end, _join("_my_not(", inner, ")")
    end = tr.primary_end(toks, i)
    if end < 0:
        return -1, []
    return end, rule_bang(toks[i:end])


def rule_bang(toks: list[Tok]) -> list[Tok]:
    """`! x` -> _my_not(x): binds tighter than + - * (so `! 1+1` is (!1)+1) and gives 0/1/NULL."""
    out: list[Tok] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if _is_bang(t):
            end, inner = _operand(toks, i + 1)
            if end > 0:
                out.extend(_join("_my_not(", inner, ")"))
                i = end
                continue
        out.append(t)
        i += 1
    return out


_XOR_STOP_LEFT = {"OR", "SELECT", "WHERE", "HAVING", "ON", "WHEN", "THEN", "ELSE", "CASE", "BY",
                  "DISTINCT", "ALL", "FROM", "XOR", "SET", "AS", "IF", "QUALIFY"}
_XOR_STOP_RIGHT = {"OR", "XOR", "FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION",
                   "INTERSECT", "EXCEPT", "WINDOW", "THEN", "ELSE", "END", "WHEN", "AS", "ON",
                   "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "NATURAL", "USING",
                   "OFFSET", "FOR", "ASC", "DESC", "QUALIFY", "OVER", "INTO"}


def _operand_bounds(toks: list[Tok], i: int) -> tuple[int, int]:
    """[start, end) of the AND-level operands on both sides of the XOR at toks[i]."""
    j, depth = i - 1, 0
    while j >= 0:
        t = toks[j]
        if t.kind == tr.PUNCT and t.text in _CLOSE:
            depth += 1
        elif t.kind == tr.PUNCT and t.text in _OPEN:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (t.kind == tr.PUNCT and t.text in ",;"
                             or t.kind == tr.WORD and t.text.strip().upper() in _XOR_STOP_LEFT):
            break
        j -= 1
    start = j + 1
    j, depth = i + 1, 0
    while j < len(toks):
        t = toks[j]
        if t.kind == tr.PUNCT and t.text in _OPEN:
            depth += 1
        elif t.kind == tr.PUNCT and t.text in _CLOSE:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (t.kind == tr.PUNCT and t.text in ",;"
                             or t.kind == tr.WORD and t.text.strip().upper() in _XOR_STOP_RIGHT):
            break
        j += 1
    return start, j


def rule_xor(toks: list[Tok]) -> list[Tok]:
    """a XOR b -> _my_xor(a, b); XOR binds tighter than OR and looser than AND."""
    guard = 0
    while guard < 200:
        guard += 1
        at = next((i for i, t in enumerate(toks) if t.is_word("XOR")
                   and not (tr.prev_sig(toks, i) >= 0 and toks[tr.prev_sig(toks, i)].is_punct("."))), None)
        if at is None:
            break
        start, end = _operand_bounds(toks, at)
        left, right = tr.strip(toks[start:at]), tr.strip(toks[at + 1:end])
        if not left or not right:
            break
        lead = toks[start:start + len(toks[start:at]) - len(tr.strip(toks[start:at]))]
        toks = toks[:start] + _join(" _my_xor(", left, ", ", right, ") ") + toks[end:]
    return toks


# --------------------------------------------------------------------------- #
#  Window syntax: NTH_VALUE(...) FROM FIRST / RESPECT NULLS OVER ...
# --------------------------------------------------------------------------- #
def rule_window_defaults(toks: list[Tok]) -> list[Tok]:
    drop: set[int] = set()
    for i, t in enumerate(toks):
        if not t.is_word("OVER"):
            continue
        j = tr.prev_sig(toks, i)
        while j >= 0:
            p = tr.prev_sig(toks, j)
            if toks[j].is_word("NULLS") and p >= 0 and toks[p].is_word("RESPECT"):
                drop.update(range(p, j + 1))
                j = tr.prev_sig(toks, p)
            elif toks[j].is_word("FIRST") and p >= 0 and toks[p].is_word("FROM"):
                q = tr.prev_sig(toks, p)
                if q >= 0 and toks[q].is_punct(")"):
                    drop.update(range(p, j + 1))
                    j = q
                else:
                    break
            else:
                break
    return [t for i, t in enumerate(toks) if i not in drop] if drop else toks


# --------------------------------------------------------------------------- #
#  GROUP BY ... WITH ROLLUP
# --------------------------------------------------------------------------- #
_ROLLUP_END = {"LIMIT", "UNION", "INTERSECT", "EXCEPT", "FOR", "INTO", "OFFSET"}


def _select_items(toks: list[Tok], group_at: int) -> list[str] | None:
    """Expressions of the SELECT list that owns the GROUP BY at toks[group_at]."""
    depth, j = 0, group_at - 1
    while j >= 0:
        t = toks[j]
        if t.kind == tr.PUNCT and t.text in _CLOSE:
            depth += 1
        elif t.kind == tr.PUNCT and t.text in _OPEN:
            if depth == 0:
                return None
            depth -= 1
        elif depth == 0 and t.is_word("SELECT"):
            break
        j -= 1
    if j < 0:
        return None
    from_at = None
    for k, t in _depth0_words(toks[j + 1:group_at]):
        if t.is_word("FROM"):
            from_at = j + 1 + k
            break
    if from_at is None:
        return None
    body = toks[j + 1:from_at]
    k = _first_sig(body)
    if k < len(body) and body[k].is_word("DISTINCT", "ALL"):
        body = body[k + 1:]
    items = []
    for item in tr.split_args(body, 0, len(body)):
        s = tr.strip(item)
        if len(s) >= 3 and s[-2].is_word("AS"):
            s = tr.strip(s[:-2])
        elif len(s) >= 2 and s[-1].kind == tr.WORD and not s[-1].is_word("END", "NULL", "TRUE", "FALSE")                 and (s[-2].kind in (tr.WORD, tr.NUM, tr.STR) or s[-2].is_punct(")")):
            s = tr.strip(s[:-1])                 # implicit alias
        items.append(tr.text_of(s))
    return items


def rule_rollup(toks: list[Tok]) -> list[Tok]:
    guard = 0
    while guard < 50:
        guard += 1
        at = None
        for i, t in enumerate(toks):
            if t.is_word("WITH"):
                k = tr.next_sig(toks, i)
                if k < len(toks) and toks[k].is_word("ROLLUP"):
                    at = (i, k)
                    break
        if at is None:
            break
        w, r = at
        # back to GROUP BY at the same nesting level
        depth, j, by_at = 0, w - 1, None
        while j >= 0:
            t = toks[j]
            if t.kind == tr.PUNCT and t.text in _CLOSE:
                depth += 1
            elif t.kind == tr.PUNCT and t.text in _OPEN:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and t.is_word("BY"):
                p = tr.prev_sig(toks, j)
                if p >= 0 and toks[p].is_word("GROUP"):
                    by_at = (p, j)
                    break
            j -= 1
        if by_at is None:
            break
        g, by = by_at
        items = [tr.text_of(a) for a in tr.split_args(toks, by + 1, w)]
        if not items:
            break
        # ordinals (GROUP BY 1, 2) -> the SELECT expressions they stand for
        select = None
        resolved = []
        for it in items:
            if re.fullmatch(r"\d+", it):
                if select is None:
                    select = _select_items(toks, g) or []
                idx = int(it) - 1
                resolved.append(select[idx] if 0 <= idx < len(select) else it)
            else:
                resolved.append(it)
        # where does this query block end, and has it an ORDER BY of its own?
        depth, p, has_order = 0, r + 1, False
        while p < len(toks):
            t = toks[p]
            if t.kind == tr.PUNCT and t.text in _OPEN:
                depth += 1
            elif t.kind == tr.PUNCT and t.text in _CLOSE:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0:
                if t.kind == tr.PUNCT and t.text == ";":
                    break
                if t.is_word("ORDER"):
                    has_order = True
                    break
                if t.kind == tr.WORD and t.upper in _ROLLUP_END:
                    break
            p += 1
        keys = []
        for e in resolved:
            keys.append(f"GROUPING({e}), {e} NULLS FIRST")
        order = [] if has_order else _plain(" ORDER BY " + ", ".join(keys) + " ")
        group = _plain("GROUP BY ROLLUP(" + ", ".join(resolved) + ")")
        pos = p if not has_order else r + 1
        toks = toks[:g] + group + toks[r + 1:pos] + order + toks[pos:]
    return toks


# --------------------------------------------------------------------------- #
#  Python implementations: network addresses, UUIDs
# --------------------------------------------------------------------------- #
def f_inet_aton(s):
    if s is None:
        return None
    byte = result = dots = 0
    c = "."
    for c in s:
        if "0" <= c <= "9":
            byte = byte * 10 + (ord(c) - 48)
            if byte > 255:
                return None
        elif c == ".":
            dots += 1
            result = (result << 8) + byte
            byte = 0
        else:
            return None
    if c == "." or dots > 3:
        return None
    if dots == 1:
        result <<= 16
    elif dots == 2:
        result <<= 8
    return (result << 8) + byte


def f_inet_ntoa(n):
    if n is None or n < 0 or n > 0xFFFFFFFF:
        return None
    return ".".join(str((n >> s) & 255) for s in (24, 16, 8, 0))


_V4 = re.compile(r"(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})", re.ASCII)


def _v4_bytes(s: str):
    m = _V4.fullmatch(s)
    if not m:
        return None
    vals = [int(g) for g in m.groups()]
    return bytes(vals) if all(v <= 255 for v in vals) else None


def _v6_bytes(s: str):
    if "%" in s or not s.isascii() or ":" not in s:
        return None
    try:
        return ipaddress.IPv6Address(s).packed
    except ValueError:
        return None


def f_inet6_aton(s):
    if s is None:
        return None
    return _v4_bytes(s) or _v6_bytes(s)


def _ntop6(b: bytes) -> str:
    """inet_ntop for IPv6 (BIND's algorithm, which MySQL uses): dotted tail for
    IPv4-compatible / mapped addresses."""
    words = [(b[2 * i] << 8) | b[2 * i + 1] for i in range(8)]
    best_base, best_len, cur_base, cur_len = -1, 0, -1, 0
    for i, w in enumerate(words + [1]):                 # sentinel closes a trailing run
        if w == 0:
            if cur_base == -1:
                cur_base, cur_len = i, 1
            else:
                cur_len += 1
        elif cur_base != -1:
            if best_base == -1 or cur_len > best_len:
                best_base, best_len = cur_base, cur_len
            cur_base = -1
    if best_base != -1 and best_len < 2:
        best_base = -1
    out: list[str] = []
    for i in range(8):
        if best_base != -1 and best_base <= i < best_base + best_len:
            if i == best_base:
                out.append(":")
            continue
        if i != 0:
            out.append(":")
        if i == 6 and best_base == 0 and (best_len == 6 or (best_len == 7 and words[7] != 1)
                                          or (best_len == 5 and words[5] == 0xFFFF)):
            out.append(".".join(str(x) for x in b[12:]))
            break
        out.append("%x" % words[i])
    if best_base != -1 and best_base + best_len == 8:
        out.append(":")
    return "".join(out)


def f_inet6_ntoa(b):
    if b is None:
        return None
    b = bytes(b)
    if len(b) == 4:
        return ".".join(str(x) for x in b)
    if len(b) == 16:
        return _ntop6(b)
    return None


def f_is_ipv4(s):
    return None if s is None else int(_v4_bytes(s) is not None)


def f_is_ipv6(s):
    return None if s is None else int(_v6_bytes(s) is not None)


def f_is_ipv4_compat(b):
    if b is None:
        return None
    b = bytes(b)
    return int(len(b) == 16 and b[:12] == bytes(12) and int.from_bytes(b[12:], "big") > 1)


def f_is_ipv4_mapped(b):
    if b is None:
        return None
    b = bytes(b)
    return int(len(b) == 16 and b[:10] == bytes(10) and b[10:12] == b"\xff\xff")


_UUID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")


def _uuid_hex(s):
    if s is None:
        return None
    if len(s) >= 2 and s[0] == "{" and s[-1] == "}":
        s = s[1:-1]
    if re.fullmatch(r"[0-9a-fA-F]{32}", s) or (
            re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", s)):
        return s.replace("-", "").lower()
    return None


def f_is_uuid(s):
    return None if s is None else int(_uuid_hex(s) is not None)


def f_uuid_to_bin(s, swap):
    h = _uuid_hex(s)
    if h is None:
        return None
    if swap:
        h = h[12:16] + h[8:12] + h[0:8] + h[16:]
    return bytes.fromhex(h)


def f_bin_to_uuid(b, swap):
    if b is None:
        return None
    b = bytes(b)
    if len(b) != 16:
        return None
    h = b.hex()
    if swap:
        h = h[8:16] + h[4:8] + h[0:4] + h[16:]
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


_uuid_short = {"n": 0, "lock": threading.Lock(), "base": (1 << 56) + (int(time.time()) << 24)}


def f_uuid_short():
    with _uuid_short["lock"]:
        _uuid_short["n"] += 1
        return _uuid_short["base"] + _uuid_short["n"]


def f_sleep(secs):
    if secs is None or secs <= 0:
        return 0
    time.sleep(secs)
    return 0


def f_json_format(s):
    """Compact JSON text -> MySQL's spacing ('{"a": 1, "b": [1, 2]}'); [] / {} -> NULL."""
    if s is None or s in ("[]", "{}"):
        return None
    out: list[str] = []
    in_str = esc = False
    for ch in s:
        out.append(ch)
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in ",:":
            out.append(" ")
    return "".join(out)


# --------------------------------------------------------------------------- #
#  Registration
# --------------------------------------------------------------------------- #
def register(con) -> None:
    V, B, BLOB = H.VARCHAR, H.BIGINT, H.T.BLOB
    u = lambda name, fn, params, ret: H.udf(con, name, fn, params, ret)  # noqa: E731
    u("_my_inet_aton", f_inet_aton, [V], B)
    u("_my_inet_ntoa", f_inet_ntoa, [B], V)
    u("_my_inet6_aton", f_inet6_aton, [V], BLOB)
    u("_my_inet6_ntoa", f_inet6_ntoa, [BLOB], V)
    u("_my_is_ipv4", f_is_ipv4, [V], B)
    u("_my_is_ipv6", f_is_ipv6, [V], B)
    u("_my_is_ipv4_compat", f_is_ipv4_compat, [BLOB], B)
    u("_my_is_ipv4_mapped", f_is_ipv4_mapped, [BLOB], B)
    u("_my_is_uuid", f_is_uuid, [V], B)
    u("_my_uuid_to_bin", f_uuid_to_bin, [V, B], BLOB)
    u("_my_bin_to_uuid", f_bin_to_uuid, [BLOB, B], V)
    u("_my_jfmt", f_json_format, [V], V)
    # side effects: must be evaluated per row, never folded into a constant
    con.create_function("_my_uuid_short", f_uuid_short, [], B, side_effects=True)
    con.create_function("_my_sleep", f_sleep, [H.DOUBLE], B, null_handling="special",
                        exception_handling="return_null", side_effects=True)
    # MySQL ordering: NULL is the smallest value (first for ASC, last for DESC)
    con.execute("SET default_null_order = 'nulls_first_on_asc_last_on_desc'")
    # stable output order for window partitions / GROUP BY without ORDER BY
    con.execute("SET threads = 1")


# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
_INT_TYPES = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
              "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")

PRELUDE: list[str] = [
    # truth value / NOT / XOR on MySQL terms: numbers and strings are converted to numbers
    "CREATE OR REPLACE TEMP MACRO _my_not(x) AS "
    "CASE WHEN x IS NULL THEN NULL WHEN COALESCE(TRY_CAST(x AS DOUBLE), 0) = 0 THEN 1 ELSE 0 END",
    "CREATE OR REPLACE TEMP MACRO _my_xor(a, b) AS "
    "CASE WHEN a IS NULL OR b IS NULL THEN NULL "
    "WHEN (COALESCE(TRY_CAST(a AS DOUBLE), 0) <> 0) = (COALESCE(TRY_CAST(b AS DOUBLE), 0) <> 0) "
    "THEN 0 ELSE 1 END",
    # DISTINCT inside aggregates is not collation aware in DuckDB: compare text case-insensitively
    "CREATE OR REPLACE TEMP MACRO _my_ci(x VARCHAR) AS lower(x), (x) AS x",
    # AVG: DECIMAL for integers (4 extra digits) and decimals; DOUBLE stays DOUBLE
    "CREATE OR REPLACE TEMP MACRO _my_avg(x DECIMAL) AS CAST(system.main.avg(x) AS DECIMAL(38, 6)), "
    "(x DOUBLE) AS system.main.avg(x), "
    + ", ".join(f"(x {t}) AS CAST(system.main.avg(x) AS DECIMAL(38, 4))" for t in _INT_TYPES)
    + ", (x) AS system.main.avg(x)",
    # BIT_* results are BIGINT UNSIGNED
    "CREATE OR REPLACE TEMP MACRO _my_u64(r) AS "
    "CASE WHEN r < 0 THEN CAST(r AS HUGEINT) + 18446744073709551616 ELSE CAST(r AS HUGEINT) END",
    # network addresses
    "CREATE OR REPLACE TEMP MACRO inet_aton(x) AS _my_inet_aton(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO inet_ntoa(n) AS _my_inet_ntoa(TRY_CAST(n AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO inet6_aton(x) AS _my_inet6_aton(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO inet6_ntoa(b) AS _my_inet6_ntoa(CAST(b AS BLOB))",
    "CREATE OR REPLACE TEMP MACRO is_ipv4(x) AS _my_is_ipv4(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO is_ipv6(x) AS _my_is_ipv6(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO is_ipv4_compat(b) AS _my_is_ipv4_compat(CAST(b AS BLOB))",
    "CREATE OR REPLACE TEMP MACRO is_ipv4_mapped(b) AS _my_is_ipv4_mapped(CAST(b AS BLOB))",
    # UUIDs
    "CREATE OR REPLACE TEMP MACRO is_uuid(x) AS _my_is_uuid(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO uuid_to_bin(x) AS _my_uuid_to_bin(CAST(x AS VARCHAR), 0), "
    "(x, f) AS _my_uuid_to_bin(CAST(x AS VARCHAR), CAST(f AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO bin_to_uuid(b) AS _my_bin_to_uuid(CAST(b AS BLOB), 0), "
    "(b, f) AS _my_bin_to_uuid(CAST(b AS BLOB), CAST(f AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO uuid() AS CAST(system.main.uuid() AS VARCHAR)",
    "CREATE OR REPLACE TEMP MACRO uuid_short() AS _my_uuid_short()",
    "CREATE OR REPLACE TEMP MACRO sleep(n) AS _my_sleep(CAST(n AS DOUBLE))",
]

HANDLERS = {
    "GROUP_CONCAT": h_group_concat,
    "COUNT": h_count,
    "STD": _rename("stddev_pop"),
    "STDDEV": _rename("stddev_pop"),
    "VARIANCE": _rename("var_pop"),
    "NAME_CONST": h_name_const,
}

PRE_RULES: list = [rule_bang, rule_xor, rule_window_defaults, rule_rollup, rule_aggregates]
POST_RULES: list = []
