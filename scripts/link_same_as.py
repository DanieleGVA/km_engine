"""Passo 10 PROGRAMMA-UNICO: legami SAME_AS cross-corpus nel grafo.

Legge le adjudications kind='dictionary' APPROVATE (Postgres) e crea le
relazioni SAME_AS tra i :CanonicalTerm MSC e libro con lo stesso
(ingredient_core, states). Nessuna SAME_AS senza approved_by: la relazione
porta approved_by/approved_at dal record di adjudication.

Uso:
    uv run python scripts/link_same_as.py
"""
from __future__ import annotations

import json

import psycopg

from app.domain.verify import list_adjudications
from app.storage.client import Neo4jClient


def main() -> int:
    dsn = "postgresql://km:km_dev_password@localhost:5432/km_engine"
    conn = psycopg.connect(dsn, autocommit=True)
    client = Neo4jClient.from_env()
    client.verify_connectivity()

    approved = [
        a for a in list_adjudications(conn, status="approved")
        if a.get("kind") == "dictionary" and a.get("verdict_json")
    ]
    by_core: dict[tuple, list[dict]] = {}
    for a in approved:
        v = a["verdict_json"]
        core = (v.get("ingredient_core", "").casefold(),
                tuple(sorted(s.casefold() for s in v.get("states", []))))
        by_core.setdefault(core, []).append(a)

    linked = 0
    with client.session() as session:
        for core, group in by_core.items():
            msc = [a for a in group if a["verdict_json"]["corpus"] == "msc"]
            books = [a for a in group if a["verdict_json"]["corpus"] == "book"]
            for m in msc:
                for b in books:
                    m_label = m["verdict_json"]["canonical_name_en"]
                    b_label = b["verdict_json"]["canonical_name_en"]
                    # trova/crea i CanonicalTerm per label
                    m_id = _term_id_for_label(session, m_label)
                    b_id = _term_id_for_label(session, b_label)
                    if m_id is None or b_id is None:
                        continue
                    session.run(
                        """
                        MATCH (a:CanonicalTerm {id: $a_id})
                        MATCH (b:CanonicalTerm {id: $b_id})
                        MERGE (a)-[r:SAME_AS]->(b)
                        SET r.approved_by = $approved_by,
                            r.approved_at = $approved_at
                        """,
                        a_id=m_id, b_id=b_id,
                        approved_by=str(m["resolved_by"] or ""),
                        approved_at=m["resolved_at"] or "",
                    )
                    linked += 1
    print(json.dumps({"approved": len(approved), "same_as_linked": linked}))
    conn.close()
    client.close()
    return 0


def _term_id_for_label(session, label: str) -> str | None:
    row = session.run(
        "MATCH (t:CanonicalTerm) WHERE t.label_en = $label "
        "RETURN t.id AS id LIMIT 1",
        label=label,
    ).single()
    return row["id"] if row else None


if __name__ == "__main__":
    raise SystemExit(main())
