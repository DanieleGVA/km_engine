"""Verification engine (WP-A3): template parser + L1/L2/L3.

- ``parse_source_md`` / ``parse_translated_md`` parse the two IR stages.
- ``verify_l1`` checks structure and the P2 number invariant (no LLM).
- ``verify_l2`` compares sections with deterministic glossary-normalized
  token overlap and escalates divergent sections to L3.
- L3 is the Postgres adjudication queue (``adjudications``) plus the
  ``glossary_proposals`` queue used by canonicalization (WP-A5).
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
import yaml
from psycopg.rows import dict_row

from app.auth import record_audit
from app.domain.errors import (
    AdjudicationAlreadyResolvedError,
    AdjudicationNotFoundError,
    GlossaryProposalAlreadyResolvedError,
    GlossaryProposalNotFoundError,
    ParseError,
)
from app.domain.numbers import extract_numbers, numbers_multiset_equal
from app.domain.pack import DomainPackBundle

DIFFICULTY_MAP = {"facile": "easy", "medio": "medium", "difficile": "hard"}
DIFFICULTY_EN = {"easy", "medium", "hard"}

DEFAULT_KNOWN_UNITS: set[str] = {
    "g", "kg", "ml", "l", "dl", "°C", "min", "h",
    "cucchiaio", "cucchiai", "tablespoon", "tablespoons",
    "tazza", "tazze", "cup", "cups",
    "pizzico", "pizzichi", "pinch", "pinches",
    "spicchio", "spicchi", "clove", "cloves",
    "foglia", "foglie", "leaf", "leaves",
    "rametto", "rametti", "sprig", "sprigs",
    "bustina", "bustine", "sachet", "sachets",
    "mazzetto", "mazzetti", "bunch", "bunches",
}

_TOKEN_RE = re.compile(r"[^\W\d_]+", flags=re.UNICODE)
_INGREDIENT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s+(.*)$")
_STEP_RE = re.compile(r"^(\d+)\.\s+(.*)$")

# Suffisso strutturale (passo 1): "{code: X, waste: Y%, component: Z}" in coda
# alla riga ingrediente. Mai parte dell'item: e' metadato (item code, sfrido,
# componente) escluso da L2 e da P2.
_SUFFIX_RE = re.compile(
    r"\s*\{code:\s*([^,}]+?)(?:,\s*waste:\s*([^,}]+?))?"
    r"(?:,\s*component:\s*([^,}]+?))?\}\s*$"
)


def render_ingredient_suffix(
    code: str | None, waste: str | None, component: str | None
) -> str:
    """Rende il suffisso strutturale (speculare a ``_SUFFIX_RE``)."""
    parts: list[str] = []
    if code:
        parts.append(f"code: {code}")
    if waste:
        parts.append(f"waste: {waste}")
    if component:
        parts.append(f"component: {component}")
    return " {" + ", ".join(parts) + "}" if parts else ""

VALID_ADJUDICATION_STATUSES = {"pending", "approved", "rejected"}
VALID_DECISIONS = {"approved", "rejected"}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IngredientLine:
    """One parsed ingredient line (``- {qty} {unit} {item}``).

    ``code``/``waste``/``component`` are structural metadata carried in the
    ``{code: ..., waste: ..., component: ...}`` suffix (passo 1 PROGRAMMA-UNICO):
    they are never part of ``item``, never cross the LLM, and are excluded
    from L2 tokens and P2 numbers.
    """

    raw: str
    qty: str
    unit: str | None
    item: str
    code: str | None = None
    waste: str | None = None
    component: str | None = None


@dataclass
class ParsedDoc:
    """A parsed markdown document in the recipe template."""

    frontmatter: dict[str, Any]
    title: str
    ingredients: list[IngredientLine]
    steps: list[str]
    body: str
    source_md: str


def _split_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    lines = md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ParseError("missing frontmatter: document must start with '---'", line=1)
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise ParseError("unterminated frontmatter: missing closing '---'", line=1)
    fm_text = "\n".join(lines[1:end])
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as exc:
        raise ParseError(f"invalid frontmatter YAML: {exc}", line=1) from exc
    if not isinstance(fm, dict):
        raise ParseError("frontmatter must be a YAML mapping", line=1)
    return fm, "\n".join(lines[end + 1:])


def _validate_frontmatter(
    fm: dict[str, Any],
    *,
    require_source_lang: bool,
    optional_when_native: tuple[str, ...] = (),
) -> None:
    """Validate frontmatter keys.

    ``optional_when_native``: keys that may be absent when the document is
    native in the canonical language (``lang == source_lang``), e.g. MSC cards
    have no ``time_min``/``difficulty``. Never filled with a placeholder.
    """
    required = ["title", "id", "lang", "servings", "time_min", "difficulty"]
    if require_source_lang:
        required.append("source_lang")
    native = require_source_lang and fm.get("lang") == fm.get("source_lang")
    for key in required:
        if native and key in optional_when_native:
            continue
        if key not in fm:
            raise ParseError(f"missing required frontmatter key {key!r}", line=1)
    if isinstance(fm["servings"], bool) or not isinstance(fm["servings"], int):
        raise ParseError("frontmatter 'servings' must be an integer", line=1)
    if "time_min" in fm and (
        isinstance(fm["time_min"], bool) or not isinstance(fm["time_min"], int)
    ):
        raise ParseError("frontmatter 'time_min' must be an integer", line=1)
    allowed_difficulty = DIFFICULTY_EN if require_source_lang else set(DIFFICULTY_MAP)
    if "difficulty" in fm and fm["difficulty"] not in allowed_difficulty:
        raise ParseError(
            f"frontmatter 'difficulty' must be one of {sorted(allowed_difficulty)}",
            line=1,
        )


def _parse_ingredient(
    content: str,
    line_no: int,
    known_units: set[str] | None,
    countable_units: set[str] | None = None,
) -> IngredientLine:
    match = _INGREDIENT_RE.match(content)
    if not match:
        raise ParseError(
            f"line {line_no}: ingredient must start with a quantity "
            f"(e.g. '200 g flour'), got {content!r}",
            line=line_no,
        )
    qty = match.group(1)
    rest = match.group(2).strip()
    if not rest:
        raise ParseError(f"line {line_no}: ingredient item is empty", line=line_no)

    units = known_units if known_units is not None else DEFAULT_KNOWN_UNITS
    countable = countable_units if countable_units is not None else set()
    unit: str | None = None
    item = rest
    first, _sep, remainder = rest.partition(" ")
    if first in units:
        if remainder.strip():
            if first == "egg" and remainder.strip().casefold() in (
                "whites", "white", "yolk", "yolks",
            ):
                # composto: "egg whites"/"egg yolk" e' un unico ingrediente,
                # non unita' + item (il grafo e l'embedding devono vedere
                # "egg whites", non "whites")
                item = rest
            else:
                unit = first
                item = remainder.strip()
        elif first in countable:
            # unita' di conteggio da sola = ingrediente ("- 4 eggs")
            item = first
        else:
            item = ""
    if not item:
        raise ParseError(f"line {line_no}: ingredient item is empty", line=line_no)

    # suffisso strutturale {code, waste, component}: mai parte dell'item
    code = waste = component = None
    suffix = _SUFFIX_RE.search(item)
    if suffix:
        code = suffix.group(1).strip() or None
        waste = suffix.group(2).strip() if suffix.group(2) else None
        component = suffix.group(3).strip() if suffix.group(3) else None
        item = item[: suffix.start()].rstrip()
        if not item:
            raise ParseError(f"line {line_no}: ingredient item is empty", line=line_no)
    return IngredientLine(
        raw=content, qty=qty, unit=unit, item=item,
        code=code, waste=waste, component=component,
    )


def _parse_sections(
    body: str,
    ingredient_section: str,
    method_section: str,
    known_units: set[str] | None,
    countable_units: set[str] | None = None,
) -> tuple[str | None, list[IngredientLine], list[str]]:
    lines = body.splitlines()
    ingredient_idx: int | None = None
    method_idx: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"## {ingredient_section}":
            ingredient_idx = index
        elif stripped == f"## {method_section}":
            method_idx = index

    if ingredient_idx is None:
        raise ParseError(f"missing section '## {ingredient_section}'")
    if method_idx is None:
        raise ParseError(f"missing section '## {method_section}'")
    if method_idx < ingredient_idx:
        raise ParseError(
            f"section '## {method_section}' must come after '## {ingredient_section}'",
            line=method_idx + 1,
        )

    body_title: str | None = None
    for line in lines[:ingredient_idx]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            body_title = stripped
            break

    ingredients: list[IngredientLine] = []
    for index in range(ingredient_idx + 1, method_idx):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("- "):
            raise ParseError(
                f"line {index + 1}: expected ingredient line starting with '- '",
                line=index + 1,
            )
        ingredients.append(
            _parse_ingredient(
                stripped[2:], index + 1, known_units, countable_units
            )
        )
    if not ingredients:
        raise ParseError("no ingredients found", line=ingredient_idx + 1)

    steps: list[str] = []
    expected = 1
    for index in range(method_idx + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            continue
        match = _STEP_RE.match(stripped)
        if not match:
            raise ParseError(
                f"line {index + 1}: expected numbered step 'N. text'",
                line=index + 1,
            )
        number = int(match.group(1))
        if number != expected:
            raise ParseError(
                f"line {index + 1}: expected step {expected}, got {number}",
                line=index + 1,
            )
        text = match.group(2).strip()
        if not text:
            raise ParseError(f"line {index + 1}: step text is empty", line=index + 1)
        steps.append(text)
        expected += 1
    if not steps:
        raise ParseError("no steps found", line=method_idx + 1)

    return body_title, ingredients, steps


def _parse_document(
    md: str,
    ingredient_section: str,
    method_section: str,
    known_units: set[str] | None,
    *,
    require_source_lang: bool,
    optional_when_native: tuple[str, ...] = (),
    countable_units: set[str] | None = None,
) -> ParsedDoc:
    frontmatter, body = _split_frontmatter(md)
    _validate_frontmatter(
        frontmatter,
        require_source_lang=require_source_lang,
        optional_when_native=optional_when_native,
    )
    body_title, ingredients, steps = _parse_sections(
        body, ingredient_section, method_section, known_units, countable_units
    )
    title = body_title or str(frontmatter["title"])
    return ParsedDoc(
        frontmatter=frontmatter,
        title=title,
        ingredients=ingredients,
        steps=steps,
        body=body,
        source_md=md,
    )


def parse_source_md(
    md: str,
    *,
    known_units: set[str] | None = None,
    countable_units: set[str] | None = None,
) -> ParsedDoc:
    """Parse an Italian source document (``## Ingredienti`` / ``## Procedimento``)."""
    return _parse_document(
        md, "Ingredienti", "Procedimento", known_units,
        require_source_lang=False, countable_units=countable_units,
    )


def parse_translated_md(
    md: str,
    *,
    known_units: set[str] | None = None,
    optional_when_native: tuple[str, ...] = (),
    countable_units: set[str] | None = None,
) -> ParsedDoc:
    """Parse an English translated document (``## Ingredients`` / ``## Method``).

    ``optional_when_native``: frontmatter keys that may be absent when the
    document is native in English (``lang == source_lang``), e.g. MSC cards
    without ``time_min``/``difficulty``.
    """
    return _parse_document(
        md,
        "Ingredients",
        "Method",
        known_units,
        require_source_lang=True,
        optional_when_native=optional_when_native,
        countable_units=countable_units,
    )


def parse_translated_body(
    body: str,
    *,
    known_units: set[str] | None = None,
    countable_units: set[str] | None = None,
) -> tuple[str, list[IngredientLine], list[str]]:
    """Parse an LLM-translated body (title line + ``## Ingredients``/``## Method``).

    Unlike :func:`parse_translated_md`, the title is read from the body because
    the LLM output has no frontmatter.
    """
    body_title, ingredients, steps = _parse_sections(
        body, "Ingredients", "Method", known_units, countable_units
    )
    if body_title is None:
        raise ParseError("translated body is missing a title line", line=1)
    return body_title, ingredients, steps


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------

@dataclass
class VerificationIssue:
    """One explicit verification problem."""

    level: str
    code: str
    message: str
    section: str | None = None
    line: int | None = None


@dataclass
class L1Report:
    """Result of ``verify_l1``."""

    passed: bool
    issues: list[VerificationIssue]
    source_numbers: list[str]
    translated_numbers: list[str]
    source_parsed: ParsedDoc | None
    translated_parsed: ParsedDoc | None


def _check_frontmatter_l1(
    source: ParsedDoc,
    translated: ParsedDoc,
    issues: list[VerificationIssue],
    pack: DomainPackBundle | None,
) -> None:
    sfm = source.frontmatter
    tfm = translated.frontmatter

    if sfm.get("id") != tfm.get("id"):
        issues.append(
            VerificationIssue(
                "L1", "ID_MISMATCH",
                f"id mismatch: source={sfm.get('id')!r} translated={tfm.get('id')!r}",
            )
        )
    if tfm.get("lang") != "en":
        issues.append(
            VerificationIssue(
                "L1", "LANG_MISMATCH",
                f"translated lang must be 'en', got {tfm.get('lang')!r}",
            )
        )
    expected_source_lang = sfm.get("lang")
    if tfm.get("source_lang") != expected_source_lang:
        issues.append(
            VerificationIssue(
                "L1", "SOURCE_LANG_MISMATCH",
                f"translated source_lang must be {expected_source_lang!r}, "
                f"got {tfm.get('source_lang')!r}",
            )
        )
    if sfm.get("servings") != tfm.get("servings"):
        issues.append(
            VerificationIssue(
                "L1", "SERVINGS_MISMATCH",
                f"servings mismatch: source={sfm.get('servings')!r} "
                f"translated={tfm.get('servings')!r}",
            )
        )
    if sfm.get("time_min") != tfm.get("time_min"):
        issues.append(
            VerificationIssue(
                "L1", "TIME_MIN_MISMATCH",
                f"time_min mismatch: source={sfm.get('time_min')!r} "
                f"translated={tfm.get('time_min')!r}",
            )
        )
    expected_difficulty = DIFFICULTY_MAP.get(sfm.get("difficulty"))
    if tfm.get("difficulty") != expected_difficulty:
        issues.append(
            VerificationIssue(
                "L1", "DIFFICULTY_MISMATCH",
                f"difficulty mismatch: expected {expected_difficulty!r}, "
                f"got {tfm.get('difficulty')!r}",
            )
        )


def verify_l1(
    source_md: str,
    translated_md: str,
    *,
    pack: DomainPackBundle | None = None,
) -> L1Report:
    """Run deterministic L1 verification (structure + P2 numbers)."""
    units = pack.known_units() if pack is not None else None
    countable = pack.countable_units() if pack is not None else None
    issues: list[VerificationIssue] = []

    source: ParsedDoc | None = None
    translated: ParsedDoc | None = None
    try:
        translated = parse_translated_md(
            translated_md,
            known_units=units,
            optional_when_native=tuple(pack.frontmatter_optional_when_native)
            if pack is not None else (),
            countable_units=countable,
        )
    except ParseError as exc:
        issues.append(
            VerificationIssue("L1", "TRANSLATED_PARSE", str(exc), line=exc.line)
        )
    if source_md == translated_md and translated is not None:
        # documento nativo EN (es. card MSC, bypass stage-1): non esiste un
        # sorgente IT separato — source == translated per definizione
        source = translated
    else:
        try:
            source = parse_source_md(
                source_md, known_units=units, countable_units=countable
            )
        except ParseError as exc:
            issues.append(
                VerificationIssue("L1", "SOURCE_PARSE", str(exc), line=exc.line)
            )

    source_numbers = extract_numbers(source_md)
    translated_numbers = extract_numbers(translated_md)

    if source is not None and translated is not None:
        _check_frontmatter_l1(source, translated, issues, pack)
        if len(source.ingredients) != len(translated.ingredients):
            issues.append(
                VerificationIssue(
                    "L1", "INGREDIENT_COUNT_MISMATCH",
                    f"ingredient count mismatch: source={len(source.ingredients)} "
                    f"translated={len(translated.ingredients)}",
                    section="ingredients",
                )
            )
        if len(source.steps) != len(translated.steps):
            issues.append(
                VerificationIssue(
                    "L1", "STEP_COUNT_MISMATCH",
                    f"step count mismatch: source={len(source.steps)} "
                    f"translated={len(translated.steps)}",
                    section="steps",
                )
            )

    if not numbers_multiset_equal(source_numbers, translated_numbers):
        issues.append(
            VerificationIssue(
                "L1", "P2_NUMBER_INVARIANT",
                f"P2 number multiset mismatch: source={source_numbers} "
                f"translated={translated_numbers}",
            )
        )

    return L1Report(
        passed=not issues,
        issues=issues,
        source_numbers=source_numbers,
        translated_numbers=translated_numbers,
        source_parsed=source,
        translated_parsed=translated,
    )


# ---------------------------------------------------------------------------
# L2
# ---------------------------------------------------------------------------

@dataclass
class SectionComparison:
    """Token-overlap comparison for one section."""

    section: str
    overlap: float
    threshold: float
    divergent: bool
    source_tokens: list[str]
    translated_tokens: list[str]


@dataclass
class L2Report:
    """Result of ``verify_l2``."""

    passed: bool
    sections: list[SectionComparison]
    issues: list[VerificationIssue]
    escalations: list[VerificationIssue]


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.casefold())


