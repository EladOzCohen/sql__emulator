"""BigQuery math, conditional and conversion functions on DuckDB.

* CAST / SAFE_CAST: BigQuery type names -> DuckDB types, hex strings to INT64,
  FORMAT '...' [AT TIME ZONE ...] through a Python formatter / parser.
* ROUND(x, n, mode), TRUNC(x, n), LOG, GREATEST / LEAST (NULL-propagating).
* Macros for the functions DuckDB lacks (SEC, CSC, COTH, DIV, IEEE_DIVIDE, ...).

Nothing here is named like an operator, a core aggregate or a window function.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Context, Decimal

from .. import translate as tr
from . import helpers as H

_BIG = Context(prec=120)

# --------------------------------------------------------------------------- #
#  Macros
# --------------------------------------------------------------------------- #
_DIV0 = "CAST('division by zero' AS INTEGER)"
PRELUDE: list[str] = [
    "CREATE OR REPLACE TEMP MACRO safe_divide(x, y) AS "
    "(CASE WHEN y = 0 THEN NULL ELSE CAST(x AS DOUBLE) / y END)",
    "CREATE OR REPLACE TEMP MACRO safe_add(x, y) AS TRY(x + y)",
    "CREATE OR REPLACE TEMP MACRO safe_subtract(x, y) AS TRY(x - y)",
    "CREATE OR REPLACE TEMP MACRO safe_multiply(x, y) AS TRY(x * y)",
    "CREATE OR REPLACE TEMP MACRO safe_negate(x) AS TRY(-x)",
    f"CREATE OR REPLACE TEMP MACRO sec(x) AS (CASE WHEN cos(x) = 0 THEN {_DIV0} ELSE 1.0 / cos(x) END)",
    f"CREATE OR REPLACE TEMP MACRO csc(x) AS (CASE WHEN sin(x) = 0 THEN {_DIV0} ELSE 1.0 / sin(x) END)",
    "CREATE OR REPLACE TEMP MACRO sech(x) AS (1.0 / cosh(x))",
    f"CREATE OR REPLACE TEMP MACRO csch(x) AS (CASE WHEN sinh(x) = 0 THEN {_DIV0} ELSE 1.0 / sinh(x) END)",
    f"CREATE OR REPLACE TEMP MACRO coth(x) AS (CASE WHEN tanh(x) = 0 THEN {_DIV0} ELSE 1.0 / tanh(x) END)",
    "CREATE OR REPLACE TEMP MACRO ieee_divide(x, y) AS (CASE "
    "WHEN x IS NULL OR y IS NULL THEN NULL "
    "WHEN isnan(CAST(x AS DOUBLE)) OR isnan(CAST(y AS DOUBLE)) THEN CAST('nan' AS DOUBLE) "
    "WHEN y = 0 AND x = 0 THEN CAST('nan' AS DOUBLE) "
    "WHEN isinf(CAST(x AS DOUBLE)) AND isinf(CAST(y AS DOUBLE)) THEN CAST('nan' AS DOUBLE) "
    "ELSE CAST(x AS DOUBLE) / CAST(y AS DOUBLE) END)",
    "CREATE OR REPLACE TEMP MACRO is_inf(x) AS isinf(CAST(x AS DOUBLE))",
    "CREATE OR REPLACE TEMP MACRO is_nan(x) AS isnan(CAST(x AS DOUBLE))",
    f"CREATE OR REPLACE TEMP MACRO div(x, y) AS "
    f"(CASE WHEN y = 0 THEN {_DIV0} ELSE sign(x) * sign(y) * (abs(x) // abs(y)) END)",
    "CREATE OR REPLACE TEMP MACRO range_bucket(p, arr) AS "
    "(CASE WHEN p IS NULL THEN NULL ELSE len(list_filter(arr, v -> v <= p)) END)",
    "CREATE OR REPLACE TEMP MACRO parse_numeric(s) AS CAST(_bq_parse_num(s, 9) AS DECIMAL(38,9))",
    "CREATE OR REPLACE TEMP MACRO parse_bignumeric(s) AS CAST(_bq_parse_num(s, 18) AS DECIMAL(38,18))",
    "CREATE OR REPLACE TEMP MACRO _bq_cosd_dense(a, b) AS list_cosine_distance(a, b)",
    "CREATE OR REPLACE TEMP MACRO _bq_eucd_dense(a, b) AS list_distance(a, b)",
    # sparse vectors: ARRAY<STRUCT<key, value>>
    "CREATE OR REPLACE TEMP MACRO _bq_sp_dot(a, b) AS "
    "list_sum(list_transform(a, x -> coalesce(list_sum(list_transform("
    "list_filter(b, y -> struct_extract(y, 1) = struct_extract(x, 1)), "
    "y -> CAST(struct_extract(x, 2) AS DOUBLE) * CAST(struct_extract(y, 2) AS DOUBLE))), 0.0)))",
    "CREATE OR REPLACE TEMP MACRO _bq_sp_norm(a) AS "
    "sqrt(coalesce(list_sum(list_transform(a, x -> CAST(struct_extract(x, 2) AS DOUBLE) ^ 2)), 0.0))",
    "CREATE OR REPLACE TEMP MACRO _bq_cosd_sparse(a, b) AS "
    f"(CASE WHEN _bq_sp_norm(a) = 0 OR _bq_sp_norm(b) = 0 THEN {_DIV0} "
    "ELSE 1 - _bq_sp_dot(a, b) / (_bq_sp_norm(a) * _bq_sp_norm(b)) END)",
    "CREATE OR REPLACE TEMP MACRO _bq_eucd_sparse(a, b) AS "
    "sqrt(coalesce(list_sum(list_transform(a, x -> (CAST(struct_extract(x, 2) AS DOUBLE) - "
    "coalesce(list_sum(list_transform(list_filter(b, y -> struct_extract(y, 1) = struct_extract(x, 1)), "
    "y -> CAST(struct_extract(y, 2) AS DOUBLE))), 0.0)) ^ 2)), 0.0) "
    "+ coalesce(list_sum(list_transform(list_filter(b, y -> len(list_filter(a, x -> "
    "struct_extract(x, 1) = struct_extract(y, 1))) = 0), y -> CAST(struct_extract(y, 2) AS DOUBLE) ^ 2)), 0.0))",
]


# --------------------------------------------------------------------------- #
#  Type names
# --------------------------------------------------------------------------- #
_SIMPLE = {
    "INT64": "BIGINT", "INT": "BIGINT", "SMALLINT": "BIGINT", "INTEGER": "BIGINT",
    "BIGINT": "BIGINT", "TINYINT": "BIGINT", "BYTEINT": "BIGINT",
    "FLOAT64": "DOUBLE", "FLOAT": "DOUBLE", "DOUBLE": "DOUBLE",
    "FLOAT32": "FLOAT",
    "BOOL": "BOOLEAN", "BOOLEAN": "BOOLEAN",
    "STRING": "VARCHAR", "BYTES": "BLOB",
    "DATE": "DATE", "DATETIME": "TIMESTAMP", "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMPTZ", "JSON": "JSON", "INTERVAL": "INTERVAL",
}
_TYPE_TOK = re.compile(r"\s*(<|>|\(|\)|,|[A-Za-z_][A-Za-z_0-9]*|\d+)")


class _TypeError(Exception):
    pass


def _lex_type(text: str) -> list[str]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TYPE_TOK.match(text, pos)
        if not m:
            raise _TypeError(text)
        out.append(m.group(1))
        pos = m.end()
    return out


def _parse_type(tk: list[str], i: int) -> tuple[str, int]:
    if i >= len(tk):
        raise _TypeError("end")
    name = tk[i].upper()
    i += 1
    params = ""
    if i < len(tk) and tk[i] == "(":                  # STRING(10), NUMERIC(10, 2)
        j = tk.index(")", i)
        params = [p for p in tk[i + 1:j] if p != ","]
        i = j + 1
    if name in ("NUMERIC", "DECIMAL", "BIGNUMERIC", "BIGDECIMAL"):
        big = name.startswith("BIG")
        if params and params[0].isdigit():
            p = min(int(params[0]), 38)
            s = int(params[1]) if len(params) > 1 else 0
            return f"DECIMAL({p},{min(s, p)})", i
        return ("DECIMAL(38,18)" if big else "DECIMAL(38,9)"), i
    if name == "ARRAY":
        if i >= len(tk) or tk[i] != "<":
            raise _TypeError("ARRAY")
        inner, i = _parse_type(tk, i + 1)
        if i >= len(tk) or tk[i] != ">":
            raise _TypeError("ARRAY>")
        return f"{inner}[]", i + 1
    if name == "STRUCT":
        if i >= len(tk) or tk[i] != "<":
            raise _TypeError("STRUCT")
        i += 1
        fields, n = [], 0
        while True:
            n += 1
            fname = f"_field_{n}"
            if (i + 1 < len(tk) and tk[i + 1] not in (",", ">", "<", "(")
                    and tk[i].upper() not in _SIMPLE):
                fname, i = tk[i], i + 1                # `name TYPE`
            inner, i = _parse_type(tk, i)
            fields.append(f'"{fname}" {inner}')
            if i < len(tk) and tk[i] == ",":
                i += 1
                continue
            break
        if i >= len(tk) or tk[i] != ">":
            raise _TypeError("STRUCT>")
        return f"STRUCT({', '.join(fields)})", i + 1
    if name in _SIMPLE:
        return _SIMPLE[name], i
    raise _TypeError(name)


def convert_type(text: str) -> str | None:
    """A BigQuery type, written as in CAST(x AS <type>), as a DuckDB type (None if unknown)."""
    try:
        tk = _lex_type(text)
        out, i = _parse_type(tk, 0)
        return out if i == len(tk) else None
    except (_TypeError, ValueError):
        return None


def _base_kind(text: str) -> str:
    m = re.match(r"\s*([A-Za-z_0-9]+)", text)
    return m.group(1).upper() if m else ""


# --------------------------------------------------------------------------- #
#  Python helpers (registered as UDFs)
# --------------------------------------------------------------------------- #
_PN = re.compile(r"^\s*([+-])?\s*([\d,]*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?\s*([+-])?\s*$")


def parse_num(s, scale):
    """PARSE_NUMERIC rules: spaces, commas in the integer part, leading or trailing sign."""
    if s is None:
        return None
    m = _PN.match(s)
    if not m:
        raise ValueError(s)
    lead, ip, fp, ex, trail = m.groups()
    if lead and trail:
        raise ValueError(s)
    ip = ip.replace(",", "")
    if not ip and not fp:
        raise ValueError(s)
    d = Decimal(f"{ip or '0'}.{fp or '0'}E{ex or 0}")
    if (lead or trail) == "-":
        d = -d
    q = d.quantize(Decimal(1).scaleb(-int(scale)), rounding=ROUND_HALF_UP, context=_BIG)
    return format(q, "f")


def ieee_divide(x, y):
    if x is None or y is None:
        return None
    if math.isnan(x) or math.isnan(y):
        return math.nan
    if y == 0:
        if x == 0:
            return math.nan
        return math.copysign(math.inf, x) * math.copysign(1.0, y)
    if math.isinf(x) and math.isinf(y):
        return math.nan
    return x / y


_MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
           "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
_DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
_ELEMS = ["HH24", "HH12", "SSSSS", "MONTH", "YYYY", "RRRR", "TZH", "TZM", "DAY", "DDD", "FF1",
          "FF2", "FF3", "FF4", "FF5", "FF6", "FF7", "FF8", "FF9", "A.M.", "P.M.", "MON", "YYY",
          "HH", "MI", "SS", "DD", "MM", "YY", "RR", "IW", "WW", "DY", "AM", "PM", "CC", "Q",
          "D", "W", "Y", "FF"]
_TEXT_RE = re.compile(r'"[^"]*"')


def _tokenize_fmt(fmt: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == '"':
            j = fmt.find('"', i + 1)
            j = len(fmt) - 1 if j < 0 else j
            out.append(("lit", fmt[i + 1:j]))
            i = j + 1
            continue
        for e in _ELEMS:
            if fmt[i:i + len(e)].upper() == e:
                out.append(("el", fmt[i:i + len(e)]))
                i += len(e)
                break
        else:
            out.append(("lit", fmt[i]))
            i += 1
    return out


def _case_as(template: str, word: str) -> str:
    letters = [c for c in template if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return word.upper()
    if letters and letters[0].isupper():
        return word.capitalize()
    return word.lower()


_TS_RE = re.compile(r"^(?:(\d{4,})-(\d\d)-(\d\d))?(?:[ T]?(\d\d):(\d\d):(\d\d)(?:\.(\d+))?)?\s*(Z|[+-]\d\d(?::?\d\d)?)?$")


def _parse_value(text: str):
    """VARCHAR of a DATE / TIME / TIMESTAMP value -> (datetime, has_date, has_time, tz_offset)."""
    m = _TS_RE.match(text.strip())
    if not m or not text.strip():
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    off = None
    if tz:
        if tz == "Z":
            off = timedelta(0)
        else:
            sign = -1 if tz[0] == "-" else 1
            digits = tz[1:].replace(":", "")
            off = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
    micro = int((frac or "0").ljust(6, "0")[:6])
    dt = datetime(int(y or 1970), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0), micro)
    return dt, y is not None, h is not None, off


def _zone(name):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        m = re.fullmatch(r"([+-])(\d\d)(?::?(\d\d))?", name.strip())
        if m:
            return timezone((-1 if m.group(1) == "-" else 1)
                            * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))
        raise


def _format_temporal(parsed, fmt, tzname):
    dt, has_date, has_time, off = parsed
    zone = _zone(tzname)
    if off is None and zone is not None and has_date and has_time:
        off = timedelta(0)                                # a TIMESTAMP is a UTC instant
    if off is not None:                                   # a TIMESTAMP value (UTC instant)
        inst = (dt - off).replace(tzinfo=timezone.utc)
        dt = inst.astimezone(zone or timezone.utc)
        off = dt.utcoffset()
    out = []
    for kind, tok in _tokenize_fmt(fmt):
        if kind == "lit":
            out.append(tok)
            continue
        u = tok.upper()
        if u in ("YYYY", "RRRR"):
            out.append(f"{dt.year:04d}")
        elif u == "YYY":
            out.append(f"{dt.year % 1000:03d}")
        elif u in ("YY", "RR"):
            out.append(f"{dt.year % 100:02d}")
        elif u == "Y":
            out.append(str(dt.year % 10))
        elif u == "CC":
            out.append(f"{(dt.year - 1) // 100 + 1:02d}")
        elif u == "Q":
            out.append(str((dt.month - 1) // 3 + 1))
        elif u == "MM":
            out.append(f"{dt.month:02d}")
        elif u == "MONTH":
            out.append(_case_as(tok, _MONTHS[dt.month - 1]))
        elif u == "MON":
            out.append(_case_as(tok, _MONTHS[dt.month - 1][:3]))
        elif u == "DDD":
            out.append(f"{dt.timetuple().tm_yday:03d}")
        elif u == "DD":
            out.append(f"{dt.day:02d}")
        elif u == "D":
            out.append(str(dt.isoweekday() % 7 + 1))
        elif u == "DAY":
            out.append(_case_as(tok, _DAYS[dt.weekday()]))
        elif u == "DY":
            out.append(_case_as(tok, _DAYS[dt.weekday()][:3]))
        elif u == "IW":
            out.append(f"{dt.isocalendar()[1]:02d}")
        elif u == "WW":
            out.append(f"{(dt.timetuple().tm_yday - 1) // 7 + 1:02d}")
        elif u == "W":
            out.append(str((dt.day - 1) // 7 + 1))
        elif u == "HH24":
            out.append(f"{dt.hour:02d}")
        elif u in ("HH12", "HH"):
            out.append(f"{dt.hour % 12 or 12:02d}")
        elif u == "MI":
            out.append(f"{dt.minute:02d}")
        elif u == "SS":
            out.append(f"{dt.second:02d}")
        elif u == "SSSSS":
            out.append(f"{dt.hour * 3600 + dt.minute * 60 + dt.second:05d}")
        elif u.startswith("FF"):
            n = int(u[2:] or 6)
            out.append(f"{dt.microsecond:06d}"[:n].ljust(n, "0"))
        elif u in ("AM", "PM"):
            out.append(_case_as(tok, "AM" if dt.hour < 12 else "PM"))
        elif u in ("A.M.", "P.M."):
            out.append(_case_as(tok, "A.M." if dt.hour < 12 else "P.M."))
        elif u in ("TZH", "TZM"):
            o = off if off is not None else timedelta(0)
            mins = int(o.total_seconds() // 60)
            sign = "-" if mins < 0 else "+"
            mins = abs(mins)
            out.append(f"{sign}{mins // 60:02d}" if u == "TZH" else f"{mins % 60:02d}")
        else:
            out.append(tok)
    return "".join(out)


def _format_number(text: str, fmt: str):
    val = Decimal(text)
    f = fmt.strip()
    upper = f.upper()
    fm = upper.startswith("FM")
    if fm:
        f = f[2:]
        upper = upper[2:]
    if "EEEE" in upper:
        body = upper.replace("EEEE", "")
        decs = len(re.sub(r"[^90]", "", body.split(".")[1])) if "." in body else 0
        sign = "-" if val < 0 else ""
        v = abs(val)
        exp = 0 if v == 0 else v.adjusted()
        mant = v.scaleb(-exp).quantize(Decimal(1).scaleb(-decs), rounding=ROUND_HALF_UP, context=_BIG)
        return f"{sign}{mant}E{'-' if exp < 0 else '+'}{abs(exp):02d}"
    lead = trail = ""
    dollar = "$" in upper
    body = upper
    if body.startswith("S"):
        lead = "S"
        body = body[1:]
    if body.endswith("MI"):
        trail = "MI"
        body = body[:-2]
    elif body.endswith("PR"):
        trail = "PR"
        body = body[:-2]
    elif body.endswith("S"):
        trail = "S"
        body = body[:-1]
    body = body.replace("$", "")
    if "." in body:
        ipart, fpart = body.split(".", 1)
    else:
        ipart, fpart = body, None
    ndec = len(re.sub(r"[^90]", "", fpart)) if fpart is not None else 0
    v = abs(val).quantize(Decimal(1).scaleb(-ndec), rounding=ROUND_HALF_UP, context=_BIG)
    s = format(v, "f")
    isec, _, fsec = s.partition(".")
    ndig = len(re.sub(r"[^90]", "", ipart))
    zeros = len(ipart) - len(ipart.lstrip(",")) and 0
    first0 = ipart.find("0")
    if len(isec) > ndig and not (isec == "0" and ndig == 0):
        if not (isec == "0" and first0 < 0):
            res = re.sub(r"[90]", "#", ipart) + ("." + re.sub(r"[90]", "#", fpart) if fpart is not None else "")
            res = res.replace(",", ",")
            num = res
            isec = None
    if isec is not None:
        digits = isec.rjust(ndig, "0") if first0 >= 0 else (isec if isec != "0" else "")
        if first0 >= 0:
            nz = len(re.sub(r"[^90]", "", ipart[first0:]))
            digits = isec.rjust(nz, "0")
        out, di = [], len(digits) - 1
        pos_digits = [c for c in ipart if c in "90"]
        # place digits right-aligned into the 9/0 slots, keeping commas between used slots
        slots = []
        for c in reversed(ipart):
            if c in "90":
                slots.append(digits[di] if di >= 0 else None)
                di -= 1
            elif c == ",":
                slots.append(",")
        piece = []
        have_digit = False
        for ch in reversed(slots):
            if ch == ",":
                if have_digit:
                    piece.append(",")
                continue
            if ch is None:
                continue
            have_digit = True
            piece.append(ch)
        num = "".join(piece)
        if fpart is not None:
            frac = fsec.ljust(ndec, "0")
            if fm:
                frac = frac.rstrip("0")
            num += "." + frac if (frac or not fm) else ""
        if not num:
            num = "0" if fpart is None else ""
    neg = val < 0 and v != 0 or (val < 0 and v == 0)
    sym = "$" if dollar else ""
    if trail == "PR":
        return f"<{sym}{num}>" if neg else f" {sym}{num} "
    if lead == "S":
        return ("-" if neg else "+") + sym + num
    if trail == "S":
        return sym + num + ("-" if neg else "+")
    if trail == "MI":
        return sym + num + ("-" if neg else " ")
    return ("-" if neg else "") + sym + num


def cast_format(value, fmt, target, tz):
    """CAST(value AS <target> FORMAT fmt [AT TIME ZONE tz]); value arrives as VARCHAR."""
    if value is None or fmt is None:
        return None
    target = (target or "").upper()
    if target == "STRING":
        f = fmt.upper().strip()
        if f in ("ASCII", "UTF-8", "UTF8"):
            raw = value
            if raw.startswith("\\x") or "\\x" in raw:
                return bytes(int(h, 16) for h in re.findall(r"\\x([0-9A-Fa-f]{2})", raw)).decode("utf-8", "replace")
            return raw
        if f in ("HEX", "BASE64", "BASE32", "BASEX"):
            return value
        parsed = _parse_value(value)
        if parsed is not None and not re.fullmatch(r"[+-]?\d+(\.\d+)?", value.strip()):
            return _format_temporal(parsed, fmt, tz)
        return _format_number(value, fmt)
    if target == "BYTES":
        return value
    return _parse_temporal(value, fmt, target)


def _parse_temporal(text, fmt, target):
    y = mo = d = None
    hour = minute = second = 0
    micro = 0
    pm = None
    tzh = tzm = None
    pos = 0
    toks = _tokenize_fmt(fmt)

    def take(pattern):
        nonlocal pos
        m = re.compile(pattern).match(text, pos)
        if not m:
            raise ValueError(text)
        pos = m.end()
        return m.group(0)

    for idx, (kind, tok) in enumerate(toks):
        if kind == "lit":
            if tok.isspace():
                take(r"\s*")
            elif text[pos:pos + len(tok)] == tok:
                pos += len(tok)
            else:
                raise ValueError(text)
            continue
        u = tok.upper()
        if u in ("YYYY", "RRRR"):
            y = int(take(r"\d{4}"))
        elif u in ("YY", "RR"):
            y = 2000 + int(take(r"\d{2}"))
        elif u == "MM":
            mo = int(take(r"\d{1,2}"))
        elif u == "MONTH":
            w = take(r"[A-Za-z]+").upper()
            mo = [m_ for m_ in _MONTHS].index(w) + 1
        elif u == "MON":
            w = take(r"[A-Za-z]{3}").upper()
            mo = [m_[:3] for m_ in _MONTHS].index(w) + 1
        elif u == "DD":
            d = int(take(r"\d{1,2}"))
        elif u == "HH24":
            hour = int(take(r"\d{1,2}"))
        elif u in ("HH12", "HH"):
            hour = int(take(r"\d{1,2}"))
        elif u == "MI":
            minute = int(take(r"\d{1,2}"))
        elif u == "SS":
            second = int(take(r"\d{1,2}"))
        elif u.startswith("FF"):
            n = int(u[2:] or 6)
            digs = take(r"\d+")
            micro = int(Decimal("0." + digs).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP) * 1000000)
        elif u in ("AM", "PM", "A.M.", "P.M."):
            w = take(r"[AaPp]\.?[Mm]\.?").upper().replace(".", "")
            pm = w == "PM"
        elif u == "TZH":
            s = take(r"[+-]?\d{1,2}")
            tzh = int(s)
        elif u == "TZM":
            tzm = int(take(r"\d{2}"))
        else:
            raise ValueError(text)
    if pm is not None:
        hour = hour % 12 + (12 if pm else 0)
    dt = datetime(y if y is not None else 1970, mo or 1, d or 1, hour, minute, second, micro)
    if target == "DATE":
        return dt.strftime("%Y-%m-%d")
    if target == "TIME":
        return dt.strftime("%H:%M:%S") + (f".{micro:06d}".rstrip("0") if micro else "")
    s = dt.strftime("%Y-%m-%d %H:%M:%S") + (f".{micro:06d}".rstrip("0") if micro else "")
    if target == "TIMESTAMP":
        sign = "-" if (tzh or 0) < 0 else "+"
        return f"{s}{sign}{abs(tzh or 0):02d}:{tzm or 0:02d}"
    return s


def _hex_int(text: str):
    m = re.fullmatch(r"\s*([+-]?)0[xX]([0-9A-Fa-f]+)\s*", text)
    return None if not m else f"{m.group(1)}{int(m.group(2), 16)}"


def register(con) -> None:
    H.udf(con, "_bq_parse_num", parse_num, [H.VARCHAR, H.INTEGER], H.VARCHAR)
    H.udf(con, "_bq_cast_fmt", cast_format, [H.VARCHAR, H.VARCHAR, H.VARCHAR, H.VARCHAR], H.VARCHAR)


# --------------------------------------------------------------------------- #
#  Handlers
# --------------------------------------------------------------------------- #
def _join(args: list[tr.Arg]) -> list[tr.Tok]:
    toks: list[tr.Tok] = []
    for i, a in enumerate(args):
        if i:
            toks.append(tr.Tok(tr.PUNCT, ","))
        toks.extend(a.toks)
    return toks


def _split_cast(args: list[tr.Arg]):
    toks = _join(args)
    depth, as_idx = 0, -1
    for i, t in enumerate(toks):
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.is_word("AS"):
            as_idx = i
    if as_idx < 0:
        return None
    expr = tr.text_of(toks[:as_idx])
    rest = toks[as_idx + 1:]
    depth, fidx = 0, -1
    for i, t in enumerate(rest):
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.is_word("FORMAT"):
            fidx = i
            break
    if fidx < 0:
        return expr, tr.text_of(rest), None, None
    type_text = tr.text_of(rest[:fidx])
    tail = rest[fidx + 1:]
    tz = None
    sig = [i for i, t in enumerate(tail) if t.kind not in (tr.WS, tr.COMMENT)]
    for k, i in enumerate(sig):
        if (tail[i].is_word("AT") and k + 2 < len(sig) and tail[sig[k + 1]].is_word("TIME")
                and tail[sig[k + 2]].is_word("ZONE")):
            tz = tr.text_of(tail[sig[k + 3]:]) if k + 3 < len(sig) else None
            tail = tail[:i]
            break
    return expr, type_text, tr.text_of(tail), tz


_SPECIAL_FLOAT = {"+inf": "inf", "inf": "inf", "-inf": "-inf", "infinity": "inf",
                  "+infinity": "inf", "-infinity": "-inf", "nan": "nan"}


def _cast(args: list[tr.Arg], fn: str):
    parts = _split_cast(args)
    if parts is None:
        return None
    expr, type_text, fmt, tz = parts
    duck = convert_type(type_text)
    if duck is None:
        return None
    kind = _base_kind(type_text)
    lit = tr.is_string_literal(tr.tokenize_plain(expr))
    if fmt is not None:
        target = {"STRING": "STRING", "BYTES": "BYTES"}.get(kind, kind)
        tzs = tz if tz else "NULL"
        base = f"_bq_cast_fmt(CAST({expr} AS VARCHAR), {fmt}, '{target}', {tzs})"
        if target in ("STRING", "BYTES"):
            return f"CAST({base} AS {duck})" if target == "BYTES" else base
        return f"{fn}({base} AS {duck})"
    if lit is not None:
        if kind in ("INT64", "INT", "INTEGER", "SMALLINT", "BIGINT", "TINYINT", "BYTEINT"):
            h = _hex_int(lit)
            if h is not None:
                return f"{fn}('{h}' AS {duck})"
        if kind in ("FLOAT64", "FLOAT", "FLOAT32", "DOUBLE"):
            sp = _SPECIAL_FLOAT.get(lit.strip().lower())
            if sp:
                return f"{fn}('{sp}' AS {duck})"
    return f"{fn}({expr} AS {duck})"


def _h_cast(args):
    return _cast(args, "CAST")


def _h_safe_cast(args):
    return _cast(args, "TRY_CAST")


def _h_round(args):
    if len(args) != 3:
        return None
    mode = args[2].literal()
    if mode is None:
        return None
    m = mode.upper()
    if m == "ROUND_HALF_EVEN":
        return f"round_even({args[0]}, {args[1]})"
    if m == "ROUND_HALF_AWAY_FROM_ZERO":
        return f"round({args[0]}, {args[1]})"
    return None


def _h_trunc(args):
    if len(args) != 2:
        return None
    x, n = args[0], args[1]
    return f"(sign({x}) * floor(abs({x}) * power(10, {n})) / power(10, {n}))"


def _h_log(args):
    if len(args) == 1:
        return f"ln({args[0]})"
    if len(args) == 2:
        return f"log({args[1]}, {args[0]})"
    return None


def _null_propagating(fn):
    def handler(args):
        if len(args) < 2:
            return None
        guard = " OR ".join(f"({a}) IS NULL" for a in args)
        return f"(CASE WHEN {guard} THEN NULL ELSE {fn}({', '.join(a.text for a in args)}) END)"
    return handler


def _vector(name):
    def handler(args):
        if len(args) != 2:
            return None
        sparse = all(re.match(r"\s*\[\s*\(", a.text) for a in args)
        return f"_bq_{name}_{'sparse' if sparse else 'dense'}({args[0]}, {args[1]})"
    return handler


def _inf_nan(fn):
    """fn(x) with +-inf -> NaN (DuckDB raises out-of-range errors where GoogleSQL answers NaN)."""
    def handler(args):
        if len(args) != 1:
            return None
        x = args[0]
        return (f"(CASE WHEN isinf(CAST({x} AS DOUBLE)) THEN CAST('nan' AS DOUBLE) "
                f"ELSE {fn}({x}) END)")
    return handler


def _h_log10(args):
    if len(args) != 1:
        return None
    x = args[0]
    return (f"(CASE WHEN CAST({x} AS DOUBLE) = CAST('-inf' AS DOUBLE) THEN CAST('nan' AS DOUBLE) "
            f"ELSE log10({x}) END)")


def _h_sign(args):
    if len(args) != 1:
        return None
    x = args[0]
    return f"(CASE WHEN isnan(CAST({x} AS DOUBLE)) THEN CAST({x} AS DOUBLE) ELSE sign({x}) END)"


HANDLERS: dict = {
    "CAST": _h_cast,
    "SAFE_CAST": _h_safe_cast,
    "ROUND": _h_round,
    "TRUNC": _h_trunc,
    "LOG": _h_log,
    "GREATEST": _null_propagating("greatest"),
    "LEAST": _null_propagating("least"),
    "SIGN": _h_sign,
    "LOG10": _h_log10,
    **{n: _inf_nan(n.lower()) for n in ("SIN", "COS", "TAN", "COT", "ACOS", "ASIN", "ATANH")},
    "SEC": _inf_nan("sec"),
    "CSC": _inf_nan("csc"),
    "COSINE_DISTANCE": _vector("cosd"),
    "EUCLIDEAN_DISTANCE": _vector("eucd"),
}
PRE_RULES: list = []
POST_RULES: list = []
