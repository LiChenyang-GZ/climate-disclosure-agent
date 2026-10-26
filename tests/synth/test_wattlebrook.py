import re
from collections import Counter
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from cda.parsing.parts import load_parts_file, resolve_part
from cda.parsing.pdf import parse_pdf
from cda.parsing.units import normalize_text
from cda.synth.answer_key import AnswerKey
from cda.synth.wattlebrook import ANNUAL, SUPPLEMENT, build_pack


@pytest.fixture(scope="session")
def pack(tmp_path_factory):
    folder = tmp_path_factory.mktemp("wattlebrook")
    manifest = build_pack(folder)
    parsed = {name: parse_pdf(folder / name) for name in (ANNUAL, SUPPLEMENT)}
    return folder, manifest, parsed


def validate_locations(folder, parsed):
    key = AnswerKey.model_validate(
        yaml.safe_load((folder / "answer_key.yaml").read_text(encoding="utf-8"))
    )
    parts = {d.filename: d for d in load_parts_file(folder / "parts.yaml").documents}
    locations = [e for c in key.cells for e in c.evidence]
    locations += [e for issue in key.planted_issues for e in issue.locations]
    for e in locations:
        units = [u for u in parsed[e.document].units if u.pdf_page == e.pdf_page]
        assert any(normalize_text(e.quote) in normalize_text(u.text) for u in units), e
        if e.source_type == "table":
            assert any(e.quote in u.text for u in units if u.kind == "table_row"), e
        d = parts[e.document]
        assert resolve_part(d.parts, d.kind, e.pdf_page).kind == e.part
    return key


def test_determinism_and_committed_drift(pack, tmp_path):
    folder, _, _ = pack
    build_pack(tmp_path)
    for name in (ANNUAL, SUPPLEMENT, "parts.yaml", "answer_key.yaml"):
        assert (folder / name).read_bytes() == (tmp_path / name).read_bytes()
        assert (folder / name).read_bytes() == (
            Path("data/synthetic/wattlebrook") / name
        ).read_bytes()


def test_parts_and_quotes(pack):
    folder, manifest, parsed = pack
    validate_locations(folder, parsed)
    part = load_parts_file(folder / "parts.yaml").documents[0].parts[0]
    assert part.first_pdf_page == manifest.locations["sr_start"].pdf_page
    assert part.last_pdf_page == manifest.locations["index"].pdf_page
    assert manifest.locations["sr_start"].quote == "Climate-related financial disclosures"


def test_layout_and_reading_order(pack):
    _, manifest, parsed = pack
    for name, doc in parsed.items():
        for u in doc.units:
            assert "Wattlebrook Energy Limited | Annual Report 2026" not in u.text
            assert u.text != "Wattlebrook Climate Data Supplement 2026"
            if name == SUPPLEMENT or u.pdf_page >= 4:
                assert u.printed_page == str(u.pdf_page - (name == ANNUAL))
    assert {u.page_half for u in parsed[ANNUAL].units if u.pdf_page == 3} == {"left", "right"}
    populated_pages = 0
    for name, doc in parsed.items():
        for page in range(1, doc.page_count + 1):
            positions = {"left": [], "right": []}
            for loc in manifest.locations.values():
                if loc.document == name and loc.pdf_page == page and loc.column in positions:
                    matches = [
                        i
                        for i, u in enumerate(doc.units)
                        if u.pdf_page == page and loc.quote in u.text
                    ]
                    assert matches, loc
                    positions[loc.column].append(min(matches))
            if all(len(v) >= 2 for v in positions.values()):
                populated_pages += 1
                assert max(positions["left"]) < min(positions["right"])

    assert populated_pages >= 5


@pytest.mark.parametrize("tag,count", [("risk_table", 7), ("emissions_table", 3), ("scorecard", 3)])
def test_table_rows(pack, tag, count):
    _, manifest, parsed = pack
    loc = manifest.locations[tag]
    units = parsed[loc.document].units
    parents = [u for u in units if u.pdf_page == loc.pdf_page and u.kind == "table"]
    assert len(parents) == 1
    assert len([u for u in units if u.parent_key == parents[0].key]) == count


