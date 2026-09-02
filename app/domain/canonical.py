"""Stage 2 canonicalization (WP-A5): deterministic translated -> canonical.

``canonicalize`` never calls an LLM. It reuses the stage-1 parser
(:func:`app.domain.verify.parse_translated_md`), applies the unit rules from
``units.yaml`` with exact ``Decimal`` arithmetic, normalizes ingredient terms
through the pack glossaries (exact full-phrase match, longest-first map) and
rewrites the document to the Appendix A shape.

Every difference between ``translated.md`` and ``canonical.md`` is emitted as a
:class:`CanonLogEntry`. The invariant checked by :func:`verify_canon_log` is
bidirectional: applying the entries to ``translated.md`` reproduces
``canonical.md`` exactly, and no entry is a no-op (no orphan rows).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import psycopg

from app.domain.errors import DomainError
from app.domain.normalize import normalize_key
from app.domain.pack import DomainPackBundle, UnitRule
from app.domain.verify import (
    IngredientLine,
    ParsedDoc,
    create_glossary_proposal,
    parse_translated_md,
    render_ingredient_line,
)

# Structural rule ids used when a change is not a unit/glossary rule.
RULE_STRUCT_FRONTMATTER = "STRUCT-FRONTMATTER"
RULE_STRUCT_INGREDIENT = "STRUCT-INGREDIENT"
RULE_STRUCT_METHOD = "STRUCT-METHOD"
RULE_STRUCT_SERIALIZATION = "STRUCT-SERIALIZATION"
RULE_STRUCT_UNIT = "STRUCT-UNIT"
RULE_GLOSSARY_LITERAL = "GLOSSARY-LITERAL"

CANONICAL_FRONTMATTER_ORDER = (
    "title",
    "id",
    "lang",
    "source_lang",
    "servings",
    "time_min",
    "difficulty",
    "verification_level",
    "canonical_version",
)

class CanonicalizationError(DomainError):
    """Base error for stage-2 canonicalization."""


class CanonLogVerificationError(CanonicalizationError):
    """Raised when the canon-log invariant (T9) is violated."""


@dataclass(frozen=True)
class CanonLogEntry:
    """One explained difference between translated and canonical markdown."""

    document_id: str
    field: str
    before_text: str
    after_text: str
    rule_id: str


@dataclass
class CanonicalDocument:
    """Result of :func:`canonicalize`."""

    canonical_md: str
    document_id: str
    title: str
    parsed: ParsedDoc
    log_entries: list[CanonLogEntry]
    unresolved_terms: list[str]


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

def _apply_unit_rule(qty: Decimal, rule: UnitRule) -> Decimal:
    """Exact Decimal conversion with the rule's declared rounding.

    ``rounding=None`` keeps the exact product; ``0`` rounds to an integer;
    ``n > 0`` keeps ``n`` decimal places. Rounding mode is half-up (declared
    here because ``UnitRule`` only declares the precision, not the mode).
    """
    value = qty * Decimal(str(rule.factor))
    if rule.rounding is None:
        return value
    if rule.rounding == 0:
        return value.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return value.quantize(
        Decimal("1." + "0" * rule.rounding), rounding=ROUND_HALF_UP
    )


def _convert_quantity(qty: str | None, rule: UnitRule | None) -> str | None:
    """Applica la regola di unita' a una quantita' che puo' essere assente."""
    if qty is None:
        return None
    value = Decimal(qty)
    if rule is not None:
        value = _apply_unit_rule(value, rule)
    return _format_decimal(value)


def _format_decimal(value: Decimal) -> str:
    """Serialize a Decimal per Appendix A (int without .0, max 3 decimals)."""
    if value == value.to_integral_value():
        return str(int(value))
    quantized = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    return text.rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Glossary helpers
# ---------------------------------------------------------------------------

def _build_term_map(pack: DomainPackBundle) -> dict[str, tuple[str, str]]:
    """Map ``normalize_key(term) -> (labels_en, glossary_id)``.

    La chiave e' prodotta da :func:`app.domain.normalize.normalize_key`, la
    stessa funzione applicata all'item da canonicalizzare (WP-F1): senza
    questa simmetria ``olio extravergine di oliva`` nel glossario e
    ``olio extravergine oliva`` nell'item non si incontrano mai (D2).

    Exact full-phrase matching is deliberate: a term with an extra modifier
    (e.g. ``mandorle dolci sbucciate``) must NOT resolve to the shorter alias
    ``mandorle dolci`` (T10). The map is built longest-first so duplicate
    normalized keys keep the longest source term deterministically.
    """
    pairs: list[tuple[str, str, str]] = []
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            if key:
                pairs.append((key, entry.labels_en, entry.id))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    term_map: dict[str, tuple[str, str]] = {}
    for term, label_en, entry_id in pairs:
        term_map.setdefault(term, (label_en, entry_id))
    return term_map