def normalize_terms(text: str, term_map: list[tuple[str, str]]) -> str:
    """Replace glossary terms with their canonical English label.

    Single-pass alternation (longest term first) so a shorter alias that is a
    substring of a longer term or of its replacement is not re-replaced.
    """
    if not term_map:
        return text.casefold().replace("\u2019", "'")
    # Normalizza gli apostrofi unicode (U+2019) a ASCII: i testi reali usano
    # "sott\u2019olio", i glossari "sott'olio".
    norm_terms = [(t.casefold().replace("\u2019", "'"), r) for t, r in term_map]
    replacements = dict(norm_terms)
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(t) for t, _ in norm_terms) + r")\b",
        flags=re.IGNORECASE,
    )
    norm_text = text.casefold().replace("\u2019", "'")
    return pattern.sub(
        lambda match: replacements[match.group(1).casefold().replace("\u2019", "'")],
        norm_text,
    )


def _section_tokens(text: str, term_map: list[tuple[str, str]]) -> list[str]:
    return _tokens(normalize_terms(text, term_map))


def _overlap(left: list[str], right: list[str]) -> float:
    """Token overlap with bidirectional containment.

    The old denominator ``min(len(left), len(right))`` was blind to additions:
    a translation that adds content not present in the source could still score
    1.0. Containment in both directions penalises both omissions and additions.
    """
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    inter = len(left_set & right_set)
    return min(inter / len(left_set), inter / len(right_set))


