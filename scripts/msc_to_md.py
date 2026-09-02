"""Convertitore MSC: PDF CalcMenu/Pareto -> translated.md EN-native (passo 0).

Bypass dello stage-1: le card sono EN-native, entrano direttamente nella
forma ``translated.md`` (``## Ingredients`` / ``## Method``, ``lang=en``,
``source_lang=en``) — stesso principio del dominio ``code`` (gia' EN).

Normalizzazioni deterministiche (mai inventate, P3):
- numeri senza separatore di migliaia: "1,500 g" -> qty 1500 (mai 1)
- procedure rinumerate in passi ``N.`` strettamente sequenziali, testo
  preservato parola per parola (numerazione originale rimossa)
- yield -> ``servings`` int ("24 serve", "100 pax"); formati non risolvibili
  ("10 [_]", "1 pz", "10 KG", assente) -> coda errori con motivo, mai default
- righe-sezione della distinta ("— CRUMBLE —") -> metadato ``{component: ...}``
  sulle righe successive
- item code e sfrido nel suffisso ``{code, waste, component}``

Gate (passo 0): riconciliazione 1.653 card / 19.500 righe / 1.591 procedure;
zero righe corrotte da "1,500"; L1 verde sul convertito.

Uso:
    uv run python scripts/msc_to_md.py --pdf Pareto_Recipe_Cards_v001.pdf --out msc_md
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

from app.domain import load_domain_pack, parse_translated_md, verify_l1
from app.domain.errors import ParseError

# Unita' riconosciute nella tabella ingredienti CalcMenu (forme esatte per
# case: il parser del template e' case-sensitive; qui normalizziamo a minuscolo
# per l'estrazione, il suffisso conserva la forma originale).
TABLE_UNITS = {
    "g", "kg", "ml", "l", "cl", "dl", "lt", "mg", "ea", "pz", "pc", "pcs",
    "serving", "servings", "serve", "tt", "slice", "slices", "tablespoon",
    "teaspoon", "cup", "pinch", "clove", "leaf", "leaves", "sprig", "sachet",
    "bunch", "thread", "drop", "rib", "tuft", "walnut", "grain", "zest",
    "etto", "h", "min", "°c", "t", "tsp", "tbsp", "ltr", "lts", "piece",
    "pieces", "each", "unit", "units", "egg", "eggs", "portion", "portions",
}

# Unita' yield valide come servings (pax = persone).
YIELD_SERVINGS_UNITS = {"serve", "serving", "servings", "pax", "portion", "portions"}

YIELD_RE = re.compile(r"Yield\s+(.+?)\s*·\s*Record type", re.IGNORECASE)
CARD_START_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
CODE_RE = re.compile(r"^\s*(RF\d+|SF\d+)\s*$")
ROW_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.*)$")


@dataclass
class IngredientRow:
    """Una riga della distinta (ingrediente o riga-sezione)."""

    line_no: int
    code: str | None
    name: str
    qty: str | None          # normalizzato ("1500", "0"); None per righe-sezione
    unit: str | None
    prep: str | None
    waste: str | None
    is_section: bool = False
    component: str | None = None


@dataclass
class Card:
    """Una card CalcMenu."""

    index: int
    name: str
    code: str
    yield_raw: str | None
    servings: int | None
    yield_error: str | None
    ingredients: list[IngredientRow] = field(default_factory=list)
    procedure: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_qty(raw: str) -> str | None:
    """'1,500' -> '1500' (mai 1); '2.500.000' -> '2500000'; '1,5' -> '1.5';
    '0' -> '0'; '—' -> None."""
    raw = raw.strip()
    if raw in ("—", "-", "", "–"):
        return None
    if "," in raw:
        parts = raw.split(",")
        # separatore di migliaia: "1,500" -> "1500"; "2,500,000" -> "2500000"
        if (len(parts) > 1 and all(p.isdigit() for p in parts)
                and all(len(p) == 3 for p in parts[1:])):
            return "".join(parts)
        return raw.replace(",", ".")       # virgola decimale: "1,5" -> "1.5"
    if "." in raw:
        parts = raw.split(".")
        # punti come separatore di migliaia: "2.500.000" -> "2500000"
        if (parts[0].isdigit() and len(parts) > 1
                and all(p.isdigit() and len(p) == 3 for p in parts[1:])):
            return "".join(parts)
    return raw


def parse_yield(raw: str) -> tuple[int | None, str | None]:
    """Ritorna (servings, errore). Mai default: formati non risolvibili -> errore.

    ``raw`` e' il valore estratto dopo "Yield" (es. "24 serve", "10 [_]").
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "yield assente"
    m2 = re.match(r"^([\d\.,]+)\s+([A-Za-z_\[\]\.]+)$", raw)
    if not m2:
        return None, f"formato yield non riconosciuto: {raw!r}"
    num_raw, unit = m2.group(1), m2.group(2).lower()
    num = _normalize_qty(num_raw)
    if num is None or not re.fullmatch(r"\d+(\.\d+)?", num):
        return None, f"quantita' yield non valida: {num_raw!r}"
    if unit in YIELD_SERVINGS_UNITS:
        return int(float(num)), None
    if unit == "[_]":
        return None, f"unita' yield ignota [_] ({raw!r})"
    if unit == "pz":
        return None, f"unita' yield di conteggio pz, non servings ({raw!r})"
    if unit in ("kg", "g", "lt", "l", "ml", "cl", "dl"):
        return None, f"resa in peso/volume, non servings ({raw!r})"
    return None, f"unita' yield non riconosciuta {unit!r} ({raw!r})"


