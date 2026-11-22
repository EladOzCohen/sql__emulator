"""MySQL string functions, LIKE / ESCAPE / STRCMP and REGEXP on DuckDB.

How it is built
---------------
* Python UDFs named `_my_*` hold the MySQL semantics that SQL cannot express
  (character vs byte positions, collation-aware search, MySQL's TRIM / PAD /
  SUBSTRING corner cases, ICU-flavoured regular expressions, ...).
* Public names (ASCII, LPAD, REPLACE ...) are TEMP macros that normalise their
  arguments (numbers and booleans become text the way MySQL converts them) and
  call the UDF.  A macro named like a DuckDB builtin shadows it completely.
* HANDLERS rewrite calls that need syntax macros cannot express:
  CONCAT, CONCAT_WS, ELT, FIELD, MAKE_SET (variable argument lists),
  TRIM(BOTH x FROM y), POSITION(a IN b), SUBSTRING(s FROM a FOR b), CHAR(... USING cs),
  INSERT / LEFT / RIGHT (keywords), REGEXP_* (match types and COLLATE).
* PRE_RULES handle literals and operators: X'..' / 0b hex-bit literals, N'..',
  `_utf8mb4 '..'` introducers, adjacent string literals, BINARY expr,
  `a REGEXP b`, `a SOUNDS LIKE b`, case-sensitive LIKE.  POST_RULES map
  `COLLATE <mysql collation>` to the closest DuckDB collation.

Text comparison follows MySQL's default collation (utf8mb4_0900_ai_ci):
case- *and* accent-insensitive.  Functions that compare text (INSTR, LOCATE,
FIELD, FIND_IN_SET, STRCMP, LIKE, SOUNDS LIKE) fold both sides that way;
REPLACE, SUBSTRING_INDEX and TRIM compare bytes, as in MySQL.

Binary strings (UNHEX, FROM_BASE64, CHAR without USING) are returned as BLOB so
that HEX(UNHEX('E4')) round-trips; DuckDB shows them as bytes.
"""

from __future__ import annotations

import base64
import binascii
import functools
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .. import translate as tr
from . import helpers as H

V, B, BOOL = H.VARCHAR, H.BIGINT, H.BOOLEAN
BLOB = H.T.BLOB

# --------------------------------------------------------------------------- #
#  Small Python helpers
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=65536)
def _fold_char(c: str) -> str:
    """utf8mb4_0900_ai_ci: accent- and case-insensitive, one char in -> one char out."""
    d = unicodedata.normalize("NFD", c)
    base = d[0] if d else c
    low = base.lower()
    return low if len(low) == 1 else base


def fold(s: str) -> str:
    return "".join(map(_fold_char, s))


def _enc(s: str) -> bytes:
    return s.encode("utf-8", "surrogatepass")


def _dec(b: bytes, charset: str = "utf-8") -> str:
    return b.decode(charset, "replace")


_CHARSETS = {"utf8": "utf-8", "utf8mb3": "utf-8", "utf8mb4": "utf-8", "ascii": "ascii",
             "latin1": "cp1252", "binary": "latin-1", "utf16": "utf-16-be",
             "ucs2": "utf-16-be", "utf32": "utf-32-be", "cp1250": "cp1250",
             "cp1251": "cp1251", "cp1256": "cp1256", "cp1257": "cp1257",
             "cp850": "cp850", "cp852": "cp852", "cp866": "cp866", "cp932": "cp932",
             "gbk": "gbk", "big5": "big5", "sjis": "shift_jis", "euckr": "euc_kr",
             "greek": "iso8859-7", "hebrew": "iso8859-8", "latin2": "iso8859-2",
             "latin5": "iso8859-9", "latin7": "iso8859-13", "koi8r": "koi8-r"}


_NUM_PREFIX = re.compile(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def _to_int(text) -> int | None:
    """MySQL string -> integer: leading numeric prefix, rounded half away from zero."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if t == "true":
        return 1
    if t == "false":
        return 0
    m = _NUM_PREFIX.match(t)
    if not m:
        return 0
    try:
        return int(Decimal(m.group(1)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


# --------------------------------------------------------------------------- #
#  Conversion helpers used by the public macros
# --------------------------------------------------------------------------- #
def f_unesc(text):
    """DuckDB's text form of a BLOB ('\\xE4' escapes) -> text (UTF-8 if valid)."""
    if text is None:
        return None
    return _dec(_unesc_bytes(text))


def _unesc_bytes(text: str) -> bytes:
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        if text[i] == "\\" and text[i + 1:i + 2] == "x" and i + 3 < n + 0 and \
                re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 2:i + 4] or ""):
            out.append(int(text[i + 2:i + 4], 16))
            i += 4
        else:
            out += _enc(text[i])
            i += 1
    return bytes(out)


def f_unesc_blob(text):
    return None if text is None else _unesc_bytes(text)


def f_enc(text):
    return None if text is None else _enc(text)


# --------------------------------------------------------------------------- #
#  Numbers <-> text
# --------------------------------------------------------------------------- #
def f_ascii(s):
    if s is None:
        return None
    return _enc(s)[0] if s else 0


def f_ord(s):
    if s is None:
        return None
    if not s:
        return 0
    b = _enc(s[0])
    v = 0
    for x in b:
        v = v * 256 + x
    return v


def _unsigned(n: int) -> int:
    return n & 0xFFFFFFFFFFFFFFFF


def f_bin(n):
    return None if n is None else format(_unsigned(n), "b")


def f_oct(n):
    return None if n is None else format(_unsigned(n), "o")


def f_unhex(s):
    if s is None:
        return None
    if not re.fullmatch(r"[0-9A-Fa-f]*", s):
        return None
    if len(s) % 2:
        s = "0" + s
    return bytes.fromhex(s)


_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def f_conv(n, from_base, to_base):
    if n is None or from_base is None or to_base is None:
        return None
    fb, tb = abs(from_base), abs(to_base)
    if not (2 <= fb <= 36 and 2 <= tb <= 36):
        return None
    s = n.strip()
    neg = False
    if s[:1] in "+-":
        neg = s[0] == "-"
        s = s[1:]
    val = 0
    for ch in s:
        d = _DIGITS.find(ch.upper())
        if d < 0 or d >= fb:
            break
        val = val * fb + d
        if val > 0xFFFFFFFFFFFFFFFF:
            val = 0xFFFFFFFFFFFFFFFF
            break
    if neg:
        val = -val
    if from_base > 0:                       # unsigned unless from_base is negative
        val = _unsigned(val)
    else:
        if val > 0x7FFFFFFFFFFFFFFF:
            val = 0x7FFFFFFFFFFFFFFF
        elif val < -0x8000000000000000:
            val = -0x8000000000000000
    if to_base < 0:                         # signed output
        if val >= 1 << 63:
            val -= 1 << 64
    else:
        val = _unsigned(val)
    sign = "-" if val < 0 else ""
    val = abs(val)
    out = ""
    while val:
        val, r = divmod(val, tb)
        out = _DIGITS[r] + out
    return sign + (out or "0")


