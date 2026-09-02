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

class OntologyRef(BaseModel):
    """External ontology reference (P7)."""

    prefix: str
    uri: str


# Enum chiuso delle classi ingrediente (passo 2 PROGRAMMA-UNICO): usato dal
# dizionario, da plausibilita.yaml e dal giudice di canone (Fase 1).
INGREDIENT_CLASSES = {
    "proteina", "amido", "verdura", "frutta", "latticino", "grasso",
    "condimento", "spezia", "erba", "liquido", "dolcificante", "legume",
    "cereale", "uovo", "fungo", "frutta_secca", "bevanda", "altro",
}

# Allergeni EU-FIC (Reg. 1169/2011, 14 categorie).
EU_FIC_ALLERGENS = {
    "gluten", "crustaceans", "eggs", "fish", "peanuts", "soy", "milk",
    "nuts", "celery", "mustard", "sesame", "sulphites", "lupin", "molluscs",
}


class GlossaryEntry(BaseModel):
    """One canonical term in a glossary.

    Campi estesi (passo 2, tutti opzionali e retro-compatibili): ``class``
    (enum chiuso), ``allergen_tags`` (EU-FIC 14), ``unit_weight_g``,
    ``countable_unit`` + ``count_policy`` (integer|exact) per i contabili,
    ``density_g_per_ml`` per i liquidi, ``is_food``, ``confidence``,
    ``ambiguous``.
    """

    id: str
    labels_en: str
    labels_it: str
    aliases: list[str] = Field(default_factory=list)
    definition: str = ""
    ontology_uri: str | None = None
    class_: str | None = Field(default=None, alias="class")
    allergen_tags: list[str] = Field(default_factory=list)
    unit_weight_g: float | None = None
    countable_unit: str | None = None
    count_policy: str | None = None
    density_g_per_ml: float | None = None
    is_food: bool = True
    confidence: str | None = None
    ambiguous: bool = False

    @field_validator("id", "labels_en", "labels_it")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

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
            raise ValueError(
                f"allergen_tags must be EU-FIC: unknown {sorted(unknown)}"
            )
        return value

    @field_validator("count_policy")
    @classmethod
    def _count_policy(cls, value: str | None) -> str | None:
        if value is not None and value not in ("integer", "exact"):
            raise ValueError("count_policy must be 'integer' or 'exact'")
        return value

    @model_validator(mode="after")
    def _countable_consistency(self) -> GlossaryEntry:
        # un contabile senza peso o senza policy e' un errore di schema
        if self.countable_unit is not None:
            if self.unit_weight_g is None:
                raise ValueError(
                    f"countable_unit {self.countable_unit!r} requires unit_weight_g"
                )
            if self.count_policy is None:
                raise ValueError(
                    f"countable_unit {self.countable_unit!r} requires count_policy"
                )
        return self


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
    """A deterministic unit conversion/rename rule.

    ``from_forms``/``to_forms`` (WP-F2): le varianti accettate della stessa
    unita' (plurali, abbreviazioni, sinonimi). Sono qui e in nessun altro
    posto: prima le stesse tabelle vivevano duplicate in
    ``pack._UNIT_PLURALS``, ``verify.DEFAULT_KNOWN_UNITS`` e
    ``canonical._ITALIAN_PLURALS``/``_ENGLISH_PLURALS``, e divergevano
    (``rametto`` singolare non era riconosciuto da nessuna delle tre).

    ``countable``: True per le unita' di conteggio (identita', factor 1.0):
    il parser le riconosce come unita' ma, quando il token compare da solo
    (es. "- 4 eggs"), lo tratta come ingrediente, non come unita'.
    """

    rule_id: str
    from_unit: str
    to_unit: str
    factor: float
    rounding: int | None = None
    note: str | None = None
    from_forms: list[str] = Field(default_factory=list)
    to_forms: list[str] = Field(default_factory=list)
    countable: bool = False

    def source_forms(self) -> set[str]:
        """Forme accettate in ingresso (documento sorgente)."""
        return {self.from_unit, *self.from_forms}

    def target_forms(self) -> set[str]:
        """Forme accettate in uscita (documento tradotto/canonico)."""
        return {self.to_unit, *self.to_forms}

    def forms(self) -> set[str]:
        """Tutti i token che questa regola riconosce."""
        return self.source_forms() | self.target_forms()

    @field_validator("from_forms", "to_forms")
    @classmethod
    def _forms_non_empty(cls, value: list[str]) -> list[str]:
        forms = [form.strip() for form in value]
        if any(not form for form in forms):
            raise ValueError("unit forms must not be empty")
        return forms

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
    # Frontmatter keys that may be absent when the document is native in the
    # canonical language (lang == source_lang), e.g. MSC cards have no
    # time_min/difficulty. Never filled with a placeholder (P3).
    frontmatter_optional_when_native: list[str] = Field(default_factory=list)

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

    @property
    def frontmatter_optional_when_native(self) -> list[str]:
        return self.pack.frontmatter_optional_when_native

    def unit_rules_by_from(self) -> dict[str, UnitRule]:
        return {rule.from_unit: rule for rule in self.units}

    def _unit_index(self) -> dict[str, UnitRule]:
        """Token -> regola. Le forme *sorgente* vincono sulle forme di arrivo.

        ``ml`` e' forma sorgente di UNIT-ML e forma di arrivo di UNIT-DL: chi
        parsa ``250 ml`` deve trovare l'identita', non la conversione da dl.
        """
        index = self.__dict__.get("_unit_index_cache")
        if index is None:
            index = {}
            for rule in self.units:
                for form in sorted(rule.source_forms()):
                    index.setdefault(form, rule)
            for rule in self.units:
                for form in sorted(rule.target_forms()):
                    index.setdefault(form, rule)
            self.__dict__["_unit_index_cache"] = index
        return index

    def unit_rule_for(self, token: str | None) -> UnitRule | None:
        """La regola che riconosce ``token`` (sorgente o canonico), o None."""
        if not token:
            return None
        return self._unit_index().get(token)

    def known_units(self) -> set[str]:
        """All unit tokens the parser should recognize (source + canonical).

        Sorgente unica: ``units.yaml``. Nessuna tabella di plurali altrove.
        """
        return set(self._unit_index())

    def countable_units(self) -> set[str]:
        """Unita' di conteggio (identita'): riconosciute come unita' ma, da
        sole, trattate come ingrediente dal parser (es. "- 4 eggs")."""
        return {
            form
            for rule in self.units
            if rule.countable
            for form in rule.forms()
        }

    def msc_mapping(self) -> dict[str, str]:
        """Mappa item code MSC -> termine canonico (msc_mapping.yaml, passo 7).

        Assente o vuota => comportamento identico a prima (retro-compatibile).
        """
        path = self.root / "msc_mapping.yaml"
        if not path.exists():
            return {}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}
        return {str(k): str(v) for k, v in raw.items()}

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
            source_owner: dict[str, str] = {}
            target_unit: dict[str, tuple[str, str]] = {}
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
                # WP-F2: un token non puo' essere forma sorgente di due regole
                # (il parser non saprebbe quale conversione applicare).
                for form in sorted(rule.source_forms()):
                    owner = source_owner.get(form)
                    if owner is not None and owner != rule.rule_id:
                        errors.append(
                            f"{units_path}[{index}]: source form {form!r} is "
                            f"already claimed by rule {owner!r}"
                        )
                    source_owner.setdefault(form, rule.rule_id)
                # Una forma di arrivo puo' ripetersi (``ml`` e' arrivo di
                # UNIT-DL e sorgente di UNIT-ML) ma non puo' portare a due
                # unita' canoniche diverse.
                for form in sorted(rule.target_forms()):
                    previous = target_unit.get(form)
                    if previous is not None and previous[1] != rule.to_unit:
                        errors.append(
                            f"{units_path}[{index}]: target form {form!r} maps to "
                            f"{rule.to_unit!r} but rule {previous[0]!r} maps it "
                            f"to {previous[1]!r}"
                        )
                    target_unit.setdefault(form, (rule.rule_id, rule.to_unit))

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
