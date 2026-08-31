"""Regole deterministiche del dizionario (R1-R9, direttiva chef 31/08).

Ordine di esecuzione:
- R1-R2 a monte dell'adjudication (quarantena non-ingredienti, ri-segmentazione)
- R3 e R9 come vincoli di ricomposizione/grafo (anti-fusione, classe unica)
- R4-R8 come post-processor deterministici prima del gate umano

Al gate arriva solo il delta: la revisione umana torna a fare giudizio
culinario, non caccia agli errori sistematici.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


def _norm(s: str) -> str:
    """NFKD + rimozione accenti + casefold (bechamel == béchamel)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()

# ---------------------------------------------------------------------------
# R1 — Quarantena non-ingredienti
# ---------------------------------------------------------------------------

# Core in stoplist: unita' di misura, tempi, temperature, porzioni/rese,
# attrezzatura, toponimi. Trigger: core in stoplist O confidenza < 0.5.
NON_INGREDIENT_CORES = {
    "ingredient", "serving", "servings", "time", "hours", "minutes", "seconds",
    "pound", "ounce", "litre", "litres", "piece", "pieces", "unit", "units",
    "bread", "unknown", "% oz", "time and temperature", "time and temperature condition",
    "cheesecloth", "small", "33 oz", "beef fish", "skewer", "skewers",
    "united states", "u.s.", "oz", "hour", "hour at 80°f (27°c) at 65% rh",
    "minutes.", "hours.", "to 6 servings", "to 30 minutes", "to 16 breads",
    "to 10 breads", "to 20 pieces", "to 25 pieces", "(33 oz)", "(34 oz)", "@ oz)",
    "litres (34 pt or 9 u.s. cups) ordinary con-",
    "small piece muslin or 4 layers cheesecloth (sufficient to cover the mouth of the jar)",
    "hour 30 minutes to 2 hours at 80°f (27°c)", "hour 30 minutes at 80°f (27°c) at 65% rh",
    "di pesce di manzo", "ib (0.454 kg)", "oz (0.085 kg)",
}

# ---------------------------------------------------------------------------
# R2 — Ri-segmentazione righe composte (key -> componenti)
# ---------------------------------------------------------------------------

COMPOUND_SPLITS: dict[str, list[dict]] = {
    "carrot, 1⁄2 costa di celery": [
        {"canonical_name_en": "carrot", "ingredient_core": "carrot", "class": "verdura"},
        {"canonical_name_en": "celery", "ingredient_core": "celery", "class": "verdura",
         "allergen_tags": ["celery"]},
    ],
    "di extra virgin olive oil, salt and black pepper": [
        {"canonical_name_en": "extra virgin olive oil", "ingredient_core": "olive oil", "class": "grasso"},
        {"canonical_name_en": "salt", "ingredient_core": "salt", "class": "condimento"},
        {"canonical_name_en": "black pepper", "ingredient_core": "pepper", "class": "spezia"},
    ],
    "di wheat flour di mais": [
        {"canonical_name_en": "wheat flour", "ingredient_core": "flour", "class": "amido",
         "allergen_tags": ["gluten"]},
        {"canonical_name_en": "corn flour", "ingredient_core": "corn flour", "class": "amido"},
    ],
    "di butter, salt": [
        {"canonical_name_en": "butter", "ingredient_core": "butter", "class": "latticino",
         "allergen_tags": ["milk"]},
        {"canonical_name_en": "salt", "ingredient_core": "salt", "class": "condimento"},
    ],
    "onion, 1⁄2 costa di celery": [
        {"canonical_name_en": "onion", "ingredient_core": "onion", "class": "verdura"},
        {"canonical_name_en": "celery", "ingredient_core": "celery", "class": "verdura",
         "allergen_tags": ["celery"]},
    ],
    "di parsley, salt": [
        {"canonical_name_en": "parsley", "ingredient_core": "parsley", "class": "erba"},
        {"canonical_name_en": "salt", "ingredient_core": "salt", "class": "condimento"},
    ],
}

# ---------------------------------------------------------------------------
# R3 — Vincolo anti-fusione (correzioni puntuali)
# ---------------------------------------------------------------------------

ANTI_FUSION_CANONICAL: dict[str, str] = {
    "CM01099": "dijon mustard",      # 121: Dijon != mustard generico
    "CM01156": "semi-skimmed milk",  # 128: != whole milk
    "CM01277": "boston lettuce",      # 103: != lettuce generico
    "CM01797": "white chocolate substitute",  # 487: surrogato != cioccolato
}

# Alias da rimuovere (riga 76: la passata e' voce distinta dalla salsa).
REMOVE_ALIASES: dict[str, list[str]] = {
    "SF00715": ["tomato passata"],
}

# ---------------------------------------------------------------------------
# R4 — Solfiti sui derivati del vino
# ---------------------------------------------------------------------------

