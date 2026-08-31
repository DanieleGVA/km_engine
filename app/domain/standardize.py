"""Passo 5 PROGRAMMA-UNICO: standardizzazione del dizionario (LLM batch).

Schema per voce (dalla spec §2.2): canonical_name_en (ordine culinario,
singolare), ingredient_core, states[], pack_format, class (enum chiuso),
aliases[], allergen_tags[] (EU-FIC 14), is_food, countable_unit +
count_policy (integer|exact) per i contabili, density_g_per_ml per i liquidi,
confidence, ambiguous.

Divieti nel prompt: mai fondere item d'acquisto distinti; mai inventare la
specie ("CHEESE GRATED" resta cheese + ambiguo); identita' solo dal contesto
fornito; stato e formato fuori dal nome canonico.

La consolidazione e' deterministica: validazione schema/enum, collisioni
(core+stato+formato uguali) come proposte di merge, coerenza (stesso core =>
stessa classe e stessi allergeni), allineamento cross-corpus SAME_AS.
Nessuna proposta viene applicata qui: decide l'umano (P5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.domain.llm import LLMClient
from app.domain.pack import EU_FIC_ALLERGENS, INGREDIENT_CLASSES

# Batch 40-60 voci (spec §2.2).
BATCH_SIZE = 50

# Prompt fisso (few-shot implicito nelle regole; ordine deterministico).
SYSTEM_PROMPT = (
    "You are a culinary ingredient standardizer for a recipe knowledge base.\n"
    "For each industrial ingredient entry, produce a canonical proposal.\n"
    "Rules (they outrank completeness):\n"
    "- NEVER merge distinct purchase items: one input entry -> one proposal.\n"
    "- NEVER invent the species: if the name is generic ('CHEESE GRATED'), "
    "the core is the generic term and ambiguous=true.\n"
    "- Identity comes ONLY from the provided context (forms, units, usage).\n"
    "- State and format go in states[]/pack_format, never in the canonical name.\n"
    "- canonical_name_en: culinary word order, singular.\n"
    "- class must be one of: " + ", ".join(sorted(INGREDIENT_CLASSES)) + "\n"
    "- allergen_tags must be EU-FIC: " + ", ".join(sorted(EU_FIC_ALLERGENS)) + "\n"
    "- countable items: countable_unit + count_policy ('integer'|'exact') + "
    "unit_weight_g; liquids: density_g_per_ml.\n"
    "Respond with a single JSON object: {\"entries\": [...]}."
)


class DictionaryEntryProposal(BaseModel):
    """Proposta di standardizzazione per una voce del dizionario."""

    key: str
    corpus: str = Field(pattern="^(msc|book)$")
    canonical_name_en: str
    ingredient_core: str
    states: list[str] = Field(default_factory=list)
    pack_format: str | None = None
    class_: str | None = Field(default=None, alias="class")
    aliases: list[str] = Field(default_factory=list)
    allergen_tags: list[str] = Field(default_factory=list)
    is_food: bool = True
    countable_unit: str | None = None
    count_policy: str | None = None
    unit_weight_g: float | None = None
    density_g_per_ml: float | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    ambiguous: bool = False

    @field_validator("class_")
    @classmethod
    def _class_in_enum(cls, value: str | None) -> str | None:
        if value is not None and value not in INGREDIENT_CLASSES:
            raise ValueError(
                f"class must be one of {sorted(INGREDIENT_CLASSES)}, got {value!r}"
            )
        return value

    @field_validator("allergen_tags")
    @classmethod
    def _allergens_valid(cls, value: list[str]) -> list[str]:
        unknown = set(value) - EU_FIC_ALLERGENS
        if unknown:
            raise ValueError(f"allergen_tags must be EU-FIC: unknown {sorted(unknown)}")
        return value

    @field_validator("count_policy")
    @classmethod
    def _count_policy(cls, value: str | None) -> str | None:
        if value is not None and value not in ("integer", "exact"):
            raise ValueError("count_policy must be 'integer' or 'exact'")
        return value


class BatchProposal(BaseModel):
    """Wrapper JSON per un batch di voci."""

    entries: list[DictionaryEntryProposal]


def _entry_context(entry: dict[str, Any]) -> str:
    return (
        f"- key={entry['key']!r} corpus={entry['corpus']!r} "
        f"forms={entry['forms']!r} units={entry['units']!r} "
        f"contexts={entry['contexts'][:3]!r}"
    )


def build_batch_prompt(batch: list[dict[str, Any]]) -> str:
    """Prompt utente deterministico per un batch (ordine per chiave)."""
    lines = [
        ("Standardize these dictionary entries (one proposal per entry, "
         "same order):")
    ]
    for entry in batch:
        lines.append(_entry_context(entry))
    return "\n".join(lines)


async def standardize_batch(
    judge: LLMClient, batch: list[dict[str, Any]]
) -> list[DictionaryEntryProposal]:
    """Chiama judge() su un batch e valida lo schema (retry interno a judge)."""
    result = await judge.judge(SYSTEM_PROMPT, build_batch_prompt(batch), BatchProposal)
    return [
        DictionaryEntryProposal.model_validate(entry) for entry in result["entries"]
    ]


# ---------------------------------------------------------------------------
# Consolidazione deterministica
# ---------------------------------------------------------------------------

@dataclass
class ConsolidationReport:
    """Esito della consolidazione: proposte, collisioni, SAME_AS, incoerenze."""

    proposals: list[DictionaryEntryProposal]
    collisions: list[dict[str, Any]]
    same_as: list[dict[str, Any]]
    incoherent: list[dict[str, Any]]


def consolidate(
    proposals: list[DictionaryEntryProposal],
) -> ConsolidationReport:
    """Validazione + collisioni + coerenza + SAME_AS (mai applicati, P5)."""
    # 1) validazione schema/enum: le voci non valide non sopravvivono
    valid: list[DictionaryEntryProposal] = []
    for p in proposals:
        try:
            DictionaryEntryProposal.model_validate(p.model_dump(by_alias=True))
            valid.append(p)
        except ValidationError:
            continue  # coda manuale (il chiamante le traccia)

    # 2) collisioni: stesso (core, states, pack_format) -> proposta di merge
    by_core: dict[tuple, list[DictionaryEntryProposal]] = {}
    for p in valid:
        key = (p.ingredient_core.casefold(),
               tuple(sorted(s.casefold() for s in p.states)),
               (p.pack_format or "").casefold())
        by_core.setdefault(key, []).append(p)
    collisions = [
        {"core": core[0], "states": list(core[1]), "pack_format": core[2] or None,
         "keys": [p.key for p in group]}
        for core, group in by_core.items() if len(group) > 1
    ]

    # 3) coerenza: stesso core => stessa classe e stessi allergeni
    class_by_core: dict[str, set[str]] = {}
    allergens_by_core: dict[str, set[str]] = {}
    for p in valid:
        class_by_core.setdefault(p.ingredient_core.casefold(), set()).add(p.class_ or "")
        allergens_by_core.setdefault(p.ingredient_core.casefold(), set()).update(
            p.allergen_tags
        )
    incoherent = [
        {"core": core, "classes": sorted(cls),
         "allergens": sorted(allergens_by_core[core])}
        for core, cls in class_by_core.items()
        if len(cls) > 1 or len(allergens_by_core[core]) > 1
    ]

    # 4) SAME_AS cross-corpus: stesse (core, states) MSC <-> libro
    msc = {p.key: p for p in valid if p.corpus == "msc"}
    book = {p.key: p for p in valid if p.corpus == "book"}
    same_as: list[dict[str, Any]] = []
    for m in msc.values():
        for b in book.values():
            if (m.ingredient_core.casefold() == b.ingredient_core.casefold()
                    and sorted(s.casefold() for s in m.states)
                    == sorted(s.casefold() for s in b.states)):
                same_as.append({"msc": m.key, "book": b.key,
                                "core": m.ingredient_core})
    return ConsolidationReport(
        proposals=valid, collisions=collisions,
        same_as=same_as, incoherent=incoherent,
    )
