"""MySQL clause-level rewrites that are not tied to one function family."""

from __future__ import annotations

from .. import translate as tr


def limit_offset(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`LIMIT a, b` -> `LIMIT b OFFSET a`."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.is_word("LIMIT"):
            a_end = tr.primary_end(toks, i + 1)
            if a_end > 0:
                k = tr.next_sig(toks, a_end - 1)
                if k < len(toks) and toks[k].is_punct(","):
                    b_end = tr.primary_end(toks, k + 1)
                    if b_end > 0:
                        a = tr.text_of(toks[i + 1:a_end])
                        b = tr.text_of(toks[k + 1:b_end])
                        out.extend(tr.tokenize_plain(f"LIMIT {b} OFFSET {a}"))
                        i = b_end
                        continue
        out.append(t)
        i += 1
    return out


def like_case_insensitive(toks: list[tr.Tok]) -> list[tr.Tok]:
    """MySQL LIKE: case-insensitive (default collation) and `\\` escapes by default."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        prev = tr.prev_sig(toks, i)
        if t.is_word("LIKE") and not (prev >= 0 and toks[prev].is_punct(".")):
            out.append(tr.Tok(tr.WORD, "ILIKE"))
            end = tr.primary_end(toks, i + 1)
            if end > 0:
                nxt = tr.next_sig(toks, end - 1)
                has_escape = nxt < len(toks) and toks[nxt].is_word("ESCAPE")
                out.extend(toks[i + 1:end])
                if not has_escape:
                    out.extend(tr.tokenize_plain(" ESCAPE '\\'"))
                i = end
                continue
        else:
            out.append(t)
        i += 1
    return out


def operators(toks: list[tr.Tok]) -> list[tr.Tok]:
    """<=>, &&, ||, ! -> DuckDB spellings (|| is logical OR in default MySQL mode)."""
    out: list[tr.Tok] = []
    for t in toks:
        if t.kind == tr.PUNCT and t.text == "<=>":
            out.append(tr.Tok(tr.WORD, " IS NOT DISTINCT FROM "))
        elif t.kind == tr.PUNCT and t.text == "&&":
            out.append(tr.Tok(tr.WORD, " AND "))
        elif t.kind == tr.PUNCT and t.text == "||":
            out.append(tr.Tok(tr.WORD, " OR "))
        elif t.kind == tr.PUNCT and t.text == "!":
            out.append(tr.Tok(tr.WORD, " NOT "))
        else:
            out.append(t)
    return out


def from_dual(toks: list[tr.Tok]) -> list[tr.Tok]:
    """`FROM DUAL` -> nothing."""
    out: list[tr.Tok] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.is_word("FROM"):
            k = tr.next_sig(toks, i)
            if k < len(toks) and toks[k].is_word("DUAL"):
                i = k + 1
                continue
        out.append(t)
        i += 1
    return out


def insert_forms(toks: list[tr.Tok]) -> list[tr.Tok]:
    """INSERT IGNORE -> INSERT OR IGNORE; REPLACE INTO -> INSERT OR REPLACE INTO."""
    sig = [i for i, t in enumerate(toks) if t.kind not in (tr.WS, tr.COMMENT)]
    if not sig:
        return toks
    first = sig[0]
    if toks[first].is_word("REPLACE") and len(sig) > 1 and toks[sig[1]].is_word("INTO"):
        return toks[:first] + tr.tokenize_plain("INSERT OR REPLACE") + toks[first + 1:]
    if toks[first].is_word("INSERT") and len(sig) > 1 and toks[sig[1]].is_word("IGNORE"):
        return toks[:sig[1]] + tr.tokenize_plain("OR IGNORE") + toks[sig[1] + 1:]
    return toks


PRE_RULES = [from_dual, insert_forms, operators]
POST_RULES = [limit_offset, like_case_insensitive]
