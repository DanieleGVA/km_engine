"""Standardizzazione delle dosi: sistema MKS + scaling a 10 persone.

Passo 8 PROGRAMMA-UNICO: tipizzazione (misurata / contabile / a-piacere),
doppia rappresentazione (unita' naturale intoccabile nel canonico; grammi
equivalenti SOLO nel dose-log), gate di plausibilita' per classe per porzione.

Regole:
- l'unita' naturale non si riscrive mai: "2 uova" restano 2 uova, "3 foglie"
  restano 3 foglie; i grammi equivalenti vivono solo nel dose-log e nel grafo
- conversione MKS solo sulle unita' di misura vere (cucchiai, cl, KG, LT...)
- scala della resa sulla quantita' naturale; count_policy integer => mezzo su,
  minimo 1
- resa mancante => errore, mai default (P3)
- gate di plausibilita' per classe per porzione sui grammi equivalenti
  (i 220 KG e i 100 mg fuori range falliscono per costruzione)
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

# Unita' di conteggio (mai convertite; grammi equivalenti solo a log).
# Le unita' di MISURA vere (tablespoon, teaspoon, cup, cl, dl, lt, kg...)
# restano in MKS_FACTORS e vengono convertite; le unita' naturali (uova,
# foglie, spicchi, fette...) restano invariate nel canonico (passo 8).
COUNT_UNITS = {
    "serving", "servings", "pcs", "piece", "pieces", "each", "unit", "units",
    "egg", "eggs", "clove", "pinch", "slice", "sprig", "leaf", "bunch",
    "sachet", "thread", "rib", "tuft", "walnut", "grain", "zest",
    "pz", "ea", "t", "tsp", "tbsp", "ltr", "lts", "pc",
}

# Peso generico per unita' di conteggio senza peso da dizionario (marcato:
# mai stima silenziosa — se assente anche qui, issue dichiarata).
GENERIC_UNIT_WEIGHT_G: dict[str, float] = {
    "egg": 50.0, "eggs": 50.0, "clove": 5.0, "leaf": 1.0, "sprig": 1.0,
    "slice": 30.0, "bunch": 50.0, "pinch": 0.5, "cup": 250.0,
    "tablespoon": 15.0, "teaspoon": 5.0, "pz": 50.0, "ea": 50.0,
    "serving": 100.0, "servings": 100.0, "piece": 50.0, "pieces": 50.0,
    "each": 50.0, "unit": 50.0, "units": 50.0, "pc": 50.0, "pcs": 50.0,
}

# Tipi di riga (passo 8).
ING_MEASURED = "measured"
ING_COUNTABLE = "countable"
ING_A_PIACERE = "a_piacere"
ING_ZERO_ANOMALOUS = "zero_anomalous"
ING_NO_UNIT = "no_unit"

TARGET_SERVINGS = 10


@dataclass
class DoseLogEntry:
    """Una conversione/scalatura registrata (P3)."""

    field: str
    before: str
    after: str
    rule_id: str
    mass_g: str | None = None  # grammi equivalenti (solo a log, mai nel canonico)


@dataclass
class DoseStandardizedDocument:
    """Risultato della standardizzazione dosi."""

    canonical_md: str
    servings: int
    scale_factor: float
    log_entries: list[DoseLogEntry] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


def _fmt_qty(value: Decimal) -> str:
    """Formatta la quantita': intero senza .0, altrimenti max 2 decimali."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if q == q.to_integral_value():
        return str(int(q))
    return format(q, "f").rstrip("0").rstrip(".")


def _round_count(qty: Decimal, policy: str | None) -> Decimal:
    """Arrotondamento contabile: integer => mezzo su, minimo 1."""
    if policy == "integer":
        q = qty.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return max(q, Decimal(1))
    return qty


def _unit_weight_g(pack: DomainPackBundle, item: str, unit: str) -> float | None:
    """Peso per unita' di conteggio: dizionario > fattore generico > None."""
    for entry in pack.glossary_entries():
        if entry.labels_en.casefold() == item.casefold():
            if entry.unit_weight_g is not None:
                return entry.unit_weight_g
            break
    return GENERIC_UNIT_WEIGHT_G.get(unit)


