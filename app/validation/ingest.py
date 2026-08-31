"""Lettura, rilevamento formato/lingua e standardizzazione delle ricette da validare.

Workflow (branch validate-recipe):
1. l'utente indica il file (PDF CalcMenu/Pareto o md)
2. il sistema legge le ricette, verifica lingua e formato e decide come
   trasformarle secondo i criteri di standardizzazione (ingredienti, dosi,
   procedure, proporzione per 10 persone)
3. se una ricetta e' composta da piu' ricette (sub-recipe: righe con item code
   RF/SF), la sub-recipe viene separata e trattata come documento a se'
4. le ricette standardizzate vengono scritte in formato standardizzato
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

from app.domain import canonicalize, parse_source_md, parse_translated_md, translate_document
from app.domain.doses import standardize_doses
from app.domain.pack import DomainPackBundle
from tests.domain.fake_llm import build_fake_llm

# Item code di sub-recipe (ricette usate come ingredienti, formato CalcMenu).
SUBRECIPE_CODE_RE = re.compile(r"^(RF\d+|SF\d+)$")

# Unita' note (per il filtro del parser CalcMenu).
from app.validation.validator import ALLOWED_UNITS  # noqa: E402


@dataclass
class RawRecipe:
    """Una ricetta grezza letta dal file."""

    source: str          # nome file / card
    name: str
    code: str | None
    servings: int
    ingredients: list[dict] = field(default_factory=list)
    procedure: list[str] = field(default_factory=list)
    language: str = "en"  # it | en
    format: str = "calcmenu"  # calcmenu | md_source | md_translated


@dataclass
class StandardizedRecipe:
    """Ricetta standardizzata (canonical + dosi MKS x10) pronta per il grafo."""

    raw: RawRecipe
    canonical_md: str
    servings_target: int
    scale_factor: float
    sub_recipes: list["StandardizedRecipe"] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing CalcMenu (PDF estratto con pdftotext)
# ---------------------------------------------------------------------------

def _parse_qty(s: str) -> float | None:
    s = s.strip()
    if not re.fullmatch(r"[\d\.,]+", s):
        return None
    if "," in s:
        before, after = s.split(",", 1)
        if len(after) <= 2 and len(before) <= 3:
            return float(before.replace(".", "") + "." + after)
        return float(s.replace(",", "").replace(".", ""))
    if "." not in s:
        return float(s)
    if s.count(".") >= 2:
        return float(s.replace(".", ""))
    before, after = s.split(".", 1)
    if len(after) == 3:
        return float(s.replace(".", ""))
    return float(s)


def _parse_pareto_card(block: list[str]) -> RawRecipe | None:
    name = block[0].strip()
    code, yield_serve = "", None
    for l in block[:6]:
        m = re.search(r"(RF\d+|SF\d+)", l)
        if m and not code:
            code = m.group(1)
        m2 = re.search(r"Yield\s+(\d+)\s+serv\w*", l)
        if m2:
            yield_serve = int(m2.group(1))
    ingredients, in_ing = [], False
    for l in block:
        if re.match(r"^\s*#\s+Item code", l):
            in_ing = True
            continue
        if in_ing:
            if re.match(r"^\s*PROCEDURE", l):
                in_ing = False
                continue
            m = re.match(r"^\s*\d+\s+(\S+)\s+(.+?)\s+([\d\.,]+)\s+(\S+)\s+(.*?)\s*$", l)
            if m:
                q = _parse_qty(m.group(3))
                unit = m.group(4).strip()
                if q is not None and q > 0 and unit.lower() in ALLOWED_UNITS:
                    ingredients.append({"code": m.group(1), "name": m.group(2).strip(),
                                        "qty": q, "unit": unit, "prep": m.group(5).strip() or None})
    proc, in_proc = [], False
    for l in block:
        if re.match(r"^\s*PROCEDURE", l):
            in_proc = True
            continue
        if in_proc and l.strip():
            proc.append(l.strip())
    if not ingredients or yield_serve is None:
        return None
    return RawRecipe(source="pdf", name=name, code=code, servings=yield_serve,
                     ingredients=ingredients, procedure=proc, language="en", format="calcmenu")


def _extract_pdf_cards(pdf: pathlib.Path, limit: int | None = None) -> list[RawRecipe]:
    with tempfile.TemporaryDirectory() as td:
        txt = pathlib.Path(td) / "out.txt"
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()

    def is_start(idx: int) -> bool:
        m = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", lines[idx])
        if not m or idx <= 5:
            return False
        for k in range(idx + 1, min(idx + 5, len(lines))):
            if re.search(r"Yield\s+\d+\s+serv\w*", lines[k]) or re.search(r"\b(RF\d+|SF\d+)\b", lines[k]):
                return True
        return False

    starts = [i for i in range(len(lines)) if is_start(i)]
    cards = []
    for si in starts:
        ei = starts[starts.index(si) + 1] if starts.index(si) + 1 < len(starts) else len(lines)
        card = _parse_pareto_card(lines[si:ei])
        if card:
            cards.append(card)
        if limit and len(cards) >= limit:
            break
    return cards


# ---------------------------------------------------------------------------
# Rilevamento formato/lingua + lettura file
# ---------------------------------------------------------------------------

def detect_language(md: str) -> str:
    """Rileva la lingua di un md: sezioni italiane o inglesi."""
    if "## Ingredienti" in md or "## Procedimento" in md:
        return "it"
    return "en"


def detect_format(path: pathlib.Path, content: str | None = None) -> str:
    """Rileva il formato: calcmenu (PDF), md_source, md_translated."""
    if path.suffix.lower() == ".pdf":
        return "calcmenu"
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")
    if "## Ingredienti" in content:
        return "md_source"
    return "md_translated"


def read_recipes(path: pathlib.Path, limit: int | None = None) -> list[RawRecipe]:
    """Legge le ricette dal file indicato dall'utente (PDF o md)."""
    if path.is_dir():
        fmt = "md_dir"
    else:
        fmt = detect_format(path)
    if fmt == "calcmenu":
        return _extract_pdf_cards(path, limit)
    # md: un file = una ricetta; una dir = piu' file (una ricetta per file)
    files = sorted(path.glob("*.md")) if path.is_dir() else [path]
    recipes = []
    for f in files[:limit] if limit else files:
        md = f.read_text(encoding="utf-8", errors="replace")
        lang = detect_language(md)
        try:
            parsed = parse_source_md(md, known_units=set()) if lang == "it" else parse_translated_md(md, known_units=set())
        except Exception:
            continue
        ingredients = [
            {"code": None, "name": i.item, "qty": float(i.qty) if i.qty else None,
             "unit": i.unit or "", "prep": None}
            for i in parsed.ingredients
        ]
        recipes.append(RawRecipe(
            source=f.name, name=str(parsed.frontmatter.get("title", f.stem)),
            code=str(parsed.frontmatter.get("id", f.stem)),
            servings=int(parsed.frontmatter.get("servings", 4)),
            ingredients=ingredients, procedure=list(parsed.steps),
            language=lang, format=fmt,
        ))
    return recipes


