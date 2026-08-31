"""Passo 6 PROGRAMMA-UNICO: coda dizionario + publish.

Obiettivo: nessuna proposta diventa artefatto di pack senza decisione umana
registrata; la pubblicazione e' riproducibile e versionata.
Verifiche: publish con zero approvazioni -> zero modifiche; ogni decisione ha
riga di audit; publish incrementa la versione e il diff corrisponde alle sole
voci approvate; secondo publish senza nuove decisioni e' un no-op; una voce
rejected non compare in nessun artefatto.
"""
from __future__ import annotations

import pathlib

import yaml

from app.domain.verify import (
    create_adjudication,
    decide_adjudication,
)
from scripts.publish_dictionary import _bump_version

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"


def _proposal(key: str, corpus: str, canonical: str, core: str) -> dict:
    return {
        "key": key, "corpus": corpus, "canonical_name_en": canonical,
        "ingredient_core": core, "class": "altro", "aliases": [],
        "allergen_tags": [], "confidence": 0.9, "ambiguous": False,
    }


def test_create_dictionary_adjudication(pg_conn, admin_user) -> None:
    a = create_adjudication(
        pg_conn, document_id="CM00001", section="dictionary",
        reason="standardizzazione dizionario (msc)",
        kind="dictionary", verdict_json=_proposal("CM00001", "msc", "salt", "salt"),
        llm_model="judge", llm_confidence=0.9,
    )
    assert a["kind"] == "dictionary"
    assert a["verdict_json"]["canonical_name_en"] == "salt"
    assert a["llm_confidence"] == 0.9
    # pulizia
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM adjudications WHERE id = %s", (a["id"],))


def test_publish_noop_without_approvals(pg_conn, tmp_path) -> None:
    """Publish con zero approvazioni produce zero modifiche."""
    import sys
    from unittest import mock

    from scripts.publish_dictionary import main as publish_main

    pack = tmp_path / "pack"
    (pack / "glossari").mkdir(parents=True)
    (pack / "glossari" / "ingredienti.yaml").write_text(
        "name: ingredienti\nentries: []\n", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: ricette\nversion: 1.0.0\n", encoding="utf-8")

    with mock.patch.object(sys, "argv", ["publish", "--pack", str(pack)]):
        publish_main()
    assert not (pack / "msc_mapping.yaml").exists()
    assert (pack / "pack.yaml").read_text() == "name: ricette\nversion: 1.0.0\n"


def test_publish_approved_only(pg_conn, admin_user, tmp_path) -> None:
    """Publish: solo le voci approvate negli artefatti; rejected mai."""
    import sys
    from unittest import mock

    from scripts.publish_dictionary import main as publish_main

    pack = tmp_path / "pack"
    (pack / "glossari").mkdir(parents=True)
    (pack / "glossari" / "ingredienti.yaml").write_text(
        "name: ingredienti\nentries: []\n", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: ricette\nversion: 1.0.0\n", encoding="utf-8")

    # pulizia residui di run precedenti (canon_adjudication_log e' cumulativo)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM canon_adjudication_log WHERE document_id IN ('CM00001', 'CM00002')")
        cur.execute("DELETE FROM adjudications WHERE document_id IN ('CM00001', 'CM00002')")

    ok = create_adjudication(
        pg_conn, document_id="CM00001", section="dictionary",
        reason="r", kind="dictionary",
        verdict_json=_proposal("CM00001", "msc", "salt", "salt"))
    bad = create_adjudication(
        pg_conn, document_id="CM00002", section="dictionary",
        reason="r", kind="dictionary",
        verdict_json=_proposal("CM00002", "msc", "pepper", "pepper"))
    decide_adjudication(pg_conn, ok["id"], "approved", admin_user["id"])
    decide_adjudication(pg_conn, bad["id"], "rejected", admin_user["id"])

    with mock.patch.object(sys, "argv", ["publish", "--pack", str(pack)]):
        publish_main()

    mapping = yaml.safe_load((pack / "msc_mapping.yaml").read_text())
    assert mapping == {"CM00001": "salt"}  # rejected mai negli artefatti
    glossary = yaml.safe_load((pack / "glossari" / "ingredienti.yaml").read_text())
    assert len(glossary["entries"]) == 1
    assert glossary["entries"][0]["labels_en"] == "salt"
    assert (pack / "pack.yaml").read_text().find("version: 1.0.1") >= 0

    # secondo publish senza nuove decisioni -> no-op (versione invariata)
    with mock.patch.object(sys, "argv", ["publish", "--pack", str(pack)]):
        publish_main()
    assert (pack / "pack.yaml").read_text().find("version: 1.0.1") >= 0

    # pulizia
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM adjudications WHERE id IN (%s, %s)", (ok["id"], bad["id"]))
        cur.execute("DELETE FROM canon_adjudication_log WHERE document_id IN ('CM00001', 'CM00002')")


def test_bump_version() -> None:
    assert _bump_version("1.0.0") == "1.0.1"
    assert _bump_version("2.3.9") == "2.3.10"
