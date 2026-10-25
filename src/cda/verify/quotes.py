from dataclasses import dataclass


@dataclass(frozen=True)
class Match:
    ok: bool
    reason: str
    spans_next: bool = False


def normalise(text: str) -> str:
    return " ".join(text.split())


def clean_quote(quote: str) -> str:
    quote = normalise(quote)
    if len(quote) >= 2 and (quote[0], quote[-1]) in (('"', '"'), ("“", "”"), ("'", "'")):
        quote = quote[1:-1]
    for ellipsis in ("...", "…"):
        if quote.startswith(ellipsis):
            quote = quote[len(ellipsis) :].lstrip()
        if quote.endswith(ellipsis):
            quote = quote[: -len(ellipsis)].rstrip()
    return quote.strip()


def match_quote(quote: str, unit_text: str, next_text: str | None = None) -> Match:
    quote = clean_quote(quote)
    if not quote:
        return Match(False, "empty")
    if len(quote) < 8:
        return Match(False, "too_short")
    unit = normalise(unit_text)
    if quote in unit:
        return Match(True, "ok")
    if next_text is not None:
        separator = "" if unit.endswith("-") else " "
        joined = unit + separator + normalise(next_text)
        next_start = len(unit) + len(separator)
        start = joined.find(quote)
        while start != -1 and start < len(unit):
            if start + len(quote) > next_start:
                return Match(True, "ok", True)
            start = joined.find(quote, start + 1)
    return Match(False, "quote_not_found")
