"""BigQuery (GoogleSQL) DATE / DATETIME / TIMESTAMP / TIME functions on DuckDB.

Calls with bare-keyword arguments (DATE_DIFF(a, b, DAY), EXTRACT(WEEK(MONDAY) FROM x),
INTERVAL 5 DAY ...) are rewritten by HANDLERS into casts plus a few Python UDFs
(`_bq_*`). The UDFs work on text ('YYYY-MM-DD HH:MM:SS[.ffffff]') so one
implementation serves DATE, DATETIME, TIMESTAMP (UTC session) and TIME.
"""

from __future__ import annotations

import datetime as dt
import re

from .. import translate as tr
from . import helpers as H

PRELUDE: list[str] = []
PRE_RULES: list = []
POST_RULES: list = []

_EPOCH = dt.datetime(1970, 1, 1)
_WD = {"MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3, "FRIDAY": 4,
       "SATURDAY": 5, "SUNDAY": 6}
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]


# --------------------------------------------------------------------------- #
#  Text <-> datetime
# --------------------------------------------------------------------------- #
_TS_RE = re.compile(
    r"^\s*(?:(\d{1,4})-(\d{1,2})-(\d{1,2}))?[ T]*"
    r"(?:(\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,9}))?)?)?\s*(?:Z|UTC|[+-]\d\d(?::?\d\d)?)?\s*$")


def _parse_ts(s: str) -> dt.datetime:
    m = _TS_RE.match(s)
    if not m or (m.group(1) is None and m.group(4) is None):
        raise ValueError(s)
    y, mo, d, h, mi, se, fr = m.groups()
    return dt.datetime(int(y or 1970), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0),
                       int(se or 0), int((fr or "0").ljust(6, "0")[:6]))


