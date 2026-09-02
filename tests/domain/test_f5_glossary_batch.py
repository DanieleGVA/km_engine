"""WP-F5 — proposte di glossario: gate umano e invarianti del lotto.

Il valore del gate e' che nessuna voce entri nel pack senza che una persona
l'abbia guardata. Questi test verificano che il codice non offra scorciatoie.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

from app.domain.normalize import normalize_key
from app.domain.pack import load_domain_pack
from scripts.merge_glossary_batch import MergeRefused, _load_proposals, plan_merge
from scripts.propose_glossary_entries import load_batch, suggest_id
from tests.domain.conftest import PACK_DIR, REPO_ROOT

REPORT = REPO_ROOT / "docs" / "coverage" / "04-after-F4.json"


def _write(tmp_path: pathlib.Path, payload: dict) -> pathlib.Path:
    path = tmp_path / "proposals.yaml"
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Gate umano
# ---------------------------------------------------------------------------

def test_f5_pending_batch_is_refused(tmp_path) -> None:
    """Un lotto non ancora rivisto non entra nel pack."""
    path = _write(
        tmp_path,
        {
            "status": "pending_human_review",
            "entries": [{"id": "ING-X", "labels_en": "x", "labels_it": "x"}],
        },
    )
    with pytest.raises(MergeRefused, match="pending_human_review"):
        _load_proposals(path)


def test_f5_approved_batch_is_accepted(tmp_path) -> None:
    path = _write(
        tmp_path,
        {
            "status": "approved",
            "entries": [{"id": "ING-X", "labels_en": "x", "labels_it": "x"}],
        },
    )
    assert len(_load_proposals(path)) == 1


def test_f5_empty_batch_is_refused(tmp_path) -> None:
    path = _write(tmp_path, {"status": "approved", "entries": []})
    with pytest.raises(MergeRefused, match="nessuna voce"):
        _load_proposals(path)


# ---------------------------------------------------------------------------
# Invarianti del merge
# ---------------------------------------------------------------------------

def test_f5_duplicate_of_becomes_an_alias(pack) -> None:
    """Una variante non diventa una voce nuova: diventa un alias."""
    proposals = [
        {
            "source_term": "olio extra vergine d'oliva",
            "duplicate_of": "ING-OLIVE-OIL",
            "labels_en": "extra virgin olive oil",
            "labels_it": "olio extravergine di oliva",
        }
    ]
    new_entries, new_aliases, refused = plan_merge(pack, proposals)
    assert new_entries == []
    assert new_aliases == [("ING-OLIVE-OIL", "olio extra vergine d'oliva")]
    assert refused == []


def test_f5_duplicate_key_is_refused(pack) -> None:
    """Due voci con la stessa chiave normalizzata renderebbero il lookup cieco."""
    proposals = [
        {
            "source_term": "aglio",
            "id": "ING-GARLIC-2",
            "labels_en": "garlic bulb",
            "labels_it": "aglio",
            "aliases": [],
        }
    ]
    new_entries, _, refused = plan_merge(pack, proposals)
    assert new_entries == []
    assert refused and "alias" in refused[0]


def test_f5_unknown_broader_than_is_refused(pack) -> None:
    proposals = [
        {
            "source_term": "brodo di carne",
            "id": "ING-MEAT-BROTH",
            "labels_en": "meat broth",
            "labels_it": "brodo di carne",
            "broader_than": "ING-DOES-NOT-EXIST",
        }
    ]
    new_entries, _, refused = plan_merge(pack, proposals)
    assert new_entries == []
    assert refused and "broader_than" in refused[0]


def test_f5_new_entry_is_accepted_with_the_source_term_as_alias(pack) -> None:
    proposals = [
        {
            "source_term": "brodo di cappone",
            "id": "ING-CAPON-BROTH",
            "labels_en": "capon broth",
            "labels_it": "brodo di cappone",
            "aliases": [],
            "broader_than": "ING-VEGETABLE-BROTH",
        }
    ]
    new_entries, _, refused = plan_merge(pack, proposals)
    assert refused == []
    assert len(new_entries) == 1
    assert new_entries[0]["id"] == "ING-CAPON-BROTH"
    assert "brodo di cappone" in new_entries[0]["aliases"]
    assert new_entries[0]["broader_than"] == "ING-VEGETABLE-BROTH"


def test_f5_clash_message_names_the_offending_term(pack) -> None:
    """Il rifiuto deve dire QUALE termine e' gia' preso, non solo che lo e'.

    Caso reale: "brodo di carne" non e' raggiungibile dall'italiano, ma la sua
    traduzione "meat broth" e' gia' la labels_en di ING-DICT-0994. Senza il
    termine nel messaggio si cerca il conflitto dalla parte sbagliata.
    """
    proposals = [
        {
            "source_term": "brodo di carne",
            "id": "ING-MEAT-BROTH",
            "labels_en": "meat broth",
            "labels_it": "brodo di carne",
        }
    ]
    _, _, refused = plan_merge(pack, proposals)
    assert refused
    assert "meat broth" in refused[0]


# ---------------------------------------------------------------------------
# Invarianti del glossario committato
# ---------------------------------------------------------------------------

# Chiavi contese gia' presenti nel glossario committato prima di WP-F5: sono
# quasi tutte voci ING-DICT-* generate da dizionario con labels_it uguale a
# labels_en (l'italiano non e' mai stato riempito), quindi il termine sorgente
# resta irraggiungibile. Il numero e' fissato qui perche' non cresca: F5 deve
# ridurlo, mai aumentarlo.
KNOWN_DUPLICATE_KEYS = 356


def test_f5_glossary_no_new_duplicate_keys(pack) -> None:
    """Le chiavi contese non aumentano: due voci per la stessa chiave rendono
    il lookup non deterministico (vince la piu' lunga, arbitrariamente)."""
    owner: dict[str, str] = {}
    clashes: set[str] = set()
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            if not key:
                continue
            previous = owner.setdefault(key, entry.id)
            if previous != entry.id:
                clashes.add(f"{key!r}: {previous} vs {entry.id}")
    assert len(clashes) <= KNOWN_DUPLICATE_KEYS, (
        f"{len(clashes)} chiavi contese (erano {KNOWN_DUPLICATE_KEYS}): "
        f"{sorted(clashes)[:10]}"
    )


def test_f5_merge_never_adds_a_duplicate_key(pack) -> None:
    """Qualunque cosa dica l'LLM, il merge non aggiunge una chiave gia' presa."""
    taken = next(iter(pack.glossaries.ingredienti.entries)).labels_en
    proposals = [
        {
            "source_term": "qualcosa",
            "id": "ING-BRAND-NEW",
            "labels_en": taken,
            "labels_it": "qualcosa",
        }
    ]
    new_entries, _, refused = plan_merge(pack, proposals)
    assert new_entries == []
    assert refused


def test_f5_broader_than_resolves(pack) -> None:
    """Ogni ``broader_than`` punta a un id esistente (lo impone il pack)."""
    ids = {entry.id for entry in pack.glossary_entries()}
    for entry in pack.glossary_entries():
        if entry.broader_than:
            assert entry.broader_than in ids, entry.id


def test_f5_broken_broader_than_fails_pack_load(tmp_path) -> None:
    """Un pack con una gerarchia rotta non si carica: fallisce presto."""
    import shutil

    from app.domain.errors import DomainPackValidationError

    pack_dir = tmp_path / "ricette"
    shutil.copytree(PACK_DIR, pack_dir)
    path = pack_dir / "glossari" / "ingredienti.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["entries"][0]["broader_than"] = "ING-NOPE"
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    with pytest.raises(DomainPackValidationError):
        load_domain_pack(pack_dir)


# ---------------------------------------------------------------------------
# Selezione del lotto
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not REPORT.is_file(), reason="report di copertura assente")
def test_f5_batch_is_taken_by_frequency() -> None:
    """Il lotto parte dai termini che pesano di piu': prima le 121 righe."""
    batch = load_batch(REPORT, batch=10, offset=0)
    assert len(batch) == 10
    counts = [term["count"] for term in batch]
    assert counts == sorted(counts, reverse=True)
    assert batch[0]["term"] == "brodo di carne"

    second = load_batch(REPORT, batch=10, offset=10)
    assert {term["term"] for term in batch} & {term["term"] for term in second} == set()


def test_f5_suggest_id_is_unique_and_kebab() -> None:
    used: set[str] = set()
    first = suggest_id("meat broth", used)
    assert first == "ING-MEAT-BROTH"
    used.add(first)
    assert suggest_id("meat broth", used) == "ING-MEAT-BROTH-2"
