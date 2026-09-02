"""Misura di copertura del glossario su un corpus (WP-F0).

Una sola funzione di misura, riusata dallo script ``scripts/measure_coverage.py``
e da ``app/agents/evaluator.py``. La copertura e' la percentuale di *righe
ingrediente* del corpus il cui item si risolve in una voce di glossario usando
**la stessa funzione di lookup** che usa ``canonicalize`` (mai una copia: se le
due divergono la misura mente).

Il report elenca i termini irrisolti ordinati per frequenza con i candidati piu'
vicini (trigram Jaccard), che sono l'input di WP-F5 (proposte di glossario).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.domain.errors import ParseError
from app.domain.normalize import (
    RULE_EXACT,
    RULE_UNRESOLVED,
    Resolver,
    normalize_key,
)
from app.domain.pack import DomainPackBundle

Stage = Literal["source", "translated"]

# I rule_id dei livelli vengono dal resolver: la misura non ne conia di suoi.
RULE_GLOSS_EXACT = RULE_EXACT
RULE_GLOSS_UNRESOLVED = RULE_UNRESOLVED


# ---------------------------------------------------------------------------
# Similarita' (nessuna dipendenza nuova)
# ---------------------------------------------------------------------------

def _trigrams(text: str) -> set[str]:
    """Trigrammi di caratteri con padding ai bordi ("  aglio ")."""
    padded = f"  {text.strip()} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def trigram_similarity(left: str, right: str) -> float:
    """Jaccard sui trigrammi di caratteri; 0.0 se uno dei due e' vuoto."""
    a, b = _trigrams(left), _trigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class TrigramIndex:
    """Indice invertito trigramma -> chiavi, per candidati in tempo utile.

    Il confronto naive (ogni termine irrisolto contro ogni chiave di glossario)
    e' O(n*m) su decine di migliaia di coppie; l'indice invertito visita solo
    le chiavi che condividono almeno un trigramma.
    """

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)
        self._sizes = [len(_trigrams(key)) for key in self.keys]
        self._postings: dict[str, list[int]] = defaultdict(list)
        for index, key in enumerate(self.keys):
            for gram in _trigrams(key):
                self._postings[gram].append(index)

    def top(self, term: str, limit: int = 3) -> list[tuple[str, float]]:
        """Le ``limit`` chiavi piu' simili a ``term`` (Jaccard), score desc."""
        grams = _trigrams(term)
        if not grams:
            return []
        shared: Counter[int] = Counter()
        for gram in grams:
            for index in self._postings.get(gram, ()):
                shared[index] += 1
        size = len(grams)
        scored = [
            (self.keys[index], common / (size + self._sizes[index] - common))
            for index, common in shared.items()
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return [(key, round(score, 4)) for key, score in scored[:limit]]


def top_candidates_for(
    term: str, keys: list[str], limit: int = 3
) -> list[tuple[str, float]]:
    """Le ``limit`` chiavi di glossario piu' simili a ``term``, score desc."""
    return TrigramIndex(keys).top(term, limit)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnresolvedTerm:
    """Un termine che il lookup non risolve, con esempi e candidati."""

    term: str
    count: int
    examples: list[str]
    candidates: list[tuple[str, float]]

    def to_json(self) -> dict:
        return {
            "term": self.term,
            "count": self.count,
            "examples": self.examples,
            "candidates": [
                {"key": key, "score": score} for key, score in self.candidates
            ],
        }


@dataclass(frozen=True)
class FuzzyResolution:
    """Una risoluzione del livello L3, da far rivedere a una persona."""

    item: str
    label_en: str
    score: float
    document: str

    def to_json(self) -> dict:
        return {
            "item": self.item,
            "label_en": self.label_en,
            "score": self.score,
            "document": self.document,
        }


@dataclass
class CoverageReport:
    """Esito della misura su un corpus."""

    pack_id: str
    corpus_dir: str
    stage: str
    docs_total: int
    docs_parsed: int
    lines_total: int
    lines_resolved: int
    coverage: float
    by_rule: dict[str, int]
    unresolved: list[UnresolvedTerm]
    parse_errors: list[str] = field(default_factory=list)
    fuzzy: list[FuzzyResolution] = field(default_factory=list)

    @property
    def unresolved_lines(self) -> int:
        return self.lines_total - self.lines_resolved

    def to_json(self) -> dict:
        return {
            "pack_id": self.pack_id,
            "corpus_dir": self.corpus_dir,
            "stage": self.stage,
            "docs_total": self.docs_total,
            "docs_parsed": self.docs_parsed,
            "lines_total": self.lines_total,
            "lines_resolved": self.lines_resolved,
            "coverage": round(self.coverage, 4),
            "by_rule": dict(sorted(self.by_rule.items())),
            "unresolved_terms": len(self.unresolved),
            "unresolved": [term.to_json() for term in self.unresolved],
            "fuzzy": [item.to_json() for item in self.fuzzy],
            "parse_errors": self.parse_errors,
        }

    def write_json(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


# ---------------------------------------------------------------------------
# Misura
# ---------------------------------------------------------------------------

def lookup_key(item: str) -> str:
    """Chiave di lookup: la stessa che usa ``canonicalize``.

    Importata, mai ricopiata: se la misura e la canonicalizzazione usassero due
    normalizzazioni diverse il report mentirebbe.
    """
    return normalize_key(item)


def build_lookup(pack: DomainPackBundle) -> dict[str, tuple[str, str]]:
    """La mappa termine -> ``(labels_en, glossary_id)`` usata da ``canonicalize``."""
    from app.domain.canonical import _build_term_map

    return _build_term_map(pack)


def build_resolver(pack: DomainPackBundle) -> Resolver:
    """Il resolver a livelli usato da ``canonicalize`` (WP-F4).

    La misura usa lo stesso oggetto, non una riproduzione: ``by_rule`` nel
    report dice davvero quale livello ha risolto ogni riga.
    """
    return Resolver(pack)


def measure_coverage(
    pack: DomainPackBundle,
    corpus_dir: str | Path,
    *,
    stage: Stage = "source",
    top_candidates: int = 3,
) -> CoverageReport:
    """Percentuale di righe ingrediente risolte dal glossario su ``corpus_dir``."""
    corpus_dir = Path(corpus_dir)
    files = sorted(corpus_dir.glob("*.md"))
    documents = {
        path.name: path.read_text(encoding="utf-8") for path in files
    }
    report = measure_documents(
        pack, documents, stage=stage, top_candidates=top_candidates
    )
    report.corpus_dir = str(corpus_dir)
    return report


def measure_documents(
    pack: DomainPackBundle,
    documents: Mapping[str, str],
    *,
    stage: Stage = "source",
    top_candidates: int = 3,
    corpus_dir: str = "<memory>",
) -> CoverageReport:
    """Come :func:`measure_coverage` ma su documenti gia' in memoria."""
    from app.domain.verify import parse_source_md, parse_translated_md

    resolver = build_resolver(pack)
    glossary_keys = sorted(build_lookup(pack))
    known_units = pack.known_units()
    countable_units = pack.countable_units()

    parse = parse_source_md if stage == "source" else parse_translated_md
    kwargs: dict = {
        "known_units": known_units,
        "countable_units": countable_units,
    }
    if stage == "translated":
        kwargs["optional_when_native"] = tuple(
            pack.frontmatter_optional_when_native
        )

    lines_total = 0
    lines_resolved = 0
    docs_parsed = 0
    by_rule: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    parse_errors: list[str] = []
    fuzzy: list[FuzzyResolution] = []

    for name in sorted(documents):
        try:
            doc = parse(documents[name], **kwargs)
        except ParseError as exc:
            parse_errors.append(f"{name}: {exc}")
            continue
        docs_parsed += 1
        for ingredient in doc.ingredients:
            lines_total += 1
            resolution = resolver.resolve(ingredient.item)
            by_rule[resolution.rule_id] += 1
            if resolution.needs_review and resolution.label_en:
                fuzzy.append(
                    FuzzyResolution(
                        item=ingredient.item,
                        label_en=resolution.label_en,
                        score=(
                            resolution.candidates[0][1]
                            if resolution.candidates
                            else 0.0
                        ),
                        document=name,
                    )
                )
            if resolution.resolved:
                lines_resolved += 1
            else:
                key = lookup_key(ingredient.item)
                counts[key] += 1
                if len(examples[key]) < 3:
                    examples[key].append(ingredient.raw)

    index = TrigramIndex(glossary_keys)
    unresolved = [
        UnresolvedTerm(
            term=term,
            count=count,
            examples=examples[term],
            candidates=index.top(term, top_candidates),
        )
        for term, count in sorted(
            counts.items(), key=lambda pair: (-pair[1], pair[0])
        )
    ]

    return CoverageReport(
        pack_id=f"{pack.pack.name}:{pack.pack.version}",
        corpus_dir=corpus_dir,
        stage=stage,
        docs_total=len(documents),
        docs_parsed=docs_parsed,
        lines_total=lines_total,
        lines_resolved=lines_resolved,
        coverage=(lines_resolved / lines_total) if lines_total else 0.0,
        by_rule=dict(by_rule),
        unresolved=unresolved,
        parse_errors=parse_errors,
        fuzzy=fuzzy,
    )