def _split_row(line: str) -> tuple[int, str, str] | None:
    m = ROW_RE.match(line)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def parse_ingredient_row(line: str) -> IngredientRow | None:
    """Parsa una riga della distinta. Righe-sezione: code '—' e nessuna unita'."""
    parts = _split_row(line)
    if parts is None:
        return None
    line_no, code, rest = parts
    tokens = rest.split()
    if not tokens:
        return None
    # l'unita' e' l'ULTIMO token noto PRECEDUTO da una quantita' numerica
    # (la colonna Preparation puo' contenere parole-unità come "SPRIG"/"LEAF":
    # "10 mg SPRIG" -> unita' mg, prep SPRIG, mai il contrario)
    unit_idx = None
    for i, tok in enumerate(tokens):
        if i >= 1 and tok.lower() in TABLE_UNITS and re.fullmatch(r"[\d\.,]+", tokens[i - 1]):
            unit_idx = i
    if unit_idx is None or unit_idx < 1:
        # nessuna unita' nota: cerca l'ultima quantita' numerica
        # (es. "SPICE CUMIN POWDER 1,000 — — —": qty 1000, unita' assente)
        qty_idx = None
        for i, tok in enumerate(tokens):
            if re.fullmatch(r"[\d\.,]+", tok):
                qty_idx = i
        if qty_idx is not None and qty_idx >= 1:
            qty = _normalize_qty(tokens[qty_idx])
            if qty is not None:
                name = " ".join(tokens[:qty_idx])
                return IngredientRow(line_no=line_no,
                                     code=None if code == "—" else code,
                                     name=name, qty=qty, unit=None,
                                     prep=None, waste=None)
        # riga-sezione ("— CRUMBLE —") o artefatto: il nome e' la sezione
        # (senza i "—" di riempimento della tabella)
        name_tokens = [t for t in tokens if t not in ("—", "-")]
        name = " ".join(name_tokens) if name_tokens else " ".join(tokens)
        return IngredientRow(line_no=line_no, code=None if code == "—" else code,
                             name=name, qty=None, unit=None, prep=None, waste=None,
                             is_section=True)
    qty_raw = tokens[unit_idx - 1]
    qty = _normalize_qty(qty_raw)
    if qty is None:
        return None
    unit = tokens[unit_idx]
    name = " ".join(tokens[:unit_idx - 1])
    tail = tokens[unit_idx + 1:]
    if not name and tail:
        # riga malformata con qty+unita' nel nome ("— 0.2 KG CHIA SEEDS —"):
        # il nome e' la coda (prep), non un ingrediente senza nome
        name = " ".join(t for t in tail if t not in ("—", "-"))
        tail = []
    # coda: prep + wastage ("TO TASTE", "GRILLED", "10%", "—")
    waste = None
    prep = None
    if tail:
        if re.fullmatch(r"\d+%", tail[-1]):
            waste = tail[-1]
            tail = tail[:-1]
        tail = [t for t in tail if t not in ("—", "-")]
        prep = " ".join(tail) if tail else None
    return IngredientRow(
        line_no=line_no,
        code=None if code == "—" else code,
        name=name,
        qty=qty,
        unit=unit,
        prep=prep,
        waste=waste,
    )


def _clean_step_text(line: str) -> str:
    """Rimuove la numerazione originale (ripetuta) preservando il testo."""
    s = line.strip()
    while True:
        m = re.match(r"^\d+[\.\)]?\s+(.*)$", s)
        if m and m.group(1).strip():
            s = m.group(1).strip()
        else:
            break
    return s


def _clean_title(name: str) -> str:
    t = re.sub(r"\(.*?\)", "", name).strip()
    t = re.sub(r"\s+", " ", t).strip()
    return t.replace(":", "-")


def extract_cards(pdf: pathlib.Path) -> list[Card]:
    """Estrae tutte le card dal PDF (pdftotext -layout).

    Ancoraggio: ogni card ha una riga con il solo codice RF/SF (1.653 nel
    PDF). La card inizia dalla riga titolo "N. ..." immediatamente sopra il
    codice (il titolo puo' andare a capo su 2 righe).
    """
    with tempfile.TemporaryDirectory() as td:
        txt = pathlib.Path(td) / "out.txt"
        subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=True)
        lines = txt.read_text(encoding="utf-8", errors="replace").splitlines()

    code_lines = [i for i, l in enumerate(lines) if CODE_RE.match(l)]
    starts: list[int] = []
    for ci in code_lines:
        found = None
        for j in range(ci - 1, max(0, ci - 6), -1):
            if not lines[j].strip():
                continue
            if CARD_START_RE.match(lines[j]):
                found = j
                break
            if re.match(r"^\s*\d+\.\s", lines[j]):
                break  # riga di procedura, non un titolo
        if found is not None:
            starts.append(found)

    cards: list[Card] = []
    for si, start in enumerate(starts):
        end = starts[si + 1] if si + 1 < len(starts) else len(lines)
        block = lines[start:end]
        cards.append(parse_card(block, si + 1))
    return cards


