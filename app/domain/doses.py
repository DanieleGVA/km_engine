"""Standardizzazione delle dosi: sistema MKS + scaling a 10 persone.

Step del workflow affinato (richiesta committente): dopo la canonicalizzazione
(ingredienti/procedure/unita'), le dosi vengono:
1. convertite in unita' MKS (g, kg, ml, l, °C, min, h) con fattori documentati
   (P3: mai distruttivo — ogni conversione e' registrata nel dose-log con rule_id);
2. scalate proporzionalmente a 10 persone (fattore = 10 / servings originali).

Il risultato e' il canonical.md finale con servings=10 e quantita' MKS, piu' un
dose-log che spiega ogni trasformazione (verificabile come il canon-log).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.domain.errors import ParseError
from app.domain.pack import DomainPackBundle
from app.domain.verify import parse_translated_md, render_ingredient_suffix

# Unita' culinarie -> MKS (fattori documentati, approssimazioni standard).
# rule_id: DOSE-<UNIT> per la tracciabilita' (P3).
MKS_FACTORS: dict[str, tuple[str, float, str]] = {
    "tablespoon": ("ml", 15.0, "DOSE-TABLESPOON"),
    "teaspoon": ("ml", 5.0, "DOSE-TEASPOON"),
    "cup": ("ml", 250.0, "DOSE-CUP"),
    "pinch": ("g", 0.5, "DOSE-PINCH"),
    "clove": ("g", 5.0, "DOSE-CLOVE"),
    "leaf": ("g", 1.0, "DOSE-LEAF"),
    "sprig": ("g", 1.0, "DOSE-SPRIG"),
    "sachet": ("g", 7.0, "DOSE-SACHET"),
    "bunch": ("g", 50.0, "DOSE-BUNCH"),
    "slice": ("g", 30.0, "DOSE-SLICE"),
    "thread": ("g", 1.0, "DOSE-THREAD"),
    "drop": ("ml", 0.05, "DOSE-DROP"),
    "rib": ("g", 20.0, "DOSE-RIB"),
    "tuft": ("g", 5.0, "DOSE-TUFT"),
    "walnut": ("g", 10.0, "DOSE-WALNUT"),
    "grain": ("g", 0.1, "DOSE-GRAIN"),
    "zest": ("g", 1.0, "DOSE-ZEST"),
    "etto": ("g", 100.0, "DOSE-ETTO"),
    # Unita' MKS aggiuntive (formato industriale CalcMenu/Pareto)
    "cl": ("ml", 10.0, "DOSE-CL"),
    "dl": ("ml", 100.0, "DOSE-DL"),
    "mg": ("g", 0.001, "DOSE-MG"),
    "lt": ("l", 1.0, "DOSE-LT"),
}

# Unita' gia' MKS: nessuna conversione.
MKS_NATIVE = {"g", "kg", "ml", "l", "°c", "min", "h"}

TARGET_SERVINGS = 10


@dataclass
class DoseLogEntry:
    """Una conversione/scalatura registrata (P3)."""

    field: str
    before: str
    after: str
    rule_id: str


@dataclass
class DoseStandardizedDocument:
    """Risultato della standardizzazione dosi."""

    canonical_md: str
    servings: int
    scale_factor: float
    log_entries: list[DoseLogEntry] = field(default_factory=list)


def _fmt_qty(value: Decimal) -> str:
    """Formatta la quantita': intero senza .0, altrimenti max 2 decimali."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return str(int(q))
    return format(q, "f").rstrip("0").rstrip(".")


def standardize_doses(
    canonical_md: str,
    pack: DomainPackBundle,
    servings_target: int = TARGET_SERVINGS,
) -> DoseStandardizedDocument:
    """Converti le dosi in MKS e scala a ``servings_target`` persone.

    Il canonical.md in ingresso e' il formato Appendice A (sezioni
    ``## Ingredients`` / ``## Method``, righe ``- qty unit item``).
    """
    parsed = parse_translated_md(
        canonical_md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native)
    )
    servings = parsed.frontmatter.get("servings")
    if not isinstance(servings, int) or servings <= 0:
        raise ParseError(
            "frontmatter 'servings' must be a positive integer for dose scaling "
            f"(got {servings!r}); the yield is never invented"
        )
    factor = Decimal(servings_target) / Decimal(servings)
    log: list[DoseLogEntry] = []

    new_ingredients: list[tuple[str, str, str, str | None]] = []
    for i, ing in enumerate(parsed.ingredients):
        qty = Decimal(str(ing.qty)) if ing.qty is not None else None
        unit = (ing.unit or "").lower()
        item = ing.item
        suffix = render_ingredient_suffix(
            ing.code, ing.waste, ing.component
        ) or None
        before = f"{ing.qty} {ing.unit} {item}".strip()

        # 1) conversione MKS
        if unit in MKS_FACTORS:
            mks_unit, mks_factor, rule_id = MKS_FACTORS[unit]
            if qty is not None:
                qty = qty * Decimal(str(mks_factor))
            unit = mks_unit
            log.append(DoseLogEntry(f"ingredients[{i}]", before, f"{qty} {unit} {item}", rule_id))
        elif unit in MKS_NATIVE or unit == "":
            pass  # gia' MKS o senza unita'
        else:
            # unita' sconosciuta: resta invariata, registrata (P3)
            log.append(DoseLogEntry(f"ingredients[{i}]", before, before, "DOSE-UNKNOWN"))

        # 2) scaling a 10 persone
        if qty is not None:
            scaled = qty * factor
            qty_text = _fmt_qty(scaled)
            log.append(DoseLogEntry(f"ingredients[{i}]", before, f"{qty_text} {unit} {item}", "DOSE-SCALE"))
        else:
            qty_text = "1"
        new_ingredients.append((qty_text, unit, item, suffix))

    # ricostruisci il canonical.md con servings=10 e dosi MKS
    fm = dict(parsed.frontmatter)
    fm["servings"] = servings_target
    lines = ["---"]
    for k in ("title", "id", "lang", "source_lang", "servings", "time_min",
              "difficulty", "verification_level", "canonical_version"):
        if k in fm:
            lines.append(f"{k}: {fm[k]}")
    lines.append("---")
    lines.append("## Ingredients")
    for qty_text, unit, item, suffix in new_ingredients:
        if unit:
            lines.append(f"- {qty_text} {unit} {item}{suffix or ''}")
        else:
            lines.append(f"- {qty_text} {item}{suffix or ''}")
    lines.append("## Method")
    for j, step in enumerate(parsed.steps, 1):
        lines.append(f"{j}. {step}")
    md = "\n".join(lines) + "\n"

    return DoseStandardizedDocument(
        canonical_md=md,
        servings=servings_target,
        scale_factor=float(factor),
        log_entries=log,
    )