def _section_texts(parsed: ParsedDoc) -> dict[str, str]:
    return {
        "title": parsed.title,
        "ingredients": " ".join(
            f"{ing.qty} {ing.unit or ''} {ing.item}".strip()
            for ing in parsed.ingredients
        ),
        "steps": " ".join(parsed.steps),
    }


def _default_l2_thresholds() -> dict[str, float]:
    return {"title": 0.4, "ingredients": 0.5, "steps": 0.3}


def verify_l2(
    source_parsed: ParsedDoc,
    translated_parsed: ParsedDoc,
    *,
    pack: DomainPackBundle | None = None,
    rules: dict[str, Any] | None = None,
) -> L2Report:
    """Compare sections with deterministic glossary-normalized token overlap.

    A section whose overlap is below its threshold is reported as divergent and
    escalated to L3.
    """
    term_map = pack.it_to_en_terms() if pack is not None else []
    thresholds = _default_l2_thresholds()
    if rules is not None:
        configured = rules.get("l2", {}).get("thresholds", {})
        thresholds.update(
            {key: float(value) for key, value in configured.items() if key in thresholds}
        )

    source_texts = _section_texts(source_parsed)
    translated_texts = _section_texts(translated_parsed)

    sections: list[SectionComparison] = []
    issues: list[VerificationIssue] = []
    escalations: list[VerificationIssue] = []

    for section in ("title", "ingredients", "steps"):
        source_tokens = _section_tokens(source_texts[section], term_map)
        translated_tokens = _section_tokens(translated_texts[section], term_map)
        overlap = _overlap(source_tokens, translated_tokens)
        threshold = thresholds[section]
        divergent = overlap < threshold
        sections.append(
            SectionComparison(
                section=section,
                overlap=round(overlap, 4),
                threshold=threshold,
                divergent=divergent,
                source_tokens=source_tokens,
                translated_tokens=translated_tokens,
            )
        )
        if divergent:
            message = (
                f"section {section!r} token overlap {overlap:.3f} is below "
                f"threshold {threshold:.3f}"
            )
            issues.append(
                VerificationIssue(
                    "L2", "SECTION_DIVERGENCE", message, section=section
                )
            )
            escalations.append(
                VerificationIssue(
                    "L3", "ESCALATE_L3", message, section=section
                )
            )

    return L2Report(
        passed=not issues,
        sections=sections,
        issues=issues,
        escalations=escalations,
    )


