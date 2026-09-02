#!/usr/bin/env python3
"""Misura la copertura del glossario su un corpus (WP-F0).

Uso:
  uv run python scripts/measure_coverage.py \
      --corpus tests/fixtures/corpus_marchesi_full \
      --pack domain-packs/ricette \
      [--stage source|translated] [--out FILE] [--top 50]

Stampa la tabella riassuntiva e i primi N termini irrisolti con i candidati
piu' vicini; con ``--out`` scrive il report JSON completo.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from app.domain.coverage import CoverageReport, measure_coverage
from app.domain.pack import load_domain_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"


def _print_report(report: CoverageReport, top: int) -> None:
    print(f"pack      : {report.pack_id}")
    print(f"corpus    : {report.corpus_dir} (stage={report.stage})")
    print(f"documenti : {report.docs_parsed}/{report.docs_total} parsati")
    print(
        f"righe     : {report.lines_resolved}/{report.lines_total} risolte "
        f"-> coverage {report.coverage:.2%}"
    )
    if report.by_rule:
        print("per regola:")
        for rule, count in sorted(report.by_rule.items(), key=lambda p: -p[1]):
            print(f"  {rule:<20} {count:>7}  ({count / report.lines_total:.2%})")
    if report.parse_errors:
        print(f"parse errors: {len(report.parse_errors)} (primi 5)")
        for error in report.parse_errors[:5]:
            print(f"  {error}")
    if not report.unresolved:
        return
    residual = report.unresolved_lines
    print(
        f"\nirrisolti : {len(report.unresolved)} termini / {residual} righe "
        f"— primi {min(top, len(report.unresolved))}:"
    )
    print(f"  {'count':>6}  {'%res':>6}  termine -> candidati")
    for term in report.unresolved[:top]:
        share = term.count / residual if residual else 0.0
        candidates = ", ".join(
            f"{key} ({score:.2f})" for key, score in term.candidates
        ) or "-"
        print(f"  {term.count:>6}  {share:>6.1%}  {term.term!r} -> {candidates}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument(
        "--stage", choices=("source", "translated"), default="source"
    )
    parser.add_argument("--out", default=None, help="file JSON del report")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        help="esce con codice 1 se la coverage e' sotto questa soglia (gate)",
    )
    args = parser.parse_args(argv)

    pack = load_domain_pack(args.pack)
    report = measure_coverage(pack, args.corpus, stage=args.stage)
    _print_report(report, args.top)
    if args.out:
        path = report.write_json(args.out)
        print(f"\nreport JSON: {path}")
    if args.min_coverage is not None and report.coverage < args.min_coverage:
        print(
            f"\nGATE FALLITO: coverage {report.coverage:.2%} < "
            f"{args.min_coverage:.2%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
