"""Document-name matching without a database or model."""

import pytest

from cda.compare.inconsistencies import _reference_names


@pytest.mark.parametrize(
    "filename,label,document_kind,part_kind,expected",
    [
        ("cba-2026-annual-report.pdf", None, "other", None, "cba 2026 annual report"),
        ("cba_2026_annual_report.pdf", None, "other", None, "cba 2026 annual report"),
        ("report.pdf", " Climate_Appendix  ", "other", "other", "climate appendix"),
        ("report.pdf", None, "annual", None, "annual report"),
        ("report.pdf", None, "other", "annual", "annual report"),
        ("report.pdf", None, "sustainability", None, "climate report"),
        ("report.pdf", None, "other", "sustainability", "climate related financial disclosures"),
    ],
)
def test_reference_names(filename, label, document_kind, part_kind, expected):
    assert expected in _reference_names(filename, label, document_kind, part_kind)


def test_other_kind_has_no_generic_names():
    assert _reference_names("appendix.pdf", None, "other", "other") == {"appendix"}
