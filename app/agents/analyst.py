"""Domain Analyst (WP-C1): corpus -> versioned :class:`DomainBrief`.

The analyst is deterministic and never calls the network. It consumes the
stage-1 IR (``translated.md``) together with the source corpus and produces a
structured brief with:

- candidate entities with frequencies (ingredients from the Ingredients
  section, techniques/states from a small deterministic culinary seed matched
  against the Method steps);
- the vocabularies to normalize (tecnica, ingredienti, stati);
- detected units with frequencies;
- normalization ambiguities (surface forms that contain another surface form,
  e.g. ``mandorle dolci sbucciate`` vs ``mandorle dolci``);
- candidate external ontologies (P7: FoodOn + DBpedia).

The translation itself is performed by the caller with
:func:`translate_corpus` (``translate_document`` + a deterministic
``FakeLLMClient``); the analyst only reads the resulting markdown, so it stays
domain-agnostic with respect to the translation stage.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.agents.models import (
    Ambiguity,
    CandidateEntity,
    DomainBrief,
    OntologyCandidate,
    UnitObservation,
    Vocabulary,
)
from app.domain import (
    LLMClient,
    TranslatedDocument,
    parse_source_md,
    parse_translated_md,
    translate_document,
)
from app.domain.normalize import normalize_key
from app.domain.pack import DomainPackBundle

# Deterministic culinary seed used to detect technique/state candidates in the
# Method steps. It is a small domain heuristic, not the manual pack: the
# analyst uses it to propose candidates, and the human gate (P5) still owns the
# final ontology. For a different domain this seed is simply empty and the
# analyst falls back to structural extraction (ingredients only).
TECHNIQUE_SEED: dict[str, list[str]] = {
    "soffritto": ["soffritto", "soffriggere"],
    "mantecatura": ["mantecatura", "mantecare"],
    "toasting": ["tostatura", "tostare"],
    "browning": ["rosolatura", "rosolare"],
    "wilting": ["appassimento", "appassire"],
    "purging": ["depurazione", "depurare"],
}

STATE_SEED: dict[str, list[str]] = {
    "al dente": ["al dente"],
    "browned": ["rosolato", "rosolata", "rosolati", "rosolate"],
    "golden": ["dorato", "dorata", "dorati", "dorate", "dorarsi"],
    "firm": ["sodo", "soda", "sodi", "sode"],
    "creamy": ["cremoso", "cremosa", "cremosi", "cremose"],
}

ONTOLOGY_CANDIDATES: list[OntologyCandidate] = [
    OntologyCandidate(
        prefix="foodon",
        uri="http://purl.obolibrary.org/obo/FOODON_",
        note="FoodOn food ontology (P7)",
    ),
    OntologyCandidate(
        prefix="dbpedia",
        uri="http://dbpedia.org/resource/",
        note="DBpedia resources (P7)",
    ),
]

_PAREN_RE = re.compile(r"\([^)]*\)")

DEFAULT_BRIEF_DIR = Path("docs/domain-briefs")


def clean_item(item: str) -> str:
    """Normalize an ingredient item for pairing source<->translated.

    Removes parenthetical weights (``(1.2 kg)``) and then applies
    :func:`app.domain.normalize.normalize_key`, la stessa chiave usata dal
    canonicalizzatore (WP-F1). Prima l'analista toglieva anche i connettori
    *interni*, cosi' il brief proponeva ``olio extravergine oliva`` come
    termine sorgente e il pack generato non poteva piu' incontrare l'item
    reale ``olio extravergine di oliva``: era D2 replicato nella pipeline
    degli agenti. Il testo originale resta nei contexts del brief (P3).
    """
    return normalize_key(_PAREN_RE.sub(" ", item))


def _word_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(term.casefold())}\b", flags=re.IGNORECASE)


def _detect_seed_terms(
    steps: list[str], seed: dict[str, list[str]], kind: str
) -> list[CandidateEntity]:
    """Detect seed terms in source steps and return candidate entities."""
    counts: Counter[str] = Counter()
    contexts: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        text = step.casefold()
        for term, source_terms in seed.items():
            for source_term in source_terms:
                if _word_pattern(source_term).search(text):
                    counts[term] += 1
                    if step not in contexts[term]:
                        contexts[term].append(step)
                    break
    return [
        CandidateEntity(
            term=term,
            source_terms=list(seed[term]),
            frequency=counts[term],
            kind=kind,
            contexts=contexts[term],
        )
        for term in seed
        if counts[term] > 0
    ]


def _merge_entities(entities: list[CandidateEntity]) -> list[CandidateEntity]:
    """Merge entities with the same ``term``, summing frequencies/aliases."""
    merged: dict[str, CandidateEntity] = {}
    for entity in entities:
        current = merged.get(entity.term)
        if current is None:
            merged[entity.term] = entity.model_copy(deep=True)
            continue
        current.frequency += entity.frequency
        for source_term in entity.source_terms:
            if source_term not in current.source_terms:
                current.source_terms.append(source_term)
        for context in entity.contexts:
            if context not in current.contexts:
                current.contexts.append(context)
    return list(merged.values())


def _detect_ambiguities(entities: list[CandidateEntity]) -> list[Ambiguity]:
    """Flag surface forms that contain another surface form as a whole word.

    This is a conservative heuristic: it only *reports* the ambiguity for the
    human gate (P5); it never suppresses an entity or invents a normalization.
    """
    source_terms = sorted(
        {term for entity in entities for term in entity.source_terms},
        key=len,
        reverse=True,
    )
    ambiguities: list[Ambiguity] = []
    seen: set[str] = set()
    for longer in source_terms:
        for shorter in source_terms:
            if shorter == longer or len(shorter) >= len(longer):
                continue
            if _word_pattern(shorter).search(longer.casefold()):
                key = (longer, shorter)
                if key in seen:
                    continue
                seen.add(key)
                ambiguities.append(
                    Ambiguity(
                        term=longer,
                        candidates=[shorter],
                        note=(
                            f"surface form {longer!r} contains {shorter!r}; "
                            "the modifier is not an alias (P5 human gate)"
                        ),
                    )
                )
    return ambiguities


async def translate_corpus(
    pack: DomainPackBundle,
    corpus: dict[str, str],
    llm: LLMClient,
) -> dict[str, TranslatedDocument]:
    """Translate every source document with ``translate_document``.

    The caller supplies the pack used for stage-1 translation and a
    deterministic ``FakeLLMClient`` (never the network in tests).
    """
    translated: dict[str, TranslatedDocument] = {}
    for name in sorted(corpus):
        translated[name] = await translate_document(pack, corpus[name], llm)
    return translated


def analyze_corpus(
    source_corpus: dict[str, str],
    translated_corpus: dict[str, TranslatedDocument | str],
    *,
    known_units: set[str],
    countable_units: set[str] | None = None,
    domain: str = "ricette",
    language: str = "it",
    canonical_language: str = "en",
    version: str = "1.0.0",
) -> DomainBrief:
    """Build a :class:`DomainBrief` from source + translated corpora.

    ``translated_corpus`` values may be :class:`TranslatedDocument` instances
    or raw translated markdown strings. ``known_units`` viene dal pack
    (``pack.known_units()``): il parser non ha piu' una tabella di default
    propria (WP-F2).
    """
    countable_units = countable_units or set()
    ingredient_entities: list[CandidateEntity] = []
    unit_counter: Counter[str] = Counter()
    unit_examples: dict[str, list[str]] = defaultdict(list)
    technique_entities: list[CandidateEntity] = []
    state_entities: list[CandidateEntity] = []
    pairing_mismatches = 0

    for name in sorted(source_corpus):
        source_md = source_corpus[name]
        translated_value = translated_corpus[name]
        translated_md = (
            translated_value.translated_md
            if isinstance(translated_value, TranslatedDocument)
            else translated_value
        )

        source = parse_source_md(
            source_md, known_units=known_units, countable_units=countable_units
        )
        translated = parse_translated_md(
            translated_md, known_units=known_units, countable_units=countable_units
        )

        if len(source.ingredients) != len(translated.ingredients):
            pairing_mismatches += 1

        for index in range(min(len(source.ingredients), len(translated.ingredients))):
            source_ing = source.ingredients[index]
            translated_ing = translated.ingredients[index]
            source_item = clean_item(source_ing.item)
            translated_item = clean_item(translated_ing.item) or source_item

            ingredient_entities.append(
                CandidateEntity(
                    term=translated_item,
                    source_terms=[source_item],
                    frequency=1,
                    kind="ingredient",
                    contexts=[source_ing.raw],
                )
            )

            for unit in (source_ing.unit, translated_ing.unit):
                if unit:
                    unit_counter[unit] += 1
                    if source_ing.raw not in unit_examples[unit]:
                        unit_examples[unit].append(source_ing.raw)

        technique_entities.extend(_detect_seed_terms(source.steps, TECHNIQUE_SEED, "technique"))
        state_entities.extend(_detect_seed_terms(source.steps, STATE_SEED, "state"))

    ingredient_entities = _merge_entities(ingredient_entities)
    technique_entities = _merge_entities(technique_entities)
    state_entities = _merge_entities(state_entities)

    # Deterministic ordering: frequency desc, then term asc.
    def sort_key(entity: CandidateEntity) -> tuple[Any, ...]:
        return (-entity.frequency, entity.term.casefold())

    ingredient_entities.sort(key=sort_key)
    technique_entities.sort(key=sort_key)
    state_entities.sort(key=sort_key)

    entities = ingredient_entities + technique_entities + state_entities
    vocabularies = [
        Vocabulary(name="ingredienti", entries=ingredient_entities),
        Vocabulary(name="tecnica", entries=technique_entities),
        Vocabulary(name="stati", entries=state_entities),
    ]

    units = [
        UnitObservation(
            unit=unit,
            frequency=frequency,
            examples=unit_examples[unit],
        )
        for unit, frequency in sorted(unit_counter.items(), key=lambda item: (-item[1], item[0]))
    ]

    ambiguities = _detect_ambiguities(ingredient_entities)

    stats = {
        "source_documents": len(source_corpus),
        "ingredient_entities": len(ingredient_entities),
        "technique_entities": len(technique_entities),
        "state_entities": len(state_entities),
        "unit_types": len(units),
        "ambiguities": len(ambiguities),
        "pairing_mismatches": pairing_mismatches,
    }

    return DomainBrief(
        domain=domain,
        language=language,
        canonical_language=canonical_language,
        version=version,
        corpus_size=len(source_corpus),
        entities=entities,
        vocabularies=vocabularies,
        units=units,
        ambiguities=ambiguities,
        ontologies=ONTOLOGY_CANDIDATES,
        stats=stats,
    )


def write_brief(brief: DomainBrief, path: str | Path) -> Path:
    """Serialize a brief to JSON (versioned artifact in the repo)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = brief.model_dump(mode="json", exclude_none=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_brief(path: str | Path) -> DomainBrief:
    """Load and validate a brief from JSON."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return DomainBrief.model_validate(raw)


def default_brief_path(version: str = "v1") -> Path:
    """Return the default versioned brief path for the ricette domain."""
    return DEFAULT_BRIEF_DIR / f"ricette-{version}.json"
