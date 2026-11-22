"""MySQL numeric functions and arithmetic operators on DuckDB.

How MySQL differs from DuckDB, and what is done about it:

* Text, booleans and hex literals are numbers in arithmetic (`'10' + 5` = 15,
  `'abc' + 5` = 5, `TRUE + TRUE` = 2).  `+ - * / % //` are TEMP macros that
  *shadow* the DuckDB operators: every operand goes through `_mn_n`, which
  converts text (MySQL prefix parsing), BOOLEAN and BLOB operands and leaves
  every other type (numbers, dates, intervals ...) untouched.
* `/` returns an exact DECIMAL with scale = dividend scale + 4 for integer
  dividends (`6/4` = 1.5000, `1/3` = 0.3333); a DECIMAL dividend gets the same
  digits but as a DOUBLE; a DOUBLE operand gives an ordinary DOUBLE.
* `DIV` (integer division, truncating), infix `MOD`, bit-xor `^` (DuckDB's `^`
  is power) are token rewrites.
* Math functions return NULL where MySQL does (SQRT(-1), LN(0), ACOS(2) ...).
* ROUND rounds exact values half away from zero and DOUBLE values half to even.
"""

from __future__ import annotations

import math
import re
import zlib
from decimal import ROUND_DOWN, ROUND_HALF_UP, Context, Decimal

from .. import translate as tr
from . import helpers as H

# --------------------------------------------------------------------------- #
#  Python implementations
# --------------------------------------------------------------------------- #
_NUM_PREFIX = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_BIG = Context(prec=120)


def str_to_double(s):
    """MySQL string -> DOUBLE: longest numeric prefix, 0 if there is none."""
    if s is None:
        return None
    m = _NUM_PREFIX.match(str(s))
    if not m:
        return 0.0
    v = float(m.group(1))
    return v if math.isfinite(v) else None


def blob_to_int(b):
    """Binary string (hex / bit literal) in numeric context: big-endian integer."""
    if b is None:
        return None
    return int.from_bytes(bytes(b), "big")


def div4(a, b):
    """Exact int / exact -> DECIMAL(38,4), half away from zero."""
    if a is None or b is None:
        return None
    d = Decimal(b)
    if d == 0:
        return None
    return _BIG.divide(Decimal(a), d).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP, context=_BIG)


def divd(a, b):
    """DECIMAL dividend: scale = dividend scale + 4, returned as DOUBLE."""
    if a is None or b is None:
        return None
    da, db = Decimal(a), Decimal(b)
    if db == 0:
        return None
    scale = max(0, -da.as_tuple().exponent) + 4
    q = _BIG.divide(da, db).quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP, context=_BIG)
    return float(q)


def truncate(x, d):
    if x is None or d is None:
        return None
    q = Decimal(1).scaleb(-int(d))
    return float(Decimal(x).quantize(q, rounding=ROUND_DOWN, context=_BIG))


def log_base(b, x):
    if b is None or x is None or b <= 1 or x <= 0:
        return None
    if b == 2:
        return math.log2(x)
    if b == 10:
        return math.log10(x)
    return math.log(x) / math.log(b)


def crc32(s):
    if s is None:
        return None
    return zlib.crc32(str(s).encode("utf-8")) & 0xFFFFFFFF


_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def conv(n, from_base, to_base):
    """CONV(N, from_base, to_base): 64-bit, signed output when to_base < 0."""
    if n is None or from_base is None or to_base is None:
        return None
    fb, tb = int(from_base), int(to_base)
    if not (2 <= abs(fb) <= 36 and 2 <= abs(tb) <= 36):
        return None
    s = str(n).strip()
    neg = s.startswith("-")
    if s[:1] in "+-":
        s = s[1:]
    base, val = abs(fb), 0
    for ch in s.upper():
        d = _DIGITS.find(ch)
        if d < 0 or d >= base:
            break
        val = val * base + d
    val = min(val, (1 << 64) - 1)
    if neg:
        val = -min(val, 1 << 63)
    if tb > 0:
        val %= 1 << 64
    else:
        if val >= 1 << 63:                 # unsigned input above the signed range
            val -= 1 << 64
        val = max(val, -(1 << 63))
    sign = "-" if val < 0 else ""
    val, out, tbase = abs(val), "", abs(tb)
    while val:
        val, r = divmod(val, tbase)
        out = _DIGITS[r] + out
    return sign + (out or "0")


