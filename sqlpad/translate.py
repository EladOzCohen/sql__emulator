"""SQL text translation toolkit (MySQL / BigQuery -> DuckDB).

A small tokenizer that understands strings, quoted identifiers, comments and
parentheses, plus helpers to rewrite function calls and clauses. Dialect
packages (sqlpad/mysql, sqlpad/bigquery) register *call handlers* (rewrite one
function) and *rules* (rewrite token streams: LIMIT a, b; INTERVAL ...; ...).

Nothing here is a full SQL parser. It is deliberately conservative: when a
pattern is not recognised the text is left untouched, so DuckDB reports the
real error instead of the translator guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

# --------------------------------------------------------------------------- #
#  Tokens
# --------------------------------------------------------------------------- #
WS, COMMENT, WORD, NUM, STR, DQ, BQ, PUNCT = (
    "ws", "comment", "word", "num", "str", "dq", "bq", "punct")

_MULTI_PUNCT = ("<=>", "->>", "<=", ">=", "<>", "!=", "||", "&&", "::", "->",
                ":=", "<<", ">>")
_WORD_RE = re.compile(r"[^\W\d][\w$]*|\d+[^\W\d][\w$]*", re.UNICODE)
_IDENT_RE = re.compile(r"@{0,2}[^\W\d][\w$]*|@{1,2}\w+", re.UNICODE)
_NUM_RE = re.compile(r"(0[xX][0-9A-Fa-f]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)")
_BACKSLASH = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "Z": "\x1a",
              "\\": "\\", "'": "'", '"': '"', "`": "`", "%": "\\%", "_": "\\_"}


@dataclass
class Tok:
    kind: str
    text: str
    value: str | None = None      # decoded content of str / dq / bq tokens
    prefix: str = ""              # 'r', 'b', 'rb' for BigQuery raw / bytes strings

    @property
    def upper(self) -> str:
        return self.text.upper()

    def is_word(self, *names: str) -> bool:
        return self.kind == WORD and (not names or self.text.upper() in names)

    def is_punct(self, *chars: str) -> bool:
        return self.kind == PUNCT and (not chars or self.text in chars)


def _read_triple(sql: str, i: int, quote: str, backslash: bool) -> tuple[str, str, int]:
    """Read a '''...''' / \"\"\"...\"\"\" string starting at sql[i]."""
    fence = quote * 3
    j = i + 3
    out: list[str] = []
    n = len(sql)
    while j < n:
        if sql.startswith(fence, j):
            return sql[i:j + 3], "".join(out), j + 3
        ch = sql[j]
        if backslash and ch == "\\" and j + 1 < n:
            out.append(_BACKSLASH.get(sql[j + 1], sql[j + 1]))
            j += 2
            continue
        out.append(ch)
        j += 1
    return sql[i:], "".join(out), n


def _read_quoted(sql: str, i: int, quote: str, backslash: bool) -> tuple[str, str, int]:
    """Read a quoted token starting at sql[i] == quote. Returns (raw, value, end)."""
    j = i + 1
    out: list[str] = []
    n = len(sql)
    while j < n:
        ch = sql[j]
        if ch == quote:
            if j + 1 < n and sql[j + 1] == quote:       # doubled quote
                out.append(quote)
                j += 2
                continue
            return sql[i:j + 1], "".join(out), j + 1
        if backslash and ch == "\\" and j + 1 < n:
            nxt = sql[j + 1]
            out.append(_BACKSLASH.get(nxt, nxt))
            j += 2
            continue
        out.append(ch)
        j += 1
    return sql[i:], "".join(out), n                    # unterminated: keep as is


_BQ_PREFIX = re.compile(r"(?i)(rb|br|r|b)(?=['\"])")


