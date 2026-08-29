"""WP8 — Gate G9: suite E2E completa (flusso unico end-to-end).

Un unico test che percorre l'intero ciclo di vita del prototipo sullo stack
dev reale (container ``km-neo4j`` + ``km-postgres``, app FastAPI vera):

1. bootstrap admin (idempotente) → login via API
2. ingestione del corpus di esempio (tests/fixtures/wp4_corpus): job code + document
3. query entità / fatti / ricerca con filtro visibilità (viewer vs admin)
4. rilevamento conflitto (2 fatti conflittuali da sorgenti diverse)
5. workflow approve (invalida il fatto perdente) e reject (grafo invariato)
6. invalidazione sorgente con propagazione ai fatti dipendenti (truth-maintenance)
7. verifica audit log (RESOLVE + INVALIDATE_SOURCE)

Tutti i dati hanno prefisso ``e2e_`` e vengono rimossi in teardown.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import bootstrap_admin
from app.conflict.detection import detect_conflicts_for_entity
from app.ingest.config import IngestSettings
from app.ingest.pipeline import IngestPipeline
from app.ingest.semantic import StubSemanticService
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility

from .conftest import (
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    PREFIX,
    VIEWER_PASSWORD,
    auth_header,
    login,
)

CORPUS = Path(__file__).parent.parent / "fixtures" / "wp4_corpus"


def create_source(
    client: Neo4jClient,
    source_id: str,
    *,
    uri: str | None = None,
    ingested_at: datetime | None = None,
) -> None:
    """Create/refresh a Source node (provenance per i fatti)."""
    with client.session() as session:
        session.run(
            """
            MERGE (s:Source {id: $id})
            SET s.uri = $uri,
                s.type = 'file',
                s.hash = $hash,
                s.language = 'en',
                s.ingested_at = $ingested_at
            """,
            id=source_id,
            uri=uri or f"{PREFIX}uri_{source_id}",
            hash=f"{PREFIX}hash_{source_id}",
            ingested_at=ingested_at or datetime.now(UTC),
        )


def link_fact_to_fact(
    client: Neo4jClient, dependent_fact_id: str, parent_fact_id: str
) -> None:
    """Link a dependent Fact to a parent Fact through DERIVED_FROM."""
    with client.session() as session:
        session.run(
            """
            MATCH (d:Fact {id: $dependent_id})
            MATCH (p:Fact {id: $parent_id})
            MERGE (d)-[:DERIVED_FROM]->(p)
            """,
            dependent_id=dependent_fact_id,
            parent_id=parent_fact_id,
        )


def link_derived_from(
    client: Neo4jClient, fact_id: str, source_id: str
) -> None:
    """Link a Fact to a Source through DERIVED_FROM (provenance)."""
    with client.session() as session:
        session.run(
            """
            MATCH (f:Fact {id: $fact_id})
            MATCH (s:Source {id: $source_id})
            MERGE (f)-[:DERIVED_FROM]->(s)
            """,
            fact_id=fact_id,
            source_id=source_id,
        )


def test_end_to_end_flow(
    client: TestClient,
    conn,
    repo: GraphRepository,
    neo4j_client: Neo4jClient,
    settings,
    tmp_path: Path,
) -> None:
    """Flusso unico end-to-end (gate G9)."""

    # ================================================================= STEP 1
    # Bootstrap admin (idempotente) + login via API
    # =================================================================
    result = bootstrap_admin(conn, settings)
    assert result["created"] is True, result
    again = bootstrap_admin(conn, settings)
    assert again["created"] is False, "bootstrap deve essere idempotente"
    assert again["repaired"] is False

    admin_token = login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert admin_token

    # ================================================================= STEP 2
    # Ingestione del corpus di esempio: job code + job document
    # =================================================================
    ingest_settings = IngestSettings(
        chunk_size=2, cache_dir=tmp_path / "km_ingest_cache"
    )
    pipeline = IngestPipeline(
        repo=repo,
        client=neo4j_client,
        conn=conn,
        settings=ingest_settings,
        semantic_service=StubSemanticService(),
        prefix=PREFIX,
    )
    job_code = pipeline.run(
        "e2e://corpus/code", CORPUS, job_type="code", chunk_size=2
    )
    assert job_code.status == "completed", job_code.error
    assert job_code.progress == 100
    job_doc = pipeline.run(
        "e2e://corpus/docs", CORPUS, job_type="document", chunk_size=1
    )
    assert job_doc.status == "completed", job_doc.error

    # ================================================================= STEP 3
    # Query entità / fatti / ricerca con visibilità
    # =================================================================
    resp = client.get("/api/v1/entities", headers=auth_header(admin_token))
    assert resp.status_code == 200
    e2e_entities = [e for e in resp.json() if e["id"].startswith(PREFIX)]
    labels = {e["label"] for e in e2e_entities}
    assert "Calculator" in labels, "entità dal job code mancante"
    assert "add()" in labels, "entità dal job code mancante"
    assert "WP4 Notes" in labels, "entità dal job document mancante"
    assert len(e2e_entities) >= 10

    # fatti di un'entità ingerita
    calc = next(e for e in e2e_entities if e["label"] == "Calculator")
    resp = client.get(
        f"/api/v1/entities/{calc['id']}/facts", headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    fact_props = {f["property"] for f in resp.json()}
    assert "label" in fact_props

    # ricerca full-text
    resp = client.get(
        "/api/v1/search", params={"q": "Calculator"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    assert any(r["id"].startswith(PREFIX) for r in resp.json())

    # --- visibilità: viewer (team e2e_team_a) vs admin ---
    from app.auth.users import create_user

    create_user(
        conn,
        f"{PREFIX}viewer",
        f"{PREFIX}viewer@example.test",
        VIEWER_PASSWORD,
        roles=("viewer",),
        teams=(f"{PREFIX}team_a",),
    )
    repo.create_entity(
        entity_id=f"{PREFIX}public_entity",
        label="E2EPublic",
        type="code",
        visibility=Visibility(is_public=True),
    )
    repo.create_entity(
        entity_id=f"{PREFIX}team_entity",
        label="E2ETeam",
        type="code",
        visibility=Visibility(teams=(f"{PREFIX}team_a",)),
    )
    repo.create_entity(
        entity_id=f"{PREFIX}private_entity",
        label="E2EPrivate",
        type="code",
        visibility=Visibility(roles=("admin",)),
    )

    viewer_token = login(client, f"{PREFIX}viewer", VIEWER_PASSWORD)
    resp = client.get("/api/v1/entities", headers=auth_header(viewer_token))
    assert resp.status_code == 200
    viewer_ids = {e["id"] for e in resp.json()}
    assert f"{PREFIX}public_entity" in viewer_ids
    assert f"{PREFIX}team_entity" in viewer_ids, "viewer deve vedere il proprio team"
    assert f"{PREFIX}private_entity" not in viewer_ids, "default-deny per admin-only"

    resp = client.get("/api/v1/entities", headers=auth_header(admin_token))
    admin_ids = {e["id"] for e in resp.json()}
    assert f"{PREFIX}private_entity" in admin_ids, "admin bypassa la visibilità"

    # ricerca con visibilità: il viewer non trova l'entità admin-only
    resp = client.get(
        "/api/v1/search", params={"q": "E2EPrivate"}, headers=auth_header(viewer_token)
    )
    assert resp.status_code == 200
    assert all(not r["id"].startswith(f"{PREFIX}private") for r in resp.json())

    # ================================================================= STEP 4
    # Rilevamento conflitto: 2 fatti conflittuali da sorgenti diverse
    # =================================================================
    repo.create_entity(
        entity_id=f"{PREFIX}conflict_entity",
        label="E2EConflict",
        type="code",
        visibility=Visibility(is_public=True),
    )
    create_source(neo4j_client, f"{PREFIX}src_a", uri=f"{PREFIX}uri_a")
    create_source(neo4j_client, f"{PREFIX}src_b", uri=f"{PREFIX}uri_b")
    repo.create_fact(
        fact_id=f"{PREFIX}fact_a",
        entity_id=f"{PREFIX}conflict_entity",
        property="version",
        value="1.0",
        source_id=f"{PREFIX}src_a",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}fact_b",
        entity_id=f"{PREFIX}conflict_entity",
        property="version",
        value="2.0",
        source_id=f"{PREFIX}src_b",
    )
    created = detect_conflicts_for_entity(repo, conn, f"{PREFIX}conflict_entity")
    assert len(created) >= 1, "nessun conflitto rilevato"
    conflict = created[0]
    assert conflict["status"] == "pending"
    assert conflict["suggestion"], "manca il suggerimento automatico (Q10)"

    # il conflitto è visibile via API
    resp = client.get(
        "/api/v1/conflicts", params={"status": "pending"}, headers=auth_header(admin_token)
    )
    assert resp.status_code == 200
    pending = [c for c in resp.json() if c["entity_id"] == f"{PREFIX}conflict_entity"]
    assert len(pending) == 1

    # ================================================================= STEP 5
    # Workflow: approve (invalida il fatto perdente) + reject (grafo invariato)
    # =================================================================
    resp = client.post(
        f"/api/v1/conflicts/{conflict['id']}/approve",
        json={"choice": "a"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    # il fatto perdente (b) è invalidato nel grafo; il vincente (a) resta valido
    history_b = repo.get_fact_history(f"{PREFIX}fact_b")
    assert history_b, "storico del fatto perdente mancante"
    assert history_b[-1]["status"] == "obsolete"
    assert history_b[-1]["valid_to"] is not None
    assert repo.get_fact(f"{PREFIX}fact_a")["status"] == "valid"

    # secondo conflitto → reject (nessuna modifica al grafo)
    repo.create_entity(
        entity_id=f"{PREFIX}reject_entity",
        label="E2EReject",
        type="code",
        visibility=Visibility(is_public=True),
    )
    repo.create_fact(
        fact_id=f"{PREFIX}reject_fact_a",
        entity_id=f"{PREFIX}reject_entity",
        property="mode",
        value="fast",
        source_id=f"{PREFIX}src_a",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}reject_fact_b",
        entity_id=f"{PREFIX}reject_entity",
        property="mode",
        value="safe",
        source_id=f"{PREFIX}src_b",
    )
    created_rej = detect_conflicts_for_entity(repo, conn, f"{PREFIX}reject_entity")
    assert len(created_rej) >= 1
    rej = created_rej[0]
    resp = client.post(
        f"/api/v1/conflicts/{rej['id']}/reject", headers=auth_header(admin_token)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"
    assert repo.get_fact(f"{PREFIX}reject_fact_a")["status"] == "valid"
    assert repo.get_fact(f"{PREFIX}reject_fact_b")["status"] == "valid"

    # ================================================================= STEP 6
    # Invalidazione sorgente con propagazione (truth-maintenance, FR7)
    # =================================================================
    repo.create_entity(
        entity_id=f"{PREFIX}inv_entity",
        label="E2EInv",
        type="code",
        visibility=Visibility(is_public=True),
    )
    create_source(neo4j_client, f"{PREFIX}src_inv", uri=f"{PREFIX}uri_inv")
    repo.create_fact(
        fact_id=f"{PREFIX}inv_fact",
        entity_id=f"{PREFIX}inv_entity",
        property="status",
        value="active",
        source_id=f"{PREFIX}src_inv",
        confidence="EXTRACTED",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}inv_dep",
        entity_id=f"{PREFIX}inv_entity",
        property="derived",
        value="derived-value",
        confidence="INFERRED",
    )
    link_fact_to_fact(neo4j_client, f"{PREFIX}inv_dep", f"{PREFIX}inv_fact")
    link_derived_from(neo4j_client, f"{PREFIX}inv_fact", f"{PREFIX}src_inv")

    resp = client.post(
        f"/api/v1/sources/{PREFIX}src_inv/invalidate",
        json={"reason": "e2e: source changed"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert f"{PREFIX}inv_fact" in body["invalidated_facts"]
    assert f"{PREFIX}inv_dep" in body["under_review_facts"]
    assert repo.get_fact(f"{PREFIX}inv_fact") is None, "fatto diretto deve essere chiuso"
    assert repo.get_fact(f"{PREFIX}inv_dep")["status"] == "under_review"

    # ================================================================= STEP 7
    # Verifica audit log (FR5.2): RESOLVE + INVALIDATE_SOURCE
    # =================================================================
    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, entity_id, entity_type FROM audit_log "
            "WHERE entity_id LIKE %s ORDER BY id",
            (f"{PREFIX}%",),
        )
        rows = cur.fetchall()
    actions = {r[0] for r in rows}
    assert "INVALIDATE_SOURCE" in actions, "audit invalidazione mancante"
    assert any(
        r[0] == "INVALIDATE_SOURCE" and r[1] == f"{PREFIX}src_inv" for r in rows
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT action, entity_id, entity_type FROM audit_log "
            "WHERE entity_id = %s ORDER BY id",
            (str(conflict["id"]),),
        )
        resolve_rows = cur.fetchall()
    assert any(r[0] == "RESOLVE" for r in resolve_rows), "audit RESOLVE mancante"