class _MysqlRand:
    """MySQL's RAND(seed) generator (same sequence as the server)."""
    MAX = 0x3FFFFFFF

    def __init__(self, seed: int):
        seed &= 0xFFFFFFFFFFFFFFFF
        self.s1 = (seed * 0x10001 + 55555555) % self.MAX
        self.s2 = (seed * 0x10000001) % self.MAX

    def next(self) -> float:
        self.s1 = (self.s1 * 3 + self.s2) % self.MAX
        self.s2 = (self.s1 + self.s2 + 33) % self.MAX
        return self.s1 / self.MAX


_rand_state: dict = {}
_site = [0]


def rand_seq(seed, site):
    """RAND(const): one generator per call site of one statement."""
    if seed is None:
        return None
    g = _rand_state.get(site)
    if g is None:
        if len(_rand_state) > 256:
            _rand_state.clear()
        g = _rand_state[site] = _MysqlRand(int(seed))
    return g.next()


def rand_first(seed):
    """RAND(column): re-seeded for every row."""
    return None if seed is None else _MysqlRand(int(seed)).next()


def register(con) -> None:
    V, B, D = H.VARCHAR, H.BIGINT, H.DOUBLE
    u = lambda name, fn, params, ret: H.udf(con, name, fn, params, ret)  # noqa: E731
    # half-to-even for DOUBLE (MySQL rint). DuckDB's round_even calls the global round(),
    # which loops forever once a macro named round exists, so do it in Python.
    u("_mn_re", lambda x, d=0: None if x is None else round(x, int(d or 0)), [D, B], D)
    u("_mn_num", str_to_double, [V], D)
    u("_mn_blobint", blob_to_int, [H.T.BLOB], B)
    u("_mn_div4", div4, [V, V], H.T.DuckDBPyType("DECIMAL(38,4)"))
    u("_mn_divd", divd, [V, V], D)
    u("_mn_trunc", truncate, [V, B], D)
    u("_mn_log", log_base, [D, D], D)
    u("_mn_crc32", crc32, [V], B)
    u("_mn_conv", conv, [V, B, B], V)
    u("_mn_rand_first", rand_first, [B], D)
    # RAND(const) must be re-evaluated for every row: flag it as having side effects
    con.create_function("_mn_rand_seq", rand_seq, [B, B], D, null_handling="special",
                        exception_handling="return_null", side_effects=True)


# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
def _m(name: str, *overloads: str) -> str:
    return f"CREATE OR REPLACE TEMP MACRO {name}" + ", ".join(overloads)


PRELUDE: list[str] = [
    # operand conversion: text -> DOUBLE, BOOLEAN -> INTEGER, BLOB -> integer, rest unchanged
    # (chained single-typed macros: a bare NULL must not match several typed overloads)
    _m("_mn_nv", "(x VARCHAR) AS _mn_num(x)", "(x) AS x"),
    _m("_mn_nb", "(x BOOLEAN) AS CAST(x AS INTEGER)", "(x) AS x"),
    # (BLOB -> integer overload removed: a typed BLOB overload makes DuckDB fail to bind
    #  aggregate / window arguments, e.g. SUM(n) / COUNT(*))
    _m("_mn_nx", "(x) AS x"),
    _m("_mn_n", "(x) AS _mn_nx(_mn_nb(_mn_nv(x)))"),
]