def check_invariants(folder, parsed):
    key = validate_locations(folder, parsed)
    part = load_parts_file(folder / "parts.yaml").documents[0].parts[0]
    sr = " ".join(
        u.text
        for u in parsed[ANNUAL].units
        if part.first_pdf_page <= u.pdf_page <= part.last_pdf_page
    )
    ar = " ".join(
        u.text
        for u in parsed[ANNUAL].units
        if not part.first_pdf_page <= u.pdf_page <= part.last_pdf_page
    )
    sup = " ".join(u.text for u in parsed[SUPPLEMENT].units)
    assert not re.search(
        r"flood|inundat|storm|cyclone|heavy rain|extreme weather|severe weather", sr, re.IGNORECASE
    )
    assert "A$14.2 million" in ar and "nine days" in ar
    assert "1,842,600" in sr and "1,842,600" in sup and "1.91 Mt" in ar
    assert "1.91 Mt" not in sr + sup
    assert "Safeguard" in ar and "Safeguard" not in sr + sup
    assert "should not be relied upon" in sr
    text = sr + ar + sup
    assert all(float(v) < 2 for v in re.findall(r"(\d+(?:\.\d+)?)\s*°C", text))
    assert not re.search(r"Current Policies|Stated Policies|STEPS|SSP[235]|RCP", text)
    injections = [i for i in key.planted_issues if i.type == "injection"]
    assert len(injections) == 2
    for i in injections:
        assert i.locations[0].quote in text
        assert all(i.locations[0].quote != e.quote for c in key.cells for e in c.evidence)


def test_planted_invariants(pack):
    folder, _, parsed = pack
    check_invariants(folder, parsed)


def trigrams(text):
    stops = {
        "a",
        "an",
        "and",
        "the",
        "of",
        "to",
        "in",
        "for",
        "on",
        "by",
        "with",
        "its",
        "our",
        "their",
        "is",
        "are",
        "be",
    }
    words = [w for w in re.sub(r"[^\w\s]", "", text.lower()).split() if w not in stops]
    return set(zip(words, words[1:], words[2:]))


def test_difficulty_absence_and_distractors(pack):
    folder, manifest, parsed = pack
    key = validate_locations(folder, parsed)
    checks = yaml.safe_load(Path("src/cda/checks/checks.yaml").read_text(encoding="utf-8"))[
        "checks"
    ]
    assert Counter(c.check_id for c in key.cells) == Counter(c["id"] for c in checks)
    mix = Counter(c.difficulty for c in key.cells)
    for label, minimum in [("paraphrase", 3), ("split", 3), ("table", 2), ("direct", 1)]:
        assert mix[label] >= minimum
    assert any(c.status == "not_found" for c in key.cells)
    for cell in key.cells:
        check = next(c for c in checks if c["id"] == cell.check_id)
        forbidden = set().union(*(trigrams(t) for t in [check["title"], *check["queries"]]))
        for e in cell.evidence:
            if e.difficulty != "direct":
                assert not trigrams(e.quote) & forbidden, (
                    cell.check_id,
                    e.quote,
                    trigrams(e.quote) & forbidden,
                )
        if cell.status in ("not_found", "inconsistent"):
            for name, doc in parsed.items():
                assert any(
                    s.document == name
                    and s.first_pdf_page == 1
                    and s.last_pdf_page == doc.page_count
                    for s in cell.scanned
                )
    evidence = {e.quote for c in key.cells for e in c.evidence}
    for tag, loc in manifest.locations.items():
        if tag.startswith("distractor"):
            assert loc.quote not in evidence
            assert any(
                loc.quote in u.text
                for u in parsed[loc.document].units
                if u.pdf_page == loc.pdf_page
            )


