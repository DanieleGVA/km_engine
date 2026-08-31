"""Applica le regole R1-R9 (direttiva chef 31/08) alle proposte del dizionario.

- R1: quarantena non-ingredienti (mai ripubblicate)
- R2: ri-segmentazione righe composte in componenti
- R3: anti-fusione (canonical corretti, alias rimossi)
- R4-R8: post-processor allergenici deterministici
- R9: canone unico di classe

Output: proposte corrette (JSONL) + aggiornamento glossario/mapping.

Uso:
    uv run python scripts/apply_dictionary_rules.py --verdicts /tmp/verdicts_full.json
"""
from __future__ import annotations

import argparse
import json
import pathlib

from app.domain.dictionary_rules import apply_rules

PACK_DIR = pathlib.Path(__file__).resolve().parents[1] / "domain-packs" / "ricette"

# Riga 195 (veloute' di pesce, gia' pubblicata OK): R6a aggiunge gluten.
FISH_VELOUTE_KEY = "SF00258"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verdicts", required=True, type=pathlib.Path)
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("/tmp/proposals_corrected.jsonl"))
    args = ap.parse_args()

    rows = json.loads(args.verdicts.read_text(encoding="utf-8"))
    # forms dal dizionario
    dict_path = pathlib.Path("/tmp/term_dictionary.jsonl")
    forms = {}
    if dict_path.exists():
        for line in dict_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                e = json.loads(line)
                forms[e["key"]] = e["forms"]

    corrected: list[dict] = []
    quarantined: list[dict] = []
    for r in rows:
        if r["verdetto"] != "NOK":
            continue  # solo le righe delle regole (NOK)
        res = apply_rules(
            key=r["key"], canonical=r["canonical_name_en"],
            core=r["ingredient_core"], class_=r["class"],
            aliases=r["aliases"], allergens=r["allergen_tags"],
            confidence=r.get("confidence", 0.9), ambiguous=r.get("ambiguous", False),
            corpus=r["corpus"], forms=forms.get(r["key"], []),
        )
        if res.split_into:
            # R2 (precede R1): ogni componente diventa una voce autonoma
            for comp in res.split_into:
                corrected.append({
                    "key": f"{r['key']}::{comp['ingredient_core']}",
                    "corpus": r["corpus"],
                    "canonical_name_en": comp["canonical_name_en"],
                    "ingredient_core": comp["ingredient_core"],
                    "class": comp.get("class"),
                    "aliases": comp.get("aliases", []),
                    "allergen_tags": comp.get("allergen_tags", []),
                    "confidence": 0.9, "ambiguous": False,
                    "rules_applied": ["R2"],
                })
            continue
        if res.quarantined:
            quarantined.append({"key": r["key"], "rules": res.rules_applied})
            continue
        corrected.append({
            "key": r["key"], "corpus": r["corpus"],
            "canonical_name_en": res.canonical_name_en,
            "ingredient_core": res.ingredient_core,
            "class": res.class_, "aliases": res.aliases,
            "allergen_tags": res.allergen_tags,
            "confidence": res.confidence, "ambiguous": res.ambiguous,
            "rules_applied": res.rules_applied,
        })

    # riga 195: veloute' di pesce -> +gluten (R6a)
    for r in rows:
        if (r["key"] == FISH_VELOUTE_KEY and r["verdetto"] == "OK"
                and "gluten" not in r["allergen_tags"]):
            r["allergen_tags"].append("gluten")
            corrected.append({
                    "key": r["key"], "corpus": r["corpus"],
                    "canonical_name_en": r["canonical_name_en"],
                    "ingredient_core": r["ingredient_core"], "class": r["class"],
                    "aliases": r["aliases"], "allergen_tags": r["allergen_tags"],
                    "confidence": 0.9, "ambiguous": False,
                    "rules_applied": ["R6"],
                })

    args.out.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in corrected) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "corrette": len(corrected),
        "quarantena_R1": len(quarantined),
        "split_R2": sum(1 for c in corrected if "R2" in c.get("rules_applied", [])),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
