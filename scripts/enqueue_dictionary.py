"""Passo 6 PROGRAMMA-UNICO: enqueue delle proposte dizionario in adjudications.

Legge proposals.jsonl (passo 5) e crea una riga adjudications per voce con
kind='dictionary' e verdict_json = proposta. Idempotente: una voce gia'
accodata (stesso document_id + kind) non viene ri-accodata.

Uso:
    uv run python scripts/enqueue_dictionary.py --proposals proposals.jsonl
"""
from __future__ import annotations

import argparse
import json
import pathlib

import psycopg

from app.domain.verify import create_adjudication, list_adjudications


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--proposals", required=True, type=pathlib.Path)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    dsn = args.dsn or "postgresql://km:km_dev_password@localhost:5432/km_engine"
    conn = psycopg.connect(dsn, autocommit=True)

    proposals = [
        json.loads(line) for line in args.proposals.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    existing = {
        a["document_id"]
        for a in list_adjudications(conn, status="pending")
        if a.get("kind") == "dictionary"
    }
    enqueued = skipped = 0
    for p in proposals:
        key = p["key"]
        if key in existing:
            skipped += 1
            continue
        create_adjudication(
            conn,
            document_id=key,
            section="dictionary",
            reason=f"standardizzazione dizionario ({p['corpus']})",
            suggestion=p.get("canonical_name_en"),
            kind="dictionary",
            verdict_json=p,
            llm_model="judge",
            llm_confidence=p.get("confidence"),
        )
        enqueued += 1
    print(json.dumps({"proposals": len(proposals), "enqueued": enqueued,
                      "skipped": skipped}, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