def test_index_pages(pack):
    _, manifest, parsed = pack
    for tag, requirement in [
        ("governance", "6"),
        ("strategy", "10-22"),
        ("risk", "24-26"),
        ("metrics", "29"),
        ("targets", "33-36"),
    ]:
        loc = manifest.locations[tag]
        assert any(
            u.kind == "table_row"
            and f"Requirement: AASB S2 {'para' if requirement in ('6', '29') else 'paras'} {requirement} ("
            in u.text
            and f"Printed page: {loc.printed_page}" in u.text
            for u in parsed[ANNUAL].units
        )


def test_risk_method_subheading(pack):
    _, _, parsed = pack
    assert any(
        u.kind == "section" and u.text == "How we identify and assess climate risks"
        for u in parsed[ANNUAL].units
    )


@pytest.mark.parametrize("position", ["start", "end"])
def test_s5_variants(tmp_path, position):
    manifest = build_pack(tmp_path, s5_position=position)
    parsed = {name: parse_pdf(tmp_path / name) for name in (ANNUAL, SUPPLEMENT)}
    check_invariants(tmp_path, parsed)
    assert_near_misses(tmp_path, manifest, parsed)
    s5 = manifest.locations["s5"].pdf_page
    if position == "start":
        assert 3 < s5 < manifest.locations["sr_start"].pdf_page
    else:
        assert s5 > manifest.locations["financial_end"].pdf_page


def test_key_rejects_missing_scan(pack):
    folder, _, _ = pack
    raw = yaml.safe_load((folder / "answer_key.yaml").read_text(encoding="utf-8"))
    next(c for c in raw["cells"] if c["status"] == "not_found")["scanned"] = []
    with pytest.raises(ValidationError):
        AnswerKey.model_validate(raw)


def body_paragraphs(parsed):
    return [u for doc in parsed.values() for u in doc.units if u.kind == "paragraph"]


def test_text_density(pack):
    _, _, parsed = pack
    for name, doc in parsed.items():
        counts = [
            sum(
                len(u.text)
                for u in doc.units
                if u.pdf_page == page and u.kind in ("paragraph", "table_row", "section")
            )
            for page in range(1, doc.page_count + 1)
        ]
        if name == ANNUAL:
            assert all(2500 <= n <= 4500 for n in counts[3:]), counts
            assert sum(counts) >= 90000
        else:
            assert min(counts) >= 1500
            assert sum(counts) >= 12000


def test_no_repeated_prose(pack):
    paragraphs = [normalize_text(u.text) for u in body_paragraphs(pack[2])]
    assert len(paragraphs) == len(set(paragraphs))
    sentences = Counter(s.strip() for p in paragraphs for s in re.split(r"(?<=[.!?])\s+", p))
    assert max(sentences.values()) <= 2


def test_evidence_context_and_distinctiveness(pack):
    folder, _, parsed = pack
    key = validate_locations(folder, parsed)
    paragraphs = body_paragraphs(parsed)
    locations = [e for cell in key.cells for e in cell.evidence]
    locations += [e for issue in key.planted_issues for e in issue.locations]
    for e in locations:
        if e.source_type == "text":
            assert len(e.quote) >= 30
            matches = [u for u in paragraphs if normalize_text(e.quote) in normalize_text(u.text)]
            assert len(matches) == (2 if "Hunter River flooding" in e.quote else 1), e
            assert all(3 <= len(re.split(r"(?<=[.!?])\s+", u.text)) <= 6 for u in matches)
        else:
            rows = [
                u
                for doc in parsed.values()
                for u in doc.units
                if u.kind == "table_row" and e.quote in u.text
            ]
            assert len(rows) == 1, e
            assert re.search(r"[A-Za-z]", e.quote), e


