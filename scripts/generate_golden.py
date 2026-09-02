#!/usr/bin/env python3
"""Generate the deterministic RAG golden set (WP-B1, gate GB1).

The golden set is built **only** from the 15 committed Iteration-A recipes and
the committed Domain Pack. It never calls the network, an LLM or Neo4j: the
English titles/ingredients/steps are produced with the same deterministic
glossary normalisation used by the stage-1 fake translator
(``normalize_terms`` + ``pack.it_to_en_terms()``).

Composition per recipe (8 queries, 120 total):

- 2 EN title queries (canonical title, ``recipe <title>``)
- 2 IT title queries (source title, ``ricetta <title>``)
- 2 ingredient queries (two canonical EN ingredients)
- 1 technique query (canonical technique term when present, otherwise the
  first verb of the first step as a deterministic fallback)
- 1 state query (canonical state term when present, otherwise the
  deterministic fallback ``golden``)

Technique/state terms are detected on the normalised EN steps with the same
word-boundary ``labels_en`` matching used by ``extract_document``.

Usage:
    uv run python scripts/generate_golden.py [--pack-dir domain-packs/ricette]
        [--corpus-dir tests/fixtures/corpus_ricette]
        [--out tests/fixtures/rag_golden.json]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.domain import load_domain_pack, normalize_terms, parse_source_md

# The 15 committed Iteration-A recipes. The corpus directory may contain
# additional in-progress files from other workers; the golden set is pinned to
# the validated pilot corpus.
GOLDEN_CORPUS_FILES = (
    "ric-001-pomodoro.md",
    "ric-002-risotto.md",
    "ric-003-torta.md",
    "ric-004-pane.md",
    "ric-005-pollo.md",
    "ric-006-insalata.md",
    "ric-007-zuppa.md",
    "ric-008-frittata.md",
    "ric-009-tiramisu.md",
    "ric-010-sugo.md",
    "ric-011-crepes.md",
    "ric-012-polpette.md",
    "ric-101-asparagi-burro.md",
    "ric-102-fregola-vongole.md",
    "ric-103-amaretti.md",
)

STATE_FALLBACK = "golden"


def _find_terms(pack, steps_en: list[str]) -> tuple[list, list]:
    """Return ``(techniques, states)`` using ``labels_en`` word boundaries."""
    techniques: list = []
    states: list = []
    for step in steps_en:
        text = step.casefold()
        for namespace in ("tecnica", "stati"):
            glossary = getattr(pack.glossaries, namespace)
            for entry in glossary.entries:
                label = entry.labels_en.strip()
                if not label:
                    continue
                if re.search(rf"\b{re.escape(label.casefold())}\b", text):
                    (techniques if namespace == "tecnica" else states).append(entry)
    return techniques, states


def _first_verb(step: str) -> str:
    """Deterministic pseudo-technique fallback: first token of the first step."""
    tokens = step.strip().split()
    return tokens[0].lower() if tokens else "cook"


def generate_pairs(pack, corpus: dict[str, str]) -> list[dict]:
    """Build the deterministic ``(query, document_id, lang, kind)`` pairs."""
    pairs: list[dict] = []
    for name in sorted(corpus):
        source_md = corpus[name]
        source = parse_source_md(
            source_md,
            known_units=pack.known_units(),
            countable_units=pack.countable_units(),
        )
        title_it = source.title
        title_en = normalize_terms(title_it, pack.it_to_en_terms())
        document_id = source.frontmatter["id"]

        ingredients_en = [
            normalize_terms(ingredient.item, pack.it_to_en_terms())
            for ingredient in source.ingredients
        ]
        steps_en = [
            normalize_terms(step, pack.it_to_en_terms())
            for step in source.steps
        ]
        techniques, states = _find_terms(pack, steps_en)

        def add(kind: str, query: str, lang: str, doc_id: str = document_id) -> None:
            pairs.append(
                {
                    "query": query,
                    "document_id": doc_id,
                    "lang": lang,
                    "kind": kind,
                }
            )

        # 2 EN + 2 IT title queries.
        add("title_en_exact", title_en, "en")
        add("title_en_natural", f"recipe {title_en}", "en")
        add("title_it_exact", title_it, "it")
        add("title_it_natural", f"ricetta {title_it}", "it")

        # 2 ingredient queries (two canonical EN ingredients together).
        if len(ingredients_en) >= 2:
            first, second = ingredients_en[0], ingredients_en[1]
            add("ingredient_pair", f"recipe with {first} and {second}", "en")
            add("ingredient_dish", f"dish with {first} and {second}", "en")
        else:  # pragma: no cover - the 15-recipe corpus always has >= 2
            only = ingredients_en[0] if ingredients_en else "ingredients"
            add("ingredient_single", f"recipe with {only}", "en")
            add("ingredient_dish", f"dish with {only}", "en")

        # 1 technique query.
        if techniques:
            technique = techniques[0]
            add(
                "technique",
                f"recipe {title_en} using {technique.labels_en}",
                "en",
            )
        else:
            add(
                "technique_fallback",
                f"recipe {title_en} using {_first_verb(source.steps[0])}",
                "en",
            )

        # 1 state query.
        if states:
            state = states[0]
            add("state", f"recipe {title_en} state {state.labels_en}", "en")
        else:
            add(
                "state_fallback",
                f"recipe {title_en} state {STATE_FALLBACK}",
                "en",
            )

    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack-dir",
        default="domain-packs/ricette",
        help="Domain Pack directory (default: domain-packs/ricette)",
    )
    parser.add_argument(
        "--corpus-dir",
        default="tests/fixtures/corpus_ricette",
        help="Corpus directory (default: tests/fixtures/corpus_ricette)",
    )
    parser.add_argument(
        "--out",
        default="tests/fixtures/rag_golden.json",
        help="Output JSON path (default: tests/fixtures/rag_golden.json)",
    )
    parser.add_argument(
        "--corpus",
        choices=("pilot", "full"),
        default="pilot",
        help="pilot=15 ricette validate (curated asset); full=tutte le ric-*.md del corpus dir",
    )
    args = parser.parse_args()

    pack = load_domain_pack(Path(args.pack_dir))
    corpus_dir = Path(args.corpus_dir)
    if args.corpus == "full":
        names = sorted(
            p.name for p in corpus_dir.glob("ric-*.md") if p.name not in ("README.md",)
        )
        corpus = {
            name: (corpus_dir / name).read_text(encoding="utf-8")
            for name in names
        }
        corpus_label = f"{corpus_dir} (full: {len(corpus)} recipes)"
    else:
        corpus = {
            name: (corpus_dir / name).read_text(encoding="utf-8")
            for name in GOLDEN_CORPUS_FILES
        }
        corpus_label = f"{corpus_dir} (15 committed recipes)"
    missing = [name for name, text in corpus.items() if not text]
    if missing:
        raise SystemExit(f"missing corpus files: {missing}")

    pairs = generate_pairs(pack, corpus)
    payload = {
        "version": "1.0",
        "generated_by": "scripts/generate_golden.py",
        "corpus": corpus_label,
        "pairs": pairs,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"golden set written to {out}: {len(pairs)} pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