WINE_CORES = {"wine", "marsala", "sherry", "port", "vermouth", "wine vinegar",
              "balsamic vinegar", "red vinegar", "balsamic syrup"}
# I distillati NON dichiarano solfiti (la distillazione li elimina).
DISTILLATES = {"brandy", "cognac", "rum", "grappa", "pernod"}

# ---------------------------------------------------------------------------
# R5 — Ereditarieta' del sedano
# ---------------------------------------------------------------------------

# Famiglie che ereditano celery dalla mirepoix/soffritto di base.
CELERY_FAMILIES = {"stock", "broth", "brodo", "bouillon", "court-bouillon",
                   "consomme", "petite marmite", "mirepoix", "ragu", "gravy",
                   "demi-glace", "demi-glaze"}
# Il fumet di pesce resta escluso dall'automatismo (verifica manuale).
FISH_FUMET_EXCLUDED = {"fish fumet", "fumet de poisson"}

# ---------------------------------------------------------------------------
# R6 — Glutine strutturale
# ---------------------------------------------------------------------------

# (a) legati a roux per definizione
ROUX_BOUND_CORES = {"bechamel", "mornay", "espagnole", "demi-glace",
                    "demi-glaze", "gravy", "veloute"}
# (b) prodotti industriali a standard di mercato noto
INDUSTRIAL_GLUTEN_CORES = {"oyster sauce", "hoisin", "rice crispies",
                           "asafoetida"}
# (c) farina generica -> default frumento
GENERIC_FLOUR_CORES = {"flour", "pancake mix"}

# ---------------------------------------------------------------------------
# R7 — Composizioni definitorie (canonical -> allergeni minimi)
# ---------------------------------------------------------------------------

DEFINITIVE_COMPOSITIONS: dict[str, list[str]] = {
    "ganache": ["milk"],
    "vanilla sauce": ["milk", "eggs"],
    "almond cream": ["milk", "eggs", "nuts"],
    "caesar": ["eggs", "fish", "milk"],
    "biscuit": ["gluten", "eggs"],
    "sponge": ["gluten", "eggs"],
    "choux": ["gluten", "eggs", "milk"],
    "tuile": ["gluten", "eggs", "milk"],
    "crepe": ["gluten", "eggs", "milk"],
    "vanilla souffle": ["milk", "gluten", "eggs"],
    "duchesse": ["milk", "eggs"],
    "flan pasta": ["milk", "eggs"],
    "frolla": ["milk"],
    "shortcrust": ["milk"],
    "puree": ["milk"],
    "mashed potato": ["milk"],
    "flan crust": ["milk", "eggs"],
    "pesto": ["nuts", "milk"],
}

# ---------------------------------------------------------------------------
# R8 — Lecitina di soia sulle coperture
# ---------------------------------------------------------------------------

COVERING_CORES = {"chocolate covering", "couverture"}
COVERING_BRANDS = {"barry", "callebaut", "valrhona"}
# Righe indicate dal chef (il brand non e' nelle forme ma il prodotto e' noto).
COVERING_KEYS = {"CM01818", "CM01817", "CM01815"}

# ---------------------------------------------------------------------------
# R9 — Canone unico di classe
# ---------------------------------------------------------------------------

CLASS_CANON: dict[str, str] = {
    "olive": "verdura",
    "tomato paste": "condimento",
    "gelatina": "altro",
    "gelatin": "altro",
    "capperi": "verdura",
    "caper": "verdura",
    "gelato": "latticino",
    "ice cream": "latticino",
    "pane": "cereale",
    "panini": "cereale",
    "breadcrumbs": "cereale",
    "bun": "cereale",
    "buns": "cereale",
    "burro chiarificato": "grasso",
    "clarified butter": "grasso",
    "puree": "verdura",
    "mashed potato": "verdura",
}


@dataclass
class RuleResult:
    """Esito dell'applicazione delle regole a una proposta."""

    key: str
    canonical_name_en: str
    ingredient_core: str
    class_: str | None = None
    aliases: list[str] = field(default_factory=list)
    allergen_tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    ambiguous: bool = False
    corpus: str = "msc"
    quarantined: bool = False          # R1: non-ingrediente
    split_into: list[dict] | None = None  # R2: componenti
    rules_applied: list[str] = field(default_factory=list)


def _core_matches(core: str, family: set[str]) -> bool:
    c = _norm(core)
    return any(_norm(f) in c for f in family)


