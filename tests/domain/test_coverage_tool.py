"""WP-F0 — lo strumento di misura della copertura glossario.

Il gate di ogni WP della serie F si legge da qui: ``EXPECTED_COVERAGE`` e'
il valore misurato sul corpus ``corpus_marchesi_full`` con il codice corrente
e va aggiornato (con il commit che lo cambia) a ogni WP. Se un WP alza la
copertura senza aggiornare questa costante il test fallisce: e' voluto, la
misura e' il contratto.
"""
from __future__ import annotations

import json

import pytest

from app.domain.coverage import (
    TrigramIndex,
    measure_coverage,
    measure_documents,
    trigram_similarity,
)
from tests.domain.conftest import REPO_ROOT

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"

# F0 0.4795 -> F1 0.6518 -> F2 0.6671 -> F3 0.6800 -> F4 0.7376 (resolver).
EXPECTED_COVERAGE = 0.7376
COVERAGE_TOLERANCE = 0.005
EXPECTED_LINES = 10892
EXPECTED_DOCS = 1462


@pytest.fixture(scope="module")
def corpus_report(pack):
    return measure_coverage(pack, CORPUS_DIR)


def test_coverage_baseline_corpus_marchesi(corpus_report) -> None:
    """La copertura reale del corpus, non quella dichiarata sui 93 termini."""
    assert corpus_report.docs_total == EXPECTED_DOCS
    assert corpus_report.docs_parsed == EXPECTED_DOCS
    assert corpus_report.parse_errors == []
    assert corpus_report.lines_total == EXPECTED_LINES
    assert corpus_report.coverage == pytest.approx(
        EXPECTED_COVERAGE, abs=COVERAGE_TOLERANCE
    )
    assert (
        corpus_report.lines_resolved + corpus_report.unresolved_lines
        == corpus_report.lines_total
    )
    assert sum(corpus_report.by_rule.values()) == corpus_report.lines_total


def test_coverage_unresolved_sorted_with_candidates(corpus_report) -> None:
    """Gli irrisolti sono ordinati per frequenza e portano i candidati vicini."""
    unresolved = corpus_report.unresolved
    assert unresolved, "il corpus ha ancora termini irrisolti"
    counts = [term.count for term in unresolved]
    assert counts == sorted(counts, reverse=True)

    first = unresolved[0]
    assert first.term == "brodo di carne"
    assert first.count == 121
    assert first.examples
    assert first.candidates
    best_key, best_score = first.candidates[0]
    assert best_key == "brodo di pesce"
    assert best_score > 0.4

    # D2 chiuso in WP-F1: le tre forme che dominavano il residuo di F0
    # (olio extravergine, sale e pepe, d'aglio) non sono piu' irrisolte.
    terms = {term.term for term in unresolved}
    assert "olio extravergine di oliva" not in terms
    assert "sale e pepe" not in terms
    assert "aglio" not in terms


def test_f4_by_rule_reports_every_level(corpus_report) -> None:
    """Il report dice QUALE livello ha risolto: senza questo il numero e' cieco."""
    by_rule = corpus_report.by_rule
    assert by_rule["GLOSS-EXACT"] > 0
    assert by_rule["GLOSS-ALIAS"] > 0
    assert by_rule["GLOSS-HEAD"] > 0
    assert by_rule["GLOSS-UNRESOLVED"] > 0
    resolved = sum(
        count for rule, count in by_rule.items() if rule != "GLOSS-UNRESOLVED"
    )
    assert resolved == corpus_report.lines_resolved

    # Il livello fuzzy e' conservativo per costruzione (soglia 0.92 con
    # margine): su questo corpus non scatta mai, e comunque non puo' superare
    # il 3% delle righe (gate GF4).
    fuzzy = by_rule.get("GLOSS-FUZZY", 0)
    assert fuzzy / corpus_report.lines_total <= 0.03


def test_coverage_report_json_roundtrip(corpus_report, tmp_path) -> None:
    """``to_json``/``write_json`` producono un report rileggibile."""
    path = corpus_report.write_json(tmp_path / "report.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lines_total"] == corpus_report.lines_total
    assert payload["coverage"] == pytest.approx(corpus_report.coverage, abs=1e-4)
    assert len(payload["unresolved"]) == len(corpus_report.unresolved)
    assert payload["unresolved"][0]["candidates"][0]["score"] > 0.0


def test_coverage_measure_documents_matches_directory(pack, corpus_report) -> None:
    """La misura in memoria e quella su directory danno lo stesso numero."""
    documents = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("*.md"))[:50]
    }
    report = measure_documents(pack, documents)
    assert report.docs_total == 50
    assert report.lines_total > 0
    assert 0.0 <= report.coverage <= 1.0
    assert report.corpus_dir == "<memory>"


def test_trigram_similarity_is_symmetric_and_bounded() -> None:
    assert trigram_similarity("aglio", "aglio") == 1.0
    assert trigram_similarity("aglio", "olio") == trigram_similarity("olio", "aglio")
    assert trigram_similarity("aglio", "") == 0.0
    assert 0.0 < trigram_similarity("prezzemolo", "prezemolo") < 1.0


def test_trigram_index_matches_bruteforce() -> None:
    """L'indice invertito e' un'ottimizzazione, non una semantica diversa."""
    keys = ["aglio", "olio d'oliva", "sale e pepe", "prezzemolo", "burro"]
    index = TrigramIndex(keys)
    for term in ("aglio fresco", "sale pepe", "prezemolo"):
        expected = sorted(
            ((key, round(trigram_similarity(term, key), 4)) for key in keys),
            key=lambda pair: (-pair[1], pair[0]),
        )
        expected = [pair for pair in expected if pair[1] > 0.0][:3]
        assert index.top(term, 3) == expected
