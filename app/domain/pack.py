"""Domain Pack schema, validator and loader (WP-A1).

A Domain Pack is a directory with ``pack.yaml``, ``template.md``, glossaries,
``units.yaml`` and deterministic rules. ``load_domain_pack`` validates the
whole directory and returns a :class:`DomainPackBundle`; malformed packs raise
:class:`DomainPackValidationError` with every explicit error message.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.errors import DomainPackValidationError

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_LANG_RE = re.compile(r"^[a-z]{2}$")

# Italian plural forms used by the corpus. The parser needs them to split
# ``2 cucchiai olio`` into unit + item even though units.yaml stores the
# canonical singular ``cucchiaio``.
_UNIT_PLURALS: dict[str, str] = {
    "cucchiaio": "cucchiai",
    "tazza": "tazze",
    "spicchio": "spicchi",
    "pizzico": "pizzichi",
    "bustina": "bustine",
    "mazzetto": "mazzetti",
    "foglia": "foglie",
    "rametto": "rametti",
}


class OntologyRef(BaseModel):
    """External ontology reference (P7)."""

    prefix: str
    uri: str


class GlossaryEntry(BaseModel):
    """One canonical term in a glossary."""

    id: str
    labels_en: str
    labels_it: str
    aliases: list[str] = Field(default_factory=list)
    definition: str = ""
    ontology_uri: str | None = None

    @field_validator("id", "labels_en", "labels_it")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class Glossary(BaseModel):
    """A named glossary (tecnica, ingredienti, stati)."""

    name: str
    entries: list[GlossaryEntry] = Field(default_factory=list)

    @field_validator("entries")
    @classmethod
    def _unique_ids(cls, entries: list[GlossaryEntry]) -> list[GlossaryEntry]:
        seen: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                raise ValueError(f"duplicate glossary id {entry.id!r}")
            seen.add(entry.id)
        return entries


class Glossaries(BaseModel):
    """The three seed glossaries of the ricette pack."""

    tecnica: Glossary
    ingredienti: Glossary
    stati: Glossary

    @model_validator(mode="after")
    def _unique_across(self) -> Glossaries:
        seen: dict[str, str] = {}
        for name in ("tecnica", "ingredienti", "stati"):
            glossary = getattr(self, name)
            for entry in glossary.entries:
                if entry.id in seen:
                    raise ValueError(
                        f"duplicate glossary id {entry.id!r} in {name!r} "
                        f"(already used in {seen[entry.id]!r})"
                    )
                seen[entry.id] = name
        return self


class UnitRule(BaseModel):
    """A deterministic unit conversion/rename rule."""

    rule_id: str
    from_unit: str
    to_unit: str
    factor: float
    rounding: int | None = None
    note: str | None = None

    @field_validator("rule_id", "from_unit", "to_unit")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("factor")
    @classmethod
    def _positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("factor must be > 0")
        return value

    def apply(self, qty: float) -> float:
        """Apply the rule to a quantity.

        ``rounding=None`` means exact (no rounding); ``0`` rounds to an
        integer; ``n > 0`` keeps ``n`` decimal places.
        """
        value = qty * self.factor
        if self.rounding is None:
            return value
        if self.rounding == 0:
            return round(value)
        return round(value, self.rounding)


class PackPaths(BaseModel):
    """Relative paths inside a Domain Pack directory."""

    template: str = "template.md"
    glossaries: str = "glossari"
    units: str = "units.yaml"
    rules: str = "regole"


class DomainPack(BaseModel):
    """Metadata of a Domain Pack (``pack.yaml``)."""

    name: str
    language: str
    canonical_language: str
    version: str
    ontologies: list[OntologyRef] = Field(default_factory=list)
    paths: PackPaths = Field(default_factory=PackPaths)
    glossaries: list[str] = Field(default_factory=list)
    units_source: str = "units.yaml"

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value

    @field_validator("language", "canonical_language")
    @classmethod
    def _lang(cls, value: str) -> str:
        value = value.strip().lower()
        if not _LANG_RE.fullmatch(value):
            raise ValueError(f"language must be a 2-letter code, got {value!r}")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if not _VERSION_RE.fullmatch(value):
            raise ValueError(f"version must be semver-like (X.Y.Z), got {value!r}")
        return value

    @model_validator(mode="after")
    def _check(self) -> DomainPack:
        if not self.glossaries:
            raise ValueError("glossaries must list at least one glossary name")
        if not self.units_source:
            raise ValueError("units_source must not be empty")
        return self


def format_quantity(value: float) -> str:
    """Serialize a quantity per Appendix A (no trailing zeros, max 3 decimals)."""
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer()):
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


@dataclass
class DomainPackBundle:
    """A fully loaded and validated Domain Pack."""

    pack: DomainPack
    template: str
    glossaries: Glossaries
    units: list[UnitRule]
    rules: dict[str, Any]
    root: Path

    @property
    def language(self) -> str:
        return self.pack.language

    @property
    def canonical_language(self) -> str:
        return self.pack.canonical_language

    def unit_rules_by_from(self) -> dict[str, UnitRule]:
        return {rule.from_unit: rule for rule in self.units}

    def known_units(self) -> set[str]:
        """All unit tokens the parser should recognize (source + canonical)."""
        units: set[str] = set()
        for rule in self.units:
            units.add(rule.from_unit)
            units.add(rule.to_unit)
            plural = _UNIT_PLURALS.get(rule.from_unit)
            if plural:
                units.add(plural)
        return units

    def glossary_entries(self) -> list[GlossaryEntry]:
        entries: list[GlossaryEntry] = []
        for name in ("tecnica", "ingredienti", "stati"):
            entries.extend(getattr(self.glossaries, name).entries)
        return entries

    def it_to_en_terms(self) -> list[tuple[str, str]]:
        """Longest-first ``(term, canonical_en_label)`` pairs for L2.

        Both Italian labels/aliases and English labels/aliases are mapped to
        the canonical English label so source and translated sections can be
        compared deterministically.
        """
        pairs: list[tuple[str, str]] = []
        for entry in self.glossary_entries():
            en = entry.labels_en.casefold()
            for term in [entry.labels_it, *entry.aliases, entry.labels_en]:
                term = term.strip().casefold()
                if term:
                    pairs.append((term, en))
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return pairs

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly control dump used by ``scripts/load_domain_pack.py``."""
        return {
            "pack": self.pack.model_dump(mode="json"),
            "template": self.template,
            "glossaries": {
                name: {
                    "name": getattr(self.glossaries, name).name,
                    "entries": [
                        entry.model_dump(mode="json")
                        for entry in getattr(self.glossaries, name).entries
                    ],
                }
                for name in ("tecnica", "ingredienti", "stati")
            },
            "units": [rule.model_dump(mode="json") for rule in self.units],
            "rules": self.rules,
        }


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DomainPackValidationError([f"{path}: invalid YAML: {exc}"]) from exc