# --------------------------------------------------------------------------- #
#  FORMAT
# --------------------------------------------------------------------------- #
_LOCALES_DOT_COMMA = ("de_DE", "de_AT", "de_CH", "it_IT", "es_ES", "nl_NL", "pt_BR",
                      "pt_PT", "da_DK", "tr_TR", "el_GR", "id_ID", "ro_RO", "hr_HR",
                      "sl_SI", "sk_SK", "hu_HU", "nb_NO", "fi_FI", "sv_SE", "pl_PL",
                      "ru_RU", "uk_UA", "cs_CZ", "bg_BG", "es_AR", "es_CL", "es_CO",
                      "es_MX", "es_VE", "vi_VN", "en_ZA", "ca_ES", "eu_ES", "gl_ES")
_LOCALE_SEPS = {loc: (".", ",") for loc in _LOCALES_DOT_COMMA}
# locales whose thousands separator is not '.' in MySQL's tables
for _loc, _seps in {"ru_RU": (" ", ","), "uk_UA": (" ", ","), "cs_CZ": (" ", ","),
                    "sk_SK": (" ", ","), "pl_PL": (" ", ","), "bg_BG": (" ", ","),
                    "fi_FI": (" ", ","), "sv_SE": (" ", ","), "nb_NO": (" ", ","),
                    "hu_HU": (" ", ","), "en_ZA": (" ", ","), "de_CH": ("'", ".")}.items():
    _LOCALE_SEPS[_loc] = _seps


def f_format(x, d, locale="en_US"):
    if x is None or d is None or locale is None:
        return None
    d = max(0, min(int(d), 30))
    seps = _LOCALE_SEPS.get(locale)
    if seps is None:
        if re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
            seps = (",", ".")
        else:
            raise ValueError(f"Unknown locale: '{locale}'")
    t = str(x).strip()
    if t.lower() in ("true", "false"):
        t = "1" if t.lower() == "true" else "0"
    m = _NUM_PREFIX.match(t)
    num = Decimal(m.group(1)) if m else Decimal(0)
    q = num.quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    text = f"{abs(q):f}"
    ip, _, fp = text.partition(".")
    groups = []
    while len(ip) > 3:
        groups.insert(0, ip[-3:])
        ip = ip[:-3]
    groups.insert(0, ip)
    out = seps[0].join(groups)
    if d > 0:
        out += seps[1] + fp
    return ("-" if q < 0 else "") + out


# --------------------------------------------------------------------------- #
#  Base64
# --------------------------------------------------------------------------- #
def f_to_base64(s):
    if s is None:
        return None
    raw = base64.b64encode(_enc(s)).decode("ascii")
    return "\n".join(raw[i:i + 76] for i in range(0, len(raw), 76))


def f_from_base64(s):
    if s is None:
        return None
    t = re.sub(r"[\s]", "", s)
    try:
        return base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError):
        return None


# --------------------------------------------------------------------------- #
#  Search / substring
# --------------------------------------------------------------------------- #
def f_locate(sub, s, pos=1):
    if sub is None or s is None or pos is None:
        return None
    if pos < 1:
        return 0
    if pos - 1 > len(s):
        return 0
    return fold(s).find(fold(sub), pos - 1) + 1


def f_substr(s, pos, length=None):
    if s is None or pos is None:
        return None
    n = len(s)
    if pos == 0:
        return ""
    if length is not None and length <= 0:
        return ""
    start = pos - 1 if pos > 0 else n + pos
    if start < 0 or start >= n:
        return ""
    return s[start:] if length is None else s[start:start + length]


def f_substr2(s, pos):
    return f_substr(s, pos)


def f_substr3(s, pos, length):
    if length is None:
        return None
    return f_substr(s, pos, length)


def f_insert(s, pos, length, new):
    if s is None or pos is None or length is None or new is None:
        return None
    n = len(s)
    if pos < 1 or pos > n:
        return s
    if length < 0 or length > n - pos + 1:
        return s[:pos - 1] + new
    return s[:pos - 1] + new + s[pos - 1 + length:]


def f_left(s, n):
    if s is None or n is None:
        return None
    return "" if n <= 0 else s[:n]


def f_right(s, n):
    if s is None or n is None:
        return None
    return "" if n <= 0 else s[-n:] if n < len(s) else s


def f_substring_index(s, delim, count):
    if s is None or delim is None or count is None:
        return None
    if count == 0 or delim == "":
        return ""
    parts = s.split(delim)
    if abs(count) > len(parts) - 1:
        return s
    return delim.join(parts[:count]) if count > 0 else delim.join(parts[count:])


def f_replace(s, a, b):
    if s is None or a is None or b is None:
        return None
    return s if a == "" else s.replace(a, b)


def f_reverse(s):
    return None if s is None else s[::-1]


def f_repeat(s, n):
    if s is None or n is None:
        return None
    return "" if n <= 0 else s * n


def f_space(n):
    if n is None:
        return None
    return "" if n <= 0 else " " * n


def _pad(s, n, pad, left):
    if s is None or n is None or pad is None:
        return None
    if n < 0:
        return None
    if n <= len(s):
        return s[:n]
    if pad == "":
        return None
    need = n - len(s)
    fill = (pad * (need // len(pad) + 1))[:need]
    return fill + s if left else s + fill


def f_lpad(s, n, pad):
    return _pad(s, n, pad, True)


def f_rpad(s, n, pad):
    return _pad(s, n, pad, False)


def f_trim(s, rem, mode):
    """mode 0 BOTH, 1 LEADING, 2 TRAILING; removes whole repeats of `rem` (not a char set)."""
    if s is None or rem is None:
        return None
    if rem == "":
        return s
    k = len(rem)
    if mode in (0, 1):
        i = 0
        while s.startswith(rem, i):
            i += k
        s = s[i:]
    if mode in (0, 2):
        while s.endswith(rem) and s:
            s = s[:-k]
    return s


def f_ltrim(s):
    return None if s is None else s.lstrip(" ")


def f_rtrim(s):
    return None if s is None else s.rstrip(" ")


def f_strcmp(a, b):
    if a is None or b is None:
        return None
    fa, fb = fold(a), fold(b)
    return (fa > fb) - (fa < fb)


def f_find_in_set(needle, haystack):
    if needle is None or haystack is None:
        return None
    if "," in needle or haystack == "":
        return 0
    fn = fold(needle)
    for i, item in enumerate(haystack.split(","), 1):
        if fold(item) == fn:
            return i
    return 0


def f_export_set(bits, on, off, sep, nbits):
    if bits is None or on is None or off is None or sep is None or nbits is None:
        return None
    nbits = 64 if nbits < 0 or nbits > 64 else nbits
    bits = _unsigned(bits)
    return sep.join(on if (bits >> i) & 1 else off for i in range(nbits))


_SOUNDEX_MAP = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "01230120022455012623010202"))