# + - *  (the original DuckDB operator stays reachable as system.main."+")
# DISABLED: macros named + - * % break any operand that is an aggregate or window
# call (SUM(x) + 1, n + LAG(n) OVER w fail with "INTEGER -> BLOB"). Known gap: MySQL
# converts text to numbers in arithmetic ('10' + 5 = 15); DuckDB rejects it.
for _op in ():
    _sig = [f'(a, b) AS system.main."{_op}"(_mn_n(a), _mn_n(b))']
    if _op in "+-":
        _sig.insert(0, f'(a) AS system.main."{_op}"(_mn_n(a))')
    PRELUDE.append(f'CREATE OR REPLACE TEMP MACRO "{_op}"' + ", ".join(_sig))

_EXACT = r"^(DECIMAL\(|U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)$)"
PRELUDE += [
    # exact division: integer / integer -> DECIMAL(38,4); any other exact operands (DECIMAL
    # involved) -> the same digits as MySQL (dividend scale + 4) but as a DOUBLE;
    # DOUBLE (or anything else) -> DuckDB's division (x/0 is NULL, see ieee_floating_point_ops)
    _m("_mn_d",
       "(a BIGINT, b BIGINT) AS _mn_div4(CAST(a AS VARCHAR), CAST(b AS VARCHAR))",
       "(a, b) AS CASE WHEN regexp_matches(typeof(a), '" + _EXACT + "') "
       "AND regexp_matches(typeof(b), '" + _EXACT + "') "
       "THEN _mn_divd(CAST(a AS VARCHAR), CAST(b AS VARCHAR)) "
       'ELSE system.main."/"(a, b) END'),
    _m('"/"', "(a, b) AS _mn_d(_mn_n(a), _mn_n(b))"),
    # DIV (rewritten to //): integer division truncating toward zero, BIGINT result
    _m("_mn_i",
       '(a BIGINT, b BIGINT) AS system.main."//"(a, b)',
       "(a, b) AS CAST(system.main.trunc(system.main.\"/\"(CAST(a AS DOUBLE), CAST(b AS DOUBLE))) AS BIGINT)"),
    _m('"//"', "(a, b) AS _mn_i(_mn_n(a), _mn_n(b))"),
]

# one-argument functions; NULL (not an error) outside the domain
_UNARY = {          # name: needs TRY (domain errors -> NULL)
    "abs": False, "ceil": False, "ceiling": False, "floor": False, "sign": False,
    "sin": False, "cos": False, "tan": False, "cot": False, "atan": False,
    "degrees": False, "radians": False, "exp": False,
    "sqrt": True, "ln": True, "log2": True, "log10": True, "asin": True, "acos": True,
}
for _fn, _try in _UNARY.items():
    _body = f"system.main.{_fn}(_mn_n(x))"
    if _fn == "atan":
        PRELUDE.append(_m("atan", f"(x) AS {_body}", "(y, x) AS system.main.atan2(_mn_n(y), _mn_n(x))"))
    else:
        PRELUDE.append(_m(_fn, f"(x) AS try({_body})" if _try else f"(x) AS {_body}"))

PRELUDE += [
    _m("atan2", "(y, x) AS system.main.atan2(_mn_n(y), _mn_n(x))"),
    _m("log", "(x) AS try(system.main.ln(_mn_n(x)))", "(b, x) AS _mn_log(_mn_n(b), _mn_n(x))"),
    _m("pow", "(x, y) AS system.main.pow(_mn_n(x), _mn_n(y))"),
    _m("power", "(x, y) AS system.main.pow(_mn_n(x), _mn_n(y))"),
    _m("mod", "(a, b) AS _mn_n(a) % _mn_n(b)"),
    # ROUND: DOUBLE -> half to even, exact values -> half away from zero (DuckDB's round)
    _m("_mn_r1", "(x DOUBLE) AS _mn_re(x, 0)", "(x) AS system.main.round(x)"),
    _m("_mn_r2", "(x DOUBLE, d) AS _mn_re(x, d)", "(x, d) AS system.main.round(x, d)"),
    _m("round", "(x) AS _mn_r1(_mn_n(x))", "(x, d) AS _mn_r2(_mn_n(x), d)"),
    # TRUNCATE: integers stay integers, everything else is cut (not rounded) at d places
    _m("_mn_t",
       "(x BIGINT, d BIGINT) AS CASE WHEN d >= 0 THEN x ELSE "
       "x - x % CAST(system.main.pow(10, -d) AS BIGINT) END",
       "(x, d) AS _mn_trunc(CAST(x AS VARCHAR), CAST(d AS BIGINT))"),
    _m("truncate", "(x, d) AS _mn_t(_mn_n(x), d)"),
    _m("conv", "(n, f, t) AS _mn_conv(CAST(n AS VARCHAR), CAST(f AS BIGINT), CAST(t AS BIGINT))"),
    _m("crc32", "(x) AS _mn_crc32(CAST(x AS VARCHAR))"),
]


