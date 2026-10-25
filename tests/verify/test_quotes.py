import pytest

from cda.verify.quotes import clean_quote, match_quote, normalise


def test_exact():
    result = match_quote("climate disclosures", "The climate disclosures follow.")
    assert (result.ok, result.reason, result.spans_next) == (True, "ok", False)


def test_whitespace():
    assert normalise(" a\u00a0 b\n\tc ") == "a b c"
    assert match_quote("climate disclosures follow", "climate  disclosures\n\t\u00a0follow").ok


@pytest.mark.parametrize(
    "quote",
    [
        "Climate 2026, isn’t low–risk",
        "climate 2027, isn’t low–risk",
        "climate 2026. isn’t low–risk",
        "climate 2026, isn't low–risk",
        "climate 2026, isn’t low-risk",
    ],
)
def test_changed_character(quote):
    assert match_quote(quote, "climate 2026, isn’t low–risk").reason == "quote_not_found"


@pytest.mark.parametrize(
    "quote", ['"climate disclosures"', "“climate disclosures”", "'climate disclosures'"]
)
def test_wrapping(quote):
    assert match_quote(quote, "climate disclosures").ok


def test_unpaired_quote():
    assert match_quote('"climate disclosures', "climate disclosures").reason == "quote_not_found"


@pytest.mark.parametrize(
    "quote",
    [
        "... climate disclosures",
        "climate disclosures ...",
        "… climate disclosures",
        "climate disclosures …",
        "... climate disclosures …",
    ],
)
def test_edge_ellipsis(quote):
    assert match_quote(quote, "climate disclosures").ok


def test_internal_ellipsis():
    assert clean_quote("first ... last") == "first ... last"
    assert (
        match_quote("first ... last", "first sentence. last sentence.").reason == "quote_not_found"
    )


@pytest.mark.parametrize("quote", ["", " \n\t", "...", "…"])
def test_empty(quote):
    assert match_quote(quote, "anything").reason == "empty"


def test_seven_characters():
    assert match_quote("1234567", "1234567").reason == "too_short"


def test_eight_characters():
    assert match_quote("12345678", "12345678").ok


def test_spanning():
    result = match_quote("climate disclosures", "The climate", "disclosures follow")
    assert result.ok and result.spans_next


def test_wholly_next():
    assert (
        match_quote("climate disclosures", "intro", "climate disclosures").reason
        == "quote_not_found"
    )


def test_hyphenated():
    assert match_quote("climate-related disclosures", "climate-", "related disclosures").spans_next


def test_no_next():
    assert match_quote("climate disclosures", "climate").reason == "quote_not_found"