def test_issue_labels_match_cells(pack):
    key = validate_locations(pack[0], pack[2])
    for issue in key.planted_issues:
        for loc in issue.locations:
            matches = [
                e
                for c in key.cells
                for e in c.evidence
                if (e.document, e.pdf_page, e.quote) == (loc.document, loc.pdf_page, loc.quote)
            ]
            assert all(e == loc for e in matches)


def test_no_repeated_eight_word_sequences(pack):
    counts = Counter()
    for unit in (u for doc in pack[2].values() for u in doc.units if u.kind != "table"):
        words = re.sub(r"[^\w\s]", "", unit.text.lower()).split()
        assert not re.search(r"\bworking papers?\b", unit.text, re.IGNORECASE)
        counts.update(zip(*(words[i:] for i in range(8))))
    assert not {phrase: n for phrase, n in counts.items() if n > 3}


def assert_near_misses(folder, manifest, parsed):
    key = validate_locations(folder, parsed)
    part = load_parts_file(folder / "parts.yaml").documents[0].parts[0]
    evidence = [normalize_text(e.quote) for c in key.cells for e in c.evidence]
    for section, heading in manifest.locations.items():
        if not (
            heading.document == ANNUAL
            and part.first_pdf_page <= heading.pdf_page <= part.last_pdf_page
            and section
            in {
                "sr_start",
                "governance",
                "strategy",
                "risk_table_page",
                "exposure",
                "pathway",
                "financial_effects",
                "resilience",
                "risk",
                "metrics",
                "targets",
                "references",
                "important",
                "index",
            }
        ):
            continue
        near = [
            loc
            for tag, loc in manifest.locations.items()
            if tag.startswith("near_miss_") and loc.heading == heading.heading
        ]
        assert len(near) >= 2, section
        for loc in near:
            assert any(
                normalize_text(loc.quote) in normalize_text(u.text)
                for u in parsed[loc.document].units
                if u.pdf_page == loc.pdf_page
            )
            sentences = re.split(r"(?<=[.!?])\s+", normalize_text(loc.quote))
            assert all(not any(q in s or s in q for q in evidence) for s in sentences)


def test_all_sr_sections_have_near_misses(pack):
    assert_near_misses(pack[0], pack[1], pack[2])
    for check in ("G1", "G2", "S1", "S2", "S3", "S4", "S5", "R1", "M1", "M2", "M3", "A1", "A3"):
        assert any(tag.startswith(f"near_miss_{check}_") for tag in pack[1].locations)


@pytest.mark.parametrize(
    "section",
    [
        "governance",
        "strategy",
        "exposure",
        "pathway",
        "financial_effects",
        "resilience",
        "risk",
        "metrics",
        "targets",
        "important",
        "references",
    ],
)
def test_near_miss_paragraphs(pack, section):
    folder, manifest, parsed = pack
    key = validate_locations(folder, parsed)
    heading = manifest.locations[section].heading
    near = [
        loc
        for tag, loc in manifest.locations.items()
        if tag.startswith("near_miss_") and loc.heading == heading
    ]
    assert len(near) >= 2
    evidence = [normalize_text(e.quote) for c in key.cells for e in c.evidence]
    for loc in near:
        assert any(
            normalize_text(loc.quote) in normalize_text(u.text)
            for u in parsed[loc.document].units
            if u.pdf_page == loc.pdf_page
        )
        sentences = re.split(r"(?<=[.!?])\s+", normalize_text(loc.quote))
        assert all(not any(q in s or s in q for q in evidence) for s in sentences)


def test_quotes_meet_verifier_minimum(pack):
    # T10's verifier rejects quotes shorter than 8 characters as too_short.
    folder, _, _ = pack
    key = AnswerKey.model_validate(
        yaml.safe_load((folder / "answer_key.yaml").read_text(encoding="utf-8"))
    )
    quotes = [e.quote for c in key.cells for e in c.evidence]
    quotes += [e.quote for i in key.planted_issues for e in i.locations]
    assert all(len(normalize_text(q)) >= 8 for q in quotes)