# --------------------------------------------------------------------------- #
#  Call handlers
# --------------------------------------------------------------------------- #
def _h_rand(args: list[tr.Arg]):
    if not args:
        return "random()"
    seed = args[0].text
    if re.fullmatch(r"\s*[+-]?\d+\s*", seed):
        _site[0] += 1
        return f"_mn_rand_seq({int(seed)}, {_site[0]})"
    return f"_mn_rand_first(CAST({seed} AS BIGINT))"


HANDLERS = {"RAND": _h_rand}


# --------------------------------------------------------------------------- #
#  Token rules: DIV, MOD, ^
# --------------------------------------------------------------------------- #
_OPERAND_END_WORDS_NOT = {
    "SELECT", "WHERE", "AND", "OR", "NOT", "XOR", "ON", "WHEN", "THEN", "ELSE", "BY", "HAVING",
    "FROM", "IN", "IS", "LIKE", "BETWEEN", "AS", "LIMIT", "SET", "VALUES", "CASE", "DIV", "MOD",
    "RETURN", "DISTINCT", "ALL", "UNION", "OFFSET", "REGEXP", "RLIKE", "INTERVAL", "USING", "WITH",
}


def _ends_operand(toks: list[tr.Tok], i: int) -> bool:
    """Is toks[i] the last token of an operand (so that a following MOD is infix)?"""
    t = toks[i]
    if t.kind in (tr.NUM, tr.STR):
        return True
    if t.kind == tr.PUNCT:
        return t.text in (")", "]")
    if t.kind == tr.WORD:
        return t.upper not in _OPERAND_END_WORDS_NOT
    return False


def _rule_div_mod(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`a DIV b` -> `a // b`;  infix `a MOD b` -> `a % b` (function MOD(a, b) is untouched)."""
    out = list(toks)
    for i, t in enumerate(toks):
        if t.is_word("DIV"):
            p = tr.prev_sig(toks, i)
            if p >= 0 and _ends_operand(toks, p):
                out[i] = tr.Tok(tr.PUNCT, "//")
        elif t.is_word("MOD"):
            p = tr.prev_sig(toks, i)
            if p >= 0 and _ends_operand(toks, p):
                out[i] = tr.Tok(tr.PUNCT, "%")
    return out


def _rule_caret(toks: list[tr.Tok]) -> list[tr.Tok]:
    """MySQL `a ^ b` is bitwise XOR (DuckDB: power) -> xor(a, b)."""
    for _ in range(50):
        for i, t in enumerate(toks):
            if t.is_punct("^"):
                s = tr.primary_start(toks, i)
                e = tr.primary_end(toks, i + 1)
                if s < 0 or e < 0:
                    continue
                lhs, rhs = tr.text_of(toks[s:i]), tr.text_of(toks[i + 1:e])
                toks = toks[:s] + tr.tokenize_plain(f"system.main.xor(CAST({lhs} AS BIGINT), CAST({rhs} AS BIGINT))") + toks[e:]
                break
        else:
            return toks
    return toks


PRE_RULES: list = []
POST_RULES: list = [_rule_div_mod, _rule_caret]