def tokenize(sql: str, *, backslash_escapes: bool = True,
             hash_comments: bool = True, bq_strings: bool = False) -> list[Tok]:
    """Split SQL into tokens. `bq_strings` adds BigQuery's r'..', b'..' and triple quotes."""
    toks: list[Tok] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if bq_strings and ch in "rRbB" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$")):
            pm = _BQ_PREFIX.match(sql, i)
            if pm:
                prefix = pm.group(1).lower()
                q = sql[pm.end()]
                raw_mode = "r" in prefix
                start = pm.end()
                if sql.startswith(q * 3, start):
                    raw, val, end = _read_triple(sql, start, q, not raw_mode)
                else:
                    raw, val, end = _read_quoted(sql, start, q, not raw_mode)
                toks.append(Tok(STR, sql[i:start] + raw, val, prefix="".join(sorted(prefix))))
                i = end
                continue
        if ch.isspace():
            j = i
            while j < n and sql[j].isspace():
                j += 1
            toks.append(Tok(WS, sql[i:j]))
            i = j
        elif ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok(COMMENT, sql[i:j]))
            i = j
        elif ch == "#" and hash_comments:
            j = sql.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok(COMMENT, sql[i:j]))
            i = j
        elif ch == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            toks.append(Tok(COMMENT, sql[i:j]))
            i = j
        elif ch == "'":
            if bq_strings and sql.startswith("'''", i):
                raw, val, i = _read_triple(sql, i, "'", backslash_escapes)
            else:
                raw, val, i = _read_quoted(sql, i, "'", backslash_escapes)
            toks.append(Tok(STR, raw, val))
        elif ch == '"':
            if bq_strings and sql.startswith('"""', i):
                raw, val, i = _read_triple(sql, i, '"', backslash_escapes)
                toks.append(Tok(STR, raw, val))     # a triple-quoted string is always a string
            else:
                raw, val, i = _read_quoted(sql, i, '"', backslash_escapes)
                toks.append(Tok(DQ, raw, val))
        elif ch == "`":
            raw, val, i = _read_quoted(sql, i, "`", False)
            toks.append(Tok(BQ, raw, val))
        elif ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _WORD_RE.match(sql, i) if ch.isdigit() else None
            if m and m.group(0)[0].isdigit() and not _NUM_RE.fullmatch(m.group(0)):
                toks.append(Tok(WORD, m.group(0)))      # e.g. 1abc identifier
                i = m.end()
            else:
                m = _NUM_RE.match(sql, i)
                toks.append(Tok(NUM, m.group(0)))
                i = m.end()
        elif ch.isalpha() or ch in "_$@":
            m = _IDENT_RE.match(sql, i)
            if m is None:
                toks.append(Tok(PUNCT, ch))
                i += 1
            else:
                toks.append(Tok(WORD, m.group(0)))
                i = m.end()
        else:
            for op in _MULTI_PUNCT:
                if sql.startswith(op, i):
                    toks.append(Tok(PUNCT, op))
                    i += len(op)
                    break
            else:
                toks.append(Tok(PUNCT, ch))
                i += 1
    return toks


def detokenize(toks: Iterable[Tok]) -> str:
    return "".join(t.text for t in toks)


# --------------------------------------------------------------------------- #
#  Literals
# --------------------------------------------------------------------------- #
def quote_string(value: str) -> str:
    """A DuckDB string literal holding exactly `value`."""
    if any(c in value for c in "\\\n\r\t\0\b\x1a"):
        esc = (value.replace("\\", "\\\\").replace("'", "''")
               .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
               .replace("\0", "\\0").replace("\b", "\\b").replace("\x1a", "\\x1a"))
        return "E'" + esc + "'"
    return "'" + value.replace("'", "''") + "'"


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def normalize_literals(toks: list[Tok]) -> list[Tok]:
    """'str' / "str" -> DuckDB string literal; `ident` -> "ident"."""
    out: list[Tok] = []
    for t in toks:
        if t.kind in (STR, DQ):
            lit = quote_string(t.value or "")
            if "b" in t.prefix:                       # BigQuery bytes literal
                lit = f"CAST({lit} AS BLOB)"
            out.append(Tok(STR, lit, t.value, t.prefix))
        elif t.kind == BQ:
            out.append(Tok(WORD, quote_ident(t.value or ""), t.value))
        else:
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
#  Navigation helpers
# --------------------------------------------------------------------------- #
def _skip(t: Tok) -> bool:
    return t.kind in (WS, COMMENT)


def next_sig(toks: list[Tok], i: int) -> int:
    """Index of the next significant token after i, or len(toks)."""
    j = i + 1
    while j < len(toks) and _skip(toks[j]):
        j += 1
    return j


def prev_sig(toks: list[Tok], i: int) -> int:
    """Index of the previous significant token before i, or -1."""
    j = i - 1
    while j >= 0 and _skip(toks[j]):
        j -= 1
    return j


