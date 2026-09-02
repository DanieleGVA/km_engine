#!/usr/bin/env python3
"""Bonifica del corpus fixture: rimuove le dosi "1 pizzico" iniettate (WP-F3b).

Il corpus ``tests/fixtures/corpus_marchesi_full`` e' stato generato da un
estrattore che pretendeva una cifra all'inizio di ogni riga ingrediente (il
vecchio ``verify._INGREDIENT_RE``). Dove il libro non dava una dose,
l'estrattore ne inventava una: ``- 1 pizzico sale``. Sono 2.160 righe, il
19,8% del corpus, e la dose inventata falsava sia la risoluzione dei termini
sia il gate di plausibilita' delle dosi.

Il libro originale usa "pizzico" nove volte in tutto: non e' la sua unita' di
default. Questa bonifica lo rimette a posto in modo deterministico e
riproducibile, leggendo solo il corpus (nessuna sorgente esterna):

  R1  il resto e' gia' una dose      "- 1 pizzico ½ cipolla"  -> "- ½ cipolla"
  R2  pizzico confermato dal testo   "- 1 pizzico sale"       -> invariato
      (spezia da pizzico E il procedimento della ricetta dice "pizzico")
  R3  tutto il resto                 "- 1 pizzico olio"       -> "- q.b. olio"

Idempotente: rieseguirlo non cambia nulla. Reversibile: e' un solo commit di
sole righe ingrediente.

Uso:
  uv run python scripts/build_corpus_fixtures.py            # anteprima
  uv run python scripts/build_corpus_fixtures.py --apply
  uv run python scripts/build_corpus_fixtures.py --apply --report out.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter

from app.domain.errors import ParseError
from app.domain.pack import load_domain_pack
from app.domain.quantities import QTY_RANGE_RE
from app.domain.verify import parse_source_md

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"

INJECTED_RE = re.compile(r"^- 1 pizzico (?P<rest>.+)$")
PROCEDURE_HEADING = "## Procedimento"
PINCH_IN_TEXT_RE = re.compile(r"pizzic", re.IGNORECASE)

# Spezie che in un ricettario si dosano davvero a pizzico. Fuori da questa
# lista un "pizzico" e' certamente dell'estrattore: non si misura a pizzichi
# l'olio per friggere ne' il pangrattato.
PINCH_SPICES = frozenset({
    "sale", "sale grosso", "sale fino", "pepe", "pepe nero", "pepe bianco",
    "sale e pepe", "noce moscata", "moscata", "cannella",
    "cannella in polvere", "origano", "origano secco", "peperoncino",
    "peperoncino in polvere", "zafferano", "zucchero", "zucchero a velo",
    "paprica", "vanillina", "bicarbonato",
})

RULE_KEEP_QUANTITY = "R1-quantita-gia-presente"
RULE_KEEP_PINCH = "R2-pizzico-confermato-dal-procedimento"
RULE_TO_TASTE = "R3-q.b."


def _procedure_mentions_pinch(markdown: str) -> bool:
    _, _, procedure = markdown.partition(PROCEDURE_HEADING)
    return bool(procedure) and bool(PINCH_IN_TEXT_RE.search(procedure))


def classify(rest: str, procedure_mentions_pinch: bool) -> tuple[str, str]:
    """Restituisce ``(rule_id, nuova_riga)`` per il resto di una riga iniettata."""
    stripped = rest.strip()
    if QTY_RANGE_RE.match(stripped):
        return RULE_KEEP_QUANTITY, f"- {stripped}"
    if procedure_mentions_pinch and stripped.casefold() in PINCH_SPICES:
        return RULE_KEEP_PINCH, f"- 1 pizzico {stripped}"
    return RULE_TO_TASTE, f"- q.b. {stripped}"


def transform(markdown: str) -> tuple[str, Counter]:
    """Bonifica un documento; restituisce ``(markdown, conteggi per regola)``."""
    mentions_pinch = _procedure_mentions_pinch(markdown)
    counts: Counter[str] = Counter()
    lines = markdown.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        body = line.rstrip("\n")
        match = INJECTED_RE.match(body)
        if match is None:
            out.append(line)
            continue
        rule_id, new_body = classify(match.group("rest"), mentions_pinch)
        counts[rule_id] += 1
        if new_body == body:
            out.append(line)
        else:
            out.append(new_body + line[len(body):])
    return "".join(out), counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument(
        "--apply", action="store_true", help="scrive i file (default: anteprima)"
    )
    parser.add_argument("--report", default=None, help="file JSON con il dettaglio")
    parser.add_argument("--show", type=int, default=10, help="esempi da stampare")
    args = parser.parse_args(argv)

    corpus = pathlib.Path(args.corpus)
    pack = load_domain_pack(args.pack)
    known_units = pack.known_units()
    countable_units = pack.countable_units()

    totals: Counter[str] = Counter()
    changed_files: list[str] = []
    examples: list[dict[str, str]] = []
    parse_errors: list[str] = []

    for path in sorted(corpus.glob("*.md")):
        original = path.read_text(encoding="utf-8")
        rewritten, counts = transform(original)
        totals.update(counts)
        if rewritten == original:
            continue
        changed_files.append(path.name)
        # Nessun file esce dalla bonifica in uno stato che il parser rifiuta.
        try:
            parse_source_md(
                rewritten,
                known_units=known_units,
                countable_units=countable_units,
            )
        except ParseError as exc:
            parse_errors.append(f"{path.name}: {exc}")
            continue
        for before, after in zip(
            original.splitlines(), rewritten.splitlines(), strict=True
        ):
            if before != after and len(examples) < args.show:
                examples.append({"file": path.name, "before": before, "after": after})
        if args.apply:
            path.write_text(rewritten, encoding="utf-8")

    print(f"corpus     : {corpus}")
    print(f"file toccati: {len(changed_files)}")
    print("righe '- 1 pizzico' per regola:")
    for rule_id, count in sorted(totals.items()):
        print(f"  {rule_id:<38} {count:>6}")
    print(f"  {'TOTALE':<38} {sum(totals.values()):>6}")
    if examples:
        print("\nesempi:")
        for example in examples:
            print(f"  [{example['file']}]")
            print(f"    - {example['before']}")
            print(f"    + {example['after']}")
    if parse_errors:
        print(f"\nERRORI DI PARSE ({len(parse_errors)}): nessuna scrittura per questi file")
        for error in parse_errors[:10]:
            print(f"  {error}")
    if not args.apply:
        print("\n(anteprima: nessun file scritto — usa --apply)")

    if args.report:
        payload = {
            "corpus": str(corpus),
            "applied": args.apply,
            "files_changed": len(changed_files),
            "by_rule": dict(sorted(totals.items())),
            "parse_errors": parse_errors,
            "examples": examples,
        }
        report_path = pathlib.Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nreport: {report_path}")

    if parse_errors:
        print("bonifica incompleta: righe non parsabili", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
