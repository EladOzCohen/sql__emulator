"""BigQuery (GoogleSQL) clause-level rewrites not tied to one function family."""

from __future__ import annotations

from .. import translate as tr


def dotted_backticks(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`project.dataset.table` -> "project"."dataset"."table" (one quoted part each)."""
    out: list[tr.Tok] = []
    for t in toks:
        if t.kind == tr.WORD and t.value is not None and "." in t.value:
            parts = ".".join(tr.quote_ident(p) for p in t.value.split("."))
            out.append(tr.Tok(tr.WORD, parts, t.value))
        else:
            out.append(t)
    return out


def except_replace(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`SELECT * EXCEPT (a, b)` -> `SELECT * EXCLUDE (a, b)` (REPLACE is shared syntax)."""
    out: list[tr.Tok] = []
    for i, t in enumerate(toks):
        if t.is_word("EXCEPT"):
            p = tr.prev_sig(toks, i)
            n = tr.next_sig(toks, i)
            if p >= 0 and toks[p].is_punct("*") and n < len(toks) and toks[n].is_punct("("):
                out.append(tr.Tok(tr.WORD, "EXCLUDE"))
                continue
        out.append(t)
    return out


def safe_prefix(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`SAFE.fn(args)` -> `TRY(fn(args))`: errors become NULL, as GoogleSQL's SAFE. does."""
    for _ in range(100):
        for i, t in enumerate(toks):
            if not t.is_word("SAFE"):
                continue
            dot = tr.next_sig(toks, i)
            if dot >= len(toks) or not toks[dot].is_punct("."):
                continue
            name = tr.next_sig(toks, dot)
            if name >= len(toks) or toks[name].kind != tr.WORD:
                continue
            paren = tr.next_sig(toks, name)
            if paren >= len(toks) or not toks[paren].is_punct("("):
                continue
            close = tr.match_paren(toks, paren)
            if close < 0:
                continue
            inner = toks[name:close + 1]
            toks = (toks[:i] + tr.tokenize_plain("TRY(") + inner
                    + tr.tokenize_plain(")") + toks[close + 1:])
            break
        else:
            return toks
    return toks


PRE_RULES = [dotted_backticks, except_replace]
POST_RULES = [safe_prefix]