def match_paren(toks: list[Tok], i: int) -> int:
    """Index of the ')' closing the '(' at toks[i]; -1 if unbalanced."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for j in range(i, len(toks)):
        t = toks[j]
        if t.kind != PUNCT:
            continue
        if t.text in pairs:
            stack.append(pairs[t.text])
        elif stack and t.text == stack[-1]:
            stack.pop()
            if not stack:
                return j
    return -1


def split_args(toks: list[Tok], start: int, end: int) -> list[list[Tok]]:
    """Split toks[start:end] (the inside of a call) at top-level commas."""
    args: list[list[Tok]] = [[]]
    depth = 0
    for t in toks[start:end]:
        if t.kind == PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == PUNCT and t.text in ")]}":
            depth -= 1
        if depth == 0 and t.kind == PUNCT and t.text == ",":
            args.append([])
        else:
            args[-1].append(t)
    if len(args) == 1 and not any(not _skip(t) for t in args[0]):
        return []
    return args


def strip(toks: list[Tok]) -> list[Tok]:
    a, b = 0, len(toks)
    while a < b and _skip(toks[a]):
        a += 1
    while b > a and _skip(toks[b - 1]):
        b -= 1
    return toks[a:b]


def text_of(toks: list[Tok]) -> str:
    return detokenize(strip(toks))


def is_string_literal(toks: list[Tok]) -> str | None:
    """If toks is exactly one string literal, return its decoded value."""
    s = strip(toks)
    if len(s) == 1 and s[0].kind == STR:
        return s[0].value
    return None


# --------------------------------------------------------------------------- #
#  Operand extraction (for infix rewrites such as  x DIV y, d + INTERVAL ...)
# --------------------------------------------------------------------------- #
def primary_end(toks: list[Tok], i: int) -> int:
    """Index just past the primary expression starting at toks[i] (-1 if none).

    A primary is a literal, an identifier chain (a.b.c), a call name(...), a
    parenthesised group, CAST/CASE-style constructs ending in ')' / END, with
    optional unary +/-.
    """
    n = len(toks)
    while i < n and _skip(toks[i]):
        i += 1
    if i >= n:
        return -1
    t = toks[i]
    if t.kind == PUNCT and t.text in "+-":
        return primary_end(toks, i + 1)
    if t.kind == PUNCT and t.text == "(":
        j = match_paren(toks, i)
        return -1 if j < 0 else j + 1
    if t.kind == WORD and t.upper == "CASE":
        depth = 0
        for j in range(i, n):
            if toks[j].is_word("CASE"):
                depth += 1
            elif toks[j].is_word("END"):
                depth -= 1
                if depth == 0:
                    return j + 1
        return -1
    if t.kind in (NUM, STR):
        return i + 1
    if t.kind == WORD:
        j = i + 1
        while True:                                   # a.b.c chain
            k = next_sig(toks, j - 1)
            if k < n and toks[k].is_punct(".") and next_sig(toks, k) < n:
                m = next_sig(toks, k)
                if toks[m].kind == WORD:
                    j = m + 1
                    continue
            break
        k = next_sig(toks, j - 1)
        if k < n and toks[k].is_punct("("):
            c = match_paren(toks, k)
            return -1 if c < 0 else c + 1
        return j
    return -1


def primary_start(toks: list[Tok], end: int) -> int:
    """Index where the primary expression ending just before `end` begins."""
    j = prev_sig(toks, end)
    if j < 0:
        return -1
    t = toks[j]
    if t.kind == PUNCT and t.text in ")]}":
        # find the matching opener, then include a preceding function name
        depth = 0
        for k in range(j, -1, -1):
            tk = toks[k]
            if tk.kind == PUNCT and tk.text in ")]}":
                depth += 1
            elif tk.kind == PUNCT and tk.text in "([{":
                depth -= 1
                if depth == 0:
                    p = prev_sig(toks, k)
                    if p >= 0 and toks[p].kind == WORD and toks[p].upper not in _NOT_FUNCTIONS:
                        return _chain_start(toks, p)
                    return k
        return -1
    if t.kind == WORD and t.upper == "END":
        depth = 0
        for k in range(j, -1, -1):
            if toks[k].is_word("END"):
                depth += 1
            elif toks[k].is_word("CASE"):
                depth -= 1
                if depth == 0:
                    return k
        return -1
    if t.kind in (NUM, STR):
        return j
    if t.kind == WORD:
        return _chain_start(toks, j)
    return -1


_NOT_FUNCTIONS = {"AND", "OR", "NOT", "IN", "IS", "LIKE", "BETWEEN", "WHEN", "THEN",
                  "ELSE", "SELECT", "WHERE", "ON", "BY", "AS", "FROM", "HAVING",
                  "XOR", "DIV", "MOD", "REGEXP", "RLIKE", "ILIKE", "INTERVAL",
                  "LIMIT", "OFFSET", "UNION", "ALL", "DISTINCT", "SET", "VALUES"}


def _chain_start(toks: list[Tok], j: int) -> int:
    """Walk left over a.b.c identifier chains from the word at toks[j]."""
    while True:
        p = prev_sig(toks, j)
        if p >= 0 and toks[p].is_punct("."):
            q = prev_sig(toks, p)
            if q >= 0 and toks[q].kind == WORD:
                j = q
                continue
        return j


# --------------------------------------------------------------------------- #
#  Call rewriting
# --------------------------------------------------------------------------- #
@dataclass
class Arg:
    """One call argument, after its own inner calls were rewritten."""
    toks: list[Tok]

    @property
    def text(self) -> str:
        return text_of(self.toks)

    def literal(self) -> str | None:
        return is_string_literal(self.toks)

    def __str__(self) -> str:
        return self.text


Handler = Callable[[list[Arg]], "str | None"]
Rule = Callable[[list[Tok]], list[Tok]]


def rewrite_calls(toks: list[Tok], handlers: dict[str, Handler]) -> list[Tok]:
    """Rewrite calls name(args...) whose name is in `handlers` (inner first).

    A handler returns the replacement text for the whole call, or None to keep
    the call as it was (its arguments are still rewritten).
    """
    out: list[Tok] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t.kind == WORD and t.upper in handlers:
            prev = prev_sig(toks, i)
            after_dot = prev >= 0 and toks[prev].is_punct(".")
            is_type = prev >= 0 and toks[prev].is_word("AS")     # CAST(x AS TIME(6))
            k = next_sig(toks, i)
            if not after_dot and not is_type and k < n and toks[k].is_punct("("):
                close = match_paren(toks, k)
                if close > 0:
                    raw_args = split_args(toks, k + 1, close)
                    args = [Arg(rewrite_calls(a, handlers)) for a in raw_args]
                    try:
                        replacement = handlers[t.upper](args)
                    except Exception:  # noqa: BLE001 - a handler bug must not hide the query
                        replacement = None
                    if replacement is None:
                        inner = ",".join(detokenize(a.toks) for a in args)
                        replacement = f"{t.text}({inner})"
                    out.extend(tokenize_plain(replacement))
                    i = close + 1
                    continue
        out.append(t)
        i += 1
    return out


def tokenize_plain(text: str) -> list[Tok]:
    """Tokenize generated SQL (already DuckDB-flavoured: no MySQL escapes)."""
    return tokenize(text, backslash_escapes=False, hash_comments=False)


# --------------------------------------------------------------------------- #
#  Keeping result column names
# --------------------------------------------------------------------------- #
_SELECT_END = {"FROM", "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "UNION",
               "INTERSECT", "EXCEPT", "WINDOW", "INTO", "QUALIFY", "OFFSET"}
_NOT_ALIAS = {"NULL", "TRUE", "FALSE", "END", "UNKNOWN", "DISTINCT", "ALL", "DAY", "HOUR",
              "MINUTE", "SECOND", "MONTH", "YEAR", "WEEK", "QUARTER", "MICROSECOND",
              "ASC", "DESC", "ESCAPE", "SEPARATOR"}


def select_item_spans(toks: list[Tok]) -> list[tuple[int, int]] | None:
    """Spans (start, end) of the first top-level SELECT's output items, or None."""
    depth = 0
    start = None
    for i, t in enumerate(toks):
        if t.kind == PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.is_word("SELECT"):
            start = i + 1
            break
    if start is None:
        return None
    j = start
    while j < len(toks) and (_skip(toks[j]) or toks[j].is_word("DISTINCT", "ALL")):
        j += 1
    spans: list[tuple[int, int]] = []
    item_start = j
    depth = 0
    for k in range(j, len(toks)):
        t = toks[k]
        if t.kind == PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and (t.is_punct(",") or t.is_punct(";")
                             or (t.kind == WORD and t.upper in _SELECT_END)):
            spans.append((item_start, k))
            if not t.is_punct(","):
                return spans
            item_start = k + 1
    spans.append((item_start, len(toks)))
    return spans