def _glossary_id_for_label(pack: DomainPackBundle, label: str) -> str | None:
    """Return the glossary id whose ``labels_en`` equals ``label``."""
    key = normalize_key(label)
    for entry in pack.glossary_entries():
        if normalize_key(entry.labels_en) == key:
            return entry.id
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fm_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render_canonical_md(
    frontmatter: dict[str, str],
    ingredients: list[IngredientLine],
    steps: list[str],
) -> str:
    """Render the Appendix A canonical markdown (single trailing newline).

    Unica implementazione: ``recompose`` la importa invece di rispecchiarla,
    cosi' il round-trip T9/T11 non puo' rompersi per una divergenza di
    formattazione fra due copie.
    """
    lines = ["---"]
    for key in CANONICAL_FRONTMATTER_ORDER:
        if key in frontmatter:
            lines.append(f"{key}: {frontmatter[key]}")
    lines.append("---")
    lines.append("## Ingredients")
    lines.extend(render_ingredient_line(ingredient) for ingredient in ingredients)
    lines.append("## Method")
    for index, step in enumerate(steps, start=1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines) + "\n"


# Nome storico mantenuto per i chiamanti interni.
_render_canonical_md = render_canonical_md


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------

def canonicalize(
    pack: DomainPackBundle,
    translated_md: str,
    conn: psycopg.Connection | None = None,
) -> CanonicalDocument:
    """Deterministically canonicalize a translated document (never an LLM).

    Steps (fixed order): parse ``translated.md``, convert units with exact
    Decimal arithmetic, normalize ingredient terms through the glossaries
    (unresolved terms are queued in ``glossary_proposals`` when ``conn`` is
    given and are never rewritten), then rewrite to the Appendix A shape.
    """
    units = pack.known_units()
    countable_units = pack.countable_units()
    parsed = parse_translated_md(
        translated_md, known_units=units,
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=countable_units,
    )
    document_id = _fm_str(parsed.frontmatter.get("id", ""))

    frontmatter: dict[str, str] = {
        "title": _fm_str(parsed.frontmatter.get("title", "")),
        "id": document_id,
        "lang": pack.canonical_language,
        "source_lang": _fm_str(
            parsed.frontmatter.get("source_lang", pack.language)
        ),
        "servings": _fm_str(parsed.frontmatter.get("servings", "")),
        "verification_level": "L1",
        "canonical_version": "1",
    }
    # time_min/difficulty: presenti solo se nel documento (mai placeholder,
    # P3) — le card MSC EN-native non li hanno
    if parsed.frontmatter.get("time_min") is not None:
        frontmatter["time_min"] = _fm_str(parsed.frontmatter["time_min"])
    if parsed.frontmatter.get("difficulty") is not None:
        frontmatter["difficulty"] = _fm_str(parsed.frontmatter["difficulty"])

    term_map = _build_term_map(pack)
    msc_mapping = pack.msc_mapping()
    ingredients: list[IngredientLine] = []
    unresolved: list[str] = []
    seen_unresolved: set[str] = set()

    for ingredient in parsed.ingredients:
        rule = pack.unit_rule_for(ingredient.unit)
        unit = rule.to_unit if rule is not None else ingredient.unit
        # WP-F3: una riga senza dose ("q.b. sale") non ha una quantita' da
        # convertire; l'unita' viene comunque rinominata.
        qty_text = _convert_quantity(ingredient.qty, rule)
        qty_max_text = _convert_quantity(ingredient.qty_max, rule)
        item = ingredient.item
        resolved = None
        if ingredient.code and ingredient.code in msc_mapping:
            # code-first (passo 7): l'identita' (item code) prevale sulla
            # stringa; il canon-log registra MAP-<code>@versione
            item = msc_mapping[ingredient.code]
            resolved = (item, "MAP")
        else:
            lookup = normalize_key(item)
            resolved = term_map.get(lookup)
            if resolved is None and unit is not None and unit in countable_units:
                # "2 egg whites" -> unit=egg, item=whites: prova "egg whites"
                resolved = term_map.get(normalize_key(f"{unit} {item}"))
                if resolved is not None:
                    item = resolved[0]
                    unit = None  # il nome contiene gia' il contabile
        if resolved is not None:
            item = resolved[0]
        elif lookup not in seen_unresolved:
            seen_unresolved.add(lookup)
            unresolved.append(lookup)

        # unita' di conteggio che duplica il nome nell'item ("2 egg egg yolk"):
        # l'unita' cade, il nome contiene gia' il contabile
        if (unit is not None and unit in countable_units
                and item.casefold().startswith(unit.casefold() + " ")):
            unit = None

        ingredients.append(
            IngredientLine(
                raw=ingredient.raw,
                qty=qty_text,
                unit=unit,
                item=item,
                code=ingredient.code,
                waste=ingredient.waste,
                component=ingredient.component,
                qty_max=qty_max_text,
                to_taste=ingredient.to_taste,
            )
        )

    steps = list(parsed.steps)
    canonical_md = _render_canonical_md(frontmatter, ingredients, steps)
    entries = generate_canon_log(pack, translated_md, canonical_md)

    if conn is not None:
        for term in unresolved:
            create_glossary_proposal(conn, term, context=document_id)

    canonical_parsed = parse_translated_md(
        canonical_md, known_units=units,
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    return CanonicalDocument(
        canonical_md=canonical_md,
        document_id=document_id,
        title=frontmatter["title"],
        parsed=canonical_parsed,
        log_entries=entries,
        unresolved_terms=unresolved,
    )


# ---------------------------------------------------------------------------
# Canon-log generation and verification
# ---------------------------------------------------------------------------

def generate_canon_log(
    pack: DomainPackBundle,
    translated_md: str,
    canonical_md: str,
) -> list[CanonLogEntry]:
    """Emit one :class:`CanonLogEntry` for every translated->canonical diff."""
    units = pack.known_units()
    translated = parse_translated_md(
        translated_md, known_units=units,
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    canonical = parse_translated_md(
        canonical_md, known_units=units,
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    document_id = _fm_str(
        canonical.frontmatter.get("id") or translated.frontmatter.get("id")
    )
    entries: list[CanonLogEntry] = []

    # Frontmatter: compare by key (renderer fixes the output order).
    translated_fm = {k: _fm_str(v) for k, v in translated.frontmatter.items()}
    canonical_fm = {k: _fm_str(v) for k, v in canonical.frontmatter.items()}
    fm_keys: list[str] = []
    for key in CANONICAL_FRONTMATTER_ORDER:
        if key in translated_fm or key in canonical_fm:
            fm_keys.append(key)
    for key in translated_fm:
        if key not in fm_keys:
            fm_keys.append(key)
    for key in canonical_fm:
        if key not in fm_keys:
            fm_keys.append(key)
    for key in fm_keys:
        before = translated_fm.get(key, "")
        after = canonical_fm.get(key, "")
        if before != after:
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"frontmatter.{key}",
                    before,
                    after,
                    RULE_STRUCT_FRONTMATTER,
                )
            )

    # Ingredients: qty, unit and item are compared independently.
    translated_ings = translated.ingredients
    canonical_ings = canonical.ingredients
    for index in range(max(len(translated_ings), len(canonical_ings))):
        if index >= len(translated_ings):
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}]",
                    "",
                    _ingredient_line(canonical_ings[index]),
                    RULE_STRUCT_INGREDIENT,
                )
            )
            continue
        if index >= len(canonical_ings):
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}]",
                    _ingredient_line(translated_ings[index]),
                    "",
                    RULE_STRUCT_INGREDIENT,
                )
            )
            continue

        before_ing = translated_ings[index]
        after_ing = canonical_ings[index]

        if before_ing.qty != after_ing.qty:
            rule = pack.unit_rule_for(before_ing.unit) or pack.unit_rule_for(
                after_ing.unit
            )
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}].qty",
                    before_ing.qty or "",
                    after_ing.qty or "",
                    rule.rule_id if rule else RULE_STRUCT_SERIALIZATION,
                )
            )

        if before_ing.qty_max != after_ing.qty_max:
            rule = pack.unit_rule_for(before_ing.unit) or pack.unit_rule_for(
                after_ing.unit
            )
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}].qty_max",
                    before_ing.qty_max or "",
                    after_ing.qty_max or "",
                    rule.rule_id if rule else RULE_STRUCT_SERIALIZATION,
                )
            )

        before_unit = before_ing.unit or ""
        after_unit = after_ing.unit or ""
        if before_unit != after_unit:
            rule = pack.unit_rule_for(before_ing.unit) or pack.unit_rule_for(
                after_ing.unit
            )
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}].unit",
                    before_unit,
                    after_unit,
                    rule.rule_id if rule else RULE_STRUCT_UNIT,
                )
            )

        if before_ing.item != after_ing.item:
            rule_id = _glossary_id_for_label(pack, after_ing.item)
            if before_ing.code and before_ing.code in pack.msc_mapping():
                # code-first (passo 7): rule_id = MAP-<code>@versione
                rule_id = f"MAP-{before_ing.code}@{pack.pack.version}"
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"ingredients[{index}].item",
                    before_ing.item,
                    after_ing.item,
                    rule_id or RULE_GLOSSARY_LITERAL,
                )
            )

    # Steps: text is preserved by canonicalization; log any difference.
    translated_steps = translated.steps
    canonical_steps = canonical.steps
    for index in range(max(len(translated_steps), len(canonical_steps))):
        if index >= len(translated_steps):
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"steps[{index}]",
                    "",
                    canonical_steps[index],
                    RULE_STRUCT_METHOD,
                )
            )
        elif index >= len(canonical_steps):
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"steps[{index}]",
                    translated_steps[index],
                    "",
                    RULE_STRUCT_METHOD,
                )
            )
        elif translated_steps[index] != canonical_steps[index]:
            entries.append(
                CanonLogEntry(
                    document_id,
                    f"steps[{index}]",
                    translated_steps[index],
                    canonical_steps[index],
                    RULE_STRUCT_METHOD,
                )
            )

    return entries