def _pydantic_errors(exc: Exception) -> list[str]:
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return [str(exc)]


def validate_pack(pack_dir: str | Path) -> list[str]:
    """Validate a Domain Pack directory and return every error found."""
    root = Path(pack_dir)
    errors: list[str] = []

    pack_path = root / "pack.yaml"
    if not pack_path.is_file():
        return [f"{pack_path}: missing pack.yaml"]

    raw = _read_yaml(pack_path)
    if not isinstance(raw, dict):
        return [f"{pack_path}: pack.yaml must be a YAML mapping"]

    try:
        pack = DomainPack.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - collected into explicit report
        return [f"{pack_path}: {e}" for e in _pydantic_errors(exc)]

    template_path = root / pack.paths.template
    if not template_path.is_file():
        errors.append(f"{template_path}: missing template file")
    elif not template_path.read_text(encoding="utf-8").strip():
        errors.append(f"{template_path}: template is empty")

    for name in pack.glossaries:
        glossary_path = root / pack.paths.glossaries / f"{name}.yaml"
        if not glossary_path.is_file():
            errors.append(f"{glossary_path}: missing glossary file")
            continue
        glossary_raw = _read_yaml(glossary_path)
        entries = _glossary_entries(glossary_raw)
        if entries is None:
            errors.append(f"{glossary_path}: expected a mapping with 'entries' or a list")
            continue
        try:
            Glossary(name=name, entries=[GlossaryEntry.model_validate(e) for e in entries])
        except Exception as exc:  # noqa: BLE001
            errors.extend(f"{glossary_path}: {e}" for e in _pydantic_errors(exc))

    units_path = root / pack.units_source
    if not units_path.is_file():
        errors.append(f"{units_path}: missing units file")
    else:
        units_raw = _read_yaml(units_path)
        if not isinstance(units_raw, list):
            errors.append(f"{units_path}: units file must be a YAML list")
        else:
            seen_rule_ids: set[str] = set()
            for index, item in enumerate(units_raw):
                try:
                    rule = UnitRule.model_validate(item)
                except Exception as exc:  # noqa: BLE001
                    errors.extend(
                        f"{units_path}[{index}]: {e}" for e in _pydantic_errors(exc)
                    )
                    continue
                if rule.rule_id in seen_rule_ids:
                    errors.append(f"{units_path}[{index}]: duplicate rule_id {rule.rule_id!r}")
                seen_rule_ids.add(rule.rule_id)

    rules_dir = root / pack.paths.rules
    if rules_dir.is_dir():
        for rule_file in sorted(rules_dir.glob("*.yaml")):
            rule_raw = _read_yaml(rule_file)
            if not isinstance(rule_raw, dict):
                errors.append(f"{rule_file}: rule file must be a YAML mapping")

    return errors


