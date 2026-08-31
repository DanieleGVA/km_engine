"""Audit dosi v2: confronto POSIZIONALE riga-per-riga (l'ordine e' preservato)."""
from __future__ import annotations

import collections
import json
import pathlib
import re
from decimal import ROUND_HALF_UP, Decimal

from app.domain.doses import MKS_FACTORS, MKS_NATIVE
from app.validation.ingest import read_recipes
from app.validation.validator import COUNT_UNITS

PDF = pathlib.Path("/Users/daniele.buonaiuto/Dev/rcps/foodmdm/deliverables/DLV-26_menu_pareto/Pareto_Recipe_Cards_v001.pdf")
OUT_DIR = pathlib.Path("deploy/validation_out")
TARGET = 10


def fmt_qty(v: Decimal) -> str:
    q = v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return str(int(q))
    return format(q, "f").rstrip("0").rstrip(".")


def expected_scaled(ing: dict, factor: Decimal) -> tuple[str, str, str]:
    qty = Decimal(str(ing["qty"]))
    unit = ing["unit"].lower()
    item = ing["name"].lower()
    if unit in MKS_FACTORS:
        mks_unit, mks_factor, _ = MKS_FACTORS[unit]
        qty = qty * Decimal(str(mks_factor))
        unit = mks_unit
    scaled = qty * factor
    return fmt_qty(scaled), unit, item


def parse_output_md(md: str) -> list[tuple[str, str, str]]:
    out = []
    for line in md.splitlines():
        m = re.match(r"^- ([\d\.]+) (\S+) (.+)$", line)
        if m:
            out.append((m.group(1), m.group(2).lower(), m.group(3).strip()))
    return out


def main() -> None:
    recipes = read_recipes(PDF)
    outputs: dict[str, dict] = {}
    for f in OUT_DIR.glob("*.md"):
        txt = f.read_text(encoding="utf-8")
        m = re.search(r"^id: (\S+)$", txt, re.MULTILINE)
        if m:
            outputs[m.group(1)] = {"md": txt, "file": f.name}

    stats = collections.Counter()
    qty_mismatches: list[dict] = []
    unit_mismatches: list[dict] = []
    edge_cases: dict[str, list] = collections.defaultdict(list)
    checked = 0
    n_lines_compared = 0

    for r in recipes:
        if r.code is None:
            stats["no_code"] += 1
            continue
        out = outputs.get(r.code)
        if out is None:
            stats["no_output"] += 1
            continue
        factor = Decimal(TARGET) / Decimal(r.servings)
        exp = [expected_scaled(i, factor) for i in r.ingredients]
        act = parse_output_md(out["md"])
        checked += 1
        n = min(len(exp), len(act))
        n_lines_compared += n
        for i in range(n):
            eq, eu, ei = exp[i]
            aq, au, _ai = act[i]
            if eq != aq:
                qty_mismatches.append({"code": r.code, "idx": i, "item": ei, "expected_qty": eq, "actual_qty": aq, "unit": eu})
                stats["qty_diversa"] += 1
            if eu != au:
                unit_mismatches.append({"code": r.code, "idx": i, "item": ei, "expected_unit": eu, "actual_unit": au})
                stats["unita_diversa"] += 1
        if len(exp) != len(act):
            stats["n_righe_diverso"] += 1
            if len(exp) > len(act):
                stats["righe_mancanti_output"] += len(exp) - len(act)
            else:
                stats["righe_extra_output"] += len(act) - len(exp)
        # edge cases
        for ing in r.ingredients:
            q = ing["qty"]
            u = ing["unit"].lower()
            if q == 0:
                edge_cases["qty_zero"].append((r.code, ing["name"], ing["unit"]))
            elif u == "mg":
                edge_cases["unita_mg"].append((r.code, ing["name"], q, u))
            elif u in COUNT_UNITS:
                edge_cases["unita_conteggio"].append((r.code, ing["name"], q, u))
            elif u not in MKS_NATIVE and u not in MKS_FACTORS and u not in COUNT_UNITS:
                edge_cases["unita_sconosciuta"].append((r.code, ing["name"], q, u))

    print(f"confrontate {checked} card, {n_lines_compared} righe")
    print("stats:", dict(stats))
    print("qty mismatches:", len(qty_mismatches))
    print("unit mismatches:", len(unit_mismatches))
    print("edge cases:", {k: len(v) for k, v in edge_cases.items()})

    report = {
        "pdf": str(PDF),
        "cards_pdf": 1653,
        "cards_parsed": len(recipes),
        "cards_with_output": checked,
        "lines_compared": n_lines_compared,
        "stats": dict(stats),
        "qty_mismatch_count": len(qty_mismatches),
        "qty_mismatch_sample": qty_mismatches[:40],
        "unit_mismatch_count": len(unit_mismatches),
        "unit_mismatch_sample": unit_mismatches[:40],
        "edge_case_counts": {k: len(v) for k, v in edge_cases.items()},
        "edge_cases_sample": {k: v[:15] for k, v in edge_cases.items()},
    }
    out = pathlib.Path("deploy/validation_out/dose_audit_report.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("report:", out)


if __name__ == "__main__":
    main()
