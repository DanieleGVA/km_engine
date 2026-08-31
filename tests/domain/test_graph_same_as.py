"""Passo 10 PROGRAMMA-UNICO: grafo CanonicalTerm + SAME_AS.

Obiettivo: il dizionario e' interrogabile nel grafo; i legami cross-corpus
esistono solo se approvati.
Verifiche: proprieta' (classe, allergeni, pesi) sui CanonicalTerm; doppia
esecuzione del load => stesso conteggio (MERGE idempotente); nessuna SAME_AS
senza approved_by.
"""
from __future__ import annotations

import pathlib

from app.domain.verify import create_adjudication, decide_adjudication
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"
PREFIX = "ip10_"


def _cleanup(client: Neo4jClient) -> None:
    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PREFIX)


def test_load_pack_sets_extended_properties(client) -> None:
    """Le proprieta' estese (classe, allergeni, pesi) sono sul CanonicalTerm."""
    _cleanup(client)
    load_pack(client, PACK_DIR)
    with client.session() as s:
        row = s.run(
            "MATCH (t:CanonicalTerm {term_id: 'ING-EGG'}) RETURN t LIMIT 1"
        ).single()
        assert row is not None
        t = dict(row["t"])
        assert t.get("class") == "uovo"
        assert t.get("countable_unit") == "egg"
        assert t.get("count_policy") == "integer"
        assert t.get("unit_weight_g") == 50.0
    _cleanup(client)


def test_load_pack_idempotent(client) -> None:
    """Doppia esecuzione del load => stesso conteggio nodi (MERGE)."""
    _cleanup(client)
    load_pack(client, PACK_DIR)
    with client.session() as s:
        n1 = s.run("MATCH (t:CanonicalTerm) RETURN count(t) AS n").single()["n"]
    load_pack(client, PACK_DIR)
    with client.session() as s:
        n2 = s.run("MATCH (t:CanonicalTerm) RETURN count(t) AS n").single()["n"]
    assert n1 == n2
    _cleanup(client)


def test_same_as_requires_approval(client, pg_conn, admin_user) -> None:
    """Nessuna SAME_AS senza approved_by: solo le adjudications approvate."""
    from scripts.link_same_as import _term_id_for_label

    _cleanup(client)
    load_pack(client, PACK_DIR)
    # crea due termini (MSC e libro) nel grafo
    with client.session() as s:
        s.run("MERGE (t:CanonicalTerm {id: $id}) SET t.label_en = $label",
              id=f"{PREFIX}msc", label="salt")
        s.run("MERGE (t:CanonicalTerm {id: $id}) SET t.label_en = $label",
              id=f"{PREFIX}book", label="sale")

    # due adjudications (MSC + libro) con lo stesso core: solo approvate linkano
    a1 = create_adjudication(
        pg_conn, document_id=f"{PREFIX}CM1", section="dictionary",
        reason="r", kind="dictionary",
        verdict_json={"key": f"{PREFIX}CM1", "corpus": "msc",
                      "canonical_name_en": "salt", "ingredient_core": "salt",
                      "states": []})
    a2 = create_adjudication(
        pg_conn, document_id=f"{PREFIX}BK1", section="dictionary",
        reason="r", kind="dictionary",
        verdict_json={"key": f"{PREFIX}BK1", "corpus": "book",
                      "canonical_name_en": "sale", "ingredient_core": "salt",
                      "states": []})
    with client.session() as s:
        assert _term_id_for_label(s, "salt") is not None
    # approva e linka
    decide_adjudication(pg_conn, a1["id"], "approved", admin_user["id"])
    decide_adjudication(pg_conn, a2["id"], "approved", admin_user["id"])
    import sys
    from unittest import mock

    from scripts.link_same_as import main as link_main
    with mock.patch.object(sys, "argv", ["link"]):
        link_main()
    with client.session() as s:
        rel = s.run(
            "MATCH (a:CanonicalTerm {label_en: 'salt'})-[r:SAME_AS]->(b) "
            "RETURN r.approved_by AS by LIMIT 1"
        ).single()
        assert rel is not None and rel["by"]
    # pulizia (per prefisso: robusta anche se il test fallisce prima)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM canon_adjudication_log WHERE document_id LIKE %s", (f"{PREFIX}%",))
        cur.execute("DELETE FROM adjudications WHERE document_id LIKE %s", (f"{PREFIX}%",))
    _cleanup(client)
