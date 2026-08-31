#!/usr/bin/env python3
"""CLI di ricerca e validazione ricette (branch validate-recipe).

Input:
  --pdf <file>   PDF di ricette in formato CalcMenu/Pareto (estratto con pdftotext)
  --md <file|dir> ricette in formato md (translated/canonical)

Per ogni ricetta:
  1. parse + standardizzazione dosi MKS a N persone (default 10)
  2. validazione: unita' riconosciute, copertura glossario, procedura
  3. ingestione nel grafo (con riferimenti sorgente) + embedding
  4. ricerca RAG: la ricetta normalizzata viene ritrovata?

Output: tabella esiti + report JSON opzionale (--out).

Uso:
  uv run python scripts/validate_recipes.py --pdf Pareto_Recipe_Cards_v001.pdf --limit 20
  uv run python scripts/validate_recipes.py --md tests/fixtures/corpus_ricette --limit 5
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

from app.auth import Principal
from app.domain import parse_translated_md
from app.domain.pack import load_domain_pack
from app.storage.client import Neo4jClient
from app.validation.validator import ALLOWED_UNITS, validate_and_ingest

PACK_DIR = pathlib.Path(__file__).resolve().parents[1] / "domain-packs" / "ricette"


def _parse_qty(s: str) -> float | None:
    s = s.strip()
    if not re.fullmatch(r"[\d\.,]+", s):
        return None
    if "," in s:
        before, after = s.split(",", 1)
        if len(after) <= 2 and len(before) <= 3:
            return float(before.replace(".", "") + "." + after)
        return float(s.replace(",", "").replace(".", ""))
    if "." not in s:
        return float(s)
    if s.count(".") >= 2:
        return float(s.replace(".", ""))
    before, after = s.split(".", 1)
    if len(after) == 3:
        return float(s.replace(".", ""))
    return float(s)


def _parse_pareto_card(block: list[str]) -> dict | None:
    name = block[0].strip()
    code, yield_serve = "", None
    for l in block[:6]:
        m = re.search(r"(RF\d+|SF\d+)", l)
        if m and not code:
            code = m.group(1)
        m2 = re.search(r"Yield\s+(\d+)\s+serv\w*", l)
        if m2:
            yield_serve = int(m2.group(1))
    ingredients, in_ing = [], False
    for l in block:
        if re.match(r"^\s*#\s+Item code", l):
            in_ing = True
            continue
        if in_ing:
            if re.match(r"^\s*PROCEDURE", l):
                in_ing = False
                continue
            m = re.match(r"^\s*\d+\s+(\S+)\s+(.+?)\s+([\d\.,]+)\s+(\S+)\s+(.*?)\s*$", l)
            if m:
                q = _parse_qty(m.group(3))
                unit = m.group(4).strip()
                # salta artefatti: unita' non note, "—" (wastage), ")" (tag), qty 0 (codice prodotto)
                if q is not None and q > 0 and unit.lower() in ALLOWED_UNITS:
                    ingredients.append({"code": m.group(1), "name": m.group(2).strip(),
                                        "qty": q, "unit": unit, "prep": m.group(5).strip() or None})
    proc, in_proc = [], False
    for l in block:
        if re.match(r"^\s*PROCEDURE", l):
            in_proc = True
            continue
        if in_proc and l.strip():
            proc.append(l.strip())
    if not ingredients or yield_serve is None:
        return None
    return {"name": name, "code": code, "yield": yield_serve, "ingredients": ingredients, "procedure": proc}


def _pareto_to_md(card: dict, idx: int) -> str:
    name = re.sub(r"\(.*?\)", "", card["name"]).strip().replace(":", "-")
    ing = "\n".join(f"- {i['qty']:g} {i['unit']} {i['name'].lower()}" for i in card["ingredients"])
    steps = "\n".join(f"{j}. {s}" for j, s in enumerate(card["procedure"][:12], 1))
    return (f"---\ntitle: {name}\nid: PAR-{idx+1:04d}\nlang: en\nsource_lang: en\nservings: {card['yield']}\n"
            f"time_min: 30\ndifficulty: medium\n---\n## Ingredients\n{ing}\n\n## Method\n{steps}\n")


def _extract_pdf_cards(pdf: pathlib.Path, limit: int) -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        txt = pathlib.Path(td) / "out.txt"
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()

    def is_start(idx: int) -> bool:
        m = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", lines[idx])
        if not m or idx <= 5:
            return False
        for k in range(idx + 1, min(idx + 5, len(lines))):
            if re.search(r"Yield\s+\d+\s+serv\w*", lines[k]) or re.search(r"\b(RF\d+|SF\d+)\b", lines[k]):
                return True
        return False

    starts = [i for i in range(len(lines)) if is_start(i)]
    cards = []
    for si in starts:
        ei = starts[starts.index(si) + 1] if starts.index(si) + 1 < len(starts) else len(lines)
        card = _parse_pareto_card(lines[si:ei])
        if card:
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", type=pathlib.Path, help="PDF ricette CalcMenu/Pareto")
    ap.add_argument("--md", type=pathlib.Path, help="file o dir di ricette md")
    ap.add_argument("--limit", type=int, default=20, help="max ricette da validare")
    ap.add_argument("--servings", type=int, default=10, help="persone target per le dosi")
    ap.add_argument("--prefix", default="val_", help="prefisso id documenti nel grafo")
    ap.add_argument("--out", type=pathlib.Path, help="report JSON opzionale")
    ap.add_argument("--workflow", action="store_true",
                    help="esegue il workflow completo: lettura+rilevamento formato/lingua, "
                         "sub-recipe, standardizzazione, scrittura, ricerca (impronta+procedura+nome), "
                         "validazione con correzioni, note e report globale")
    ap.add_argument("--out-dir", type=pathlib.Path, default=pathlib.Path("validation_out"),
                    help="dir di output per il workflow (standardizzati + note + report)")
    args = ap.parse_args()

    if not args.pdf and not args.md:
        ap.error("serve --pdf o --md")

    pack = load_domain_pack(str(PACK_DIR))
    client = Neo4jClient.from_env()
    client.verify_connectivity()

    if args.workflow:
        import asyncio
        from app.auth import Principal
        from app.validation.workflow import run_validation_workflow

        principal = Principal("val_admin", ("admin",), (), "default", "val_j_admin")
        report = asyncio.run(run_validation_workflow(
            args.pdf or args.md, pack, client, principal,
            out_dir=args.out_dir, servings_target=args.servings, limit=args.limit,
        ))
        print(f"\nWORKFLOW VALIDAZIONE: {report.total} ricette | {report.found} trovate | "
              f"{report.not_found} non presenti | {report.sub_recipes} sub-recipe separate")
        print(f"report globale: {args.out_dir / 'validation_report.json'}")
        client.close()
        return 0 if report.not_found == 0 else 1

    recipes_md: list[tuple[str, dict]] = []
    if args.pdf:
        cards = _extract_pdf_cards(args.pdf, args.limit)
        for i, c in enumerate(cards):
            recipes_md.append((c["name"], _pareto_to_md(c, i)))
    else:
        files = sorted(args.md.glob("*.md")) if args.md.is_dir() else [args.md]
        for i, f in enumerate(files[: args.limit]):
            recipes_md.append((f.stem, f.read_text(encoding="utf-8")))

    print(f"ricette da validare: {len(recipes_md)}")
    print(f"{'#':>3} {'ricetta':<48} {'yield':>5} {'x10':>6} {'ing':>3} {'cov%':>5} {'RAG':>5}")
    print("-" * 80)

    reports = []
    for idx, (name, md) in enumerate(recipes_md):
        try:
            report = validate_and_ingest(
                client, pack, md,
                source_ref={"author": "MSC Cruises F&B", "book": "Pareto Recipe Cards v001",
                            "page": f"card {idx+1}", "position": f"pdf#card{idx+1}"},
                servings_target=args.servings, prefix=args.prefix,
            )
            reports.append(report)
            print(f"{idx+1:>3} {report.title[:46]:<48} {report.servings:>5} {report.scale_factor:>6.2f} "
                  f"{report.n_ingredients:>3} {report.coverage*100:>5.0f} {'SI' if report.rag_found else 'NO':>5}")
        except Exception as exc:  # noqa: BLE001
            print(f"{idx+1:>3} {name[:46]:<48} ERRORE: {str(exc)[:40]}")

    ok = sum(1 for r in reports if r.passed)
    print(f"\nESITO: {ok}/{len(reports)} ricette superano la validazione completa "
          f"(parse + unita' MKS + procedura + RAG retrieval)")

    if args.out:
        payload = [{**r.__dict__, "passed": r.passed} for r in reports]
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
        print(f"report scritto in {args.out}")

    client.close()
    return 0 if ok == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
