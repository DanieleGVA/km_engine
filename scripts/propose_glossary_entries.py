#!/usr/bin/env python3
"""Proposte di glossario per il residuo irrisolto (WP-F5).

Legge il report di copertura (``scripts/measure_coverage.py --out``), prende un
lotto di termini irrisolti in ordine di frequenza e chiede all'LLM una voce di
glossario per ciascuno. **Non scrive mai dentro ``domain-packs/ricette``**:
l'output e' un file di proposte in staging, che una persona approva prima che
``scripts/merge_glossary_batch.py`` lo fonda nel pack.

Il gate umano non e' aggirabile: e' l'unico punto in cui qualcuno decide che
"brodo di carne" e' una voce nuova e non un alias di "brodo vegetale".

Uso:
  # 1. anteprima del lotto (nessuna chiamata all'LLM, nessun costo)
  uv run python scripts/propose_glossary_entries.py --from docs/coverage/04-after-F4.json --batch 50

  # 2. generazione delle proposte (richiede KM_LLM_* configurate)
  uv run python scripts/propose_glossary_entries.py \
      --from docs/coverage/04-after-F4.json --batch 50 --offset 0 --generate \
      --out domain-packs/ricette-agents-draft/glossari/ingredienti.proposals.yaml

  # 3. revisione umana del file, poi:
  uv run python scripts/merge_glossary_batch.py --approved <file>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys

import yaml

from app.domain.llm import HttpLLMClient, LLMClient
from app.domain.pack import load_domain_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"
DEFAULT_REPORT = REPO_ROOT / "docs" / "coverage" / "04-after-F4.json"
DEFAULT_OUT = (
    REPO_ROOT / "domain-packs" / "ricette-agents-draft" / "glossari"
    / "ingredienti.proposals.yaml"
)

_SLUG_RE = re.compile(r"[^A-Z0-9]+")

SYSTEM_PROMPT = (
    "Sei un lessicografo di cucina italiana. Ricevi termini di ingrediente "
    "estratti da un ricettario e devi proporre la voce di glossario "
    "corrispondente. Non inventare: se il termine e' gia' coperto da una voce "
    "esistente, dillo con duplicate_of invece di crearne una nuova."
)


def _prompt(term: str, examples: list[str], existing: list[str]) -> str:
    return (
        f"Termine italiano da mappare: {term!r}\n"
        f"Righe del ricettario in cui compare:\n"
        + "\n".join(f"  - {example}" for example in examples[:3])
        + "\n\nVoci di glossario gia' esistenti (labels_en), non duplicarle:\n"
        + "\n".join(f"  - {label}" for label in existing)
        + "\n\nRispondi con un solo oggetto JSON con queste chiavi:\n"
        '{"id": "ING-<EN-UPPER-KEBAB>", "labels_en": "...", '
        '"labels_it": "...", "aliases": ["..."], "definition": "...", '
        '"ontology_uri": null, "broader_than": null, "duplicate_of": null}\n'
        "duplicate_of: l'id della voce esistente se il termine ne e' solo una "
        "variante (allora diventera' un alias, non una voce nuova).\n"
        "broader_than: l'id della voce piu' generale, se esiste "
        '(es. "brodo di carne" ha broader_than "ING-BROTH").'
    )


def suggest_id(label_en: str, used: set[str]) -> str:
    """``ING-<EN-UPPER-KEBAB>`` univoco."""
    base = _SLUG_RE.sub("-", label_en.upper()).strip("-") or "TERM"
    entry_id = f"ING-{base}"
    suffix = 2
    while entry_id in used:
        entry_id = f"ING-{base}-{suffix}"
        suffix += 1
    return entry_id


def load_batch(report_path: pathlib.Path, batch: int, offset: int) -> list[dict]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    return payload["unresolved"][offset: offset + batch]


def print_batch(terms: list[dict], residual_lines: int) -> None:
    print(f"{'righe':>6}  {'%res':>6}  termine -> candidati piu' vicini")
    for term in terms:
        share = term["count"] / residual_lines if residual_lines else 0.0
        candidates = ", ".join(
            f"{c['key']} ({c['score']:.2f})" for c in term["candidates"]
        ) or "-"
        print(f"{term['count']:>6}  {share:>6.1%}  {term['term']!r} -> {candidates}")
    covered = sum(term["count"] for term in terms)
    print(
        f"\nlotto: {len(terms)} termini, {covered} righe"
        + (f" ({covered / residual_lines:.1%} del residuo)" if residual_lines else "")
    )


async def _generate(
    llm: LLMClient, terms: list[dict], existing_labels: list[str]
) -> list[dict]:
    from pydantic import BaseModel

    class ProposedEntry(BaseModel):
        id: str
        labels_en: str
        labels_it: str
        aliases: list[str] = []
        definition: str = ""
        ontology_uri: str | None = None
        broader_than: str | None = None
        duplicate_of: str | None = None

    proposals: list[dict] = []
    for term in terms:
        proposal = await llm.judge(
            SYSTEM_PROMPT,
            _prompt(term["term"], term["examples"], existing_labels),
            ProposedEntry,
        )
        proposal["source_term"] = term["term"]
        proposal["occurrences"] = term["count"]
        proposals.append(proposal)
    return proposals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="report", default=str(DEFAULT_REPORT))
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--batch", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument(
        "--generate",
        action="store_true",
        help="chiama l'LLM (default: solo anteprima del lotto, nessun costo)",
    )
    args = parser.parse_args(argv)

    report_path = pathlib.Path(args.report)
    if not report_path.is_file():
        print(
            f"report non trovato: {report_path}\n"
            "Generalo con: uv run python scripts/measure_coverage.py "
            f"--out {report_path}",
            file=sys.stderr,
        )
        return 1

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    residual = payload["lines_total"] - payload["lines_resolved"]
    terms = load_batch(report_path, args.batch, args.offset)
    if not terms:
        print("nessun termine in questo lotto: residuo esaurito")
        return 0

    print_batch(terms, residual)
    if not args.generate:
        print(
            "\n(anteprima: nessuna chiamata all'LLM. Aggiungi --generate per "
            "produrre le proposte.)"
        )
        return 0

    pack = load_domain_pack(args.pack)
    existing_labels = sorted(
        {entry.labels_en for entry in pack.glossaries.ingredienti.entries}
    )
    out_path = pathlib.Path(args.out)
    if out_path.resolve().is_relative_to(pathlib.Path(args.pack).resolve()):
        print(
            "rifiuto di scrivere dentro il pack di produzione: le proposte "
            "vanno in staging e passano dal gate umano",
            file=sys.stderr,
        )
        return 1

    llm = HttpLLMClient()
    proposals = asyncio.run(_generate(llm, terms, existing_labels))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(
            {
                "name": "ingredienti",
                "generated_from": str(report_path),
                "batch": {"offset": args.offset, "size": len(terms)},
                "status": "pending_human_review",
                "entries": proposals,
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    print(f"\nproposte: {out_path} ({len(proposals)} voci) — status pending_human_review")
    print(
        "Rivedile a mano, poi:\n"
        f"  uv run python scripts/merge_glossary_batch.py --approved {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