def apply_rules(
    key: str,
    canonical: str,
    core: str,
    class_: str | None,
    aliases: list[str],
    allergens: list[str],
    confidence: float,
    ambiguous: bool,
    corpus: str,
    forms: list[str] | None = None,
) -> RuleResult:
    """Applica R1-R9 a una proposta del dizionario (deterministico).

    ``forms``: forme grezze viste nel corpus (per R8: il brand industriale
    delle coperture sta nelle forme, non nel canonical).
    """
    r = RuleResult(
        key=key, canonical_name_en=canonical, ingredient_core=core,
        class_=class_, aliases=list(aliases), allergen_tags=list(allergens),
        confidence=confidence, ambiguous=ambiguous, corpus=corpus,
    )
    c = _norm(core)
    canon = _norm(canonical)
    all_forms = " ".join(forms or [])

    # R1: quarantena non-ingredienti
    if c in NON_INGREDIENT_CORES or confidence < 0.5:
        r.quarantined = True
        r.rules_applied.append("R1")

    # R2: ri-segmentazione righe composte
    if key in COMPOUND_SPLITS:
        r.split_into = COMPOUND_SPLITS[key]
        r.rules_applied.append("R2")

    # R3: anti-fusione (correzioni puntuali)
    if key in ANTI_FUSION_CANONICAL:
        r.canonical_name_en = ANTI_FUSION_CANONICAL[key]
        r.rules_applied.append("R3")
    if key in REMOVE_ALIASES:
        r.aliases = [a for a in r.aliases if a not in REMOVE_ALIASES[key]]
        r.rules_applied.append("R3")

    # R4: solfiti sui derivati del vino (esclusi i distillati)
    if (_core_matches(c, WINE_CORES) and not _core_matches(c, DISTILLATES)
            and "sulphites" not in r.allergen_tags):
        r.allergen_tags.append("sulphites")
        r.rules_applied.append("R4")

    # R5: ereditarieta' del sedano
    if ("celery" in c or (_core_matches(c, CELERY_FAMILIES)
                          and not _core_matches(c, FISH_FUMET_EXCLUDED))
            and "celery" not in r.allergen_tags):
        r.allergen_tags.append("celery")
        r.rules_applied.append("R5")

    # R6: glutine strutturale
    if (_core_matches(c, ROUX_BOUND_CORES)
            or _core_matches(c, INDUSTRIAL_GLUTEN_CORES)
            or _core_matches(c, GENERIC_FLOUR_CORES)) and "gluten" not in r.allergen_tags:
        r.allergen_tags.append("gluten")
        r.rules_applied.append("R6")

    # R7: composizioni definitorie
    for comp, comp_allergens in DEFINITIVE_COMPOSITIONS.items():
        if comp in canon:
            for a in comp_allergens:
                if a not in r.allergen_tags:
                    r.allergen_tags.append(a)
            r.rules_applied.append("R7")
            break

    # R8: lecitina di soia sulle coperture (il brand sta nelle forme)
    if (_core_matches(c, COVERING_CORES) or _core_matches(canon, COVERING_CORES)
            or key in COVERING_KEYS):
        brand = any(_norm(b) in canon or _norm(b) in all_forms
                    for b in COVERING_BRANDS)
        if (brand or key in COVERING_KEYS) and "soy" not in r.allergen_tags:
            r.allergen_tags.append("soy")
            r.rules_applied.append("R8")

    # R9: canone unico di classe (match su core E canonical)
    for cls_core, cls in CLASS_CANON.items():
        if _norm(cls_core) in c or _norm(cls_core) in canon:
            r.class_ = cls
            r.rules_applied.append("R9")
            break

    return r


# ---------------------------------------------------------------------------
# Vincoli strutturali (R3, R9) — validati a ogni ingest
# ---------------------------------------------------------------------------

def validate_dictionary_constraints(entries: list[dict]) -> list[str]:
    """Vincoli di ricomposizione/grafo.

    R3: l'alias di un nodo non puo' essere il canonical di un altro nodo
        (anti-fusione: due key distinte non convergono sullo stesso termine).
    R9: un canonical = una sola classe (property constraint sul nodo).

    Ritorna la lista dei problemi (vuota = ok).
    """
    problems: list[str] = []

    # R3: alias vs canonical
    canon_by_label: dict[str, str] = {}
    for e in entries:
        label = _norm(e.get("labels_en", ""))
        if label:
            canon_by_label.setdefault(label, e.get("id", "?"))
    for e in entries:
        for alias in e.get("aliases", []):
            a = _norm(alias)
            if a in canon_by_label and canon_by_label[a] != e.get("id"):
                problems.append(
                    f"R3: alias {alias!r} di {e.get('id')} collide col "
                    f"canonical di {canon_by_label[a]}"
                )

    # R9: stesso canonical con classi diverse
    class_by_label: dict[str, set[str]] = {}
    for e in entries:
        label = _norm(e.get("labels_en", ""))
        cls = e.get("class")
        if label and cls:
            class_by_label.setdefault(label, set()).add(str(cls))
    for label, classes in class_by_label.items():
        if len(classes) > 1:
            problems.append(
                f"R9: canonical {label!r} con classi multiple {sorted(classes)}"
            )
    return problems
