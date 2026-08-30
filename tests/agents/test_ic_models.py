"""WP-C1 schema tests: DomainBrief and AgentReport validate strictly."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.models import (
    AgentReport,
    CandidateEntity,
    DomainBrief,
    OntologyCandidate,
    UnitObservation,
    Vocabulary,
)


def _valid_brief() -> dict:
    return {
        "domain": "ricette",
        "language": "it",
        "canonical_language": "en",
        "version": "1.0.0",
        "corpus_size": 1,
        "entities": [
            {
                "term": "spaghetti",
                "source_terms": ["spaghetti"],
                "frequency": 1,
                "kind": "ingredient",
            }
        ],
        "vocabularies": [
            {
                "name": "ingredienti",
                "entries": [
                    {
                        "term": "spaghetti",
                        "source_terms": ["spaghetti"],
                        "frequency": 1,
                        "kind": "ingredient",
                    }
                ],
            }
        ],
        "units": [{"unit": "g", "frequency": 1}],
        "ambiguities": [],
        "ontologies": [{"prefix": "foodon", "uri": "http://purl.obolibrary.org/obo/FOODON_"}],
    }


def test_ic_domain_brief_validates() -> None:
    brief = DomainBrief.model_validate(_valid_brief())
    assert brief.domain == "ricette"
    assert brief.entity_map()["spaghetti"].frequency == 1
    assert brief.vocabulary("ingredienti") is not None


def test_ic_domain_brief_rejects_unknown_field() -> None:
    payload = _valid_brief()
    payload["surprise"] = True
    with pytest.raises(ValidationError):
        DomainBrief.model_validate(payload)


def test_ic_domain_brief_rejects_empty_domain() -> None:
    payload = _valid_brief()
    payload["domain"] = "  "
    with pytest.raises(ValidationError):
        DomainBrief.model_validate(payload)


def test_ic_candidate_entity_rejects_bad_kind() -> None:
    with pytest.raises(ValidationError):
        CandidateEntity(term="x", kind="verb")


def test_ic_candidate_entity_rejects_zero_frequency() -> None:
    with pytest.raises(ValidationError):
        CandidateEntity(term="x", frequency=0)


def test_ic_agent_report_validates_status() -> None:
    report = AgentReport(agent="codegen", status="ok", metrics={"a": 1})
    assert report.status == "ok"
    with pytest.raises(ValidationError):
        AgentReport(agent="codegen", status="maybe")


def test_ic_models_roundtrip_json() -> None:
    brief = DomainBrief.model_validate(_valid_brief())
    restored = DomainBrief.model_validate_json(brief.model_dump_json())
    assert restored == brief


def test_ic_unit_observation_and_vocabulary() -> None:
    unit = UnitObservation(unit="g", frequency=3)
    vocab = Vocabulary(name="ingredienti", entries=[])
    ontology = OntologyCandidate(prefix="dbpedia", uri="http://dbpedia.org/resource/")
    assert unit.frequency == 3
    assert vocab.name == "ingredienti"
    assert ontology.prefix == "dbpedia"
