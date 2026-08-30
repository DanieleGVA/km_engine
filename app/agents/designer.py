"""Ontology Designer (WP-C2): brief -> draft Domain Pack in a staging dir.

The designer is deterministic and writes **only** inside the staging directory
(``domain-packs/ricette-agents-draft`` by default). This is the mechanical
enforcement of the human gate (P5): the draft is a proposal, never the
production pack, and no code path may write outside the staging root.

Every generated file conforms to the pydantic schema of ``app.domain.pack``
(``load_domain_pack`` validates the whole directory). The designer never
imports or mutates ``app/domain/*``; it only produces pack content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.agents.models import CandidateEntity, DomainBrief

STAGING_DIR = Path("domain-packs/ricette-agents-draft")
MANUAL_PACK_DIR = Path("domain-packs/ricette")

GLOSSARY_NAMES = ("tecnica", "ingredienti", "stati")

TEMPLATE_MD = """---
title: <titolo>
id: <id>
lang: it
servings: <int>
time_min: <int>
difficulty: facile|medio|difficile
---
## Ingredienti
- {qty} {unit} {item}
## Procedimento
1. <step>
"""

# Deterministic unit knowledge base (SI + canonical culinary units, P7).
# ``from_unit`` is the canonical singular source form; the parser already knows
# the Italian plural forms. The designer emits one rule per ``from_unit``.
_UNIT_SPECS: list[dict] = [
    {"from_unit": "g", "to_unit": "g", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "kg", "to_unit": "kg", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "ml", "to_unit": "ml", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "l", "to_unit": "l", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "dl", "to_unit": "ml", "factor": 100.0, "rounding": 0, "note": "1 dl = 100 ml"},
    {"from_unit": "°C", "to_unit": "°C", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "min", "to_unit": "min", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "h", "to_unit": "h", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "cucchiaio", "to_unit": "tablespoon", "factor": 1.0, "rounding": None, "note": "1 tablespoon = 15 ml"},
    {"from_unit": "tazza", "to_unit": "cup", "factor": 1.0, "rounding": None, "note": "1 cup = 250 ml"},
    {"from_unit": "pizzico", "to_unit": "pinch", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "spicchio", "to_unit": "clove", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "foglia", "to_unit": "leaf", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "rametto", "to_unit": "sprig", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "bustina", "to_unit": "sachet", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "mazzetto", "to_unit": "bunch", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "fette", "to_unit": "slice", "factor": 1.0, "rounding": None, "note": None},
    {"from_unit": "fili", "to_unit": "thread", "factor": 1.0, "rounding": None, "note": None},
]

# Observed plural/singular surface forms -> canonical ``from_unit``.
_UNIT_ALIASES: dict[str, str] = {
    "cucchiai": "cucchiaio",
    "tazze": "tazza",
    "spicchi": "spicchio",
    "pizzichi": "pizzico",
    "bustine": "bustina",
    "mazzetti": "mazzetto",
    "foglie": "foglia",
    "rametti": "rametto",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class DesignError(RuntimeError):
    """Raised when the designer refuses to write outside the staging dir."""


@dataclass
class DesignResult:
    """Outcome of :func:`design_pack`."""

    staging_dir: Path
    files: list[Path] = field(default_factory=list)
    glossary_entries: int = 0
    unit_rules: int = 0
    detected_units: int = 0
    ambiguities: int = 0


def slugify(text: str) -> str:
    """Deterministic ASCII slug for glossary/unit ids."""
    text = text.strip().lower()
    text = text.replace("\u2019", "'")
    return _SLUG_RE.sub("-", text).strip("-")


def _safe_write(root: Path, relpath: str, content: str) -> Path:
    """Write ``content`` to ``root/relpath``, refusing any path escape.

    The resolved target must stay inside the resolved staging root. This is the
    mechanical human-gate guarantee: even a malicious ``relpath`` (``../x``) or
    a symlink pointing outside the staging dir is rejected.
    """
    root = root.resolve()
    target = (root / relpath).resolve()
    if not target.is_relative_to(root):
        raise DesignError(
            f"refusing to write outside staging dir {root}: {relpath!r} -> {target}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _yaml_dump(data: object) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def _glossary_entry(entity: CandidateEntity, prefix: str, used_ids: set[str]) -> dict:
    """Convert a candidate entity to a glossary entry with a unique id."""
    base = slugify(entity.term) or "term"
    entry_id = f"{prefix}-{base.upper()}"
    suffix = 2
    while entry_id in used_ids:
        entry_id = f"{prefix}-{base.upper()}-{suffix}"
        suffix += 1
    used_ids.add(entry_id)

    source_terms = [term for term in entity.source_terms if term.strip()]
    labels_it = source_terms[0] if source_terms else entity.term
    aliases = source_terms[1:] if len(source_terms) > 1 else []
    return {
        "id": entry_id,
        "labels_en": entity.term,
        "labels_it": labels_it,
        "aliases": aliases,
        "definition": "",
        "ontology_uri": entity.ontology_uri,
    }


def _build_glossary(name: str, entities: list[CandidateEntity], prefix: str) -> dict:
    used_ids: set[str] = set()
    entries = [_glossary_entry(entity, prefix, used_ids) for entity in entities]
    return {"name": name, "entries": entries}


def _build_units(brief: DomainBrief) -> list[dict]:
    """Build the deterministic units.yaml rule list.

    The full unit knowledge base is emitted (the designer owns the SI/culinary
    unit mapping); the brief's detected units are reported in the result for
    traceability, not used to silently drop rules.
    """
    rules: list[dict] = []
    for spec in _UNIT_SPECS:
        rule_id = f"UNIT-{slugify(spec['from_unit']).upper()}"
        rule = {
            "rule_id": rule_id,
            "from_unit": spec["from_unit"],
            "to_unit": spec["to_unit"],
            "factor": spec["factor"],
            "rounding": spec["rounding"],
        }
        if spec["note"]:
            rule["note"] = spec["note"]
        rules.append(rule)
    return rules


def _build_pack_yaml(brief: DomainBrief) -> dict:
    return {
        "name": brief.domain,
        "language": brief.language,
        "canonical_language": brief.canonical_language,
        "version": brief.version,
        "ontologies": [
            {"prefix": ontology.prefix, "uri": ontology.uri}
            for ontology in brief.ontologies
        ],
        "units_source": "units.yaml",
        "glossaries": list(GLOSSARY_NAMES),
        "paths": {
            "template": "template.md",
            "glossaries": "glossari",
            "units": "units.yaml",
            "rules": "regole",
        },
    }


def _build_normalization_rules() -> dict:
    return {
        "name": "normalizzazione",
        "version": "1.0.0",
        "order": ["units", "terms", "structure"],
        "unit_plural_map": {
            "cucchiai": "cucchiaio",
            "tazze": "tazza",
            "spicchi": "spicchio",
            "pizzichi": "pizzico",
            "bustine": "bustina",
            "mazzetti": "mazzetto",
            "foglie": "foglia",
            "rametti": "rametto",
        },
    }


def _build_verification_rules() -> dict:
    return {
        "name": "verifica",
        "version": "1.0.0",
        "l2": {
            "metric": "overlap_coefficient",
            "thresholds": {"title": 0.4, "ingredients": 0.5, "steps": 0.3},
        },
        "l3": {"statuses": ["pending", "approved", "rejected"]},
    }


def design_pack(
    brief: DomainBrief,
    *,
    staging_dir: str | Path = STAGING_DIR,
    overwrite: bool = False,
) -> DesignResult:
    """Generate the draft Domain Pack from ``brief`` inside ``staging_dir``.

    Raises :class:`DesignError` when ``staging_dir`` is the production pack or
    when any write would escape the staging root.
    """
    root = Path(staging_dir).resolve()
    if root == MANUAL_PACK_DIR.resolve():
        raise DesignError(
            f"staging dir must not be the production pack dir {MANUAL_PACK_DIR}"
        )

    if root.exists() and any(root.iterdir()):
        if not overwrite:
            raise DesignError(
                f"staging dir {root} is not empty; pass overwrite=True to replace it"
            )
        for child in root.iterdir():
            if child.is_dir() and not child.is_symlink():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()

    root.mkdir(parents=True, exist_ok=True)

    vocabularies = {vocabulary.name: vocabulary for vocabulary in brief.vocabularies}
    ingredient_entities = getattr(vocabularies.get("ingredienti"), "entries", [])
    technique_entities = getattr(vocabularies.get("tecnica"), "entries", [])
    state_entities = getattr(vocabularies.get("stati"), "entries", [])

    files: list[Path] = []

    files.append(
        _safe_write(root, "pack.yaml", _yaml_dump(_build_pack_yaml(brief)))
    )
    files.append(_safe_write(root, "template.md", TEMPLATE_MD))

    glossaries = {
        "tecnica": _build_glossary("tecnica", technique_entities, "TEC"),
        "ingredienti": _build_glossary("ingredienti", ingredient_entities, "ING"),
        "stati": _build_glossary("stati", state_entities, "STA"),
    }
    for name in GLOSSARY_NAMES:
        files.append(
            _safe_write(root, f"glossari/{name}.yaml", _yaml_dump(glossaries[name]))
        )

    units = _build_units(brief)
    files.append(_safe_write(root, "units.yaml", _yaml_dump(units)))

    files.append(
        _safe_write(root, "regole/normalizzazione.yaml", _yaml_dump(_build_normalization_rules()))
    )
    files.append(
        _safe_write(root, "regole/verifica.yaml", _yaml_dump(_build_verification_rules()))
    )

    return DesignResult(
        staging_dir=root,
        files=files,
        glossary_entries=sum(len(glossaries[name]["entries"]) for name in GLOSSARY_NAMES),
        unit_rules=len(units),
        detected_units=len(brief.units),
        ambiguities=len(brief.ambiguities),
    )