def _has_alias(item: list[Tok]) -> bool:
    sig = [t for t in item if not _skip(t)]
    if len(sig) < 2:
        return False
    depth = 0
    for t in sig:
        if t.kind == PUNCT and t.text in "([{":
            depth += 1
        elif t.kind == PUNCT and t.text in ")]}":
            depth -= 1
        elif depth == 0 and t.is_word("AS"):
            return True
    last, before = sig[-1], sig[-2]
    return (last.kind in (WORD, STR) and last.upper not in _NOT_ALIAS
            and not (before.kind == PUNCT and before.text not in ")]}")
            and not before.is_word("IS", "NOT", "AND", "OR", "THEN", "ELSE", "WHEN", "CASE",
                                   "INTERVAL", "DISTINCT", "SELECT", "BY", "FROM"))


def _is_column_ref(item: list[Tok]) -> bool:
    """True for `col` / `t.col` / `"quoted"` (a plain column reference)."""
    sig = [t for t in item if not _skip(t)]
    if not sig or len(sig) % 2 == 0:
        return False
    for i, t in enumerate(sig):
        if i % 2 == 0:
            if t.kind != WORD or t.upper in _NOT_ALIAS:
                return False
        elif not t.is_punct("."):
            return False
    return True


def keep_column_names(raw: list[Tok], before: list[Tok], after: list[Tok],
                      style: str = "expression") -> list[Tok]:
    """Name un-aliased SELECT items the way the source dialect would.

    style "expression" (MySQL): the text the user typed, for items the
    translator rewrote (DuckDB would otherwise show the rewritten SQL).
    style "fN" (BigQuery): f0_, f1_, ... for every un-aliased item that is not
    a plain column reference.
    """
    try:
        sb, sa = select_item_spans(before), select_item_spans(after)
    except Exception:  # noqa: BLE001
        return after
    if not sb or not sa or len(sb) != len(sa):
        return after
    out = list(after[:sa[0][0]])
    for idx, ((b0, b1), (a0, a1)) in enumerate(zip(sb, sa)):
        item_after = after[a0:a1]
        sig_before = strip(before[b0:b1])
        if style == "fN":
            wanted = (bool(sig_before) and not sig_before[0].is_punct("*")
                      and not _has_alias(before[b0:b1]) and not _is_column_ref(before[b0:b1])
                      and not (len(sig_before) > 2 and sig_before[-1].is_punct("*")))
            name = f"f{idx}_"
        else:
            wanted = (detokenize(before[b0:b1]) != detokenize(item_after)
                      and not _has_alias(before[b0:b1]) and bool(strip(raw[b0:b1]))
                      and not sig_before[0].is_punct("*"))
            name = " ".join(detokenize(strip(raw[b0:b1])).split()) if wanted else ""
        if wanted:
            core = strip(item_after)
            out.extend(item_after[:item_after.index(core[0])] if core else [])   # leading space
            out.extend(core)
            out.extend(tokenize_plain(f" AS {quote_ident(name)}"))
            if item_after and _skip(item_after[-1]):
                out.append(item_after[-1])
        else:
            out.extend(item_after)
        if idx + 1 < len(sa):
            out.extend(after[a1:sa[idx + 1][0]])        # the comma between items
    out.extend(after[sa[-1][1]:])
    return out


