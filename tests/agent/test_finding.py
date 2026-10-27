from uuid import uuid4

import pytest

from cda.agent.finding import SubmitFinding

UID = str(uuid4())


def finding(status="evidence_found", evidence_ids=(UID,), fact_ids=()):
    return SubmitFinding.model_validate(
        {
            "status": status,
            "rationale": "Located",
            "missing_aspects": [],
            "relief_applied": False,
            "relief_id": None,
            "evidence": [
                {"unit_id": u, "quote": "climate risk", "role": "supports"} for u in evidence_ids
            ],
            "facts": [
                {
                    "name": "amount",
                    "value": 1,
                    "unit": None,
                    "scope": None,
                    "year": None,
                    "unit_id": u,
                    "raw": str(i),
                }
                for i, u in enumerate(fact_ids)
            ],
        }
    )


def test_drops_only_uncited_facts():
    f = finding(fact_ids=(UID, str(uuid4()), UID.upper()))
    assert f.drop_uncited_facts() == 1
    assert [x.raw for x in f.facts] == ["0", "2"]  # same UUID in another case still cites


def test_nothing_to_drop():
    f = finding(fact_ids=(UID,))
    assert f.drop_uncited_facts() == 0 and len(f.facts) == 1


@pytest.mark.parametrize("evidence_ids", [(), (UID,)])
def test_no_evidence_status_keeps_its_rule(evidence_ids):
    f = finding(status="no_evidence_found", evidence_ids=evidence_ids, fact_ids=(UID,))
    assert f.drop_uncited_facts() == 0 and len(f.facts) == 1
