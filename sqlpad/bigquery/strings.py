"""BigQuery (GoogleSQL) string / bytes functions, REGEXP_* and FORMAT on DuckDB.

Python UDFs named `_bqs_*` hold the GoogleSQL semantics (RE2 regular expressions
via the `regex` module, code points, base32/64, FORMAT, ...).  Public names are
TEMP macros that choose between the STRING and the BYTES flavour with typeof()
and call the UDF; HANDLERS rewrite calls macros cannot express (FORMAT with
varargs, named arguments such as json_scope=>, REGEXP_EXTRACT_GROUPS whose
result struct depends on the regular expression, NORMALIZE modes, COLLATE).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata

import duckdb

from .. import translate as tr
from . import helpers as H

try:                                    # RE2-compatible enough, and has \p{L}
    import regex as _rx
except ImportError:                     # pragma: no cover
    _rx = re

V, B, BOOL = H.VARCHAR, H.BIGINT, H.BOOLEAN
BLOB = H.T.BLOB
LV, LB = duckdb.list_type(V), duckdb.list_type(B)

# --------------------------------------------------------------------------- #
#  Regular expressions
# --------------------------------------------------------------------------- #
_cache: dict = {}


def rx(pattern: str):
    c = _cache.get(pattern)
    if c is None:
        try:
            c = _rx.compile(pattern)
        except Exception:               # noqa: BLE001 - e.g. (?<n>..) on plain `re`
            c = _rx.compile(re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pattern))
        _cache[pattern] = c
    return c


def f_rx_contains(s, p):
    if s is None or p is None:
        return None
    return rx(p).search(s) is not None


def _matches(s, p, pos, occ):
    """The occ-th match of p in s starting at 1-based pos, or None."""
    if pos < 1 or occ < 1:
        raise ValueError("position/occurrence must be positive")
    r = rx(p)
    for i, m in enumerate(r.finditer(s, pos - 1), 1):
        if i == occ:
            return r, m
    return r, None


def f_rx_extract(s, p, pos, occ):
    if s is None or p is None or pos is None or occ is None:
        return None
    r, m = _matches(s, p, pos, occ)
    if r.groups > 1:
        raise ValueError("regexp has more than one capturing group")
    if m is None:
        return None
    return m.group(1) if r.groups == 1 else m.group(0)


def f_rx_extract_all(s, p):
    if s is None or p is None:
        return None
    r = rx(p)
    if r.groups > 1:
        raise ValueError("regexp has more than one capturing group")
    return [m.group(1) if r.groups == 1 else m.group(0) for m in r.finditer(s)]


def f_rx_instr(s, p, pos, occ, occpos):
    if s is None or p is None or pos is None or occ is None or occpos is None:
        return None
    r, m = _matches(s, p, pos, occ)
    if m is None:
        return 0
    return m.end() + 1 if occpos == 1 else m.start() + 1


def f_rx_replace(s, p, repl):
    if s is None or p is None or repl is None:
        return None

    def expand(m):
        out, i = [], 0
        while i < len(repl):
            c = repl[i]
            if c == "\\" and i + 1 < len(repl):
                n = repl[i + 1]
                if n.isdigit():
                    k = int(n)
                    out.append(m.group(k) or "" if k <= m.re.groups else "")
                elif n == "\\":
                    out.append("\\")
                else:
                    raise ValueError("invalid escape in replacement")
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return rx(p).sub(expand, s)


def f_rx_group(s, p, idx):
    """Text of capture group idx (1-based) or None; used by REGEXP_EXTRACT_GROUPS."""
    if s is None or p is None:
        return None
    m = rx(p).search(s)
    return None if m is None else m.group(idx)


def f_rx_group_int(s, p, idx):
    t = f_rx_group(s, p, idx)
    if t is None:
        return None
    t = t.strip()
    return int(t, 16) if t.lower().startswith(("0x", "-0x")) else int(t, 10)


def regex_groups(pattern: str) -> list[tuple[str | None, str | None]]:
    """[(name, type_suffix)] for each capturing group of an RE2 pattern."""
    out: list[tuple[str | None, str | None]] = []
    i, n, in_class = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if pattern.startswith("[^]", i) or pattern.startswith("[]", i):
                i += 1
        elif c == "(":
            m = re.match(r"\(\?P?<([A-Za-z_]\w*)>", pattern[i:])
            if m:
                name, typ = m.group(1), None
                if "__" in name:
                    name, typ = name.rsplit("__", 1)
                out.append((name, typ))
            elif not pattern.startswith("(?", i):
                out.append((None, None))
        i += 1
    return out


# --------------------------------------------------------------------------- #
#  Characters, code points, positions
# --------------------------------------------------------------------------- #
def f_chr(n):
    if n is None:
        return None
    if n == 0:
        return ""
    if n < 0 or n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
        raise ValueError("invalid code point")
    return chr(n)


def f_cp_to_str(a):
    if a is None or any(x is None for x in a):
        return None
    return "".join(f_chr(x) for x in a)


def f_cp_to_bytes(a):
    if a is None or any(x is None for x in a):
        return None
    return bytes(a)


def f_to_cp_s(s):
    return None if s is None else [ord(c) for c in s]


def f_to_cp_b(b):
    return None if b is None else list(b)


def f_unicode(s):
    if s is None:
        return None
    return ord(s[0]) if s else 0


def f_instr(s, sub, pos, occ):
    if s is None or sub is None or pos is None or occ is None:
        return None
    if pos == 0 or occ < 1:
        raise ValueError("invalid position/occurrence")
    if not sub:
        return 0
    if pos > 0:
        start = pos - 1
        idx = start - 1
        for _ in range(occ):
            idx = s.find(sub, idx + 1)
            if idx < 0:
                return 0
        return idx + 1
    end = len(s) + pos + len(sub)               # backwards; occurrences may overlap
    res = -1
    for _ in range(occ):
        res = s.rfind(sub, 0, end)
        if res < 0:
            return 0
        end = res + len(sub) - 1
    return res + 1


def f_left(s, n):
    if s is None or n is None:
        return None
    if n < 0:
        raise ValueError("negative length")
    return s[:n]


def f_right(s, n):
    if s is None or n is None:
        return None
    if n < 0:
        raise ValueError("negative length")
    return s[len(s) - n:] if n else s[:0]


def f_reverse(s):
    return None if s is None else s[::-1]


def _pad(s, n, p, left):
    if s is None or n is None or p is None:
        return None
    if n < 0 or len(p) == 0:
        raise ValueError("invalid padding")
    if n <= len(s):
        return s[:n]
    need = n - len(s)
    fill = (p * (need // len(p) + 1))[:need]
    return fill + s if left else s + fill


def f_lpad(s, n, p):
    return _pad(s, n, p, True)


def f_rpad(s, n, p):
    return _pad(s, n, p, False)


def f_trim_s(s, chars):
    return None if s is None else (s.strip(chars) if chars is not None else s.strip())


def f_ltrim_s(s, chars):
    return None if s is None else (s.lstrip(chars) if chars is not None else s.lstrip())


def f_rtrim_s(s, chars):
    return None if s is None else (s.rstrip(chars) if chars is not None else s.rstrip())


def f_trim_b(s, chars):
    return None if s is None or chars is None else s.strip(chars)


def f_ltrim_b(s, chars):
    return None if s is None or chars is None else s.lstrip(chars)


def f_rtrim_b(s, chars):
    return None if s is None or chars is None else s.rstrip(chars)


_INITCAP_DELIMS = set(" \t\n\r\f\v[](){}/|\\<>!?@\"^#$&~_,.:;*%+-")


def f_initcap(s, delims):
    if s is None:
        return None
    dl = _INITCAP_DELIMS if delims is None else set(delims)
    out, up = [], True
    for c in s:
        out.append(c.upper() if up else c.lower())
        up = c in dl
    return "".join(out)


def f_edit_distance(a, b, mx):
    if a is None or b is None:
        return None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    d = prev[-1]
    return min(d, mx) if mx is not None else d


def f_soundex(s):
    if s is None:
        return None
    codes = {c: d for d, cs in {"1": "BFPV", "2": "CGJKQSXZ", "3": "DT", "4": "L",
                                "5": "MN", "6": "R"}.items() for c in cs}
    letters = [c for c in s.upper() if c.isalpha() and c.isascii()]
    if not letters:
        return ""
    out = letters[0]
    last = codes.get(letters[0], "")
    for c in letters[1:]:
        d = codes.get(c, "")
        if d and d != last:
            out += d
        if c not in "HW":
            last = d
        if len(out) == 4:
            break
    return out.ljust(4, "0")


def f_normalize(s, mode):
    if s is None:
        return None
    return unicodedata.normalize(mode or "NFC", s)


def f_normalize_cf(s, mode):
    if s is None:
        return None
    mode = mode or "NFC"
    return unicodedata.normalize(mode, unicodedata.normalize(mode, s).casefold())


def f_safe_bytes_to_string(b):
    return None if b is None else bytes(b).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
#  Bytes <-> text
# --------------------------------------------------------------------------- #
def f_to_hex(b):
    return None if b is None else bytes(b).hex()


def f_from_hex(s):
    if s is None:
        return None
    if len(s) % 2:
        s = "0" + s
    return binascii.unhexlify(s)


def f_to_base32(b):
    return None if b is None else base64.b32encode(bytes(b)).decode("ascii")


def f_from_base32(s):
    return None if s is None else base64.b32decode(s)


def f_to_base64(b):
    return None if b is None else base64.b64encode(bytes(b)).decode("ascii")


def f_from_base64(s):
    return None if s is None else base64.b64decode(s, validate=True)


# --------------------------------------------------------------------------- #
#  CONTAINS_SUBSTR
# --------------------------------------------------------------------------- #
def _nf(x: str) -> str:
    return unicodedata.normalize("NFKC", x).casefold()


def _json_texts(node, scope, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if "KEYS" in scope:
                out.append(str(k))
            _json_texts(v, scope, out)
    elif isinstance(node, list):
        for v in node:
            _json_texts(v, scope, out)
    elif node is not None and "VALUES" in scope:
        out.append(node if isinstance(node, str) else json.dumps(node))


def f_contains_substr(v, typ, t, scope):
    if v is None or t is None:
        return None
    t = _nf(t)
    if typ == "JSON":
        texts: list = []
        _json_texts(json.loads(v), (scope or "JSON_VALUES").upper(), texts)
        return any(t in _nf(x) for x in texts)
    return t in _nf(v)


def f_contains_substr_list(parts, t):
    if t is None:
        return None
    t = _nf(t)
    if any(p is not None and t in _nf(p) for p in parts):
        return True
    return None if any(p is None for p in parts) else False


# --------------------------------------------------------------------------- #
#  FORMAT
# --------------------------------------------------------------------------- #
_FMT_RE = re.compile(r"%([-+ 0#']*)(\d+)?(?:\.(\d+))?([A-Za-z%])")


def _group(digits: str, size: int, sep: str) -> str:
    parts = []
    while len(digits) > size:
        parts.append(digits[-size:])
        digits = digits[:-size]
    parts.append(digits)
    return sep.join(reversed(parts))


def _padded(sign: str, body: str, width, flags: str, zero_ok: bool = True) -> str:
    w = int(width) if width else 0
    if "-" in flags:
        return (sign + body).ljust(w)
    if "0" in flags and zero_ok:
        return sign + body.rjust(w - len(sign), "0")
    return (sign + body).rjust(w)


def _quote_text(s: str, prefix: str = "") -> str:
    esc = (s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
           .replace("\r", "\\r").replace("\t", "\\t"))
    return f'{prefix}"{esc}"'


def _fmt_one(flags, width, prec, spec, typ, val):
    if val is None:
        if spec in "stT":
            return _padded("", "NULL", width, flags.replace("0", ""))
        return None
    if spec in "di":
        try:
            n = int(val)
        except ValueError:
            n = int(float(val))
        sign = "-" if n < 0 else "+" if "+" in flags else " " if " " in flags else ""
        digits = str(abs(n))
        if "'" in flags:
            digits = _group(digits, 3, ",")
        return _padded(sign, digits, width, flags, zero_ok=prec is None)
    if spec in "xXo":
        n = int(val)
        if n < 0:
            n += 1 << 64
        digits = format(n, {"x": "x", "X": "X", "o": "o"}[spec])
        if "'" in flags:
            digits = _group(digits, 4, ":" if spec in "xX" else ",")
        prefix = ("0x" if spec == "x" else "0X" if spec == "X" else "0") if "#" in flags and n else ""
        return _padded(prefix, digits, width, flags)
    if spec in "fFeEgG":
        x = float(val)
        p = 6 if prec is None else int(prec)
        if x != x or x in (float("inf"), float("-inf")):
            body = "nan" if x != x else "inf"
            sign = "-" if x < 0 else ""
            return _padded(sign, body.upper() if spec.isupper() else body, width, flags, zero_ok=False)
        sign = "-" if x < 0 or (x == 0 and str(x).startswith("-")) else "+" if "+" in flags else " " if " " in flags else ""
        body = format(abs(x), ("#" if "#" in flags else "") + f".{p}{spec}")
        if "'" in flags and spec in "fF":
            ip, _, fp = body.partition(".")
            body = _group(ip, 3, ",") + ("." + fp if fp else "")
        return _padded(sign, body, width, flags)
    if spec == "s" or spec == "t":
        s = val if prec is None else val[:int(prec)]
        return _padded("", s, width, flags.replace("0", ""))
    if spec == "T":
        t = (typ or "").upper()
        if t.startswith("VARCHAR") or t == "JSON":
            s = _quote_text(val)
        elif t == "BLOB":
            s = _quote_text(val, "b")
        elif t == "DATE":
            s = _quote_text(val, "DATE ")
        elif t.startswith("TIMESTAMP WITH"):
            s = _quote_text(val, "TIMESTAMP ")
        elif t.startswith("TIMESTAMP"):
            s = _quote_text(val, "DATETIME ")
        elif t.startswith("TIME"):
            s = _quote_text(val, "TIME ")
        else:
            s = val
        return _padded("", s, width, flags.replace("0", ""))
    raise ValueError(f"unsupported format specifier %{spec}")


def f_format(fmt, types, vals):
    if fmt is None:
        return None
    out, pos, ai = [], 0, 0
    for m in _FMT_RE.finditer(fmt):
        out.append(fmt[pos:m.start()])
        pos = m.end()
        flags, width, prec, spec = m.groups()
        if spec == "%":
            out.append("%")
            continue
        if ai >= len(vals):
            raise ValueError("too few arguments for FORMAT")
        piece = _fmt_one(flags, width, prec, spec, types[ai], vals[ai])
        ai += 1
        if piece is None:
            return None
        out.append(piece)
    out.append(fmt[pos:])
    return "".join(out)


# --------------------------------------------------------------------------- #
#  Handlers
# --------------------------------------------------------------------------- #
def _sig(a: tr.Arg) -> list[tr.Tok]:
    return tr.strip(a.toks)


def h_format(args):
    if not args:
        return None
    rest = args[1:]
    if rest:
        types = "[" + ", ".join(f"typeof({a.text})" for a in rest) + "]"
        vals = "[" + ", ".join(f"CAST({a.text} AS VARCHAR)" for a in rest) + "]"
    else:
        types = vals = "CAST([] AS VARCHAR[])"
    return f"_bqs_format(CAST({args[0].text} AS VARCHAR), {types}, {vals})"


def _tuple_parts(a: tr.Arg):
    """`(x, y, z)` written as a row value -> [x, y, z] text list, else None."""
    s = _sig(a)
    if len(s) >= 2 and s[0].is_punct("(") and tr.match_paren(s, 0) == len(s) - 1:
        parts = tr.split_args(s, 1, len(s) - 1)
        if len(parts) > 1:
            return [tr.text_of(p) for p in parts]
    return None


def h_contains_substr(args):
    if len(args) < 2:
        return None
    scope = "JSON_VALUES"
    for a in args[2:]:
        m = re.match(r"(?i)json_scope\s*=\s*>\s*'(\w+)'", a.text)
        if m:
            scope = m.group(1)
    parts = _tuple_parts(args[0])
    if parts is not None:
        lst = "[" + ", ".join(f"CAST({p} AS VARCHAR)" for p in parts) + "]"
        return f"_bqs_contains_substr_list({lst}, CAST({args[1].text} AS VARCHAR))"
    e = args[0].text
    return (f"_bqs_contains_substr(CAST({e} AS VARCHAR), typeof({e}), "
            f"CAST({args[1].text} AS VARCHAR), '{scope}')")


def h_edit_distance(args):
    if len(args) < 2:
        return None
    mx = "NULL"
    for a in args[2:]:
        m = re.match(r"(?i)max_distance\s*=\s*>\s*(.+)$", a.text, re.S)
        if m:
            mx = m.group(1)
    return f"_bqs_edit_distance({args[0].text}, {args[1].text}, {mx})"


def _mode(args):
    if len(args) > 1:
        m = args[1].text.upper().strip("'")
        return m if m in ("NFC", "NFKC", "NFD", "NFKD") else None
    return "NFC"


def h_normalize(args):
    m = _mode(args)
    return None if m is None or not args else f"_bqs_normalize({args[0].text}, '{m}')"


def h_normalize_cf(args):
    m = _mode(args)
    return None if m is None or not args else f"_bqs_normalize_cf({args[0].text}, '{m}')"


_TYPE_CAST = {"INT64": "BIGINT", "FLOAT64": "DOUBLE", "BOOL": "BOOLEAN", "STRING": "VARCHAR",
              "NUMERIC": "DECIMAL(38,9)", "BIGNUMERIC": "DOUBLE", "DATE": "DATE",
              "TIMESTAMP": "TIMESTAMP WITH TIME ZONE", "DATETIME": "TIMESTAMP", "TIME": "TIME",
              "BYTES": "BLOB"}


def h_extract_groups(args):
    if len(args) != 2:
        return None
    pat = args[1].literal()
    if pat is None:
        return None
    groups = regex_groups(pat)
    if not groups:
        return None
    s, p = args[0].text, args[1].text
    fields = []
    for i, (name, typ) in enumerate(groups, 1):
        label = name or f"_field_{i}"
        if typ and typ.upper() == "INT64":
            expr = f"_bqs_rx_group_int({s}, {p}, {i})"
        elif typ and typ.upper() in _TYPE_CAST and typ.upper() != "STRING":
            expr = f"CAST(_bqs_rx_group({s}, {p}, {i}) AS {_TYPE_CAST[typ.upper()]})"
        else:
            expr = f"_bqs_rx_group({s}, {p}, {i})"
        fields.append(f"{tr.quote_ident(label)} := {expr}")
    return (f"_bqs_xg(CASE WHEN _bqs_regexp_contains({s}, {p}) "
            f"THEN struct_pack({', '.join(fields)}) END)")


HANDLERS: dict = {
    "FORMAT": h_format,
    "CONTAINS_SUBSTR": h_contains_substr,
    "EDIT_DISTANCE": h_edit_distance,
    "NORMALIZE": h_normalize,
    "NORMALIZE_AND_CASEFOLD": h_normalize_cf,
    "REGEXP_EXTRACT_GROUPS": h_extract_groups,
}


# --------------------------------------------------------------------------- #
#  Rules
# --------------------------------------------------------------------------- #
def hex_numbers(toks):
    """0xF9B5 -> 63925 (DuckDB's parser rejects the C-style literal)."""
    out = []
    for t in toks:
        if t.kind == tr.NUM and re.fullmatch(r"0[xX][0-9A-Fa-f]+", t.text):
            out.append(tr.Tok(tr.NUM, str(int(t.text, 16))))
        else:
            out.append(t)
    return out


def xg_unwrap(toks):
    """_bqs_xg(<struct>).*  ->  UNNEST(<struct>);  _bqs_xg(<struct>) -> <struct>."""
    while True:
        for i, t in enumerate(toks):
            if t.kind == tr.WORD and t.text == "_bqs_xg":
                k = tr.next_sig(toks, i)
                close = tr.match_paren(toks, k)
                inner = toks[k + 1:close]
                d = tr.next_sig(toks, close)
                st = tr.next_sig(toks, d) if d < len(toks) else len(toks)
                if (d < len(toks) and toks[d].is_punct(".") and st < len(toks)
                        and toks[st].is_punct("*")):
                    toks = (toks[:i] + tr.tokenize_plain("UNNEST(") + inner
                            + tr.tokenize_plain(")") + toks[st + 1:])
                else:
                    toks = toks[:i] + tr.tokenize_plain("(") + inner + tr.tokenize_plain(")") + toks[close + 1:]
                break
        else:
            return toks


def _bytes_of(value: str) -> bytes:
    """Bytes of a b'..' literal: escapes arrive as chars < 256 (latin-1), text as UTF-8."""
    return value.encode("latin-1") if all(ord(c) < 256 for c in value) else value.encode("utf-8")


def bytes_literals(toks):
    """b'..' holding non-ASCII text / NUL: DuckDB's STRING->BLOB cast rejects it, use hex."""
    out = []
    for t in toks:
        if (t.kind == tr.STR and "b" in t.prefix and t.value is not None
                and (not t.value.isascii() or "\0" in t.value)):
            out.append(tr.Tok(tr.STR, f"_bqs_from_hex('{_bytes_of(t.value).hex()}')", t.value, t.prefix))
        else:
            out.append(t)
    return out


PRE_RULES: list = [hex_numbers, bytes_literals]
POST_RULES: list = [xg_unwrap]


# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
def _dual(params, s_fn, b_fn, typed):
    """Typed macro overloads: (s VARCHAR, ..) -> STRING UDF, (s BLOB, ..) -> BYTES UDF."""
    names = ", ".join(params)

    def sig(t):
        return ", ".join(f"{p} {t}" if p in typed else p for p in params)
    return (f"({sig('VARCHAR')}) AS {s_fn}({names}), ({sig('BLOB')}) AS {b_fn}({names})")


def _macro(name, *defs):
    return f"CREATE OR REPLACE TEMP MACRO {name}" + ", ".join(defs)


PRELUDE: list[str] = [
    # --- regular expressions -------------------------------------------------
    _macro("regexp_contains", "(s, p) AS _bqs_regexp_contains(CAST(s AS VARCHAR), p)"),
    _macro("regexp_extract", "(s, p) AS _bqs_rx_extract(s, p, 1, 1)",
           "(s, p, pos) AS _bqs_rx_extract(s, p, pos, 1)",
           "(s, p, pos, occ) AS _bqs_rx_extract(s, p, pos, occ)"),
    _macro("regexp_substr", "(s, p) AS _bqs_rx_extract(s, p, 1, 1)",
           "(s, p, pos) AS _bqs_rx_extract(s, p, pos, 1)",
           "(s, p, pos, occ) AS _bqs_rx_extract(s, p, pos, occ)"),
    _macro("regexp_extract_all", "(s, p) AS _bqs_rx_extract_all(s, p)"),
    _macro("regexp_instr", "(s, p) AS _bqs_rx_instr(s, p, 1, 1, 0)",
           "(s, p, pos) AS _bqs_rx_instr(s, p, pos, 1, 0)",
           "(s, p, pos, occ) AS _bqs_rx_instr(s, p, pos, occ, 0)",
           "(s, p, pos, occ, op) AS _bqs_rx_instr(s, p, pos, occ, op)"),
    _macro("regexp_replace", "(s, p, r) AS _bqs_rx_replace(s, p, r)"),
    # --- splitting / simple helpers -----------------------------------------
    _macro("split", "(s) AS string_split(s, ',')", "(s, d) AS string_split(s, d)"),
    _macro("byte_length", "(x VARCHAR) AS strlen(x)", "(x BLOB) AS octet_length(x)"),
    _macro("chr", "(n) AS _bqs_chr(n)"),
    _macro("unicode", "(s) AS _bqs_unicode(s)"),
    _macro("initcap", "(s) AS _bqs_initcap(s, NULL)", "(s, d) AS _bqs_initcap(s, d)"),
    _macro("soundex", "(s) AS _bqs_soundex(s)"),
    _macro("code_points_to_string", "(a) AS _bqs_cp_to_str(a)"),
    _macro("code_points_to_bytes", "(a) AS _bqs_cp_to_bytes(a)"),
    _macro("safe_convert_bytes_to_string", "(b) AS _bqs_safe_bytes(CAST(b AS BLOB))"),
    # --- STRING / BYTES flavours --------------------------------------------
    _macro("to_code_points", "(x) AS CASE WHEN typeof(x) = 'BLOB' THEN _bqs_cp_b(CAST(x AS BLOB)) ELSE _bqs_cp_s(CAST(x AS VARCHAR)) END"),
    _macro('"left"', _dual(["s", "n"], "_bqs_left_s", "_bqs_left_b", {"s"})),
    _macro('"right"', _dual(["s", "n"], "_bqs_right_s", "_bqs_right_b", {"s"})),
    _macro("reverse", _dual(["s"], "_bqs_reverse_s", "_bqs_reverse_b", {"s"})),
    _macro("lpad", _dual(["s", "n", "p"], "_bqs_lpad_s", "_bqs_lpad_b", {"s", "p"}),
           "(s VARCHAR, n) AS _bqs_lpad_s(s, n, ' ')"),
    _macro("rpad", _dual(["s", "n", "p"], "_bqs_rpad_s", "_bqs_rpad_b", {"s", "p"}),
           "(s VARCHAR, n) AS _bqs_rpad_s(s, n, ' ')"),
    _macro("trim", _dual(["s", "c"], "_bqs_trim_s", "_bqs_trim_b", {"s", "c"}),
           "(s VARCHAR) AS _bqs_trim_s(s, NULL)"),
    _macro("ltrim", _dual(["s", "c"], "_bqs_ltrim_s", "_bqs_ltrim_b", {"s", "c"}),
           "(s VARCHAR) AS _bqs_ltrim_s(s, NULL)"),
    _macro("rtrim", _dual(["s", "c"], "_bqs_rtrim_s", "_bqs_rtrim_b", {"s", "c"}),
           "(s VARCHAR) AS _bqs_rtrim_s(s, NULL)"),
    _macro("instr",
           "(s, sub, pos, occ) AS CASE WHEN typeof(s) = 'BLOB' THEN _bqs_instr_b(CAST(s AS BLOB), CAST(sub AS BLOB), pos, occ) "
           "ELSE _bqs_instr_s(CAST(s AS VARCHAR), CAST(sub AS VARCHAR), pos, occ) END",
           "(s, sub, pos) AS CASE WHEN typeof(s) = 'BLOB' THEN _bqs_instr_b(CAST(s AS BLOB), CAST(sub AS BLOB), pos, 1) "
           "ELSE _bqs_instr_s(CAST(s AS VARCHAR), CAST(sub AS VARCHAR), pos, 1) END",
           "(s, sub) AS CASE WHEN typeof(s) = 'BLOB' THEN _bqs_instr_b(CAST(s AS BLOB), CAST(sub AS BLOB), 1, 1) "
           "ELSE _bqs_instr_s(CAST(s AS VARCHAR), CAST(sub AS VARCHAR), 1, 1) END"),
    # --- bytes <-> text ------------------------------------------------------
    _macro("to_hex", "(b) AS _bqs_to_hex(b)"),
    _macro("from_hex", "(s) AS _bqs_from_hex(s)"),
    _macro("to_base64", "(b) AS _bqs_to_base64(b)"),
    _macro("from_base64", "(s) AS _bqs_from_base64(s)"),
    _macro("to_base32", "(b) AS _bqs_to_base32(b)"),
    _macro("from_base32", "(s) AS _bqs_from_base32(s)"),
]


def register(con) -> None:
    u = lambda name, fn, params, ret: H.udf(con, name, fn, params, ret)  # noqa: E731
    u("_bqs_regexp_contains", f_rx_contains, [V, V], BOOL)
    u("_bqs_rx_extract", f_rx_extract, [V, V, B, B], V)
    u("_bqs_rx_extract_all", f_rx_extract_all, [V, V], LV)
    u("_bqs_rx_instr", f_rx_instr, [V, V, B, B, B], B)
    u("_bqs_rx_replace", f_rx_replace, [V, V, V], V)
    u("_bqs_rx_group", f_rx_group, [V, V, B], V)
    u("_bqs_rx_group_int", f_rx_group_int, [V, V, B], B)
    u("_bqs_chr", f_chr, [B], V)
    u("_bqs_unicode", f_unicode, [V], B)
    u("_bqs_cp_to_str", f_cp_to_str, [LB], V)
    u("_bqs_cp_to_bytes", f_cp_to_bytes, [LB], BLOB)
    u("_bqs_cp_s", f_to_cp_s, [V], LB)
    u("_bqs_cp_b", f_to_cp_b, [BLOB], LB)
    u("_bqs_instr_s", f_instr, [V, V, B, B], B)
    u("_bqs_instr_b", lambda s, sub, pos, occ: f_instr(None if s is None else bytes(s),
                                                     None if sub is None else bytes(sub), pos, occ),
      [BLOB, BLOB, B, B], B)
    u("_bqs_left_s", f_left, [V, B], V)
    u("_bqs_left_b", f_left, [BLOB, B], BLOB)
    u("_bqs_right_s", f_right, [V, B], V)
    u("_bqs_right_b", f_right, [BLOB, B], BLOB)
    u("_bqs_reverse_s", f_reverse, [V], V)
    u("_bqs_reverse_b", f_reverse, [BLOB], BLOB)
    u("_bqs_lpad_s", f_lpad, [V, B, V], V)
    u("_bqs_lpad_b", f_lpad, [BLOB, B, BLOB], BLOB)
    u("_bqs_rpad_s", f_rpad, [V, B, V], V)
    u("_bqs_rpad_b", f_rpad, [BLOB, B, BLOB], BLOB)
    u("_bqs_trim_s", f_trim_s, [V, V], V)
    u("_bqs_trim_b", f_trim_b, [BLOB, BLOB], BLOB)
    u("_bqs_ltrim_s", f_ltrim_s, [V, V], V)
    u("_bqs_ltrim_b", f_ltrim_b, [BLOB, BLOB], BLOB)
    u("_bqs_rtrim_s", f_rtrim_s, [V, V], V)
    u("_bqs_rtrim_b", f_rtrim_b, [BLOB, BLOB], BLOB)
    u("_bqs_initcap", f_initcap, [V, V], V)
    u("_bqs_edit_distance", f_edit_distance, [V, V, B], B)
    u("_bqs_soundex", f_soundex, [V], V)
    u("_bqs_normalize", f_normalize, [V, V], V)
    u("_bqs_normalize_cf", f_normalize_cf, [V, V], V)
    u("_bqs_safe_bytes", f_safe_bytes_to_string, [BLOB], V)
    u("_bqs_to_hex", f_to_hex, [BLOB], V)
    u("_bqs_from_hex", f_from_hex, [V], BLOB)
    u("_bqs_to_base64", f_to_base64, [BLOB], V)
    u("_bqs_from_base64", f_from_base64, [V], BLOB)
    u("_bqs_to_base32", f_to_base32, [BLOB], V)
    u("_bqs_from_base32", f_from_base32, [V], BLOB)
    u("_bqs_contains_substr", f_contains_substr, [V, V, V, V], BOOL)
    u("_bqs_contains_substr_list", f_contains_substr_list, [LV, V], BOOL)
    u("_bqs_format", f_format, [V, LV, LV], V)
