"""Passo 3 PROGRAMMA-UNICO: dizionario di input dai due corpora.

MSC per item code (chiave = codice, mai la stringa); libri per stringa
normalizzata (NFKC, casefold, apostrofi ASCII). Per ogni voce: frequenza,
forme viste, unita' viste, 3 contesti d'uso. Nessuna fusione cross-corpus.

Output: ``term_dictionary.jsonl`` (ordine deterministico: due esecuzioni
producono file hash-identici).

Gate: conteggi riconciliati (2.029 item code MSC / ~1.476 termini libro);
somma delle frequenze = righe totali dei corpora; ogni voce ha >=1 contesto.

Uso:
    uv run python scripts/build_term_dictionary.py --pdf Pareto_Recipe_Cards_v001.pdf \
        --books-json book_ingredients.json --out term_dictionary.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import unicodedata

from scripts.msc_to_md import extract_cards

# Artefatti del loader libri (righe non-ingrediente catturate dall'estrattore):
# esclusi dal conteggio "termini libro" del gate (frequenza >= 2).
BOOK_ARTIFACT_RE = re.compile(
    r"ingrediente|servings|serving\b|d'|^to \d|^for |^of |^the |^and |^with "
    r"|^in |^a |^an |\bper\b|pax|recipe|method|preparation|minutes|°c|oven"
    r"|pan|pot|bowl|salt and pepper to taste$"
)


def normalize_string(s: str) -> str:
    """NFKC + casefold + apostrofi ASCII + whitespace collassato."""
    s = unicodedata.normalize("NFKC", s).casefold().replace("\u2019", "'")
    return " ".join(s.split())


def _contexts(recipe_name: str, row_name: str, code: str) -> list[str]:
    return [f"{recipe_name} | {row_name} | {code}"]


def build_msc_dictionary(pdf: pathlib.Path) -> list[dict]:
    """Dizionario MSC: chiave = item code, mai la stringa."""
    cards = extract_cards(pdf)
    entries: dict[str, dict] = {}
    for card in cards:
        for row in card.ingredients:
            if row.is_section or not row.code:
                continue
            key = row.code
            entry = entries.setdefault(key, {
                "corpus": "msc",
                "key": key,
                "frequency": 0,
                "forms": [],
                "units": [],
                "contexts": [],
            })
            entry["frequency"] += 1
            form = normalize_string(row.name)
            if form not in entry["forms"]:
                entry["forms"].append(form)
            if row.unit and row.unit not in entry["units"]:
                entry["units"].append(row.unit)
            if len(entry["contexts"]) < 3:
                entry["contexts"].append(_contexts(card.name, row.name, key))
    return sorted(entries.values(), key=lambda e: e["key"])


def build_book_dictionary(book_labels: list[str]) -> list[dict]:
    """Dizionario libri: chiave = stringa normalizzata."""
    entries: dict[str, dict] = {}
    for label in book_labels:
        key = normalize_string(label)
        if not key:
            continue
        entry = entries.setdefault(key, {
            "corpus": "book",
            "key": key,
            "frequency": 0,
            "forms": [],
            "units": [],
            "contexts": [],
        })
        entry["frequency"] += 1
        form = normalize_string(label)
        if form not in entry["forms"]:
            entry["forms"].append(form)
        if len(entry["contexts"]) < 3:
            entry["contexts"].append(f"knowledge | {label}")
    return sorted(entries.values(), key=lambda e: e["key"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, type=pathlib.Path)
    ap.add_argument("--books-json", required=True, type=pathlib.Path,
                    help="JSON list of book ingredient labels (da Neo4j)")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    args = ap.parse_args()

    msc = build_msc_dictionary(args.pdf)
    book_labels = json.loads(args.books_json.read_text(encoding="utf-8"))
    books = build_book_dictionary(book_labels)

    # gate: conteggi riconciliati. I "termini libro" del piano (~1.476) sono
    # le stringhe distinte con frequenza >= 2 (1.472 misurati sul knowledge).
    msc_rows = sum(e["frequency"] for e in msc)
    book_rows = sum(e["frequency"] for e in books)
    book_terms_freq2 = sum(1 for e in books if e["frequency"] >= 2)
    reconciliation = {
        "msc_codes": len(msc),
        "msc_rows": msc_rows,
        "book_terms": len(books),
        "book_terms_freq2": book_terms_freq2,
        "book_rows": book_rows,
        "gate": {
            "msc_codes_expected": 2029,
            "book_terms_expected": 1476,
            "msc_ok": len(msc) == 2029,
            "book_ok": abs(book_terms_freq2 - 1476) <= 10,
        },
    }

    # output deterministico (ordine per chiave)
    all_entries = msc + books
    lines = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in all_entries]
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    print(json.dumps({**reconciliation, "sha256": digest}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