# ---------------------------------------------------------------------------
# L3 — adjudication queue (Postgres)
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _row_to_adjudication(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "document_id": row["document_id"],
        "section": row["section"],
        "reason": row["reason"],
        "suggestion": row["suggestion"],
        "status": row["status"],
        "kind": row.get("kind", "translation"),
        "verdict_json": row.get("verdict_json"),
        "llm_model": row.get("llm_model"),
        "llm_confidence": row.get("llm_confidence"),
        "candidate_ids": row.get("candidate_ids"),
        "resolved_by": str(row["resolved_by"]) if row["resolved_by"] is not None else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
    }


def _row_to_proposal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "term": row["term"],
        "context": row["context"],
        "status": row["status"],
        "resolved_by": str(row["resolved_by"]) if row["resolved_by"] is not None else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
    }


def _get_adjudication_row(
    conn: psycopg.Connection, adjudication_id: int
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM adjudications WHERE id = %s", (adjudication_id,))
        return cur.fetchone()


def create_adjudication(
    conn: psycopg.Connection,
    document_id: str,
    section: str,
    reason: str,
    *,
    suggestion: str | None = None,
    user_id: uuid.UUID | str | None = None,
    kind: str = "translation",
    verdict_json: dict[str, Any] | None = None,
    llm_model: str | None = None,
    llm_confidence: float | None = None,
    candidate_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a pending adjudication row and record a CREATE audit entry.

    ``kind``: translation | canon | dictionary (passo 4/6 PROGRAMMA-UNICO).
    Per le voci del dizionario, ``verdict_json`` porta la proposta
    standardizzata (passo 5) e ``candidate_ids`` i candidati di canone.
    """
    if kind not in ("translation", "canon", "dictionary"):
        raise ValueError(f"kind must be translation|canon|dictionary, got {kind!r}")
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO adjudications
                    (document_id, section, reason, suggestion, kind,
                     verdict_json, llm_model, llm_confidence, candidate_ids)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, document_id, section, reason, suggestion, status,
                          resolved_by, resolved_at, created_at, kind,
                          verdict_json, llm_model, llm_confidence, candidate_ids
                """,
                (document_id, section, reason, suggestion, kind,
                 json.dumps(verdict_json) if verdict_json is not None else None,
                 llm_model, llm_confidence, candidate_ids),
            )
            row = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "CREATE",
            str(row["id"]),
            "Adjudication",
            new_value={
                "document_id": document_id,
                "section": section,
                "reason": reason,
                "suggestion": suggestion,
                "kind": kind,
                "status": "pending",
            },
        )
    return _row_to_adjudication(row)


