"""Statement splitting and pretty table rendering.

Kept dependency-free on purpose so it works anywhere Python does.
"""

from __future__ import annotations

from typing import Any, Sequence


# --------------------------------------------------------------------------- #
#  SQL statement splitting
# --------------------------------------------------------------------------- #
def iter_statements(sql: str):
    """Yield `(text, start, end)` for each top-level statement.

    `text` is the stripped statement; `start`/`end` are character offsets into
    `sql` spanning the raw statement (excluding the trailing ';'). Respects
    single/double quotes, backticks, line comments (--) and block comments
    (/* */) so semicolons inside those don't split. Not a full parser, but it
    handles everything you hit while practising.
    """
    i = 0
    n = len(sql)
    quote: str | None = None          # current string/identifier delimiter
    in_line_comment = False
    in_block_comment = False
    buf_start: int | None = None      # index where the current statement began

    def begin(idx: int) -> None:
        nonlocal buf_start
        if buf_start is None:
            buf_start = idx

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                i += 2
                in_block_comment = False
                continue
            i += 1
            continue
        if quote:
            if ch == quote and nxt == quote:   # doubled-quote escape
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue

        # not inside quote/comment
        if ch == "-" and nxt == "-":
            begin(i)
            in_line_comment = True
            i += 1
            continue
        if ch == "/" and nxt == "*":
            begin(i)
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            begin(i)
            quote = ch
            i += 1
            continue
        if ch == ";":
            if buf_start is not None:
                text = sql[buf_start:i].strip()
                if text:
                    yield (text, buf_start, i)
                buf_start = None
            i += 1
            continue
        if not ch.isspace():
            begin(i)
        i += 1

    if buf_start is not None:
        text = sql[buf_start:n].strip()
        if text:
            yield (text, buf_start, n)


def split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements on top-level ';'."""
    return [text for text, _start, _end in iter_statements(sql)]


def statement_at(sql: str, pos: int) -> str | None:
    """Return the statement the cursor at `pos` is in.

    Falls back to the last statement starting before `pos`, then the first.
    """
    spans = list(iter_statements(sql))
    if not spans:
        return None
    chosen: str | None = None
    for text, start, end in spans:
        if start <= pos <= end:
            return text
        if start <= pos:
            chosen = text
    return chosen if chosen is not None else spans[0][0]


def strip_comments_head(stmt: str) -> str:
    """Return the statement's leading keyword-ish start, comments removed,
    lowercased. Used only to decide whether a statement is 'interesting'."""
    out: list[str] = []
    i, n = 0, len(stmt)
    in_line = in_block = False
    while i < n and len("".join(out).strip()) < 12:
        ch = stmt[i]
        nxt = stmt[i + 1] if i + 1 < n else ""
        if in_line:
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if ch == "-" and nxt == "-":
            in_line = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip().lower()


# --------------------------------------------------------------------------- #
#  Table rendering
# --------------------------------------------------------------------------- #
def _cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        # avoid ugly 0.10000000000000001 while keeping precision
        text = repr(value)
        return text
    return str(value)


def _display_width(text: str) -> int:
    """Rough monospace width: count east-asian wide chars as 2."""
    import unicodedata

    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def _pad(text: str, width: int, align: str) -> str:
    gap = width - _display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def _truncate(text: str, max_width: int) -> str:
    if _display_width(text) <= max_width:
        return text
    # cut to fit, leaving room for the ellipsis
    out = []
    used = 0
    for ch in text:
        w = _display_width(ch)
        if used + w > max_width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def render_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    max_col_width: int = 40,
    max_rows: int | None = None,
) -> str:
    """Render rows as a unicode box table (mysql-client style)."""
    columns = list(columns)
    display_rows = list(rows)
    truncated_rows = False
    if max_rows is not None and len(display_rows) > max_rows:
        display_rows = display_rows[:max_rows]
        truncated_rows = True

    # figure out which columns are numeric (for right alignment)
    numeric = [True] * len(columns)
    text_rows: list[list[str]] = []
    for row in display_rows:
        text_row = []
        for j, val in enumerate(row):
            if val is not None and not isinstance(val, (int, float)):
                numeric[j] = False
            text_row.append(_truncate(_cell(val), max_col_width))
        text_rows.append(text_row)

    # column widths
    widths = [min(_display_width(c), max_col_width) for c in columns]
    widths = [max(_display_width(_truncate(c, max_col_width)), w)
              for c, w in zip(columns, widths)]
    for text_row in text_rows:
        for j, cell in enumerate(text_row):
            widths[j] = max(widths[j], _display_width(cell))

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    lines = [border("┌", "┬", "┐")]
    header = "│" + "│".join(
        " " + _pad(_truncate(c, max_col_width), widths[j], "center") + " "
        for j, c in enumerate(columns)
    ) + "│"
    lines.append(header)
    lines.append(border("├", "┼", "┤"))

    for text_row in text_rows:
        align = ["right" if numeric[j] else "left" for j in range(len(columns))]
        line = "│" + "│".join(
            " " + _pad(cell, widths[j], align[j]) + " "
            for j, cell in enumerate(text_row)
        ) + "│"
        lines.append(line)

    lines.append(border("└", "┴", "┘"))

    total = len(rows)
    footer = f"{total} row" + ("s" if total != 1 else "")
    if truncated_rows:
        footer += f" (showing first {len(display_rows)})"
    lines.append(footer)
    return "\n".join(lines)