# --------------------------------------------------------------------------- #
#  Translator
# --------------------------------------------------------------------------- #
class Translator:
    """Applies rules and call handlers to one SQL statement."""

    def __init__(self, *, backslash_escapes: bool, hash_comments: bool = True,
                 unnamed_style: str | None = "expression", bq_strings: bool = False):
        self.backslash_escapes = backslash_escapes
        self.hash_comments = hash_comments
        self.unnamed_style = unnamed_style        # "expression" | "fN" | None
        self.bq_strings = bq_strings
        self.handlers: dict[str, Handler] = {}
        self.pre_rules: list[Rule] = []     # before call handlers
        self.post_rules: list[Rule] = []    # after call handlers

    def add_handlers(self, handlers: dict[str, Handler]) -> None:
        for name, fn in handlers.items():
            self.handlers[name.upper()] = fn

    def __call__(self, sql: str) -> str:
        raw = tokenize(sql, backslash_escapes=self.backslash_escapes,
                       hash_comments=self.hash_comments, bq_strings=self.bq_strings)
        before = normalize_literals(raw)
        toks = before
        for rule in self.pre_rules:
            toks = rule(toks)
        toks = rewrite_calls(toks, self.handlers)
        for rule in self.post_rules:
            toks = rule(toks)
        if self.unnamed_style and len(before) == len(raw):
            toks = keep_column_names(raw, before, toks, self.unnamed_style)
        return detokenize(toks)