def f_soundex(s):
    if s is None:
        return None
    letters = [c.upper() for c in s if c.isascii() and c.isalpha()]
    if not letters:
        return ""
    out = letters[0]
    last = _SOUNDEX_MAP[letters[0]]
    for c in letters[1:]:
        code = _SOUNDEX_MAP[c]
        if code != "0" and code != last:
            out += code
            last = code
    return out.ljust(4, "0")


def f_quote(s):
    if s is None:
        return "NULL"
    esc = (s.replace("\\", "\\\\").replace("'", "\\'")
           .replace("\0", "\\0").replace("\x1a", "\\Z"))
    return "'" + esc + "'"


def f_char1(n):
    """One CHAR() argument -> its bytes (big endian, at least one byte)."""
    if n is None:
        return b""
    n = _to_int(n) & 0xFFFFFFFF
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


def f_decode(blob, charset):
    if blob is None or charset is None:
        return None
    enc = _CHARSETS.get(charset.lower())
    if enc is None:
        raise ValueError(f"Unknown character set: '{charset}'")
    return bytes(blob).decode(enc, "replace")


def f_weight_binary(s, n):
    if s is None or n is None:
        return None
    b = _enc(s)[:n]
    return b + b"\0" * (n - len(b))


# --------------------------------------------------------------------------- #
#  LIKE
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=2048)
def _like_regex(pat: str, esc: str, cs: bool):
    if not cs:
        pat = fold(pat)
        esc = fold(esc) if esc else esc
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if esc and c == esc and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        if c == "%":
            out.append("[\\s\\S]*")
        elif c == "_":
            out.append("[\\s\\S]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out))


def f_like(s, pat, esc, cs):
    if s is None or pat is None:
        return None
    e = esc[:1] if esc else ""
    rx = _like_regex(pat, e, bool(cs))
    return rx.fullmatch(s if cs else fold(s)) is not None


def f_like_ci(s, pat, esc):
    return f_like(s, pat, esc, False)


def f_like_cs(s, pat, esc):
    return f_like(s, pat, esc, True)


# --------------------------------------------------------------------------- #
#  Regular expressions (MySQL 8 uses ICU; Python's `re` is the closest engine
#  available offline, so the pattern is translated)
# --------------------------------------------------------------------------- #
_ALPHA = "a-zA-Z\u00aa\u00b5\u00ba\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u02ff\u0370-\u03ff\u0400-\u052f\u05d0-\u05ea\u0620-\u064a"
_POSIX = {
    "alpha": _ALPHA, "digit": "0-9", "alnum": _ALPHA + "0-9",
    "upper": "A-Z\u00c0-\u00d6\u00d8-\u00de\u0391-\u03a9\u0410-\u042f",
    "lower": "a-z\u00df-\u00f6\u00f8-\u00ff\u03b1-\u03c9\u0430-\u044f",
    "space": "\\s", "blank": " \\t", "cntrl": "\\x00-\\x1f\\x7f",
    "xdigit": "0-9A-Fa-f", "punct": "!-#%-*,-/:;?@\\[-\\]_{}", "word": "\\w",
    "print": "\\x20-\\x7e\u00a0-\uffff", "graph": "\\x21-\\x7e\u00a1-\uffff",
}
_LINE_END = "\\n\\r\\x85\\u2028\\u2029"


def _translate_regex(pat: str, dotall: bool) -> str:
    out: list[str] = []
    i, n = 0, len(pat)
    in_set = 0
    while i < n:
        c = pat[i]
        if c == "\\" and i + 1 < n:
            d = pat[i + 1]
            if d == "Q":                                    # \Q...\E literal block
                j = pat.find("\\E", i + 2)
                j = n if j < 0 else j
                out.append(re.escape(pat[i + 2:j]))
                i = j + 2
                continue
            if d == "x" and pat[i + 2:i + 3] == "{":
                j = pat.find("}", i)
                out.append(re.escape(chr(int(pat[i + 3:j], 16))))
                i = j + 1
                continue
            if d == "z":
                out.append("\\Z")
            elif d == "Z":
                out.append("(?=\\n?\\Z)")
            elif d == "h":
                out.append(" \\t" if in_set else "[ \\t]")
            elif d == "e":
                out.append("\\x1b")
            elif d in "pP" and pat[i + 2:i + 3] == "{":
                j = pat.find("}", i)
                name = pat[i + 3:j].lower().replace("is", "", 1) if pat[i + 3:j].lower().startswith("is") \
                    else pat[i + 3:j].lower()
                cls = {"l": _ALPHA, "letter": _ALPHA, "alpha": _ALPHA, "alphabetic": _ALPHA,
                       "n": "0-9", "nd": "0-9", "digit": "0-9", "lu": _POSIX["upper"],
                       "ll": _POSIX["lower"], "upper": _POSIX["upper"], "lower": _POSIX["lower"],
                       "p": _POSIX["punct"], "punct": _POSIX["punct"], "alnum": _POSIX["alnum"],
                       "space": "\\s", "z": "\\s", "zs": " \u00a0"}.get(name)
                if cls is None:
                    raise ValueError("Unsupported regular expression property: " + pat[i:j + 1])
                neg = d == "P"
                out.append(("[^" if neg else "[") + cls + "]" if not in_set else cls)
                i = j + 1
                continue
            else:
                out.append(c + d)
            i += 2
            continue
        if in_set:
            if c == "[" and pat[i + 1:i + 2] == ":":
                j = pat.find(":]", i + 2)
                if j > 0:
                    name = pat[i + 2:j]
                    neg = name.startswith("^")
                    cls = _POSIX.get(name.lstrip("^"))
                    if cls is not None and not neg:
                        out.append(cls)
                        i = j + 2
                        continue
            if c == "[":
                out.append("\\[")                           # literal '[' inside a set
                i += 1
                continue
            if c == "]":
                in_set -= 1
            out.append(c)
            i += 1
            continue
        if c == "[":
            in_set += 1
            out.append(c)
            i += 1
            if pat[i:i + 1] == "^":
                out.append("^")
                i += 1
            if pat[i:i + 1] == "]":
                out.append("\\]")
                i += 1
            continue
        if c == "(" and pat[i + 1:i + 3] == "?<" and pat[i + 3:i + 4] not in ("=", "!"):
            out.append("(?P<")
            i += 3
            continue
        if c == "." and not dotall:
            out.append("[^" + _LINE_END + "]")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=1024)