def _ingredient_line(ingredient: IngredientLine) -> str:
    """Riga completa (usata nelle entry di canon-log strutturali)."""
    return render_ingredient_line(ingredient).removeprefix("- ")


def verify_canon_log(
    pack: DomainPackBundle,
    translated_md: str,
    canonical_md: str,
    entries: list[CanonLogEntry],
) -> bool:
    """Check the T9 invariant: entries fully and exactly explain the diff.

    Returns ``True`` when applying the entries to ``translated_md`` reproduces
    ``canonical_md`` byte-for-byte and no entry is a no-op. Raises
    :class:`CanonLogVerificationError` otherwise.
    """
    units = pack.known_units()
    parsed = parse_translated_md(
        translated_md, known_units=units,
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    frontmatter = {k: _fm_str(v) for k, v in parsed.frontmatter.items()}
    ingredients: list[dict[str, Any]] = [
        {
            "qty": ing.qty,
            "qty_max": ing.qty_max,
            "to_taste": ing.to_taste,
            "unit": ing.unit,
            "item": ing.item,
            "code": ing.code,
            "waste": ing.waste,
            "component": ing.component,
        }
        for ing in parsed.ingredients
    ]
    steps = list(parsed.steps)

    for entry in entries:
        if entry.before_text == entry.after_text:
            raise CanonLogVerificationError(
                f"orphan canon-log entry (no change) for {entry.field!r}"
            )

        field = entry.field
        if field.startswith("frontmatter."):
            key = field[len("frontmatter."):]
            current = frontmatter.get(key, "")
            if current != entry.before_text:
                raise CanonLogVerificationError(
                    f"canon-log entry {field!r} before_text {entry.before_text!r} "
                    f"does not match translated value {current!r}"
                )
            if entry.after_text == "":
                frontmatter.pop(key, None)
            else:
                frontmatter[key] = entry.after_text
            continue

        if field.startswith("ingredients["):
            index = int(field.split("[")[1].split("]")[0])
            subfield = field.split(".")[1]
            ingredient = ingredients[index]
            if subfield in ("qty", "qty_max", "unit"):
                current = ingredient[subfield] or ""
            elif subfield == "item":
                current = ingredient["item"]
            else:
                raise CanonLogVerificationError(
                    f"unknown canon-log ingredient subfield {subfield!r}"
                )
            if current != entry.before_text:
                raise CanonLogVerificationError(
                    f"canon-log entry {field!r} before_text {entry.before_text!r} "
                    f"does not match translated value {current!r}"
                )
            if subfield == "item":
                ingredient["item"] = entry.after_text
            else:
                ingredient[subfield] = entry.after_text or None
            continue

        if field.startswith("steps["):
            index = int(field.split("[")[1].split("]")[0])
            current = steps[index]
            if current != entry.before_text:
                raise CanonLogVerificationError(
                    f"canon-log entry {field!r} before_text {entry.before_text!r} "
                    f"does not match translated value {current!r}"
                )
            steps[index] = entry.after_text
            continue

        raise CanonLogVerificationError(f"unknown canon-log field {field!r}")

    rebuilt = [
        IngredientLine(
            raw="",
            qty=ing["qty"],
            unit=ing["unit"],
            item=str(ing["item"]),
            code=ing.get("code"),
            waste=ing.get("waste"),
            component=ing.get("component"),
            qty_max=ing.get("qty_max"),
            to_taste=bool(ing.get("to_taste")),
        )
        for ing in ingredients
    ]
    reconstructed = render_canonical_md(frontmatter, rebuilt, steps)
    if reconstructed != canonical_md:
        raise CanonLogVerificationError(
            "canon-log does not fully explain the translated->canonical diff: "
            "reconstructed canonical markdown differs from canonical_md"
        )
    return True


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def write_canon_log(
    conn: psycopg.Connection, entries: list[CanonLogEntry]
) -> int:
    """Persist canon-log entries and return the number of rows written."""
    if not entries:
        return 0
    rows = [
        (entry.document_id, entry.field, entry.before_text, entry.after_text, entry.rule_id)
        for entry in entries
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """
                INSERT INTO canon_log
                    (document_id, field, before_text, after_text, rule_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
            rows,
        )
    return len(rows)