def get_adjudication(
    conn: psycopg.Connection, adjudication_id: int
) -> dict[str, Any] | None:
    """Return an adjudication by id, or None."""
    row = _get_adjudication_row(conn, adjudication_id)
    return _row_to_adjudication(row) if row else None


def list_adjudications(
    conn: psycopg.Connection, *, status: str | None = None
) -> list[dict[str, Any]]:
    """List adjudications, optionally filtered by status."""
    if status is not None and status not in VALID_ADJUDICATION_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of "
            f"{sorted(VALID_ADJUDICATION_STATUSES)}"
        )
    with conn.cursor(row_factory=dict_row) as cur:
        if status is None:
            cur.execute("SELECT * FROM adjudications ORDER BY id")
        else:
            cur.execute(
                "SELECT * FROM adjudications WHERE status = %s ORDER BY id",
                (status,),
            )
        rows = cur.fetchall()
    return [_row_to_adjudication(row) for row in rows]


def decide_adjudication(
    conn: psycopg.Connection,
    adjudication_id: int,
    decision: str,
    user_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Approve or reject a pending adjudication (admin workflow).

    The status change and the RESOLVE audit row are written in the same
    Postgres transaction.
    """
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}"
        )
    row = _get_adjudication_row(conn, adjudication_id)
    if row is None:
        raise AdjudicationNotFoundError(f"Adjudication {adjudication_id!r} not found")
    if row["status"] != "pending":
        raise AdjudicationAlreadyResolvedError(
            f"Adjudication {adjudication_id!r} is already {row['status']}"
        )

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE adjudications
                SET status = %s, resolved_by = %s, resolved_at = %s
                WHERE id = %s
                RETURNING id, document_id, section, reason, suggestion, status,
                          resolved_by, resolved_at, created_at
                """,
                (decision, _as_uuid(user_id), _now(), adjudication_id),
            )
            updated = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "RESOLVE",
            str(adjudication_id),
            "Adjudication",
            old_value={"status": "pending"},
            new_value={"status": decision},
        )
    return _row_to_adjudication(updated)


