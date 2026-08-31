"""Passo 5 PROGRAMMA-UNICO: standardizzazione batch del dizionario.

Legge term_dictionary.jsonl (passo 3), standardizza in batch di 50 voci con
la primitiva judge() (temperatura 0, JSON-mode), consolida in modo
deterministico e scrive le proposte (mai applicate: decide l'umano, P5).

Uso:
    uv run python scripts/standardize_terms.py --dict term_dictionary.jsonl \
        --out proposals.jsonl [--limit 100]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib

from app.domain.llm import HttpLLMClient
from app.domain.standardize import (
    BATCH_SIZE,
    consolidate,
    standardize_batch,
)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dict", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    entries = [
        json.loads(line) for line in args.dict.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        entries = entries[: args.limit]

    llm = HttpLLMClient()
    proposals = []
    for start in range(0, len(entries), BATCH_SIZE):
        batch = entries[start : start + BATCH_SIZE]
        batch_proposals = await standardize_batch(llm, batch)
        proposals.extend(batch_proposals)
        print(f"batch {start // BATCH_SIZE + 1}: {len(batch_proposals)} proposte")

    report = consolidate(proposals)
    args.out.write_text(
        "\n".join(
            json.dumps(p.model_dump(by_alias=True), ensure_ascii=False, sort_keys=True)
            for p in report.proposals
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "input": len(entries),
        "proposals": len(report.proposals),
        "collisions": len(report.collisions),
        "same_as": len(report.same_as),
        "incoherent": len(report.incoherent),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