def _out(d: dt.datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S.%f")


def _week_start(d: dt.datetime, wd: int) -> dt.datetime:
    day = dt.datetime(d.year, d.month, d.day)
    return day - dt.timedelta(days=(d.weekday() - wd) % 7)


def _wd_of(unit: str) -> int:
    if unit in ("WEEK", "WEEK_SUNDAY"):
        return 6
    if unit == "ISOWEEK":
        return 0
    return _WD[unit[5:]]


def _is_week(unit: str) -> bool:
    return unit == "ISOWEEK" or unit.startswith("WEEK")


def _trunc(d: dt.datetime, unit: str) -> dt.datetime:
    if unit == "MICROSECOND":
        return d
    if unit == "MILLISECOND":
        return d.replace(microsecond=d.microsecond // 1000 * 1000)
    if unit == "SECOND":
        return d.replace(microsecond=0)
    if unit == "MINUTE":
        return d.replace(second=0, microsecond=0)
    if unit == "HOUR":
        return d.replace(minute=0, second=0, microsecond=0)
    if unit == "DAY":
        return dt.datetime(d.year, d.month, d.day)
    if _is_week(unit):
        return _week_start(d, _wd_of(unit))
    if unit == "MONTH":
        return dt.datetime(d.year, d.month, 1)
    if unit == "QUARTER":
        return dt.datetime(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    if unit == "YEAR":
        return dt.datetime(d.year, 1, 1)
    if unit == "ISOYEAR":
        iy = d.isocalendar()[0]
        x = dt.date.fromisocalendar(iy, 1, 1)
        return dt.datetime(x.year, x.month, x.day)
    raise ValueError(unit)


_MICROS = {"MICROSECOND": 1, "MILLISECOND": 1000, "SECOND": 10**6, "MINUTE": 60 * 10**6,
           "HOUR": 3600 * 10**6, "DAY": 86400 * 10**6}


def _diff(a: dt.datetime, b: dt.datetime, unit: str, boundary: bool = False) -> int:
    if unit in _MICROS:
        if boundary:
            a, b = _trunc(a, unit), _trunc(b, unit)
        delta = a - b
        us = (delta.days * 86400 + delta.seconds) * 10**6 + delta.microseconds
        q = abs(us) // _MICROS[unit]
        return -q if us < 0 else q
    if _is_week(unit):
        return (_trunc(a, unit) - _trunc(b, unit)).days // 7
    if unit == "MONTH":
        return (a.year * 12 + a.month) - (b.year * 12 + b.month)
    if unit == "QUARTER":
        return (a.year * 4 + (a.month - 1) // 3) - (b.year * 4 + (b.month - 1) // 3)
    if unit == "YEAR":
        return a.year - b.year
    if unit == "ISOYEAR":
        return a.isocalendar()[0] - b.isocalendar()[0]
    raise ValueError(unit)


def _week_no(d: dt.datetime, wd: int) -> int:
    """GoogleSQL WEEK: days before the first <wd> of the year are week 0."""
    jan1 = dt.datetime(d.year, 1, 1)
    first = jan1 + dt.timedelta(days=(wd - jan1.weekday()) % 7)
    day = dt.datetime(d.year, d.month, d.day)
    return 0 if day < first else (day - first).days // 7 + 1


def _extract(part: str, s: str):
    d = _parse_ts(s)
    if part == "YEAR":
        return d.year
    if part == "MONTH":
        return d.month
    if part == "DAY":
        return d.day
    if part == "HOUR":
        return d.hour
    if part == "MINUTE":
        return d.minute
    if part == "SECOND":
        return d.second
    if part == "MILLISECOND":
        return d.microsecond // 1000
    if part == "MICROSECOND":
        return d.microsecond
    if part == "DAYOFWEEK":
        return (d.weekday() + 1) % 7 + 1
    if part == "DAYOFYEAR":
        return d.timetuple().tm_yday
    if part == "QUARTER":
        return (d.month - 1) // 3 + 1
    if part == "ISOYEAR":
        return d.isocalendar()[0]
    if part == "ISOWEEK":
        return d.isocalendar()[1]
    if part == "WEEK" or part.startswith("WEEK_"):
        return _week_no(d, _wd_of(part))
    raise ValueError(part)


def _last_day(s: str, unit: str) -> str:
    d = _parse_ts(s)
    if _is_week(unit):
        r = _week_start(d, _wd_of(unit)) + dt.timedelta(days=6)
    elif unit == "MONTH":
        r = (dt.datetime(d.year + d.month // 12, d.month % 12 + 1, 1)) - dt.timedelta(days=1)
    elif unit == "QUARTER":
        q = _trunc(d, "QUARTER")
        r = dt.datetime(q.year + (q.month + 2) // 12, (q.month + 2) % 12 + 1, 1) - dt.timedelta(days=1)
    elif unit == "YEAR":
        r = dt.datetime(d.year, 12, 31)
    elif unit == "ISOYEAR":
        r = _trunc(dt.datetime.combine(dt.date.fromisocalendar(d.isocalendar()[0] + 1, 1, 1),
                                       dt.time()), "ISOYEAR") - dt.timedelta(days=1)
    else:
        raise ValueError(unit)
    return _out(r)


# --------------------------------------------------------------------------- #
#  FORMAT / PARSE
# --------------------------------------------------------------------------- #
_COMPOSITE = {"c": "%a %b %e %H:%M:%S %Y", "D": "%m/%d/%y", "F": "%Y-%m-%d", "R": "%H:%M",
              "T": "%H:%M:%S", "X": "%H:%M:%S", "x": "%m/%d/%y"}


def _format(fmt: str, s: str, offset: int, tzname: str) -> str:
    d = _parse_ts(s)
    out: list[str] = []
    i, n = 0, len(fmt)
    while i < n:
        c = fmt[i]
        if c != "%" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        i += 1
        k = fmt[i]
        i += 1
        if k == "E" and i < n:                      # %Ez, %E#S, %E*S, %E4Y
            m = re.match(r"(z|\*S|\d+S|\d*Y)", fmt[i:])
            if not m:
                raise ValueError(fmt)
            sp = m.group(1)
            i += len(sp)
            if sp == "z":
                sg = "+" if offset >= 0 else "-"
                out.append(f"{sg}{abs(offset) // 3600:02d}:{abs(offset) % 3600 // 60:02d}")
            elif sp == "*S":
                t = f"{d.second:02d}"
                if d.microsecond:
                    t += f".{d.microsecond:06d}".rstrip("0")
                out.append(t)
            elif sp.endswith("S"):
                nd = int(sp[:-1])
                out.append(f"{d.second:02d}" + (f".{d.microsecond:06d}"[:nd + 1].ljust(nd + 1, "0") if nd else ""))
            else:
                out.append(f"{d.year:0{int(sp[:-1] or 0)}d}" if sp[:-1] else str(d.year))
            continue
        if k in _COMPOSITE:
            out.append(_format(_COMPOSITE[k], s, offset, tzname))
            continue
        h12 = d.hour % 12 or 12
        iso = d.isocalendar()
        simple = {
            "A": _DAYS[d.weekday()], "a": _DAYS[d.weekday()][:3],
            "B": _MONTHS[d.month - 1], "b": _MONTHS[d.month - 1][:3], "h": _MONTHS[d.month - 1][:3],
            "C": f"{d.year // 100:02d}", "d": f"{d.day:02d}", "e": f"{d.day:2d}",
            "G": str(iso[0]), "g": f"{iso[0] % 100:02d}", "H": f"{d.hour:02d}",
            "I": f"{h12:02d}", "j": f"{d.timetuple().tm_yday:03d}", "k": f"{d.hour:2d}",
            "l": f"{h12:2d}", "M": f"{d.minute:02d}", "m": f"{d.month:02d}", "n": "\n",
            "P": "am" if d.hour < 12 else "pm", "p": "AM" if d.hour < 12 else "PM",
            "Q": str((d.month - 1) // 3 + 1), "S": f"{d.second:02d}",
            "s": str(int((d - _EPOCH).total_seconds()) - offset), "t": "\t",
            "U": f"{_week_no(d, 6):02d}", "u": str(d.isoweekday()), "V": f"{iso[1]:02d}",
            "W": f"{_week_no(d, 0):02d}", "w": str(d.isoweekday() % 7), "Y": str(d.year),
            "y": f"{d.year % 100:02d}", "Z": tzname,
            "z": f"{'+' if offset >= 0 else '-'}{abs(offset) // 3600:02d}{abs(offset) % 3600 // 60:02d}",
            "%": "%",
        }
        out.append(simple[k] if k in simple else "%" + k)
    return "".join(out)


def _expand(fmt: str) -> str:
    return re.sub(r"%([cDFRTXx])", lambda m: _expand(_COMPOSITE[m.group(1)]), fmt)


def _parse(fmt: str, s: str) -> str:
    fmt = _expand(fmt)
    rx: list[str] = []
    seen: list[str] = []
    i, n = 0, len(fmt)

    def grp(name: str, pat: str) -> str:
        if name in seen:
            raise ValueError("duplicate " + name)
        seen.append(name)
        return f"(?P<{name}>{pat})"

    while i < n:
        c = fmt[i]
        if c.isspace():
            rx.append(r"\s*")
            i += 1
            continue
        if c != "%" or i + 1 >= n:
            rx.append(re.escape(c))
            i += 1
            continue
        k = fmt[i + 1]
        i += 2
        if k == "E":
            m = re.match(r"(z|\*S|\d+S|\d*Y)", fmt[i:])
            if not m:
                raise ValueError(fmt)
            sp = m.group(1)
            i += len(sp)
            if sp == "z":
                rx.append(grp("off", r"[+-]\d\d:\d\d"))
            elif sp.endswith("S"):
                rx.append(grp("S", r"\d{1,2}") + r"(?:\.(?P<frac>\d+))?")
                seen.append("frac")
            else:
                rx.append(grp("Y", r"\d{1,4}"))
            continue
        pats = {
            "Y": ("Y", r"\d{1,4}"), "y": ("y", r"\d{2}"), "m": ("m", r"\d{1,2}"),
            "d": ("d", r"\d{1,2}"), "e": ("d", r"\s*\d{1,2}"), "H": ("H", r"\d{1,2}"),
            "k": ("H", r"\s*\d{1,2}"), "I": ("I", r"\d{1,2}"), "l": ("I", r"\s*\d{1,2}"),
            "M": ("M", r"\d{1,2}"), "S": ("S", r"\d{1,2}"), "j": ("j", r"\d{1,3}"),
            "p": ("p", r"[AaPp][Mm]"), "P": ("p", r"[AaPp][Mm]"),
            "A": ("wd", "|".join(x for x in _DAYS)), "a": ("wd", "|".join(x[:3] for x in _DAYS)),
            "B": ("mon", "|".join(_MONTHS)), "b": ("mon", "|".join(x[:3] for x in _MONTHS)),
            "h": ("mon", "|".join(x[:3] for x in _MONTHS)),
            "z": ("off", r"[+-]\d\d:?\d\d"), "Z": ("tz", r"[A-Za-z_/]+"),
            "s": ("epoch", r"-?\d+"),
        }
        if k == "%":
            rx.append("%")
        elif k in pats:
            name, pat = pats[k]
            rx.append(grp(name, pat))
        else:
            raise ValueError(k)
    m = re.fullmatch("".join(rx), s.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(s)
    g = {k: v for k, v in m.groupdict().items() if v is not None}
    if "epoch" in g:
        return _out(_EPOCH + dt.timedelta(seconds=int(g["epoch"])))
    year = int(g["Y"]) if "Y" in g else (1900 + int(g["y"]) if int(g["y"]) >= 69 else 2000 + int(g["y"])) if "y" in g else 1970
    month = int(g["m"]) if "m" in g else (
        [x[:3].lower() for x in _MONTHS].index(g["mon"][:3].lower()) + 1 if "mon" in g else 1)
    day = int(g.get("d", 1))
    hour = int(g["H"]) if "H" in g else 0
    if "I" in g:
        hour = int(g["I"]) % 12 + (12 if g.get("p", "am").lower() == "pm" else 0)
    elif "p" in g and "H" not in g:
        pass
    frac = int(g["frac"].ljust(6, "0")[:6]) if "frac" in g else 0
    if "j" in g and "m" not in g and "mon" not in g:
        base = dt.datetime(year, 1, 1) + dt.timedelta(days=int(g["j"]) - 1)
        month, day = base.month, base.day
    d = dt.datetime(year, month, day, hour, int(g.get("M", 0)), int(g.get("S", 0)), frac)
    if "off" in g:
        o = g["off"].replace(":", "")
        secs = (int(o[1:3]) * 3600 + int(o[3:5]) * 60) * (-1 if o[0] == "-" else 1)
        d -= dt.timedelta(seconds=secs)
    return _out(d)


def register(con) -> None:
    V, B = H.VARCHAR, H.BIGINT

    def nn(fn):
        n = fn.__code__.co_argcount if hasattr(fn, "__code__") else 2
        params = ", ".join(f"a{i}" for i in range(n))
        ns = {"fn": fn}
        src = "def w(" + params + "):" + chr(10) + "    return None if None in (" + params + ",) else fn(" + params + ")"
        exec(src, ns)
        return ns["w"]

    H.udf(con, "_bq_trunc", nn(lambda s, u: _out(_trunc(_parse_ts(s), u))), [V, V], V)
    H.udf(con, "_bq_diff", nn(lambda a, b, u: _diff(_parse_ts(a), _parse_ts(b), u, True)), [V, V, V], B)
    H.udf(con, "_bq_tsdiff", nn(lambda a, b, u: _diff(_parse_ts(a), _parse_ts(b), u, False)), [V, V, V], B)
    H.udf(con, "_bq_extract", nn(_extract), [V, V], B)
    H.udf(con, "_bq_lastday", nn(_last_day), [V, V], V)
    H.udf(con, "_bq_format", nn(lambda f, s, o, z: _format(f, s, int(o), z)), [V, V, B, V], V)
    H.udf(con, "_bq_parse", nn(_parse), [V, V], V)
    H.udf(con, "_bq_string", nn(lambda s, o: s[:19] + (s[19:].rstrip("0").rstrip(".") if len(s) > 19 else "")
                                + f"{'+' if o >= 0 else '-'}{abs(o) // 3600:02d}"
                                + (f":{abs(o) % 3600 // 60:02d}" if abs(o) % 3600 else "")),
          [V, B], V)


# --------------------------------------------------------------------------- #
#  Handlers
# --------------------------------------------------------------------------- #
def _unit(arg: tr.Arg) -> str:
    """DAY -> 'DAY'; WEEK(MONDAY) -> 'WEEK_MONDAY'."""
    t = re.sub(r"\s+", "", arg.text).upper().strip("'\"")
    m = re.fullmatch(r"WEEK\((\w+)\)", t)
    return f"WEEK_{m.group(1)}" if m else t


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _vc(sql: str, typ: str) -> str:
    return f"CAST(CAST({sql} AS {typ}) AS VARCHAR)"


_TYP = {"DATE": "DATE", "DATETIME": "TIMESTAMP", "TIMESTAMP": "TIMESTAMPTZ", "TIME": "TIME"}


def _iv(arg: tr.Arg, sign: int):
    toks = tr.strip(arg.toks)
    if len(toks) < 3 or not toks[0].is_word("INTERVAL") or toks[-1].kind != tr.WORD:
        return None
    n = tr.text_of(toks[1:-1])
    unit = toks[-1].upper
    ctor = {"YEAR": "to_years", "QUARTER": None, "MONTH": "to_months", "WEEK": "to_weeks",
            "DAY": "to_days", "HOUR": "to_hours", "MINUTE": "to_minutes", "SECOND": "to_seconds",
            "MILLISECOND": "to_milliseconds", "MICROSECOND": "to_microseconds"}
    if unit not in ctor:
        return None
    s = "-" if sign < 0 else ""
    if unit == "QUARTER":
        return f"system.main.to_months({s}3 * CAST(({n}) AS INTEGER))"
    arg_t = "BIGINT" if unit in ("MICROSECOND", "MILLISECOND", "SECOND") else "INTEGER"
    if unit == "SECOND":
        return f"system.main.to_microseconds({s}CAST(({n}) AS BIGINT) * 1000000)"
    return f"system.main.{ctor[unit]}({s}CAST(({n}) AS {arg_t}))"


def _h_add(kind: str, sign: int):
    def h(args):
        if len(args) != 2:
            return None
        iv = _iv(args[1], sign)
        if iv is None:
            return None
        x = args[0].text
        if kind == "DATE":
            return f"CAST(CAST({x} AS DATE) + {iv} AS DATE)"
        if kind == "TIME":
            return f"CAST(CAST({x} AS TIME) + {iv} AS TIME)"
        return f"(CAST({x} AS {_TYP[kind]}) + {iv})"
    return h


def _h_diff(kind: str):
    def h(args):
        if len(args) != 3:
            return None
        t = _TYP[kind]
        fn = "_bq_tsdiff" if kind == "TIMESTAMP" else "_bq_diff"
        return f"{fn}({_vc(args[0].text, t)}, {_vc(args[1].text, t)}, {_q(_unit(args[2]))})"
    return h


def _naive(x: str, tz: str | None) -> str:
    return f"(CAST({x} AS TIMESTAMPTZ) AT TIME ZONE {tz})" if tz else f"CAST(CAST({x} AS TIMESTAMPTZ) AS TIMESTAMP)"


def _h_trunc(kind: str):
    def h(args):
        if len(args) < 2:
            return None
        u = _q(_unit(args[1]))
        x = args[0].text
        if kind == "DATE":
            return f"CAST(_bq_trunc(CAST(CAST({x} AS TIMESTAMP) AS VARCHAR), {u}) AS DATE)"
        if kind == "DATETIME":
            return f"CAST(_bq_trunc({_vc(x, 'TIMESTAMP')}, {u}) AS TIMESTAMP)"
        if kind == "TIME":
            return f"CAST(CAST(_bq_trunc({_vc(x, 'TIME')}, {u}) AS TIMESTAMP) AS TIME)"
        tz = args[2].text if len(args) > 2 else "'UTC'"
        return (f"(CAST(_bq_trunc(CAST({_naive(x, tz)} AS VARCHAR), {u}) AS TIMESTAMP) AT TIME ZONE {tz})")
    return h


def _h_last_day(args):
    u = _q(_unit(args[1])) if len(args) > 1 else "'MONTH'"
    return f"CAST(_bq_lastday(CAST(CAST({args[0].text} AS TIMESTAMP) AS VARCHAR), {u}) AS DATE)"


def _h_extract(args):
    if len(args) != 1:
        return None
    toks = tr.strip(args[0].toks)
    depth = 0
    for i, t in enumerate(toks):
        if t.kind == tr.PUNCT and t.text == "(":
            depth += 1
        elif t.kind == tr.PUNCT and t.text == ")":
            depth -= 1
        elif depth == 0 and t.is_word("FROM"):
            part = re.sub(r"\s+", "", tr.text_of(toks[:i])).upper()
            x = tr.text_of(toks[i + 1:])
            break
    else:
        return None
    m = re.fullmatch(r"WEEK\((\w+)\)", part)
    if m:
        part = f"WEEK_{m.group(1)}"
    if part == "DATE":
        return f"CAST({x} AS DATE)"
    if part == "TIME":
        return f"CAST({x} AS TIME)"
    if part == "DATETIME":
        return f"CAST({x} AS TIMESTAMP)"
    if part in ("EPOCH",):
        return None
    return f"_bq_extract({_q(part)}, CAST(CAST({x} AS TIMESTAMP) AS VARCHAR))" if False else \
        f"_bq_extract({_q(part)}, CAST(({x}) AS VARCHAR))"


def _h_format(kind: str):
    def h(args):
        if len(args) < 2:
            return None
        f, x = args[0].text, args[1].text
        if kind == "TIMESTAMP":
            tz = args[2].text if len(args) > 2 else None
            nv = _naive(x, tz)
            off = (f"CAST(epoch({nv}) - epoch(CAST({x} AS TIMESTAMPTZ)) AS BIGINT)" if tz else "0")
            return f"_bq_format({f}, CAST({nv} AS VARCHAR), {off}, {tz or chr(39) + 'UTC' + chr(39)})"
        return f"_bq_format({f}, {_vc(x, _TYP[kind])}, 0, 'UTC')"
    return h


def _h_parse(kind: str):
    def h(args):
        if len(args) < 2:
            return None
        p = f"_bq_parse({args[0].text}, {args[1].text})"
        if kind == "DATE":
            return f"CAST({p} AS DATE)"
        if kind == "DATETIME":
            return f"CAST({p} AS TIMESTAMP)"
        if kind == "TIME":
            return f"CAST(CAST({p} AS TIMESTAMP) AS TIME)"
        if len(args) > 2:
            return f"(CAST({p} AS TIMESTAMP) AT TIME ZONE {args[2].text})"
        return f"CAST({p} AS TIMESTAMPTZ)"
    return h


def _h_date(args):
    if len(args) == 3:
        return f"make_date({args[0]}, {args[1]}, {args[2]})"
    if len(args) == 2:
        return f"CAST({_naive(args[0].text, args[1].text)} AS DATE)"
    if len(args) == 1:
        return f"CAST({args[0]} AS DATE)"
    return None


def _h_datetime(args):
    if len(args) == 6:
        return f"make_timestamp(CAST({args[0]} AS BIGINT), CAST({args[1]} AS BIGINT), CAST({args[2]} AS BIGINT), CAST({args[3]} AS BIGINT), CAST({args[4]} AS BIGINT), CAST({args[5]} AS DOUBLE))"
    if len(args) == 2:
        if args[1].literal() is not None:
            return f"CAST({_naive(args[0].text, args[1].text)} AS TIMESTAMP)"
        return f"(CAST({args[0]} AS DATE) + CAST({args[1]} AS TIME))"
    if len(args) == 1:
        return f"CAST({args[0]} AS TIMESTAMP)"
    return None


def _h_time(args):
    if len(args) == 3:
        return f"make_time(CAST({args[0]} AS BIGINT), CAST({args[1]} AS BIGINT), CAST({args[2]} AS DOUBLE))"
    if len(args) == 2:
        return f"CAST({_naive(args[0].text, args[1].text)} AS TIME)"
    if len(args) == 1:
        return f"CAST({args[0]} AS TIME)"
    return None


def _h_timestamp(args):
    if len(args) == 2:
        return f"(CAST({args[0]} AS TIMESTAMP) AT TIME ZONE {args[1]})"
    if len(args) == 1:
        return f"CAST({args[0]} AS TIMESTAMPTZ)"
    return None


def _h_string(args):
    x = args[0].text
    tz = args[1].text if len(args) > 1 else None
    nv = _naive(x, tz)
    off = f"CAST(epoch({nv}) - epoch(CAST({x} AS TIMESTAMPTZ)) AS BIGINT)" if tz else "0"
    return f"_bq_string(CAST({nv} AS VARCHAR), {off})"


def _unix(div: int):
    def h(args):
        us = f"epoch_us(CAST({args[0]} AS TIMESTAMPTZ))"
        return us if div == 1 else f"CAST(floor({us} / {div}.0) AS BIGINT)"
    return h


def _from_unix(mult: int):
    def h(args):
        return f"CAST(make_timestamp(CAST({args[0]} AS BIGINT) * {mult}) AS TIMESTAMPTZ)"
    return h


def _h_gen_date_array(args):
    if len(args) < 2:
        return None
    step = "INTERVAL 1 DAY"
    if len(args) == 3:
        step = _iv(args[2], 1) or "INTERVAL 1 DAY"
    return (f"CAST(generate_series(CAST({args[0]} AS DATE), CAST({args[1]} AS DATE), {step}) AS DATE[])")


HANDLERS: dict = {
    "DATE": _h_date, "DATETIME": _h_datetime, "TIME": _h_time, "TIMESTAMP": _h_timestamp,
    "STRING": _h_string,
    "EXTRACT": _h_extract, "LAST_DAY": _h_last_day,
    "DATE_FROM_UNIX_DATE": lambda a: f"CAST(DATE '1970-01-01' + CAST({a[0]} AS INTEGER) AS DATE)",
    "UNIX_DATE": lambda a: f"date_diff('day', DATE '1970-01-01', CAST({a[0]} AS DATE))",
    "UNIX_SECONDS": _unix(10**6), "UNIX_MILLIS": _unix(1000), "UNIX_MICROS": _unix(1),
    "TIMESTAMP_SECONDS": _from_unix(10**6), "TIMESTAMP_MILLIS": _from_unix(1000),
    "TIMESTAMP_MICROS": _from_unix(1),
    "GENERATE_DATE_ARRAY": _h_gen_date_array,
}
for _k in ("DATE", "DATETIME", "TIMESTAMP", "TIME"):
    HANDLERS[f"{_k}_ADD"] = _h_add(_k, 1)
    HANDLERS[f"{_k}_SUB"] = _h_add(_k, -1)
    HANDLERS[f"{_k}_DIFF"] = _h_diff(_k)
    HANDLERS[f"{_k}_TRUNC"] = _h_trunc(_k)
    HANDLERS[f"FORMAT_{_k}"] = _h_format(_k)
    HANDLERS[f"PARSE_{_k}"] = _h_parse(_k)


# --------------------------------------------------------------------------- #
#  Rule: a CTE / table called `table` (reserved in DuckDB)
# --------------------------------------------------------------------------- #
def _rule_table_name(toks: list[tr.Tok]) -> list[tr.Tok]:
    out = list(toks)
    for i, t in enumerate(toks):
        if t.kind != tr.WORD or t.upper != "TABLE" or t.value is not None:
            continue
        p = tr.prev_sig(toks, i)
        n = tr.next_sig(toks, i)
        prev_ok = p >= 0 and toks[p].kind == tr.WORD and toks[p].upper in ("WITH", "FROM", "JOIN")
        comma_cte = p >= 0 and toks[p].is_punct(",") and n < len(toks) and toks[n].is_word("AS")
        if prev_ok or comma_cte:
            out[i] = tr.Tok(tr.WORD, '"table"', "table")
    return out


PRE_RULES.append(_rule_table_name)


def _rule_timestamp_literal(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`TIMESTAMP '2008-12-25 15:30:00+08'` -> a TIMESTAMPTZ (DuckDB's literal drops the offset)."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.is_word("TIMESTAMP"):
            n = tr.next_sig(toks, i)
            p = tr.prev_sig(toks, i)
            if n < len(toks) and toks[n].kind == tr.STR and not (p >= 0 and toks[p].is_word("AS")):
                out += tr.tokenize_plain("CAST(") + [toks[n]] + tr.tokenize_plain(" AS TIMESTAMPTZ)")
                i = n + 1
                continue
        out.append(t)
        i += 1
    return out


PRE_RULES.append(_rule_timestamp_literal)