def update_document_verification_level(md: str, level: str) -> str:
    """Set/update ``verification_level`` in a document frontmatter.

    Pure helper: the caller persists the returned markdown. ``level`` must be
    one of ``L1``/``L2``/``L3``.
    """
    if level not in ("L1", "L2", "L3"):
        raise ValueError(f"level must be one of L1/L2/L3, got {level!r}")
    lines = md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("document has no frontmatter")
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise ValueError("unterminated frontmatter")

    new_lines = list(lines)
    existing: int | None = None
    for index in range(1, end):
        if new_lines[index].startswith("verification_level:"):
            existing = index
            break
    if existing is not None:
        new_lines[existing] = f"verification_level: {level}"
    else:
        new_lines.insert(end, f"verification_level: {level}")
    return "\n".join(new_lines) + "\n"


# ---------------------------------------------------------------------------
# Glossary proposals (used by WP-A5 canonicalization, table created in A3)
# ---------------------------------------------------------------------------

def _get_proposal_row(
    conn: psycopg.Connection, proposal_id: int
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM glossary_proposals WHERE id = %s", (proposal_id,))
        return cur.fetchone()


def create_glossary_proposal(
    conn: psycopg.Connection,
    term: str,
    context: str | None = None,
    *,
    user_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Create a pending glossary proposal and record a CREATE audit entry."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                INSERT INTO glossary_proposals (term, context)
                VALUES (%s, %s)
                RETURNING id, term, context, status, resolved_by, resolved_at, created_at
                """,
                (term, context),
            )
            row = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "CREATE",
            str(row["id"]),
            "GlossaryProposal",
            new_value={"term": term, "context": context, "status": "pending"},
        )
    return _row_to_proposal(row)


def list_glossary_proposals(
    conn: psycopg.Connection, *, status: str | None = None
) -> list[dict[str, Any]]:
    """List glossary proposals, optionally filtered by status."""
    if status is not None and status not in VALID_ADJUDICATION_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of "
            f"{sorted(VALID_ADJUDICATION_STATUSES)}"
        )
    with conn.cursor(row_factory=dict_row) as cur:
        if status is None:
            cur.execute("SELECT * FROM glossary_proposals ORDER BY id")
        else:
            cur.execute(
                "SELECT * FROM glossary_proposals WHERE status = %s ORDER BY id",
                (status,),
            )
        rows = cur.fetchall()
    return [_row_to_proposal(row) for row in rows]


def decide_glossary_proposal(
    conn: psycopg.Connection,
    proposal_id: int,
    decision: str,
    user_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Approve or reject a pending glossary proposal."""
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}"
        )
    row = _get_proposal_row(conn, proposal_id)
    if row is None:
        raise GlossaryProposalNotFoundError(
            f"GlossaryProposal {proposal_id!r} not found"
        )
    if row["status"] != "pending":
        raise GlossaryProposalAlreadyResolvedError(
            f"GlossaryProposal {proposal_id!r} is already {row['status']}"
        )

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE glossary_proposals
                SET status = %s, resolved_by = %s, resolved_at = %s
                WHERE id = %s
                RETURNING id, term, context, status, resolved_by, resolved_at, created_at
                """,
                (decision, _as_uuid(user_id), _now(), proposal_id),
            )
            updated = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "RESOLVE",
            str(proposal_id),
            "GlossaryProposal",
            old_value={"status": "pending"},
            new_value={"status": decision},
        )
    return _row_to_proposal(updated)
