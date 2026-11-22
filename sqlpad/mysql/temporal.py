"""MySQL date and time functions on DuckDB.

Dates are parsed the way MySQL parses them (liberal delimiters, 2-digit years,
compact YYYYMMDD[hhmmss], zero parts such as '2001-11-00') by Python functions
registered as `_my_*` UDFs; public names (DATE_ADD, DAYNAME, WEEK ...) are TEMP
macros that cast their argument to text and call them, so they work for DATE,
TIMESTAMP, TEXT and numeric inputs alike.

Result types follow MySQL where DuckDB can express them (DATE stays DATE,
DATETIME is TIMESTAMP). TIME values can exceed 24 hours in MySQL, so TIME
results are returned as text ('HH:MM:SS') exactly as MySQL prints them.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

from .. import translate as tr
from . import helpers as H

# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #
@dataclass
class DT:
    year: int = 0
    month: int = 0
    day: int = 0
    hour: int = 0
    minute: int = 0
    second: int = 0
    micro: int = 0
    has_date: bool = True
    has_time: bool = False

    def to_date(self) -> dt.date | None:
        if not (1 <= self.year <= 9999 and 1 <= self.month <= 12 and 1 <= self.day <= 31):
            return None
        try:
            return dt.date(self.year, self.month, self.day)
        except ValueError:
            return None

    def to_datetime(self) -> dt.datetime | None:
        d = self.to_date()
        if d is None:
            return None
        return dt.datetime(d.year, d.month, d.day, self.hour, self.minute, self.second, self.micro)


_TZ_SUFFIX = re.compile(r"(?<=\d)(Z|[+-]\d\d(?::?\d\d)?)$")
_TIME_PART = re.compile(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d{1,6})\d*)?)?$")
_COMPACT_TIME = re.compile(r"^(\d{1,2})(\d{2})(\d{2})(?:\.(\d{1,6})\d*)?$")
_DELIM_DATE = re.compile(r"^(\d{1,4})([^\w\s:])(\d{1,2})\2(\d{1,2})$")   # ':' means TIME


def _year(text: str) -> int:
    y = int(text)
    if len(text) <= 2:
        return y + (2000 if y < 70 else 1900)
    return y


def _valid(d: DT) -> bool:
    if not (0 <= d.month <= 12 and 0 <= d.day <= 31):
        return False
    if d.month and d.day and d.day > calendar.monthrange(max(d.year, 1), d.month)[1]:
        return False
    return d.hour <= 23 and d.minute <= 59 and d.second <= 59


def _frac(text: str | None) -> int:
    return int((text or "").ljust(6, "0")[:6] or 0)


def parse_temporal(text) -> DT | None:
    """Parse a MySQL date / datetime literal. None if it is not one."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    d = DT()
    # split date / time on a space or 'T'
    m = re.match(r"^(\d[^\s]*?)(?:[ T]+(\d.*))?$", s)
    if not m:
        return None
    date_s, time_s = m.group(1), m.group(2)
    if time_s is not None:
        time_s = _TZ_SUFFIX.sub("", time_s.strip())
    digits = re.fullmatch(r"(\d+)(?:\.(\d+))?", date_s)
    if digits:                                   # compact YYYYMMDD[hhmmss]
        body, frac = digits.group(1), digits.group(2)
        if len(body) in (6, 8) and time_s is None and frac is None:
            ylen = 2 if len(body) == 6 else 4
            d.year, d.month, d.day = _year(body[:ylen]), int(body[ylen:ylen + 2]), int(body[ylen + 2:])
        elif len(body) in (12, 14) and time_s is None:
            ylen = 2 if len(body) == 12 else 4
            d.year, d.month, d.day = _year(body[:ylen]), int(body[ylen:ylen + 2]), int(body[ylen + 2:ylen + 4])
            r = body[ylen + 4:]
            d.hour, d.minute, d.second = int(r[0:2]), int(r[2:4]), int(r[4:6])
            d.micro = _frac(frac)
            d.has_time = True
        else:
            return None
    else:
        dm = _DELIM_DATE.match(date_s)
        if not dm:
            return None
        d.year, d.month, d.day = _year(dm.group(1)), int(dm.group(3)), int(dm.group(4))
    if time_s is not None:
        tm = _TIME_PART.match(time_s)
        if tm:
            d.hour, d.minute = int(tm.group(1)), int(tm.group(2))
            d.second = int(tm.group(3) or 0)
            d.micro = _frac(tm.group(4))
        else:
            cm = _COMPACT_TIME.match(time_s)
            if not cm:
                return None
            d.hour, d.minute, d.second, d.micro = (
                int(cm.group(1)), int(cm.group(2)), int(cm.group(3)), _frac(cm.group(4)))
        d.has_time = True
    return d if _valid(d) else None


@dataclass
class TV:
    """A MySQL TIME value: sign and microseconds (can exceed 24 hours)."""
    neg: bool
    micros: int

    @property
    def signed(self) -> int:
        return -self.micros if self.neg else self.micros


_TIME_MAX_US = (838 * 3600 + 59 * 60 + 59) * 1_000_000


def parse_time(text) -> TV | None:
    """Parse a MySQL TIME literal: [-][D ]HH:MM:SS[.f], HH:MM, or digits HHMMSS."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    neg = s.startswith("-")
    if neg:
        s = s[1:].strip()
    days = 0
    dm = re.match(r"^(\d+)\s+(.+)$", s)
    if dm:
        days, s = int(dm.group(1)), dm.group(2)
    frac_us = 0
    if ":" in s:
        parts = s.split(":")
        if len(parts) > 3:
            return None
        sec_part = parts[-1] if len(parts) == 3 else None
        try:
            hh = int(parts[0])
            mm = int(parts[1]) if len(parts) > 1 else 0
            if len(parts) == 3:
                sp = parts[2].split(".")
                ss = int(sp[0] or 0)
                frac_us = _frac(sp[1]) if len(sp) > 1 else 0
            else:
                ss = 0
        except ValueError:
            return None
        del sec_part
    else:
        m = re.fullmatch(r"(\d+)(?:\.(\d+))?", s)
        if not m:
            return None
        body = m.group(1)
        frac_us = _frac(m.group(2))
        body = body.rjust(6, "0")
        hh, mm, ss = int(body[:-4]), int(body[-4:-2]), int(body[-2:])
    if mm > 59 or ss > 59:
        return None
    total = ((days * 24 + hh) * 3600 + mm * 60 + ss) * 1_000_000 + frac_us
    return TV(neg, min(total, _TIME_MAX_US))


def parse_any_time(text) -> TV | None:
    """Time of day from either a TIME literal or the time part of a datetime."""
    d = parse_temporal(text)
    if d is not None and d.has_time:
        return TV(False, ((d.hour * 60 + d.minute) * 60 + d.second) * 1_000_000 + d.micro)
    if d is not None and not d.has_time:
        return TV(False, 0)
    return parse_time(text)


def fmt_time(tv: TV, with_frac: bool | None = None) -> str:
    total = tv.micros
    secs, us = divmod(total, 1_000_000)
    h, rem = divmod(secs, 3600)
    mi, se = divmod(rem, 60)
    out = f"{'-' if tv.neg and total else ''}{h:02d}:{mi:02d}:{se:02d}"
    if us if with_frac is None else with_frac:
        out += f".{us:06d}"
    return out


def fmt_datetime(d: dt.datetime) -> str:
    out = d.strftime("%Y-%m-%d %H:%M:%S")
    return out + (f".{d.microsecond:06d}" if d.microsecond else "")


# --------------------------------------------------------------------------- #
#  Day numbers and weeks (ports of MySQL's calc_daynr / calc_week)
# --------------------------------------------------------------------------- #
def _cdiv(a: int, b: int) -> int:
    """C-style integer division (truncates toward zero), as MySQL's code uses."""
    return int(a / b)