def _split_md_recipes(md: str) -> list[str]:
    """Splitta un file md con piu' blocchi frontmatter in piu' ricette.

    Ogni blocco = frontmatter (--- ... ---) + body (fino al prossimo '---').
    """
    lines = md.splitlines()
    blocks: list[str] = []
    cur: list[str] = []
    in_fm = False
    for l in lines:
        if l.strip() == "---":
            if not in_fm:
                in_fm = True
                cur = [l]
            else:
                in_fm = False
                cur.append(l)
                # il body segue il frontmatter: continua a raccogliere
                # finche' non inizia un nuovo frontmatter
                blocks.append("\n".join(cur))
                cur = []
        elif in_fm:
            cur.append(l)
        elif cur:
            # body della ricetta corrente (dopo il frontmatter)
            cur.append(l)
    if cur:
        blocks.append("\n".join(cur))
    return blocks or [md]


# ---------------------------------------------------------------------------
# Sub-recipe: separazione delle ricette composte
# ---------------------------------------------------------------------------

def split_subrecipes(recipe: RawRecipe) -> tuple[RawRecipe, list[RawRecipe]]:
    """Separa le sub-recipe (righe con item code RF/SF) dalla ricetta principale.

    La sub-recipe diventa una ricetta a se' (stesso nome, code RF/SF, servings
    della principale); nella principale la riga resta come riferimento.
    """
    subs: list[RawRecipe] = []
    main_ingredients: list[dict] = []
    for ing in recipe.ingredients:
        if ing.get("code") and SUBRECIPE_CODE_RE.match(ing["code"]):
            subs.append(RawRecipe(
                source=recipe.source, name=ing["name"], code=ing["code"],
                servings=recipe.servings, ingredients=[], procedure=[],
                language=recipe.language, format=recipe.format,
            ))
        main_ingredients.append(ing)
    recipe.ingredients = main_ingredients
    return recipe, subs