def _glossary_entries(raw: Any) -> list[Any] | None:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        return raw["entries"]
    return None


def _load_glossaries(root: Path, pack: DomainPack) -> Glossaries:
    data: dict[str, Glossary] = {}
    for name in pack.glossaries:
        path = root / pack.paths.glossaries / f"{name}.yaml"
        raw = _read_yaml(path)
        entries = _glossary_entries(raw)
        if entries is None:
            raise DomainPackValidationError(
                [f"{path}: expected a mapping with 'entries' or a list"]
            )
        try:
            data[name] = Glossary(
                name=name,
                entries=[GlossaryEntry.model_validate(e) for e in entries],
            )
        except Exception as exc:
            raise DomainPackValidationError(
                [f"{path}: {e}" for e in _pydantic_errors(exc)]
            ) from exc
    try:
        return Glossaries.model_validate(data)
    except Exception as exc:
        raise DomainPackValidationError(_pydantic_errors(exc)) from exc


def _load_rules(root: Path, pack: DomainPack) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    rules_dir = root / pack.paths.rules
    if rules_dir.is_dir():
        for rule_file in sorted(rules_dir.glob("*.yaml")):
            rules[rule_file.stem] = _read_yaml(rule_file)
    return rules


def load_domain_pack(pack_dir: str | Path) -> DomainPackBundle:
    """Validate and load a Domain Pack directory."""
    root = Path(pack_dir)
    errors = validate_pack(root)
    if errors:
        raise DomainPackValidationError(errors)

    pack = DomainPack.model_validate(_read_yaml(root / "pack.yaml"))
    template = (root / pack.paths.template).read_text(encoding="utf-8")
    glossaries = _load_glossaries(root, pack)
    units = [
        UnitRule.model_validate(item)
        for item in _read_yaml(root / pack.units_source)
    ]
    rules = _load_rules(root, pack)
    return DomainPackBundle(
        pack=pack,
        template=template,
        glossaries=glossaries,
        units=units,
        rules=rules,
        root=root,
    )