def _plausibility_range(pack: DomainPackBundle, class_: str | None) -> tuple[float, float] | None:
    rules = pack.rules.get("plausibilita", {})
    per_portion = rules.get("per_portion_grams", {})
    if class_ and class_ in per_portion:
        r = per_portion[class_]
        return float(r["min"]), float(r["max"])
    return None


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
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
        countable_units=pack.countable_units(),
    )
    servings = parsed.frontmatter.get("servings")
    if not isinstance(servings, int) or servings <= 0:
        raise ParseError(
            "frontmatter 'servings' must be a positive integer for dose scaling "
            f"(got {servings!r}); the yield is never invented"
        )
    factor = Decimal(servings_target) / Decimal(servings)
    log: list[DoseLogEntry] = []
    issues: list[str] = []

    # classe per item (per il gate di plausibilita')
    class_by_item = {
        e.labels_en.casefold(): e.class_ for e in pack.glossary_entries()
        if e.class_ is not None
    }

    new_ingredients: list[tuple[str, str, str, str | None]] = []
    for i, ing in enumerate(parsed.ingredients):
        qty = Decimal(str(ing.qty)) if ing.qty is not None else None
        unit = (ing.unit or "").lower()
        item = ing.item
        suffix = render_ingredient_suffix(
            ing.code, ing.waste, ing.component
        ) or None
        before = f"{ing.qty} {ing.unit} {item}".strip()
        mass_g: Decimal | None = None

        # 0) a-piacere / zero anomalo
        if qty is not None and qty == 0:
            a_piacere_classes = set(
                pack.rules.get("plausibilita", {}).get("rules", {}).get(
                    "zero_qty_a_piacere_classes", [])
            )
            cls = class_by_item.get(item.casefold())
            if unit in ("tt",) or cls in a_piacere_classes:
                # a piacere: riga invariata, nessuno scaling
                new_ingredients.append((ing.qty, ing.unit, item, suffix))
                log.append(DoseLogEntry(f"ingredients[{i}]", before, before,
                                        "DOSE-A-PIACERE"))
                continue
            issues.append(f"ingredients[{i}]: qty 0 anomalo ({before})")
            log.append(DoseLogEntry(f"ingredients[{i}]", before, before,
                                    "DOSE-ZERO-ANOMALOUS"))
            new_ingredients.append((ing.qty, ing.unit, item, suffix))
            continue

        # 1) contabile: unita' naturale intoccabile, grammi solo a log
        #    ("- 2 egg" parsa unit=None, item=egg; "egg whites" e' composto)
        count_unit = unit
        if not count_unit:
            if item.casefold() in COUNT_UNITS:
                count_unit = item.casefold()   # "- 2 egg": item = unit
            elif item.casefold().startswith(("egg ", "eggs ")):
                count_unit = "egg"
                item = item.split(" ", 1)[1]
        if count_unit in COUNT_UNITS:
            policy = None
            for entry in pack.glossary_entries():
                if entry.labels_en.casefold() == item.casefold():
                    policy = entry.count_policy
                    break
            scaled = _round_count(qty * factor, policy)
            qty_text = _fmt_qty(scaled)
            log.append(DoseLogEntry(f"ingredients[{i}]", before,
                                    f"{qty_text} {count_unit} {item}",
                                    "DOSE-SCALE-COUNT"))
            weight = _unit_weight_g(pack, item, count_unit)
            if weight is not None:
                mass_g = scaled * Decimal(str(weight))
                log.append(DoseLogEntry(f"ingredients[{i}]", before,
                                        f"{qty_text} {count_unit} {item}",
                                        "DOSE-MASS-G", mass_g=_fmt_qty(mass_g)))
            else:
                issues.append(
                    f"ingredients[{i}]: contabile senza peso ({before})")
            new_ingredients.append((qty_text, count_unit, item, suffix))
            continue

        # 2) misurata: conversione MKS + scaling
        if unit in MKS_FACTORS:
            mks_unit, mks_factor, rule_id = MKS_FACTORS[unit]
            if qty is not None:
                qty = qty * Decimal(str(mks_factor))
            unit = mks_unit
            log.append(DoseLogEntry(f"ingredients[{i}]", before,
                                    f"{qty} {unit} {item}", rule_id))
        elif unit in MKS_NATIVE:
            pass  # gia' MKS
        else:
            issues.append(f"ingredients[{i}]: unita' sconosciuta ({before})")
            log.append(DoseLogEntry(f"ingredients[{i}]", before, before,
                                    "DOSE-UNKNOWN"))
            new_ingredients.append((ing.qty, ing.unit, item, suffix))
            continue

        scaled = qty * factor
        qty_text = _fmt_qty(scaled)
        log.append(DoseLogEntry(f"ingredients[{i}]", before,
                                f"{qty_text} {unit} {item}", "DOSE-SCALE"))
        # grammi equivalenti (solo a log): g/kg diretti; ml/l via densita' se nota
        if unit in ("g", "kg"):
            mass_g = scaled if unit == "g" else scaled * Decimal(1000)
        elif unit in ("ml", "l"):
            density = None
            for entry in pack.glossary_entries():
                if (entry.labels_en.casefold() == item.casefold()
                        and entry.density_g_per_ml is not None):
                    density = entry.density_g_per_ml
                    break
            if density is not None:
                mass_g = scaled * Decimal(str(density)) if unit == "ml" else                     scaled * Decimal(str(density)) * Decimal(1000)
        if mass_g is not None:
            log.append(DoseLogEntry(f"ingredients[{i}]", before,
                                    f"{qty_text} {unit} {item}",
                                    "DOSE-MASS-G", mass_g=_fmt_qty(mass_g)))
        new_ingredients.append((qty_text, unit, item, suffix))

        # 3) gate di plausibilita' per classe per porzione
        if mass_g is not None:
            cls = class_by_item.get(item.casefold())
            rng = _plausibility_range(pack, cls)
            if rng is not None:
                per_portion = mass_g / Decimal(servings_target)
                lo, hi = Decimal(str(rng[0])), Decimal(str(rng[1]))
                if per_portion < lo or per_portion > hi:
                    issues.append(
                        f"ingredients[{i}]: plausibilita' fuori range per "
                        f"classe {cls!r} ({item!r}: {_fmt_qty(per_portion)} "
                        f"g/porzione, range {rng[0]}-{rng[1]})")

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
        issues=issues,
    )