def _compile(pat: str, match_type: str):
    flags = re.I
    dotall = False
    for ch in match_type:
        if ch == "c":
            flags &= ~re.I
        elif ch == "i":
            flags |= re.I
        elif ch == "m":
            flags |= re.M
        elif ch == "n":
            dotall = True
        elif ch == "u":
            pass
        else:
            raise ValueError(f"Incorrect arguments to regexp match_type: '{match_type}'")
    if dotall:
        flags |= re.S
    try:
        return re.compile(_translate_regex(pat, dotall), flags)
    except re.error as exc:
        raise ValueError(f"Illegal argument to a regular expression: {exc}") from None


def f_regexp_like(s, pat, mt):
    if s is None or pat is None or mt is None:
        return None
    return _compile(pat, mt).search(s) is not None


def f_regexp_instr(s, pat, pos, occ, ret, mt):
    if None in (s, pat, pos, occ, ret, mt):
        return None
    m = _nth_match(s, pat, pos, occ, mt)
    if m is None:
        return 0
    return (m.end() if ret == 1 else m.start()) + pos


def f_regexp_substr(s, pat, pos, occ, mt):
    if None in (s, pat, pos, occ, mt):
        return None
    m = _nth_match(s, pat, pos, occ, mt)
    return None if m is None else m.group(0)


def _nth_match(s, pat, pos, occ, mt):
    if pos < 1 or pos > len(s) + 1:
        if pos < 1 or (pos > len(s) and len(s) > 0):
            raise ValueError("Index out of bounds in regular expression search.")
    rx = _compile(pat, mt)
    sub = s[pos - 1:]
    occ = max(occ, 1)
    for k, m in enumerate(rx.finditer(sub), 1):
        if k == occ:
            return m
    return None


def _expand_repl(repl: str, m) -> str:
    out, i, n = [], 0, len(repl)
    while i < n:
        c = repl[i]
        if c == "\\" and i + 1 < n:
            out.append(repl[i + 1])
            i += 2
        elif c == "$" and i + 1 < n and repl[i + 1].isdigit():
            g = int(repl[i + 1])
            out.append(m.group(g) or "" if g <= (m.re.groups) else "")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def f_regexp_replace(s, pat, repl, pos, occ, mt):
    if None in (s, pat, repl, pos, occ, mt):
        return None
    rx = _compile(pat, mt)
    if pos < 1 or pos > len(s) + 1:
        raise ValueError("Index out of bounds in regular expression search.")
    head, tail = s[:pos - 1], s[pos - 1:]
    count = [0]

    def sub(m):
        count[0] += 1
        if occ == 0 or count[0] == occ:
            return _expand_repl(repl, m)
        return m.group(0)

    return head + rx.sub(sub, tail)


# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
def _m(name: str, *defs: str) -> str:
    return f"CREATE OR REPLACE TEMP MACRO {name}" + ", ".join(defs)


# text form of any value, the way MySQL converts it (BOOLEAN -> 1/0, BLOB -> text)
_STR = ("CASE typeof(x) WHEN 'BOOLEAN' THEN (CASE WHEN CAST(x AS VARCHAR) = 'true' THEN '1' ELSE '0' END) "
        "WHEN 'BLOB' THEN _my_unesc(CAST(x AS VARCHAR)) ELSE CAST(x AS VARCHAR) END")
# bytes of any value
_BYTES = ("CASE typeof(x) WHEN 'BLOB' THEN _my_unesc_blob(CAST(x AS VARCHAR)) "
          "ELSE _my_enc(_my_str(x)) END")
# numeric argument -> BIGINT (round half away from zero; NULL stays NULL)
_INT = ("CASE WHEN n IS NULL THEN NULL WHEN typeof(n) IN ('TINYINT','SMALLINT','INTEGER','BIGINT',"
        "'UTINYINT','USMALLINT','UINTEGER') THEN CAST(n AS BIGINT) "
        "ELSE COALESCE(CAST(system.main.round(TRY_CAST(n AS DOUBLE)) AS BIGINT), 0) END")

PRELUDE: list[str] = [
    _m("_my_str", f"(x) AS {_STR}"),
    _m("_my_bytes", f"(x) AS {_BYTES}"),
    _m("_my_i", f"(n) AS {_INT}"),
    # --- character <-> number -------------------------------------------- #
    _m("ascii", "(s) AS _my_ascii(_my_str(s))"),
    _m("ord", "(s) AS _my_ord(_my_str(s))"),
    _m("bin", "(n) AS _my_bin(_my_i(n))"),
    _m("oct", "(n) AS _my_oct(_my_i(n))"),
    _m("hex", "(x) AS CASE typeof(x) WHEN 'VARCHAR' THEN system.main.hex(_my_bytes(x)) "
              "WHEN 'BLOB' THEN system.main.hex(_my_bytes(x)) "
              "ELSE system.main.hex(_my_i(x)) END"),
    _m("unhex", "(s) AS _my_unhex(_my_str(s))"),
    _m("conv", "(n, f, t) AS _my_conv(_my_str(n), _my_i(f), _my_i(t))"),
    # --- lengths ---------------------------------------------------------- #
    _m("length", "(s) AS system.main.octet_length(_my_bytes(s))"),
    _m("octet_length", "(s) AS system.main.octet_length(_my_bytes(s))"),
    _m("bit_length", "(s) AS 8 * system.main.octet_length(_my_bytes(s))"),
    _m("char_length", "(s) AS system.main.length(_my_str(s))"),
    _m("character_length", "(s) AS system.main.length(_my_str(s))"),
    # --- case ------------------------------------------------------------- #
    _m("lower", "(s) AS system.main.lower(_my_str(s))"),
    _m("lcase", "(s) AS system.main.lower(_my_str(s))"),
    _m("upper", "(s) AS system.main.upper(_my_str(s))"),
    _m("ucase", "(s) AS system.main.upper(_my_str(s))"),
    # --- searching -------------------------------------------------------- #
    _m("instr", "(s, sub) AS _my_locate(_my_str(sub), _my_str(s), 1)"),
    _m("locate", "(sub, s) AS _my_locate(_my_str(sub), _my_str(s), 1)",
       "(sub, s, p) AS _my_locate(_my_str(sub), _my_str(s), _my_i(p))"),
    _m("find_in_set", "(a, b) AS _my_find_in_set(_my_str(a), _my_str(b))"),
    _m("strcmp", "(a, b) AS _my_strcmp(_my_str(a), _my_str(b))"),
    _m("soundex", "(s) AS _my_soundex(_my_str(s))"),
    # --- building / cutting ---------------------------------------------- #
    _m("replace", "(s, a, b) AS _my_replace(_my_str(s), _my_str(a), _my_str(b))"),
    _m("reverse", "(s) AS _my_reverse(_my_str(s))"),
    _m("repeat", "(s, n) AS _my_repeat(_my_str(s), _my_i(n))"),
    _m("space", "(n) AS _my_space(_my_i(n))"),
    _m("lpad", "(s, n, p) AS _my_lpad(_my_str(s), _my_i(n), _my_str(p))"),
    _m("rpad", "(s, n, p) AS _my_rpad(_my_str(s), _my_i(n), _my_str(p))"),
    _m("ltrim", "(s) AS _my_ltrim(_my_str(s))"),
    _m("rtrim", "(s) AS _my_rtrim(_my_str(s))"),
    _m("substring_index", "(s, d, c) AS _my_substring_index(_my_str(s), _my_str(d), _my_i(c))"),
    _m("quote", "(s) AS _my_quote(_my_str(s))"),
    _m("export_set", "(b, o, f) AS _my_export_set(_my_i(b), _my_str(o), _my_str(f), ',', 64)",
       "(b, o, f, s) AS _my_export_set(_my_i(b), _my_str(o), _my_str(f), _my_str(s), 64)",
       "(b, o, f, s, n) AS _my_export_set(_my_i(b), _my_str(o), _my_str(f), _my_str(s), _my_i(n))"),
    _m("format", "(x, d) AS _my_format(_my_str(x), _my_i(d), 'en_US')",
       "(x, d, loc) AS _my_format(_my_str(x), _my_i(d), _my_str(loc))"),
    _m("to_base64", "(s) AS _my_to_base64(_my_str(s))"),
    _m("from_base64", "(s) AS _my_from_base64(_my_str(s))"),
    # --- LIKE (rules.py turns every LIKE into ILIKE ... ESCAPE) ---------------- #
    _m("ilike_escape", "(a, b, e) AS _my_like_ci(_my_str(a), _my_str(b), e)"),
    _m("not_ilike_escape", "(a, b, e) AS NOT _my_like_ci(_my_str(a), _my_str(b), e)"),
    _m("like_escape", "(a, b, e) AS _my_like_cs(_my_str(a), _my_str(b), e)"),
    _m("not_like_escape", "(a, b, e) AS NOT _my_like_cs(_my_str(a), _my_str(b), e)"),
]