def parse_card(block: list[str], index: int) -> Card:
    """Una card: titolo, codice, yield, distinta, procedura."""
    name = CARD_START_RE.match(block[0]).group(2).strip()
    code = ""
    for l in block[:6]:
        m = CODE_RE.match(l)
        if m:
            code = m.group(1)
            break
    yield_raw = None
    for l in block[:8]:
        m = YIELD_RE.search(l)
        if m:
            yield_raw = m.group(1).strip()
            break
    servings, yield_error = parse_yield(yield_raw or "")

    card = Card(index=index, name=name, code=code, yield_raw=yield_raw,
                servings=servings, yield_error=yield_error)

    # distinta
    in_table = False
    current_component: str | None = None
    for l in block:
        if re.match(r"^\s*#\s+Item code", l):
            in_table = True
            continue
        if in_table:
            if re.match(r"^\s*PROCEDURE", l):
                in_table = False
                continue
            row = parse_ingredient_row(l)
            if row is None:
                continue
            if row.is_section:
                current_component = row.name.lower()
                continue
            row.component = current_component
            card.ingredients.append(row)

    # procedura (esclusi i footer di pagina)
    in_proc = False
    for l in block:
        if re.match(r"^\s*PROCEDURE", l):
            in_proc = True
            continue
        if in_proc and l.strip():
            if "FOODMDM · Pareto recipe cards" in l:
                continue
            card.procedure.append(_clean_step_text(l))

    if not code:
        card.errors.append("codice card assente")
    if not card.ingredients:
        card.errors.append("nessuna riga ingrediente")
    if not card.procedure:
        card.errors.append("procedura assente")
    return card


def card_to_md(card: Card) -> str:
    """Card -> translated.md EN-native (con suffisso {code, waste, component})."""
    title = _clean_title(card.name)
    lines = [
        "---",
        f"title: {title}",
        f"id: {card.code}",
        "lang: en",
        "source_lang: en",
        f"servings: {card.servings}",
        "---",
        "## Ingredients",
    ]
    for row in card.ingredients:
        suffix_parts = []
        if row.code:
            suffix_parts.append(f"code: {row.code}")
        if row.waste:
            suffix_parts.append(f"waste: {row.waste}")
        if row.component:
            suffix_parts.append(f"component: {row.component}")
        suffix = " {" + ", ".join(suffix_parts) + "}" if suffix_parts else ""
        lines.append(f"- {row.qty} {row.unit} {row.name}{suffix}")
    lines.append("## Method")
    for j, step in enumerate(card.procedure, 1):
        lines.append(f"{j}. {step}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--pack", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parents[1] / "domain-packs" / "ricette")
    args = ap.parse_args()

    pack = load_domain_pack(str(args.pack))
    cards = extract_cards(args.pdf)
    args.out.mkdir(parents=True, exist_ok=True)

    converted: list[dict] = []
    errors: list[dict] = []
    n_rows = 0
    n_proc = 0
    l1_fail: list[dict] = []
    for card in cards:
        n_rows += len(card.ingredients)
        if card.procedure:
            n_proc += 1
        if card.servings is None or card.errors:
            errors.append({
                "index": card.index, "code": card.code, "name": card.name,
                "yield_raw": card.yield_raw, "yield_error": card.yield_error,
                "errors": card.errors,
            })
            continue
        md = card_to_md(card)
        # gate: parse + L1 identita' sul convertito
        try:
            parse_translated_md(
                md, known_units=pack.known_units(),
                optional_when_native=tuple(pack.frontmatter_optional_when_native),
                countable_units=pack.countable_units(),
            )
            l1 = verify_l1(md, md, pack=pack)
            if not l1.passed:
                l1_fail.append({"code": card.code, "issues": [i.message for i in l1.issues]})
                errors.append({"index": card.index, "code": card.code, "name": card.name,
                               "errors": [f"L1: {[i.message for i in l1.issues]}"]})
                continue
        except (ParseError, ValueError) as exc:
            errors.append({"index": card.index, "code": card.code, "name": card.name,
                           "errors": [f"parse/L1: {exc}"]})
            continue
        out = args.out / f"{card.code}.md"
        out.write_text(md, encoding="utf-8")
        converted.append({"index": card.index, "code": card.code, "name": card.name,
                          "servings": card.servings, "n_ingredients": len(card.ingredients),
                          "n_steps": len(card.procedure)})

    reconciliation = {
        "cards_pdf": len(cards),
        "cards_converted": len(converted),
        "cards_errors": len(errors),
        "ingredient_rows": n_rows,
        "procedures": n_proc,
        "l1_failures": len(l1_fail),
    }
    (args.out / "reconciliation.json").write_text(
        json.dumps(reconciliation, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "errors.json").write_text(
        json.dumps(errors, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(reconciliation, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