def calc_daynr(year: int, month: int, day: int) -> int:
    if year == 0 and month == 0:
        return 0
    y = year
    delsum = 365 * y + 31 * (month - 1) + day
    if month <= 2:
        y -= 1
    else:
        delsum -= (month * 4 + 23) // 10
    temp = _cdiv((_cdiv(y, 100) + 1) * 3, 4)
    return delsum + _cdiv(y, 4) - temp


def calc_weekday(daynr: int, sunday_first: bool) -> int:
    return (daynr + 5 + (1 if sunday_first else 0)) % 7


def _days_in_year(year: int) -> int:
    return 366 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 365


_WEEK_MONDAY_FIRST, _WEEK_YEAR, _WEEK_FIRST_WEEKDAY = 1, 2, 4


def week_behaviour(mode: int) -> int:
    fmt = mode & 7
    if not fmt & _WEEK_MONDAY_FIRST:
        fmt ^= _WEEK_FIRST_WEEKDAY
    return fmt


def calc_week(year: int, month: int, day: int, behaviour: int) -> tuple[int, int]:
    """(week, year) exactly as MySQL's calc_week."""
    daynr = calc_daynr(year, month, day)
    first_daynr = calc_daynr(year, 1, 1)
    monday_first = bool(behaviour & _WEEK_MONDAY_FIRST)
    week_year = bool(behaviour & _WEEK_YEAR)
    first_weekday = bool(behaviour & _WEEK_FIRST_WEEKDAY)
    weekday = calc_weekday(first_daynr, not monday_first)
    out_year = year
    if month == 1 and day <= 7 - weekday:
        if not week_year and ((first_weekday and weekday != 0)
                              or (not first_weekday and weekday >= 4)):
            return 0, out_year
        week_year = True
        out_year -= 1
        days = _days_in_year(out_year)
        first_daynr -= days
        weekday = (weekday + 53 * 7 - days) % 7
    if (first_weekday and weekday != 0) or (not first_weekday and weekday >= 4):
        days = daynr - (first_daynr + (7 - weekday))
    else:
        days = daynr - (first_daynr - weekday)
    if week_year and days >= 52 * 7:
        weekday = (weekday + _days_in_year(out_year)) % 7
        if (not first_weekday and weekday < 4) or (first_weekday and weekday == 0):
            return 1, out_year + 1
    return days // 7 + 1, out_year


# --------------------------------------------------------------------------- #
#  DATE_FORMAT / STR_TO_DATE
# --------------------------------------------------------------------------- #
_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _suffix(day: int) -> str:
    if 11 <= day <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _weekday_index(d: DT) -> int | None:
    """Monday=0 .. Sunday=6; None for incomplete dates."""
    if d.month == 0 or d.day == 0:
        return None
    return calc_weekday(calc_daynr(d.year, d.month, d.day), False)


def format_datetime(d: DT, fmt: str, *, time_only: bool = False) -> str | None:
    out: list[str] = []
    i = 0
    wd = _weekday_index(d)
    h12 = d.hour % 12 or 12
    while i < len(fmt):
        ch = fmt[i]
        if ch != "%" or i + 1 >= len(fmt):
            out.append(ch)
            i += 1
            continue
        c = fmt[i + 1]
        i += 2
        if time_only and c in "aDbcdejMmUuVvWwXxYy":
            if c in "DdejmUuVvXxYycMbWwa":
                return None
        if c == "a":
            out.append(_DAYS[wd][:3] if wd is not None else "")
        elif c == "b":
            out.append(_MONTHS[d.month - 1][:3] if d.month else "")
        elif c == "c":
            out.append(str(d.month))
        elif c == "D":
            out.append(f"{d.day}{_suffix(d.day)}")
        elif c == "d":
            out.append(f"{d.day:02d}")
        elif c == "e":
            out.append(str(d.day))
        elif c == "f":
            out.append(f"{d.micro:06d}")
        elif c == "H":
            out.append(f"{d.hour:02d}")
        elif c in "hI":
            out.append(f"{h12:02d}")
        elif c == "i":
            out.append(f"{d.minute:02d}")
        elif c == "j":
            out.append(f"{calc_daynr(d.year, d.month, d.day) - calc_daynr(d.year, 1, 1) + 1:03d}"
                       if d.month and d.day else "000")
        elif c == "k":
            out.append(str(d.hour))
        elif c == "l":
            out.append(str(h12))
        elif c == "M":
            out.append(_MONTHS[d.month - 1] if d.month else "")
        elif c == "m":
            out.append(f"{d.month:02d}")
        elif c == "p":
            out.append("AM" if d.hour < 12 else "PM")
        elif c == "r":
            out.append(f"{h12:02d}:{d.minute:02d}:{d.second:02d} {'AM' if d.hour < 12 else 'PM'}")
        elif c in "Ss":
            out.append(f"{d.second:02d}")
        elif c == "T":
            out.append(f"{d.hour:02d}:{d.minute:02d}:{d.second:02d}")
        elif c in "UuVvXx":
            if d.month == 0 or d.day == 0:
                out.append("" if c in "UuVv" else "")
                continue
            mode = {"U": 0, "u": 1, "V": 2, "v": 3, "X": 2, "x": 3}[c]
            week, year = calc_week(d.year, d.month, d.day, week_behaviour(mode) | (
                _WEEK_YEAR if c in "VvXx" else 0))
            out.append(f"{year:04d}" if c in "Xx" else f"{week:02d}")
        elif c == "W":
            out.append(_DAYS[wd] if wd is not None else "")
        elif c == "w":
            out.append(str((wd + 1) % 7) if wd is not None else "")
        elif c == "Y":
            out.append(f"{d.year:04d}")
        elif c == "y":
            out.append(f"{d.year % 100:02d}")
        elif c == "%":
            out.append("%")
        else:
            out.append(c)
    return "".join(out)