# --------------------------------------------------------------------------- #
#  Call handlers
# --------------------------------------------------------------------------- #
def _split_top(toks: list[tr.Tok], word: str) -> list[list[tr.Tok]]:
    """Split at top-level occurrences of the keyword `word` (outside (), CASE..END)."""
    parts: list[list[tr.Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        elif t.is_word("CASE"):
            depth += 1
        elif t.is_word("END"):
            depth -= 1
        if depth == 0 and t.is_word(word):
            parts.append([])
        else:
            parts[-1].append(t)
    return parts


def _txt(toks: list[tr.Tok]) -> str:
    return tr.text_of(toks)


def _is_cs_collation(name: str) -> bool:
    n = name.strip('"`\'').lower()
    return n == "binary" or n.endswith("_bin") or n.endswith("_cs") or n == "utf8mb4_0900_bin"


def _strip_collate(toks: list[tr.Tok]) -> tuple[list[tr.Tok], bool]:
    """Remove `COLLATE name` pairs; return (tokens, any collation was case-sensitive)."""
    out: list[tr.Tok] = []
    cs = False
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.is_word("COLLATE"):
            k = tr.next_sig(toks, i)
            if k < len(toks):
                cs = cs or _is_cs_collation(toks[k].text)
                i = k + 1
                continue
        out.append(t)
        i += 1
    return out, cs


def _sql(s: str) -> str:
    return tr.quote_string(s)


def h_concat(args):
    if not args:
        return None
    return "(" + " || ".join(f"_my_str({a.text})" for a in args) + ")"


def h_concat_ws(args):
    if len(args) < 2:
        return None
    return "concat_ws(" + ", ".join(f"_my_str({a.text})" for a in args) + ")"


def h_elt(args):
    if len(args) < 2:
        return None
    n = f"_my_i({args[0].text})"
    items = ", ".join(f"_my_str({a.text})" for a in args[1:])
    return f"(CASE WHEN {n} BETWEEN 1 AND {len(args) - 1} THEN list_extract([{items}], {n}) END)"


def h_field(args):
    if len(args) < 2:
        return None
    s = args[0].text
    whens = " ".join(f"WHEN {s} = {a.text} THEN {i}" for i, a in enumerate(args[1:], 1))
    return f"(CASE {whens} ELSE 0 END)"


def h_make_set(args):
    if len(args) < 2:
        return None
    b = f"_my_i({args[0].text})"
    terms = ", ".join(f"CASE WHEN ({b} >> {i}) & 1 = 1 THEN _my_str({a.text}) END"
                      for i, a in enumerate(args[1:]))
    return f"(CASE WHEN {b} IS NULL THEN NULL ELSE concat_ws(',', {terms}) END)"


def h_insert(args):
    if len(args) != 4:
        return None
    s, p, ln, new = args
    return f"_my_insert(_my_str({s}), _my_i({p}), _my_i({ln}), _my_str({new}))"


def h_left(args):
    if len(args) != 2:
        return None
    return f"_my_left(_my_str({args[0]}), _my_i({args[1]}))"


def h_right(args):
    if len(args) != 2:
        return None
    return f"_my_right(_my_str({args[0]}), _my_i({args[1]}))"


def h_substring(args):
    """SUBSTRING(s, p[, l]) / SUBSTRING(s FROM p [FOR l]) / SUBSTR / MID."""
    if len(args) == 1:
        a = _split_top(args[0].toks, "FROM")
        if len(a) != 2:
            return None
        s, rest = a
        b = _split_top(rest, "FOR")
        parts = [s, b[0]] + ([b[1]] if len(b) == 2 else [])
    elif len(args) in (2, 3):
        parts = [a.toks for a in args]
    else:
        return None
    s_txt = f"_my_str({_txt(parts[0])})"
    pos = f"_my_i({_txt(parts[1])})"
    if len(parts) == 2:
        return f"_my_substr2({s_txt}, {pos})"
    return f"_my_substr3({s_txt}, {pos}, _my_i({_txt(parts[2])}))"


def h_trim(args):
    if len(args) != 1:
        return None
    toks = tr.strip(args[0].toks)
    mode = 0
    if toks and toks[0].is_word("LEADING", "TRAILING", "BOTH"):
        mode = {"BOTH": 0, "LEADING": 1, "TRAILING": 2}[toks[0].upper]
        toks = toks[1:]
        parts = _split_top(toks, "FROM")
        if len(parts) != 2:
            return None
        rem, s = parts
        rem_txt = _txt(rem) if _txt(rem) else "' '"
    else:
        parts = _split_top(toks, "FROM")
        if len(parts) == 2:
            rem, s = parts
            rem_txt = _txt(rem) if _txt(rem) else "' '"
        else:
            s, rem_txt = toks, "' '"
    return f"_my_trim(_my_str({_txt(s)}), _my_str({rem_txt}), {mode})"


def h_position(args):
    if len(args) != 1:
        return None
    parts = _split_top(args[0].toks, "IN")
    if len(parts) != 2:
        return None
    return f"_my_locate(_my_str({_txt(parts[0])}), _my_str({_txt(parts[1])}), 1)"


def h_char(args):
    if not args:
        return None
    last = _split_top(args[-1].toks, "USING")
    charset = None
    items = [a.text for a in args[:-1]]
    if len(last) == 2:
        items.append(_txt(last[0]))
        charset = _txt(last[1]).strip("'\"`")
    else:
        items.append(args[-1].text)
    joined = " || ".join(f"_my_char1(_my_str({i}))" for i in items)
    if charset is None:
        return f"({joined})"
    return f"_my_decode(({joined}), {_sql(charset)})"


def h_weight_string(args):
    if len(args) != 1:
        return None
    parts = _split_top(args[0].toks, "AS")
    if len(parts) != 2:
        return None
    spec = _txt(parts[1])
    m = re.fullmatch(r"BINARY\s*\(\s*(\d+)\s*\)", spec, re.I)
    if not m:
        return None
    return f"_my_weight_binary(_my_str({_txt(parts[0])}), {m.group(1)})"


def _regexp_args(args, defaults, mt_index):
    """Fill optional arguments; fold COLLATE-derived case sensitivity into match_type."""
    cleaned, cs = [], False
    for a in args:
        toks, c = _strip_collate(a.toks)
        cs = cs or c
        cleaned.append(_txt(toks))
    texts = cleaned + defaults[len(cleaned):]
    texts = [f"_my_str({t})" if i < 2 or (i == 2 and defaults is _REPL_DEFAULTS) else
             (f"_my_i({t})" if i != mt_index else f"_my_str({t})") for i, t in enumerate(texts)]
    mt = texts[mt_index]
    if cs:
        texts[mt_index] = f"('c' || {mt})"
    return texts


_LIKE_DEFAULTS = ["", "", "''"]
_INSTR_DEFAULTS = ["", "", "1", "1", "0", "''"]
_SUBSTR_DEFAULTS = ["", "", "1", "1", "''"]
_REPL_DEFAULTS = ["", "", "", "1", "0", "''"]


def h_regexp_like(args):
    if len(args) not in (2, 3):
        return None
    t = _regexp_args(args, _LIKE_DEFAULTS, 2)
    return f"_my_regexp_like({', '.join(t)})"


def h_regexp_instr(args):
    if not 2 <= len(args) <= 6:
        return None
    t = _regexp_args(args, _INSTR_DEFAULTS, 5)
    return f"_my_regexp_instr({', '.join(t)})"


def h_regexp_substr(args):
    if not 2 <= len(args) <= 5:
        return None
    t = _regexp_args(args, _SUBSTR_DEFAULTS, 4)
    return f"_my_regexp_substr({', '.join(t)})"


def h_regexp_replace(args):
    if not 3 <= len(args) <= 6:
        return None
    t = _regexp_args(args, _REPL_DEFAULTS, 5)
    return f"_my_regexp_replace({', '.join(t)})"


HANDLERS: dict = {
    "CONCAT": h_concat, "CONCAT_WS": h_concat_ws, "ELT": h_elt, "FIELD": h_field,
    "MAKE_SET": h_make_set, "INSERT": h_insert, "LEFT": h_left, "RIGHT": h_right,
    "SUBSTRING": h_substring, "SUBSTR": h_substring, "MID": h_substring,
    "TRIM": h_trim, "POSITION": h_position, "CHAR": h_char,
    "WEIGHT_STRING": h_weight_string,
    "REGEXP_LIKE": h_regexp_like, "REGEXP_INSTR": h_regexp_instr,
    "REGEXP_SUBSTR": h_regexp_substr, "REGEXP_REPLACE": h_regexp_replace,
}


# --------------------------------------------------------------------------- #
#  Token rules
# --------------------------------------------------------------------------- #
def _adjacent(toks: list[tr.Tok], i: int) -> bool:
    """toks[i] is a word directly followed (no space) by a string literal."""
    return (toks[i].kind == tr.WORD and i + 1 < len(toks) and toks[i + 1].kind == tr.STR)


_NUMERIC_OPS = {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>", "~"}


def _numeric_context(prev: tr.Tok | None, nxt: tr.Tok | None) -> bool:
    """A hex / bit literal next to an arithmetic or bit operator is a number (MySQL rule)."""
    def arith(t):
        return t is not None and ((t.kind == tr.PUNCT and t.text in _NUMERIC_OPS) or
                                  (t.kind == tr.WORD and t.text.strip().upper() in ("DIV", "MOD")))
    return arith(prev) or arith(nxt)


def _bytes_literal(data: bytes, numeric: bool) -> tr.Tok:
    if numeric:
        return tr.Tok(tr.NUM, str(int.from_bytes(data, "big")))
    try:
        text = data.decode("utf-8")
        return tr.Tok(tr.STR, tr.quote_string(text), text)
    except UnicodeDecodeError:
        return tr.Tok(tr.WORD, f"from_hex('{data.hex()}')")


def hex_bit_literals(toks):
    """X'4D79' / 0x4D79 / B'0101' / N'text' -> a string literal (binary strings in MySQL).

    Next to an arithmetic / bit operator the literal is a number, as in MySQL.
    """
    out: list[tr.Tok] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        prev = out[tr.prev_sig(out, len(out))] if tr.prev_sig(out, len(out)) >= 0 else None
        if t.kind == tr.NUM and re.fullmatch(r"0[xX][0-9A-Fa-f]+", t.text):
            nx = tr.next_sig(toks, i)
            digits = t.text[2:]
            digits = digits if len(digits) % 2 == 0 else "0" + digits
            out.append(_bytes_literal(bytes.fromhex(digits),
                                      _numeric_context(prev, toks[nx] if nx < n else None)))
            i += 1
            continue
        if _adjacent(toks, i) and t.text.upper() in ("X", "B", "N") and                 not (prev is not None and prev.is_punct(".")):
            val = toks[i + 1].value or ""
            kind = t.text.upper()
            nx = tr.next_sig(toks, i + 1)
            numeric = _numeric_context(prev, toks[nx] if nx < n else None)
            if kind == "N":
                out.append(toks[i + 1])
                i += 2
                continue
            if kind == "X" and re.fullmatch(r"[0-9A-Fa-f]*", val):
                v = val if len(val) % 2 == 0 else "0" + val
                out.append(_bytes_literal(bytes.fromhex(v), numeric))
                i += 2
                continue
            if kind == "B" and re.fullmatch(r"[01]*", val):
                nbits = int(val or "0", 2)
                out.append(_bytes_literal(nbits.to_bytes(max(1, (len(val) + 7) // 8), "big"), numeric))
                i += 2
                continue
        out.append(t)
        i += 1
    return out


_INTRODUCER = re.compile(r"_(utf8mb[34]|utf8|latin\d|ascii|binary|ucs2|utf16(le)?|utf32|cp\d+|"
                         r"gbk|big5|sjis|greek|hebrew|koi8[ru]|euckr)$", re.I)


def introducers(toks):
    """`_utf8mb4 'text'` -> 'text' (the literal's charset introducer is dropped)."""
    out: list[tr.Tok] = []
    for i, t in enumerate(toks):
        if t.kind == tr.WORD and _INTRODUCER.match(t.text):
            k = tr.next_sig(toks, i)
            if k < len(toks) and toks[k].kind == tr.STR:
                continue
        out.append(t)
    return out


def adjacent_strings(toks):
    """'My' 'S' 'QL' -> 'MySQL' (MySQL concatenates adjacent string literals)."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == tr.STR and t.value is not None:
            val, j = t.value, i + 1
            while True:
                k = tr.next_sig(toks, j - 1)
                if k < len(toks) and toks[k].kind == tr.STR and toks[k].value is not None \
                        and all(x.kind in (tr.WS, tr.COMMENT) for x in toks[j:k]):
                    val += toks[k].value
                    j = k + 1
                else:
                    break
            if j > i + 1:
                out.append(tr.Tok(tr.STR, tr.quote_string(val), val))
                i = j
                continue
        out.append(t)
        i += 1
    return out


_BIN_ARITH = {"+", "-", "*", "/", "%", "|", "&", "^", "<<", ">>"}
_STOP_WORDS = {"SELECT", "WHERE", "AND", "OR", "XOR", "NOT", "ON", "WHEN", "THEN", "ELSE",
               "HAVING", "BY", "FROM", "AS", "SET", "VALUES", "DISTINCT", "ALL", "IS", "IN",
               "LIKE", "ILIKE", "REGEXP", "RLIKE", "BETWEEN", "CASE", "LIMIT", "UNION",
               "ORDER", "GROUP", "JOIN", "USING", "ESCAPE", "COLLATE", "AND", "INTO"}


def _is_arith(t: tr.Tok) -> bool:
    return (t.kind == tr.PUNCT and t.text in _BIN_ARITH) or \
        (t.kind == tr.WORD and t.text.strip().upper() in ("DIV", "MOD"))


def _operand_right(toks, k):
    """End index (exclusive) of the operand starting at toks[k], or -1."""
    e = tr.primary_end(toks, k)
    if e < 0:
        return -1
    while True:
        nx = tr.next_sig(toks, e - 1)
        if nx < len(toks) and toks[nx].is_word("COLLATE"):
            nm = tr.next_sig(toks, nx)
            if nm < len(toks):
                e = nm + 1
                continue
        if nx < len(toks) and _is_arith(toks[nx]):
            e2 = tr.primary_end(toks, nx + 1)
            if e2 > 0:
                e = e2
                continue
        return e


def _operand_left(toks, end):
    """Start index of the operand ending just before toks[end], or -1."""
    s = tr.primary_start(toks, end)
    if s < 0 or toks[s].kind == tr.WORD and toks[s].text.strip().upper() in _STOP_WORDS:
        return -1
    while True:
        p = tr.prev_sig(toks, s)
        if p >= 0 and _is_arith(toks[p]):
            s2 = tr.primary_start(toks, p)
            if s2 >= 0 and not (toks[s2].kind == tr.WORD and
                                toks[s2].text.strip().upper() in _STOP_WORDS):
                s = s2
                continue
            return p if toks[p].text in "+-" else s
        return s


def _infix(toks, matcher, build):
    """Rewrite `left <op> right` where matcher(toks, i) -> (op_len, negated) | None."""
    i = 0
    toks = list(toks)
    while i < len(toks):
        hit = matcher(toks, i)
        if hit:
            op_len, neg, op_start = hit
            left_end = op_start
            ls = _operand_left(toks, left_end)
            r_start = tr.next_sig(toks, op_start + op_len - 1)
            re_ = _operand_right(toks, r_start) if r_start < len(toks) else -1
            if ls >= 0 and re_ > 0:
                left = _txt(toks[ls:op_start])
                right = _txt(toks[r_start:re_])
                new = tr.tokenize_plain(build(left, right, neg))
                toks[ls:re_] = new
                i = ls + len(new)
                continue
        i += 1
    return toks


def _match_regexp(toks, i):
    t = toks[i]
    if t.is_word("REGEXP", "RLIKE"):
        p = tr.prev_sig(toks, i)
        if p >= 0 and toks[p].is_punct("."):
            return None
        if p >= 0 and toks[p].is_word("NOT"):
            return (i - p + 1, True, p)
        return (1, False, i)
    return None


def regexp_operator(toks):
    """`a [NOT] REGEXP b` / RLIKE -> REGEXP_LIKE(a, b)."""
    def build(left, right, neg):
        call = f"REGEXP_LIKE({left}, {right})"
        return f"(NOT {call})" if neg else f"({call})"
    return _infix(toks, _match_regexp, build)


def sounds_like(toks):
    """`a SOUNDS LIKE b` -> SOUNDEX(a) = SOUNDEX(b)."""
    def match(ts, i):
        if ts[i].is_word("SOUNDS"):
            k = tr.next_sig(ts, i)
            if k < len(ts) and ts[k].is_word("LIKE"):
                return (k - i + 1, False, i)
        return None
    return _infix(toks, match, lambda l, r, n: f"(SOUNDEX({l}) = SOUNDEX({r}))")


def like_case_sensitive(toks):
    """LIKE BINARY x / LIKE x COLLATE *_bin|*_cs / x COLLATE *_bin LIKE y -> marker word.

    rules.py rewrites every plain LIKE to ILIKE (default collation is case-insensitive);
    a marker keeps it out of that rule and POST rule `like_marker` turns it into LIKE.
    """
    out: list[tr.Tok] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        prev = tr.prev_sig(toks, i)
        if t.is_word("LIKE") and not (prev >= 0 and toks[prev].is_punct(".")):
            j = tr.next_sig(toks, i)
            cs = False
            if j < n and toks[j].is_word("BINARY"):
                cs = True
                j = tr.next_sig(toks, j)
            pe = tr.primary_end(toks, j) if j < n else -1
            coll_tail = []
            if pe > 0:
                c = tr.next_sig(toks, pe - 1)
                if c < n and toks[c].is_word("COLLATE"):
                    nm = tr.next_sig(toks, c)
                    if nm < n:
                        cs = cs or _is_cs_collation(toks[nm].text)
                        coll_tail = toks[pe:nm + 1]
                        pe_all = nm + 1
                    else:
                        pe_all = pe
                else:
                    pe_all = pe
            else:
                pe_all = pe
            # `x COLLATE name LIKE ...` : look left, skipping NOT
            q = prev
            if q >= 0 and toks[q].is_word("NOT"):
                q = tr.prev_sig(toks, q)
            if q >= 1 and toks[tr.prev_sig(toks, q)].is_word("COLLATE"):
                cs = cs or _is_cs_collation(toks[q].text)
            if cs and pe > 0:
                out.append(tr.Tok(tr.WORD, "_my_like_cs_marker"))
                out.extend(toks[i + 1:i + 1])
                out.append(tr.Tok(tr.WS, " "))
                out.extend(toks[j:pe])                    # the pattern; collation dropped
                nxt = tr.next_sig(toks, pe_all - 1)
                if not (nxt < n and toks[nxt].is_word("ESCAPE")):
                    out.extend(tr.tokenize_plain(" ESCAPE '\\'"))
                i = pe_all
                continue
        out.append(t)
        i += 1
    return out


def like_marker(toks):
    return [tr.Tok(tr.WORD, "LIKE") if t.kind == tr.WORD and t.text == "_my_like_cs_marker" else t
            for t in toks]


def binary_prefix(toks):
    """`BINARY expr` (deprecated operator) -> (expr COLLATE "BINARY")."""
    out: list[tr.Tok] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.is_word("BINARY"):
            p = tr.prev_sig(toks, i)
            k = tr.next_sig(toks, i)
            prev_ok = not (p >= 0 and toks[p].is_word("AS", "CHAR", "VARCHAR", "CHARACTER"))
            if prev_ok and k < n and (toks[k].kind in (tr.STR, tr.NUM) or toks[k].is_punct("(")
                                      or (toks[k].kind == tr.WORD and
                                          toks[k].text.strip().upper() not in _STOP_WORDS
                                          and toks[k].text.upper() not in ("NULL",))) \
                    and not (p >= 0 and toks[p].kind == tr.WORD and
                             toks[p].text.strip().upper() not in _STOP_WORDS and
                             toks[p].upper not in ("SELECT",)):
                if not toks[k].is_punct("("):
                    e = tr.primary_end(toks, k)
                else:
                    e = tr.primary_end(toks, k)
                if e > 0 and not (toks[k].kind == tr.WORD and
                                  tr.next_sig(toks, k) < n and toks[tr.next_sig(toks, k)].is_punct("(")
                                  and False):
                    out.append(tr.Tok(tr.PUNCT, "("))
                    out.extend(toks[k:e])
                    out.extend(tr.tokenize_plain(' COLLATE "BINARY")'))
                    i = e
                    continue
        out.append(t)
        i += 1
    return out


def collations(toks):
    """COLLATE <mysql collation> -> the nearest DuckDB collation (or unchanged)."""
    out: list[tr.Tok] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if t.is_word("COLLATE"):
            k = tr.next_sig(toks, i)
            if k < n and not toks[k].is_punct("=") and toks[k].kind in (tr.WORD, tr.STR):
                name = (toks[k].value if toks[k].kind == tr.STR else toks[k].text).strip('"`').lower()
                if name.startswith('"') or name in ("nocase", "noaccent", "binary", "nfc"):
                    pass
                elif _is_cs_collation(name):
                    out.append(t)
                    out.append(tr.Tok(tr.WS, " "))
                    out.append(tr.Tok(tr.WORD, '"BINARY"'))
                    i = k + 1
                    continue
                elif name.endswith("_ai_ci") or name.endswith("_0900_ai_ci"):
                    out.extend([t, tr.Tok(tr.WS, " "), tr.Tok(tr.WORD, "NOCASE.NOACCENT")])
                    i = k + 1
                    continue
                elif name.endswith("_ci") or name.endswith("_as_ci"):
                    out.extend([t, tr.Tok(tr.WS, " "), tr.Tok(tr.WORD, "NOCASE")])
                    i = k + 1
                    continue
        out.append(t)
        i += 1
    return out


PRE_RULES: list = [hex_bit_literals, introducers, adjacent_strings, like_case_sensitive,
                   binary_prefix, sounds_like, regexp_operator]
POST_RULES: list = [like_marker, collations]


# --------------------------------------------------------------------------- #
#  Python function registration
# --------------------------------------------------------------------------- #
def register(con) -> None:
    u = lambda name, fn, params, ret: H.udf(con, name, fn, params, ret)  # noqa: E731
    loud = lambda name, fn, params, ret: con.create_function(              # noqa: E731
        name, fn, params, ret, null_handling="special")                    # errors surface
    u("_my_unesc", f_unesc, [V], V)
    u("_my_unesc_blob", f_unesc_blob, [V], BLOB)
    u("_my_enc", f_enc, [V], BLOB)
    u("_my_ascii", f_ascii, [V], B)
    u("_my_ord", f_ord, [V], B)
    u("_my_bin", f_bin, [B], V)
    u("_my_oct", f_oct, [B], V)
    u("_my_unhex", f_unhex, [V], BLOB)
    u("_my_conv", f_conv, [V, B, B], V)
    loud("_my_format", f_format, [V, B, V], V)
    u("_my_to_base64", f_to_base64, [V], V)
    u("_my_from_base64", f_from_base64, [V], BLOB)
    u("_my_locate", f_locate, [V, V, B], B)
    u("_my_substr2", f_substr2, [V, B], V)
    u("_my_substr3", f_substr3, [V, B, B], V)
    u("_my_insert", f_insert, [V, B, B, V], V)
    u("_my_left", f_left, [V, B], V)
    u("_my_right", f_right, [V, B], V)
    u("_my_substring_index", f_substring_index, [V, V, B], V)
    u("_my_replace", f_replace, [V, V, V], V)
    u("_my_reverse", f_reverse, [V], V)
    u("_my_repeat", f_repeat, [V, B], V)
    u("_my_space", f_space, [B], V)
    u("_my_lpad", f_lpad, [V, B, V], V)
    u("_my_rpad", f_rpad, [V, B, V], V)
    u("_my_trim", f_trim, [V, V, B], V)
    u("_my_ltrim", f_ltrim, [V], V)
    u("_my_rtrim", f_rtrim, [V], V)
    u("_my_strcmp", f_strcmp, [V, V], B)
    u("_my_find_in_set", f_find_in_set, [V, V], B)
    u("_my_export_set", f_export_set, [B, V, V, V, B], V)
    u("_my_soundex", f_soundex, [V], V)
    u("_my_quote", f_quote, [V], V)
    u("_my_char1", f_char1, [V], BLOB)
    loud("_my_decode", f_decode, [BLOB, V], V)
    u("_my_weight_binary", f_weight_binary, [V, B], BLOB)
    u("_my_like_ci", f_like_ci, [V, V, V], BOOL)
    u("_my_like_cs", f_like_cs, [V, V, V], BOOL)
    loud("_my_regexp_like", f_regexp_like, [V, V, V], BOOL)
    loud("_my_regexp_instr", f_regexp_instr, [V, V, B, B, B, V], B)
    loud("_my_regexp_substr", f_regexp_substr, [V, V, B, B, V], V)
    loud("_my_regexp_replace", f_regexp_replace, [V, V, V, B, B, V], V)