# ---------------------------------------------------------------------------
# Standardizzazione (traduzione se IT + canonicalizzazione + dosi MKS x10)
# ---------------------------------------------------------------------------

def _raw_to_md(recipe: RawRecipe, normalizer=None) -> str:
    """Ricetta grezza -> md nel formato appropriato (source IT o translated EN).

    ``normalizer``: CalcMenuNormalizer opzionale — normalizza i nomi ingredienti
    industriali verso i termini canonici del knowledge (deterministico + LLM
    nei casi dubbi).
    """
    name = re.sub(r"\(.*?\)", "", recipe.name).strip().replace(":", "-")
    ing_lines = []
    for i in recipe.ingredients:
        if i.get("qty") is None:
            continue
        iname = i["name"].lower()
        if normalizer is not None:
            iname, _ = normalizer.normalize(iname)
        if i.get("unit"):
            ing_lines.append(f"- {i['qty']:g} {i['unit']} {iname}")
        else:
            ing_lines.append(f"- {i['qty']:g} {iname}")
    ing = "\n".join(ing_lines)
    steps = "\n".join(f"{j}. {s}" for j, s in enumerate(recipe.procedure[:12], 1))
    if recipe.language == "it":
        return (f"---\ntitle: {name}\nid: {recipe.code or 'X'}\nlang: it\nservings: {recipe.servings}\n"
                f"time_min: 30\ndifficulty: medio\n---\n## Ingredienti\n{ing}\n\n## Procedimento\n{steps}\n")
    return (f"---\ntitle: {name}\nid: {recipe.code or 'X'}\nlang: en\nsource_lang: en\nservings: {recipe.servings}\n"
            f"time_min: 30\ndifficulty: medium\n---\n## Ingredients\n{ing}\n\n## Method\n{steps}\n")


async def standardize_recipe(
    recipe: RawRecipe,
    pack: DomainPackBundle,
    servings_target: int = 10,
    normalizer=None,
) -> StandardizedRecipe:
    """Standardizza una ricetta: traduzione (se IT) + canonicalizzazione + dosi MKS x10."""
    md = _raw_to_md(recipe, normalizer=normalizer)
    notes: list[str] = []
    if recipe.language == "it":
        llm = build_fake_llm(pack, {recipe.source: md})
        translated = await translate_document(pack, md, llm)
        canonical = canonicalize(pack, translated.translated_md)
        notes.append("tradotta da IT a EN (stadio 1)")
    else:
        canonical = canonicalize(pack, md)
    doses = standardize_doses(canonical.canonical_md, pack, servings_target=servings_target)
    notes.append(f"dosi scalate a {servings_target} persone (fattore {doses.scale_factor:.2f})")
    return StandardizedRecipe(
        raw=recipe, canonical_md=doses.canonical_md,
        servings_target=doses.servings, scale_factor=doses.scale_factor, notes=notes,
    )