def _as_dt(text) -> DT | None:
    """A DT from a datetime literal, or from a bare TIME literal (date 0000-00-00)."""
    d = parse_temporal(text)
    if d is not None:
        return d
    tv = parse_time(text)
    if tv is None:
        return None
    secs, us = divmod(tv.micros, 1_000_000)
    return DT(0, 0, 0, secs // 3600 % 24, secs % 3600 // 60, secs % 60, us, has_date=False, has_time=True)


def date_format(value, fmt) -> str | None:
    d = _as_dt(value)
    return None if d is None else format_datetime(d, str(fmt))


def time_format(value, fmt) -> str | None:
    tv = parse_any_time(value)
    if tv is None:
        return None
    secs, us = divmod(tv.micros, 1_000_000)
    d = DT(0, 0, 0, secs // 3600, secs % 3600 // 60, secs % 60, us)
    fs = str(fmt)
    # hours in TIME_FORMAT may exceed 23
    fs_out = []
    i = 0
    while i < len(fs):
        if fs[i] == "%" and i + 1 < len(fs):
            c = fs[i + 1]
            if c == "H":
                fs_out.append(f"{d.hour:02d}")
            elif c == "k":
                fs_out.append(str(d.hour))
            elif c in "hI":
                fs_out.append(f"{(d.hour % 12) or 12:02d}")
            elif c == "l":
                fs_out.append(str((d.hour % 12) or 12))
            elif c == "i":
                fs_out.append(f"{d.minute:02d}")
            elif c in "Ss":
                fs_out.append(f"{d.second:02d}")
            elif c == "f":
                fs_out.append(f"{d.micro:06d}")
            elif c == "p":
                fs_out.append("AM" if d.hour % 24 < 12 else "PM")
            elif c == "r":
                fs_out.append(f"{(d.hour % 12) or 12:02d}:{d.minute:02d}:{d.second:02d} "
                              f"{'AM' if d.hour % 24 < 12 else 'PM'}")
            elif c == "T":
                fs_out.append(f"{d.hour:02d}:{d.minute:02d}:{d.second:02d}")
            elif c == "%":
                fs_out.append("%")
            else:
                fs_out.append("")        # date specifiers give NULL/empty in TIME_FORMAT
            i += 2
        else:
            fs_out.append(fs[i])
            i += 1
    return "".join(fs_out)


_GET_FORMAT = {
    ("DATE", "USA"): "%m.%d.%Y", ("DATE", "JIS"): "%Y-%m-%d", ("DATE", "ISO"): "%Y-%m-%d",
    ("DATE", "EUR"): "%d.%m.%Y", ("DATE", "INTERNAL"): "%Y%m%d",
    ("DATETIME", "USA"): "%Y-%m-%d %H.%i.%s", ("DATETIME", "JIS"): "%Y-%m-%d %H:%i:%s",
    ("DATETIME", "ISO"): "%Y-%m-%d %H:%i:%s", ("DATETIME", "EUR"): "%Y-%m-%d %H.%i.%s",
    ("DATETIME", "INTERNAL"): "%Y%m%d%H%i%s",
    ("TIME", "USA"): "%h:%i:%s %p", ("TIME", "JIS"): "%H:%i:%s", ("TIME", "ISO"): "%H:%i:%s",
    ("TIME", "EUR"): "%H.%i.%s", ("TIME", "INTERNAL"): "%H%i%s",
}


def get_format(kind, locale) -> str | None:
    k = str(kind).upper()
    k = "DATETIME" if k == "TIMESTAMP" else k
    return _GET_FORMAT.get((k, str(locale).upper()))


def _read_int(s: str, pos: int, max_len: int) -> tuple[int, int] | None:
    j = pos
    while j < len(s) and j - pos < max_len and s[j].isdigit():
        j += 1
    if j == pos:
        return None
    return int(s[pos:j]), j


def _read_name(s: str, pos: int, names: list[str]) -> tuple[int, int] | None:
    rest = s[pos:].lower()
    for idx, name in enumerate(names):
        if rest.startswith(name.lower()):
            return idx, pos + len(name)
    return None


def parse_with_format(text, fmt):
    """MySQL STR_TO_DATE. Returns (DT, has_date_spec, has_time_spec) or None."""
    s, f = str(text), str(fmt)
    d = DT(0, 0, 0, 0, 0, 0, 0, has_date=False, has_time=False)
    has_date = has_time = False
    pos, i = 0, 0
    pm: bool | None = None
    hour12 = False
    yday: int | None = None
    week_no: tuple[str, int] | None = None
    week_year: int | None = None
    weekday: int | None = None

    def spec_expand(c: str) -> str | None:
        return {"r": "%I:%i:%S %p", "T": "%H:%i:%S"}.get(c)

    # expand %r / %T
    f = re.sub(r"%([rT])", lambda m: spec_expand(m.group(1)), f)
    while i < len(f):
        ch = f[i]
        if ch == "%" and i + 1 < len(f):
            c = f[i + 1]
            i += 2
            while pos < len(s) and s[pos].isspace():
                pos += 1
            if c == "%":
                if pos < len(s) and s[pos] == "%":
                    pos += 1
                    continue
                return None
            if c in "dem" or c == "c":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                val, pos = r
                if c in "de":
                    d.day = val
                    has_date = True
                else:
                    d.month = val
                    has_date = True
            elif c == "Y":
                r = _read_int(s, pos, 4)
                if not r:
                    return None
                digits = pos
                val, pos = r
                d.year = val if pos - digits >= 4 or val >= 100 else _year(str(val).zfill(2))
                has_date = True
            elif c == "y":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                val, pos = r
                d.year = val + (2000 if val < 70 else 1900)
                has_date = True
            elif c in "HkhIl":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                val, pos = r
                d.hour = val
                hour12 = c in "hIl"
                has_time = True
            elif c == "i":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                d.minute, pos = r
                has_time = True
            elif c in "Ss":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                d.second, pos = r
                has_time = True
            elif c == "f":
                j = pos
                while j < len(s) and s[j].isdigit() and j - pos < 6:
                    j += 1
                if j == pos:
                    return None
                d.micro = int(s[pos:j].ljust(6, "0"))
                pos = j
                has_time = True
            elif c == "p":
                chunk = s[pos:pos + 2].upper()
                if chunk not in ("AM", "PM"):
                    return None
                pm = chunk == "PM"
                pos += 2
                has_time = True
            elif c == "M":
                r = _read_name(s, pos, _MONTHS)
                if not r:
                    return None
                d.month, pos = r[0] + 1, r[1]
                has_date = True
            elif c == "b":
                r = _read_name(s, pos, [m[:3] for m in _MONTHS])
                if not r:
                    return None
                d.month, pos = r[0] + 1, r[1]
                has_date = True
            elif c == "W":
                r = _read_name(s, pos, _DAYS)
                if not r:
                    return None
                weekday, pos = r
            elif c == "a":
                r = _read_name(s, pos, [x[:3] for x in _DAYS])
                if not r:
                    return None
                weekday, pos = r
            elif c == "w":
                r = _read_int(s, pos, 1)
                if not r:
                    return None
                weekday = (r[0] - 1) % 7
                pos = r[1]
            elif c == "j":
                r = _read_int(s, pos, 3)
                if not r:
                    return None
                yday, pos = r
                has_date = True
            elif c == "D":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                d.day, pos = r
                pos = min(len(s), pos + 2)
                has_date = True
            elif c in "UuVv":
                r = _read_int(s, pos, 2)
                if not r:
                    return None
                week_no, pos = (c, r[0]), r[1]
                has_date = True
            elif c in "Xx":
                r = _read_int(s, pos, 4)
                if not r:
                    return None
                week_year, pos = r
                has_date = True
            else:
                return None
        else:
            i += 1
            if ch.isspace():
                continue
            if pos < len(s) and s[pos] == ch:
                pos += 1
            else:
                return None
    # extra trailing characters are tolerated (MySQL only warns)
    if pm is not None:
        if d.hour > 12 or d.hour < 1 and hour12:
            return None
        d.hour = (d.hour % 12) + (12 if pm else 0)
    elif hour12 and d.hour == 12:
        d.hour = 0
    if yday is not None and d.year:
        base = dt.date(d.year, 1, 1) + dt.timedelta(days=yday - 1)
        if base.year != d.year:
            return None
        d.month, d.day = base.month, base.day
    if week_no is not None and week_year is not None and weekday is not None:
        kind, wk = week_no
        mode = {"U": 0, "u": 1, "V": 2, "v": 3}[kind]
        first = dt.date(week_year, 1, 1)
        found = None
        for off in range(-7, 380):
            cand = first + dt.timedelta(days=off)
            w, y = calc_week(cand.year, cand.month, cand.day,
                             week_behaviour(mode) | (_WEEK_YEAR if kind in "Vv" else 0))
            if w == wk and y == week_year and calc_weekday(
                    calc_daynr(cand.year, cand.month, cand.day), False) == weekday:
                found = cand
                break
        if found is None:
            return None
        d.year, d.month, d.day = found.year, found.month, found.day
    if d.hour > 23 or d.minute > 59 or d.second > 59 or d.month > 12 or d.day > 31:
        return None
    if d.month and d.day and d.year and d.day > calendar.monthrange(d.year, d.month)[1]:
        return None
    d.has_date, d.has_time = has_date, has_time
    return d, has_date, has_time


def spec_kind(fmt: str) -> str:
    """'date' | 'time' | 'datetime' from the specifiers a format string uses."""
    f = re.sub(r"%([rT])", "%H", fmt)
    specs = set(re.findall(r"%(.)", f))
    date_specs = set("aMbcDdeJjmUuVvWwXxYy") & specs
    time_specs = set("fHhIiklpSs") & specs
    if date_specs and time_specs:
        return "datetime"
    if time_specs:
        return "time"
    return "date"


def str_to_date_d(text, fmt) -> dt.date | None:
    r = parse_with_format(text, fmt)
    return None if r is None else r[0].to_date()


def str_to_date_dt(text, fmt) -> dt.datetime | None:
    r = parse_with_format(text, fmt)
    return None if r is None else r[0].to_datetime()


def str_to_date_t(text, fmt) -> str | None:
    r = parse_with_format(text, fmt)
    if r is None:
        return None
    d = r[0]
    return fmt_time(TV(False, ((d.hour * 60 + d.minute) * 60 + d.second) * 1_000_000 + d.micro))


# --------------------------------------------------------------------------- #
#  Simple extractors
# --------------------------------------------------------------------------- #
def _dtp(text):
    return parse_temporal(text)


def f_year(x):
    d = _dtp(x)
    return None if d is None else d.year


def f_month(x):
    d = _dtp(x)
    return None if d is None else d.month


def f_day(x):
    d = _dtp(x)
    return None if d is None else d.day


def f_quarter(x):
    d = _dtp(x)
    return None if d is None else (d.month + 2) // 3 if d.month else 0


def _tv(x):
    return parse_any_time(x)


def f_hour(x):
    t = _tv(x)
    return None if t is None else t.micros // 3_600_000_000


def f_minute(x):
    t = _tv(x)
    return None if t is None else t.micros // 60_000_000 % 60


def f_second(x):
    t = _tv(x)
    return None if t is None else t.micros // 1_000_000 % 60


def f_micro(x):
    t = _tv(x)
    return None if t is None else t.micros % 1_000_000


def f_dayname(x):
    d = _dtp(x)
    wd = None if d is None else _weekday_index(d)
    return None if wd is None else _DAYS[wd]


def f_monthname(x):
    d = _dtp(x)
    return None if d is None or d.month == 0 else _MONTHS[d.month - 1]


def f_dayofweek(x):
    d = _dtp(x)
    wd = None if d is None else _weekday_index(d)
    return None if wd is None else (wd + 1) % 7 + 1


def f_weekday(x):
    d = _dtp(x)
    return None if d is None else _weekday_index(d)


def f_dayofyear(x):
    d = _dtp(x)
    if d is None or d.month == 0 or d.day == 0:
        return None
    return calc_daynr(d.year, d.month, d.day) - calc_daynr(d.year, 1, 1) + 1


def f_week(x, mode=0):
    d = _dtp(x)
    if d is None or d.month == 0 or d.day == 0:
        return None
    return calc_week(d.year, d.month, d.day, week_behaviour(int(mode)))[0]


def f_yearweek(x, mode=0):
    d = _dtp(x)
    if d is None or d.month == 0 or d.day == 0:
        return None
    week, year = calc_week(d.year, d.month, d.day, week_behaviour(int(mode)) | _WEEK_YEAR)
    return year * 100 + week


def f_to_days(x):
    d = _dtp(x)
    if d is None or (d.month == 0 and d.day == 0 and d.year == 0):
        return None
    return calc_daynr(d.year, d.month, d.day)


def f_to_seconds(x):
    d = _dtp(x)
    if d is None or (d.month == 0 and d.day == 0 and d.year == 0):
        return None
    return calc_daynr(d.year, d.month, d.day) * 86400 + d.hour * 3600 + d.minute * 60 + d.second


def f_time_to_sec(x):
    t = _tv(x)
    return None if t is None else int(t.signed // 1_000_000)


def f_last_day(x):
    d = _dtp(x)
    if d is None or d.month == 0:
        return None
    try:
        return dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    except ValueError:
        return None


def f_date(x):
    d = _dtp(x)
    return None if d is None else d.to_date()


def f_from_days(n):
    n = int(n)
    if n < 366:
        return None
    try:
        return dt.date.fromordinal(n - 365)
    except (ValueError, OverflowError):
        return None


def f_makedate(year, doy):
    if doy <= 0 or year < 0:
        return None
    y = _year(str(int(year))) if year < 100 else int(year)
    try:
        return dt.date(y, 1, 1) + dt.timedelta(days=int(doy) - 1)
    except (ValueError, OverflowError):
        return None


def f_datediff(a, b):
    da, db = _dtp(a), _dtp(b)
    if da is None or db is None:
        return None
    if da.to_date() is None or db.to_date() is None:
        return None
    return calc_daynr(da.year, da.month, da.day) - calc_daynr(db.year, db.month, db.day)


def f_sec_to_time(sec):
    micros = int(round(float(sec) * 1_000_000))
    neg = micros < 0
    tv = TV(neg, min(abs(micros), _TIME_MAX_US))
    return fmt_time(tv)


def f_maketime(h, m, s):
    if m < 0 or m > 59 or s < 0 or s >= 60:
        return None
    neg = h < 0
    micros = ((abs(int(h)) * 60 + int(m)) * 60) * 1_000_000 + int(round(float(s) * 1_000_000))
    return fmt_time(TV(neg, min(micros, _TIME_MAX_US)))


def f_time(x):
    t = _tv(x)
    return None if t is None else fmt_time(t)


def f_period_add(p, n):
    p = int(p)
    if p == 0:
        return 0
    y, m = divmod(p, 100)
    if y < 100:
        y += 2000 if y < 70 else 1900
    total = y * 12 + (m - 1) + int(n)
    return (total // 12) * 100 + total % 12 + 1


def f_period_diff(p1, p2):
    def months(p):
        y, m = divmod(int(p), 100)
        if y < 100:
            y += 2000 if y < 70 else 1900
        return y * 12 + m
    return months(p1) - months(p2)


def _ts(x) -> dt.datetime | None:
    d = _dtp(x)
    return None if d is None else d.to_datetime()


def f_timestamp(x):
    return _ts(x)


def f_timestamp2(x, t):
    base = _ts(x)
    tv = parse_time(t)
    if base is None or tv is None:
        return None
    return base + dt.timedelta(microseconds=tv.signed)


def _is_datetime_str(x) -> bool:
    d = _dtp(x)
    return d is not None


def _add_time(x, t, sign: int):
    tv = parse_time(t)
    if tv is None:
        return None
    d = _dtp(x)
    if d is not None and d.has_date and (d.has_time or True):
        base = d.to_datetime()
        if base is None:
            return None
        res = base + dt.timedelta(microseconds=sign * tv.signed)
        return fmt_datetime(res)
    t0 = parse_time(x)
    if t0 is None:
        return None
    total = t0.signed + sign * tv.signed
    return fmt_time(TV(total < 0, min(abs(total), _TIME_MAX_US)))


def f_addtime(x, t):
    return _add_time(x, t, 1)


def f_subtime(x, t):
    return _add_time(x, t, -1)


def f_timediff(a, b):
    da, db = parse_temporal(a), parse_temporal(b)
    if da is not None and db is not None and da.has_time and db.has_time:
        ta, tb = da.to_datetime(), db.to_datetime()
        if ta is None or tb is None:
            return None
        delta = ta - tb
        micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
        return fmt_time(TV(micros < 0, min(abs(micros), _TIME_MAX_US)))
    if (da is None) != (db is None) and not (da and not da.has_time) and not (db and not db.has_time):
        return None
    ta, tb = parse_any_time(a), parse_any_time(b)
    if ta is None or tb is None:
        return None
    micros = ta.signed - tb.signed
    return fmt_time(TV(micros < 0, min(abs(micros), _TIME_MAX_US)))


def f_from_unixtime(n):
    try:
        return dt.datetime.fromtimestamp(float(n)).replace(microsecond=0) if float(n) == int(float(n)) \
            else dt.datetime.fromtimestamp(float(n))
    except (OverflowError, OSError, ValueError):
        return None


def f_from_unixtime_fmt(n, fmt):
    d = f_from_unixtime(n)
    if d is None:
        return None
    return date_format(d.isoformat(sep=" "), fmt)


def f_unix_timestamp(x):
    d = _dtp(x)
    if d is None:
        return None
    base = d.to_datetime()
    if base is None:
        return None
    return int(base.timestamp())


# ---- units / interval arithmetic ------------------------------------------ #
_SINGLE_UNITS = {"MICROSECOND", "SECOND", "MINUTE", "HOUR", "DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
_COMPOUND = {
    "SECOND_MICROSECOND": ("SECOND", "MICROSECOND"),
    "MINUTE_MICROSECOND": ("MINUTE", "SECOND", "MICROSECOND"),
    "MINUTE_SECOND": ("MINUTE", "SECOND"),
    "HOUR_MICROSECOND": ("HOUR", "MINUTE", "SECOND", "MICROSECOND"),
    "HOUR_SECOND": ("HOUR", "MINUTE", "SECOND"),
    "HOUR_MINUTE": ("HOUR", "MINUTE"),
    "DAY_MICROSECOND": ("DAY", "HOUR", "MINUTE", "SECOND", "MICROSECOND"),
    "DAY_SECOND": ("DAY", "HOUR", "MINUTE", "SECOND"),
    "DAY_MINUTE": ("DAY", "HOUR", "MINUTE"),
    "DAY_HOUR": ("DAY", "HOUR"),
    "YEAR_MONTH": ("YEAR", "MONTH"),
}
_TIME_UNITS = {"MICROSECOND", "SECOND", "MINUTE", "HOUR"}


def unit_has_time(unit: str) -> bool:
    """True if adding this unit can produce a time part (result is a DATETIME)."""
    unit = unit.upper()
    if unit in _COMPOUND:
        return any(u in _TIME_UNITS for u in _COMPOUND[unit])
    return unit in _TIME_UNITS


def iv_part(expr, unit, k) -> int | None:
    """k-th component (0-based) of a compound-unit interval string."""
    units = _COMPOUND.get(str(unit).upper())
    if units is None:
        return None
    s = str(expr).strip()
    neg = s.startswith("-")
    nums = re.findall(r"\d+", s)
    if not nums:
        return None
    # fewer values than units: they fill from the right (MySQL rule)
    need = len(units)
    if len(nums) < need:
        nums = ["0"] * (need - len(nums)) + nums
    if len(nums) > need:
        nums = nums[:need - 1] + [".".join(nums[need - 1:])]
    val = nums[k]
    if units[k] == "MICROSECOND":
        val = val.ljust(6, "0")[:6] if len(val) < 6 or True else val
        # MICROSECOND as the last compound part is a decimal fraction of a second
        return (-1 if neg else 1) * int(val)
    return (-1 if neg else 1) * int(val)


def to_number(x) -> float | None:
    """MySQL string -> number: leading numeric prefix, 0 if none."""
    if x is None:
        return None
    m = re.match(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)", str(x))
    return float(m.group(1)) if m else 0.0


def _dt_from(x) -> dt.datetime | None:
    d = _dtp(x)
    return None if d is None else d.to_datetime()


def f_timestampdiff(unit, a, b):
    da, db = _dtp(a), _dtp(b)
    if da is None or db is None:
        return None
    ta, tb = da.to_datetime(), db.to_datetime()
    if ta is None or tb is None:
        return None
    unit = str(unit).upper()
    if unit in ("YEAR", "QUARTER", "MONTH"):
        months = (tb.year - ta.year) * 12 + (tb.month - ta.month)
        # not a full month yet when the day/time of b is before that of a
        key_a = (ta.day, ta.hour, ta.minute, ta.second, ta.microsecond)
        key_b = (tb.day, tb.hour, tb.minute, tb.second, tb.microsecond)
        if months > 0 and key_b < key_a:
            months -= 1
        elif months < 0 and key_b > key_a:
            months += 1
        if unit == "MONTH":
            return months
        return int(months / (12 if unit == "YEAR" else 3))
    delta = tb - ta
    micros = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    div = {"MICROSECOND": 1, "SECOND": 1_000_000, "MINUTE": 60_000_000,
           "HOUR": 3_600_000_000, "DAY": 86_400_000_000, "WEEK": 7 * 86_400_000_000}.get(unit)
    if div is None:
        return None
    return int(micros / div) if micros >= 0 else -int(-micros / div)


def f_convert_tz(value, src, dst):
    base = _dt_from(value)
    if base is None:
        return None

    def zone(name):
        name = str(name).strip()
        m = re.fullmatch(r"([+-])(\d{1,2}):(\d{2})", name)
        if m:
            off = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            return dt.timezone(-off if m.group(1) == "-" else off)
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(name)
        except Exception:  # noqa: BLE001
            return None
    zs, zd = zone(src), zone(dst)
    if zs is None or zd is None:
        return None
    return base.replace(tzinfo=zs).astimezone(zd).replace(tzinfo=None)


def f_extract(x, unit):
    unit = str(unit).upper()
    d = _dtp(x)
    t = _tv(x)
    if unit in ("YEAR", "MONTH", "DAY", "QUARTER", "WEEK"):
        if d is None:
            return None
        if unit == "WEEK":
            return f_week(x, 0)
        return {"YEAR": d.year, "MONTH": d.month, "DAY": d.day, "QUARTER": (d.month + 2) // 3}[unit]
    if unit in ("HOUR", "MINUTE", "SECOND", "MICROSECOND"):
        if t is None:
            return None
        return {"HOUR": t.micros // 3_600_000_000, "MINUTE": t.micros // 60_000_000 % 60,
                "SECOND": t.micros // 1_000_000 % 60, "MICROSECOND": t.micros % 1_000_000}[unit]
    if d is None:
        return None
    parts = {"YEAR_MONTH": d.year * 100 + d.month,
             "DAY_HOUR": d.day * 100 + d.hour,
             "DAY_MINUTE": (d.day * 100 + d.hour) * 100 + d.minute,
             "DAY_SECOND": ((d.day * 100 + d.hour) * 100 + d.minute) * 100 + d.second,
             "DAY_MICROSECOND": (((d.day * 100 + d.hour) * 100 + d.minute) * 100 + d.second) * 1_000_000 + d.micro,
             "HOUR_MINUTE": d.hour * 100 + d.minute,
             "HOUR_SECOND": (d.hour * 100 + d.minute) * 100 + d.second,
             "HOUR_MICROSECOND": ((d.hour * 100 + d.minute) * 100 + d.second) * 1_000_000 + d.micro,
             "MINUTE_SECOND": d.minute * 100 + d.second,
             "MINUTE_MICROSECOND": (d.minute * 100 + d.second) * 1_000_000 + d.micro,
             "SECOND_MICROSECOND": d.second * 1_000_000 + d.micro}
    return parts.get(unit)


# --------------------------------------------------------------------------- #
#  Registration (UDFs + macros)
# --------------------------------------------------------------------------- #
def register(con) -> None:
    V, B, I, D, DATE, TS = H.VARCHAR, H.BIGINT, H.INTEGER, H.DOUBLE, H.DATE, H.TIMESTAMP
    u = lambda name, fn, params, ret: H.udf(con, name, fn, params, ret)  # noqa: E731
    u("_my_year", f_year, [V], B)
    u("_my_month", f_month, [V], B)
    u("_my_day", f_day, [V], B)
    u("_my_quarter", f_quarter, [V], B)
    u("_my_hour", f_hour, [V], B)
    u("_my_minute", f_minute, [V], B)
    u("_my_second", f_second, [V], B)
    u("_my_micro", f_micro, [V], B)
    u("_my_dayname", f_dayname, [V], V)
    u("_my_monthname", f_monthname, [V], V)
    u("_my_dayofweek", f_dayofweek, [V], B)
    u("_my_weekday", f_weekday, [V], B)
    u("_my_dayofyear", f_dayofyear, [V], B)
    u("_my_week", f_week, [V, B], B)
    u("_my_yearweek", f_yearweek, [V, B], B)
    u("_my_to_days", f_to_days, [V], B)
    u("_my_to_seconds", f_to_seconds, [V], B)
    u("_my_time_to_sec", f_time_to_sec, [V], B)
    u("_my_last_day", f_last_day, [V], DATE)
    u("_my_date", f_date, [V], DATE)
    u("_my_from_days", f_from_days, [B], DATE)
    u("_my_makedate", f_makedate, [B, B], DATE)
    u("_my_datediff", f_datediff, [V, V], B)
    u("_my_sec_to_time", f_sec_to_time, [D], V)
    u("_my_maketime", f_maketime, [B, B, D], V)
    u("_my_time", f_time, [V], V)
    u("_my_period_add", f_period_add, [B, B], B)
    u("_my_period_diff", f_period_diff, [B, B], B)
    u("_my_timestamp", f_timestamp, [V], TS)
    u("_my_timestamp2", f_timestamp2, [V, V], TS)
    u("_my_addtime", f_addtime, [V, V], V)
    u("_my_subtime", f_subtime, [V, V], V)
    u("_my_timediff", f_timediff, [V, V], V)
    u("_my_from_unixtime", f_from_unixtime, [D], TS)
    u("_my_from_unixtime_fmt", f_from_unixtime_fmt, [D, V], V)
    u("_my_unix_timestamp", f_unix_timestamp, [V], B)
    u("_my_date_format", date_format, [V, V], V)
    u("_my_time_format", time_format, [V, V], V)
    u("_my_get_format", get_format, [V, V], V)
    u("_my_str_to_date_d", str_to_date_d, [V, V], DATE)
    u("_my_str_to_date_dt", str_to_date_dt, [V, V], TS)
    u("_my_str_to_date_t", str_to_date_t, [V, V], V)
    u("_my_timestampdiff", f_timestampdiff, [V, V, V], B)
    u("_my_convert_tz", f_convert_tz, [V, V, V], TS)
    u("_my_extract", f_extract, [V, V], B)
    u("_my_iv_part", iv_part, [V, V, B], B)
    u("_my_tonum", to_number, [V], D)


def _vc(arg: str) -> str:
    return f"CAST({arg} AS VARCHAR)"


_ONE_ARG = {
    "YEAR": "_my_year", "MONTH": "_my_month", "DAY": "_my_day", "DAYOFMONTH": "_my_day",
    "QUARTER": "_my_quarter", "HOUR": "_my_hour", "MINUTE": "_my_minute",
    "SECOND": "_my_second", "MICROSECOND": "_my_micro", "DAYNAME": "_my_dayname",
    "MONTHNAME": "_my_monthname", "DAYOFWEEK": "_my_dayofweek", "WEEKDAY": "_my_weekday",
    "DAYOFYEAR": "_my_dayofyear", "TO_DAYS": "_my_to_days", "TO_SECONDS": "_my_to_seconds",
    "TIME_TO_SEC": "_my_time_to_sec", "LAST_DAY": "_my_last_day", "DATE": "_my_date",
}

PRELUDE: list[str] = []
for _name, _impl in _ONE_ARG.items():
    PRELUDE.append(f"CREATE OR REPLACE TEMP MACRO {_name}(x) AS {_impl}(CAST(x AS VARCHAR))")
PRELUDE += [
    "CREATE OR REPLACE TEMP MACRO week(x) AS _my_week(CAST(x AS VARCHAR), 0), "
    "(x, m) AS _my_week(CAST(x AS VARCHAR), m)",
    "CREATE OR REPLACE TEMP MACRO weekofyear(x) AS _my_week(CAST(x AS VARCHAR), 3)",
    "CREATE OR REPLACE TEMP MACRO yearweek(x) AS _my_yearweek(CAST(x AS VARCHAR), 0), "
    "(x, m) AS _my_yearweek(CAST(x AS VARCHAR), m)",
    "CREATE OR REPLACE TEMP MACRO from_days(n) AS _my_from_days(CAST(n AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO makedate(y, d) AS _my_makedate(CAST(y AS BIGINT), CAST(d AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO datediff(a, b) AS _my_datediff(CAST(a AS VARCHAR), CAST(b AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO sec_to_time(s) AS _my_sec_to_time(CAST(s AS DOUBLE))",
    "CREATE OR REPLACE TEMP MACRO maketime(h, m, s) AS "
    "_my_maketime(CAST(h AS BIGINT), CAST(m AS BIGINT), CAST(s AS DOUBLE))",
    "CREATE OR REPLACE TEMP MACRO period_add(p, n) AS _my_period_add(CAST(p AS BIGINT), CAST(n AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO period_diff(a, b) AS _my_period_diff(CAST(a AS BIGINT), CAST(b AS BIGINT))",
    "CREATE OR REPLACE TEMP MACRO addtime(x, t) AS _my_addtime(CAST(x AS VARCHAR), CAST(t AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO subtime(x, t) AS _my_subtime(CAST(x AS VARCHAR), CAST(t AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO timediff(a, b) AS _my_timediff(CAST(a AS VARCHAR), CAST(b AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO date_format(x, f) AS _my_date_format(CAST(x AS VARCHAR), CAST(f AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO time_format(x, f) AS _my_time_format(CAST(x AS VARCHAR), CAST(f AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO from_unixtime(n) AS _my_from_unixtime(CAST(n AS DOUBLE)), "
    "(n, f) AS _my_from_unixtime_fmt(CAST(n AS DOUBLE), CAST(f AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO unix_timestamp() AS CAST(epoch(current_localtimestamp()) AS BIGINT), "
    "(x) AS _my_unix_timestamp(CAST(x AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO convert_tz(x, a, b) AS "
    "_my_convert_tz(CAST(x AS VARCHAR), CAST(a AS VARCHAR), CAST(b AS VARCHAR))",
    "CREATE OR REPLACE TEMP MACRO now() AS date_trunc('second', current_localtimestamp()), "
    "(f) AS current_localtimestamp()",
    "CREATE OR REPLACE TEMP MACRO sysdate() AS date_trunc('second', current_localtimestamp())",
    "CREATE OR REPLACE TEMP MACRO localtime() AS date_trunc('second', current_localtimestamp())",
    "CREATE OR REPLACE TEMP MACRO localtimestamp() AS date_trunc('second', current_localtimestamp())",
    "CREATE OR REPLACE TEMP MACRO curdate() AS current_date",
    "CREATE OR REPLACE TEMP MACRO curtime() AS strftime(current_localtimestamp(), '%H:%M:%S')",
    "CREATE OR REPLACE TEMP MACRO utc_date() AS CAST(timezone('UTC', system.main.now()) AS DATE)",
    "CREATE OR REPLACE TEMP MACRO utc_timestamp() AS "
    "date_trunc('second', timezone('UTC', system.main.now()))",
    "CREATE OR REPLACE TEMP MACRO utc_time() AS "
    "strftime(timezone('UTC', system.main.now()), '%H:%M:%S')",
]


# --------------------------------------------------------------------------- #
#  Call handlers (syntax a macro cannot express)
# --------------------------------------------------------------------------- #
_ALL_UNITS = _SINGLE_UNITS | set(_COMPOUND)


def find_unit(toks: list[tr.Tok], start: int) -> int | None:
    """Index of the first top-level interval unit keyword at or after `start`.

    A unit word followed by '(' is a function call (HOUR(t)), not a unit.
    """
    depth = 0
    for j in range(start, len(toks)):
        t = toks[j]
        if t.kind == tr.PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == tr.PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.kind == tr.WORD and t.upper in _ALL_UNITS:
            k = tr.next_sig(toks, j)
            if not (k < len(toks) and toks[k].is_punct("(")):
                return j
    return None


def parse_interval(arg: tr.Arg):
    """`INTERVAL expr UNIT` (the whole argument) -> (expr_text, UNIT) or None."""
    toks = tr.strip(arg.toks)
    if not toks or not toks[0].is_word("INTERVAL"):
        return None
    u = find_unit(toks, 1)
    if u is None or tr.strip(toks[u + 1:]):
        return None
    expr = tr.text_of(toks[1:u])
    return (expr, toks[u].upper) if expr else None


_SYS = "system.main."        # the real DuckDB function, even if a MySQL macro shadows it


def _ctor(unit: str, n: str) -> str:
    """DuckDB interval constructor for `n` units (n is a SQL number expression)."""
    i = f"CAST(round({n}) AS INTEGER)"
    if unit == "YEAR":
        return f"{_SYS}to_years({i})"
    if unit == "QUARTER":
        return f"{_SYS}to_months(3 * {i})"
    if unit == "MONTH":
        return f"{_SYS}to_months({i})"
    if unit == "WEEK":
        return f"{_SYS}to_weeks({i})"
    if unit == "DAY":
        return f"{_SYS}to_days({i})"
    if unit == "HOUR":
        return f"{_SYS}to_hours({i})"
    if unit == "MINUTE":
        return f"{_SYS}to_minutes({i})"
    if unit == "SECOND":
        return f"{_SYS}to_microseconds(CAST(round(({n}) * 1000000) AS BIGINT))"
    return f"{_SYS}to_microseconds(CAST(round({n}) AS BIGINT))"          # MICROSECOND


def interval_sql(expr: str, unit: str, sign: int = 1) -> str:
    """DuckDB interval expression for MySQL `INTERVAL expr unit` (sign applied)."""
    s = "-" if sign < 0 else ""
    if unit in _SINGLE_UNITS:
        return "(" + _ctor(unit, f"{s}_my_tonum(CAST({expr} AS VARCHAR))") + ")"
    e = f"CAST({expr} AS VARCHAR)"
    parts = [_ctor(cu, f"{s}_my_iv_part({e}, '{unit}', {k})")
             for k, cu in enumerate(_COMPOUND[unit])]
    return "(" + " + ".join(parts) + ")"


def _base_expr(arg: tr.Arg, timeful: bool):
    """(sql, kind) for the date operand: literals get a static DATE/TIMESTAMP type."""
    lit = arg.literal()
    if lit is not None:
        d = parse_temporal(lit)
        q = tr.quote_string(lit)
        if d is None:
            return f"CAST(NULL AS DATE)", "date"
        if d.has_time or timeful:
            return f"_my_timestamp({q})", "ts"
        return f"_my_date({q})", "date"
    return arg.text, "expr"


def date_add_sql(base: tr.Arg, expr: str, unit: str, sign: int) -> str:
    """MySQL DATE_ADD semantics, with DATE vs DATETIME result typing."""
    iv = interval_sql(expr, unit, sign)
    timeful = unit_has_time(unit)
    sql, kind = _base_expr(base, timeful)
    if kind in ("date", "ts"):
        if kind == "ts" or timeful:
            return f"(CAST({sql} AS TIMESTAMP) + {iv})"
        return f"CAST({sql} + {iv} AS DATE)"
    macro = "_my_dadd_t" if timeful else "_my_dadd_d"
    return f"{macro}({sql}, {iv})"


def _h_date_add(sign: int):
    def handler(args: list[tr.Arg]):
        if len(args) != 2:
            return None
        iv = parse_interval(args[1])
        if iv is None:
            return None
        return date_add_sql(args[0], iv[0], iv[1], sign)
    return handler


def _h_adddate(sign: int):
    def handler(args: list[tr.Arg]):
        if len(args) != 2:
            return None
        iv = parse_interval(args[1])
        if iv is not None:
            return date_add_sql(args[0], iv[0], iv[1], sign)
        return date_add_sql(args[0], args[1].text, "DAY", sign)
    return handler


def _unit_word(arg: tr.Arg) -> str | None:
    toks = tr.strip(arg.toks)
    if len(toks) == 1 and toks[0].kind == tr.WORD:
        return toks[0].text.upper().strip('"')
    if len(toks) == 1 and toks[0].kind == tr.STR:
        return (toks[0].value or "").upper()
    return None


def _h_timestampadd(args: list[tr.Arg]):
    if len(args) != 3:
        return None
    unit = _unit_word(args[0])
    if unit is None or unit not in _SINGLE_UNITS:
        return None
    return date_add_sql(args[2], args[1].text, unit, 1)


def _h_timestampdiff(args: list[tr.Arg]):
    if len(args) != 3:
        return None
    unit = _unit_word(args[0])
    if unit is None:
        return None
    return (f"_my_timestampdiff('{unit}', CAST({args[1].text} AS VARCHAR), "
            f"CAST({args[2].text} AS VARCHAR))")


def _h_str_to_date(args: list[tr.Arg]):
    if len(args) != 2:
        return None
    fmt = args[1].literal()
    if fmt is not None:
        kind = spec_kind(fmt)
    else:                                   # e.g. STR_TO_DATE(x, GET_FORMAT(DATE, 'USA'))
        m = re.search(r"_my_get_format\('(\w+)'", args[1].text)
        kind = {"DATE": "date", "TIME": "time"}.get(m.group(1) if m else "", "datetime")
    fn = {"date": "_my_str_to_date_d", "datetime": "_my_str_to_date_dt",
          "time": "_my_str_to_date_t"}[kind]
    return f"{fn}(CAST({args[0].text} AS VARCHAR), CAST({args[1].text} AS VARCHAR))"


def _h_get_format(args: list[tr.Arg]):
    if len(args) != 2:
        return None
    kind = _unit_word(args[0])
    if kind is None:
        return None
    return f"_my_get_format('{kind}', CAST({args[1].text} AS VARCHAR))"


def _h_extract(args: list[tr.Arg]):
    if len(args) != 1:
        return None
    toks = tr.strip(args[0].toks)
    if len(toks) < 3 or toks[0].kind != tr.WORD:
        return None
    k = tr.next_sig(toks, 0)
    if k >= len(toks) or not toks[k].is_word("FROM"):
        return None
    unit = toks[0].text.upper()
    rest = tr.text_of(toks[k + 1:])
    return f"_my_extract(CAST({rest} AS VARCHAR), '{unit}')"


def _h_time(args: list[tr.Arg]):
    if len(args) != 1:
        return None
    return f"_my_time(CAST({args[0].text} AS VARCHAR))"


def _h_timestamp(args: list[tr.Arg]):
    # TIME / TIMESTAMP are type keywords: "name(x)" cannot be a macro call
    if len(args) == 1:
        return f"_my_timestamp(CAST({args[0].text} AS VARCHAR))"
    if len(args) == 2:
        return (f"_my_timestamp2(CAST({args[0].text} AS VARCHAR), "
                f"CAST({args[1].text} AS VARCHAR))")
    return None


_NOW_SQL = "date_trunc('second', current_localtimestamp())"
_CURTIME_SQL = "strftime(current_localtimestamp(), '%H:%M:%S')"


def _h_now(_args):                    # CURRENT_TIMESTAMP(), LOCALTIME(), LOCALTIMESTAMP(), NOW(fsp)
    return _NOW_SQL


def _h_curtime(_args):                # CURRENT_TIME(), CURTIME()
    return _CURTIME_SQL


def _h_curdate(_args):                # CURRENT_DATE()
    return "current_date"


def _rule_bare_now(toks: list[tr.Tok]) -> list[tr.Tok]:
    """Bare CURRENT_TIMESTAMP / LOCALTIME[STAMP] / CURRENT_TIME: local DATETIME, not TIMESTAMPTZ."""
    out: list[tr.Tok] = []
    for i, t in enumerate(toks):
        if t.kind == tr.WORD and t.upper in ("CURRENT_TIMESTAMP", "LOCALTIME", "LOCALTIMESTAMP",
                                             "CURRENT_TIME"):
            prev = tr.prev_sig(toks, i)
            nxt = tr.next_sig(toks, i)
            skip = ((prev >= 0 and (toks[prev].is_punct(".") or toks[prev].is_word("AS", "DEFAULT")))
                    or (nxt < len(toks) and toks[nxt].is_punct("(")))
            if not skip:
                out.extend(tr.tokenize_plain(_CURTIME_SQL if t.upper == "CURRENT_TIME" else _NOW_SQL))
                continue
        out.append(t)
    return out


HANDLERS = {
    "CURRENT_TIMESTAMP": _h_now, "LOCALTIME": _h_now, "LOCALTIMESTAMP": _h_now,
    "CURRENT_TIME": _h_curtime, "CURRENT_DATE": _h_curdate,
    "TIME": _h_time, "TIMESTAMP": _h_timestamp,
    "DATE_ADD": _h_date_add(1), "DATE_SUB": _h_date_add(-1),
    "ADDDATE": _h_adddate(1), "SUBDATE": _h_adddate(-1),
    "TIMESTAMPADD": _h_timestampadd, "TIMESTAMPDIFF": _h_timestampdiff,
    "STR_TO_DATE": _h_str_to_date, "GET_FORMAT": _h_get_format, "EXTRACT": _h_extract,
}

PRELUDE += [
    # typed overloads: DATE stays DATE for date units, DATETIME stays DATETIME
    "CREATE OR REPLACE TEMP MACRO _my_dadd_d(d DATE, i INTERVAL) AS CAST(d + i AS DATE), "
    "(d TIMESTAMP, i INTERVAL) AS d + i, "
    "(d TIMESTAMPTZ, i INTERVAL) AS CAST(d AS TIMESTAMP) + i, "
    "(d VARCHAR, i INTERVAL) AS CAST(_my_timestamp(d) + i AS TIMESTAMP)",
    "CREATE OR REPLACE TEMP MACRO _my_dadd_t(d DATE, i INTERVAL) AS CAST(d AS TIMESTAMP) + i, "
    "(d TIMESTAMP, i INTERVAL) AS d + i, "
    "(d TIMESTAMPTZ, i INTERVAL) AS CAST(d AS TIMESTAMP) + i, "
    "(d VARCHAR, i INTERVAL) AS CAST(_my_timestamp(d) + i AS TIMESTAMP)",
]


# --------------------------------------------------------------------------- #
#  Rules: `<expr> + INTERVAL n UNIT` and bare INTERVAL
# --------------------------------------------------------------------------- #
def _rule_interval_ops(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`x +/- INTERVAL n UNIT` and `INTERVAL n UNIT + x` -> typed date arithmetic."""
    for _ in range(100):                        # one rewrite per pass, until stable
        for i, t in enumerate(toks):
            if not t.is_word("INTERVAL"):
                continue
            u = find_unit(toks, i + 1)
            if u is None or not tr.strip(toks[i + 1:u]):
                continue
            expr = tr.text_of(toks[i + 1:u])
            unit = toks[u].upper
            op = tr.prev_sig(toks, i)
            if op >= 0 and (toks[op].is_punct("+") or toks[op].is_punct("-")):
                start = tr.primary_start(toks, op)
                if start >= 0:
                    sign = -1 if toks[op].is_punct("-") else 1
                    repl = date_add_sql(tr.Arg(toks[start:op]), expr, unit, sign)
                    toks = toks[:start] + tr.tokenize_plain(repl) + toks[u + 1:]
                    break
            nxt = tr.next_sig(toks, u)
            if nxt < len(toks) and toks[nxt].is_punct("+"):
                end = tr.primary_end(toks, nxt + 1)
                if end > 0:
                    repl = date_add_sql(tr.Arg(toks[nxt + 1:end]), expr, unit, 1)
                    toks = toks[:i] + tr.tokenize_plain(repl) + toks[end:]
                    break
        else:
            return toks
    return toks


def _rule_bare_interval(toks: list[tr.Tok]) -> list[tr.Tok]:
    """Remaining `INTERVAL expr UNIT` (a plain value) -> DuckDB interval."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        if toks[i].is_word("INTERVAL"):
            u = find_unit(toks, i + 1)
            if u is not None and tr.strip(toks[i + 1:u]):
                out.extend(tr.tokenize_plain(
                    interval_sql(tr.text_of(toks[i + 1:u]), toks[u].upper)))
                i = u + 1
                continue
        out.append(toks[i])
        i += 1
    return out


PRE_RULES: list = []
POST_RULES = [_rule_interval_ops, _rule_bare_interval, _rule_bare_now]
