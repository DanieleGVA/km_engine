"""Caricamento dei libri del canone nel knowledge base (ambiente di produzione).

Per ogni libro in canon_corpus (pagine md grezze):
1. estrae le ricette con estrattori per-formato (ogni libro ha il suo layout)
2. converte ogni ricetta nel template md del dominio
   (frontmatter + Ingredienti/Ingredients + Procedimento/Method)
3. standardizza: traduzione IT->EN (stadio 1), canonicalizzazione (stadio 2),
   dosi MKS per 10 persone
4. carica nel grafo e nel vettoriale (extract_document + populate_embeddings)
   con i riferimenti autore/libro/pagina/posizione
5. report per libro (ricette estratte, caricate, errori)

Formati supportati:
- marchesi: "INGREDIENTI PER N PERSONE" + ingredienti MAIUSCOLI + procedura
- english:  "Makes/Serves/Yield" + ingredienti + procedura
- japanese: tabella NAME/INGREDIENTS/PROCESS

Uso:
    uv run python scripts/load_books.py --root <canon_corpus> [--books b1 b2 ...]
"""
from __future__ import annotations

import argparse
import asyncio
import pathlib
import re

from app.auth import Principal
from app.domain import canonicalize, parse_source_md, parse_translated_md, translate_document
from app.domain.doses import standardize_doses
from app.domain.extract import extract_document
from app.domain.pack import load_domain_pack
from app.rag.rag import build_embedding_from_graph, populate_embeddings
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

QTY_RX = re.compile(r"^([\d½¼¾⅓⅔⅛]+[\d\.,/½¼¾⅓⅔⅛]*)\s+(.+)$")
YIELD_IT_RX = re.compile(r"INGREDIENTI PER\s+(\d+)\s+PERSONE", re.I)
YIELD_EN_RX = re.compile(r"(?:Makes|Serves|Yield:?)\s+(\d+)", re.I)
PREP_IT_RX = re.compile(r"^PREPARAZIONE:", re.I)
PREP_EN_RX = re.compile(r"^(?:Preparation time:|Method|Procedure)", re.I)
SECTION_RX = re.compile(
    r"^(INGREDIENTI|INGREDIENTS|PROCEDIMENTO|PROCEDURE|METHOD|ANTIPASTI|PRIMI|SECONDI|DOLCI|"
    r"CONTORNI|ZUPPE|SALSE|PREPARAZIONE|DIFFICOLT|TEMPO|CONTENTS|INDEX|INTRODUZIONE|"
    r"PREFAZIONE|BIBLIOGRAFIA|INDICE|SOMMARIO|PARTE|PART\s+[A-Z]|CAPITOLO|CHAPTER|"
    r"APPENDICE|GLOSSARIO|NOTE|RICETTE|LE|LA|IL|I|GLI|UN|UNA|PER|CON|E|O)\b.*$", re.I
)


def _clean_title(t: str) -> str:
    t = re.sub(r"^\s*\d{3,4}\.?\s+", "", t).strip()
    t = re.sub(r"[^A-Za-zÀ-ÿ0-9’'\- ]+", " ", t).strip()
    return t.title() if t else ""


def _split_ingredients(lines: list[str]) -> list[str]:
    """Splitta righe di ingredienti (anche virgola-separate) in voci singole."""
    out: list[str] = []
    for l in lines:
        l = l.strip().rstrip(",")
        if not l:
            continue
        # riga con piu' ingredienti separati da virgola: "20 X, 1 Y, 2 Z"
        parts = re.split(r",\s+(?=\d)", l)
        for p in parts:
            p = p.strip().rstrip(",")
            if p:
                out.append(p)
    return out


_FRAC = {"½": "0.5", "¼": "0.25", "¾": "0.75", "⅓": "0.33", "⅔": "0.67", "⅛": "0.125"}


def _norm_qty(q: str) -> str | None:
    """Normalizza una quantita' nel formato accettato dal parser (\d+ o \d+.\d+)."""
    q = q.strip()
    for u, v in _FRAC.items():
        q = q.replace(u, v)
    if "/" in q:
        try:
            a, b = q.split("/", 1)
            return str(float(a) / float(b))
        except (ValueError, ZeroDivisionError):
            return None
    q = q.replace(",", ".")
    if re.fullmatch(r"\d+(\.\d+)?", q):
        return q
    return None


def _to_md(title: str, servings: int, ingredients: list[str], steps: list[str],
           book_name: str, idx: int, page: int, lang: str = "it") -> str:
    ing_lines = []
    for ing in ingredients:
        m = QTY_RX.match(ing)
        if m:
            qty = _norm_qty(m.group(1))
            if qty is not None:
                ing_lines.append(f"- {qty} {m.group(2).lower()}")
        # ingredienti senza quantita' o con quantita' non normalizzabile
        # (es. 'sale e pepe', '1/2 cipolla' non risolvibile): saltati
    step_lines = [f"{j}. {s}" for j, s in enumerate(steps[:15], 1)]
    ing_body = "\n".join(ing_lines) or "- 1 ingrediente"
    step_body = "\n".join(step_lines) or "1. Procedimento non disponibile."
    if lang == "it":
        return (f"---\ntitle: {title}\nid: {book_name}-{idx:04d}\nlang: it\nservings: {servings}\n"
                f"time_min: 30\ndifficulty: medio\n---\n## Ingredienti\n{ing_body}\n\n## Procedimento\n{step_body}\n")
    return (f"---\ntitle: {title}\nid: {book_name}-{idx:04d}\nlang: en\nsource_lang: en\nservings: {servings}\n"
            f"time_min: 30\ndifficulty: medium\n---\n## Ingredients\n{ing_body}\n\n## Method\n{step_body}\n")


# ---------------------------------------------------------------------------
# Estrattori per formato
# ---------------------------------------------------------------------------

def extract_marchesi(pages: list[str]) -> list[dict]:
    """Formato italiano: 'INGREDIENTI PER N PERSONE' + ingredienti MAIUSCOLI.

    Struttura: [TITOLO] / PREPARAZIONE: ... / DIFFICOLTA': ... /
    INGREDIENTI PER N PERSONE / <ingredienti maiuscoli, virgola-separati> /
    <procedura minuscola>
    """
    text = "\n".join(pages)
    lines = text.splitlines()
    recipes: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i].strip()
        m = YIELD_IT_RX.search(l)
        if m:
            servings = int(m.group(1))
            # titolo: risali le righe precedenti (titolo maiuscolo prima di PREPARAZIONE/DIFFICOLTA)
            title = ""
            j = i - 1
            while j >= 0 and j >= i - 8:
                prev = lines[j].strip()
                if not prev:
                    j -= 1
                    continue
                if PREP_IT_RX.match(prev) or prev.startswith("DIFFICOLT") or prev.startswith("COTTURA"):
                    j -= 1
                    continue
                if prev.isupper() and len(prev) > 3 and not SECTION_RX.match(prev):
                    title = _clean_title(prev)
                    break
                j -= 1
            # ingredienti: righe maiuscole dopo la resa
            ingredients: list[str] = []
            k = i + 1
            while k < n:
                il = lines[k].strip()
                if not il:
                    k += 1
                    continue
                if il.isupper() and not SECTION_RX.match(il):
                    ingredients.append(il)
                    k += 1
                else:
                    break
            # procedura: testo minuscolo dopo gli ingredienti
            steps: list[str] = []
            k2 = k
            while k2 < n:
                pl = lines[k2].strip()
                if not pl:
                    k2 += 1
                    continue
                if pl.isupper() and len(pl) > 3:
                    break
                steps.append(pl)
                k2 += 1
            if ingredients:
                recipes.append({"title": title or f"Ricetta {len(recipes)+1}",
                                "servings": servings,
                                "ingredients": _split_ingredients(ingredients),
                                "steps": steps, "line": i})
            i = k2
            continue
        i += 1
    return recipes


def extract_english(pages: list[str]) -> list[dict]:
    """Formato inglese: titolo + 'SERVES/Makes/Yield N' + ingredienti + procedura.

    La prosa introduttiva tra la resa e la lista ingredienti viene saltata;
    gli ingredienti si fermano alla prima riga di prosa (paragrafo lungo);
    la procedura si ferma al prossimo titolo di ricetta.
    """
    text = "\n".join(pages)
    lines = text.splitlines()
    recipes: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i].strip()
        m = YIELD_EN_RX.search(l)
        if m:
            servings = int(m.group(1))
        elif l.rstrip(":").strip().lower() == "yield" and i + 1 < n:
            # 'Yield:' su riga propria, valore sulla riga successiva
            m2 = re.search(r"(\d+)", lines[i + 1])
            if m2:
                servings = int(m2.group(1))
                m = m2
        if m:
            title = ""
            j = i - 1
            while j >= 0 and j >= i - 10:
                prev = lines[j].strip()
                if not prev:
                    j -= 1
                    continue
                # titolo: riga title-case breve (non MAIUSCOLA, non prosa lunga)
                if (not prev.isupper() and 3 < len(prev) < 60
                        and not SECTION_RX.match(prev)
                        and not re.match(r"^(Preparation|Cooking|Prep|Cook)\s*time", prev, re.I)
                        and not prev.endswith(".") and not prev.endswith(":")):
                    title = _clean_title(prev)
                    break
                j -= 1
            # ingredienti: salta la prosa fino alla prima riga con quantita'
            ingredients: list[str] = []
            k = i + 1
            while k < n:
                il = lines[k].strip()
                if not il:
                    k += 1
                    continue
                if QTY_RX.match(il):
                    ingredients.append(il)
                    k += 1
                    break
                if il.isupper() and len(il) > 3:
                    break
                k += 1
            # continua gli ingredienti finche' righe con quantita' o brevi
            while k < n:
                il = lines[k].strip()
                if not il:
                    k += 1
                    continue
                if QTY_RX.match(il):
                    ingredients.append(il)
                    k += 1
                    continue
                if len(il) < 60 and not il.endswith(".") and not il.endswith(":"):
                    # possibile ingrediente senza quantita' o continuazione
                    if il[0].isdigit() or il[0].isupper():
                        ingredients.append(il)
                        k += 1
                        continue
                break
            # procedura: fino al prossimo titolo (riga title-case breve)
            steps: list[str] = []
            k2 = k
            while k2 < n:
                pl = lines[k2].strip()
                if not pl:
                    k2 += 1
                    continue
                if (pl.isupper() and len(pl) > 3) or (
                        not pl.endswith(".") and 3 < len(pl) < 60
                        and not SECTION_RX.match(pl) and not QTY_RX.match(pl)
                        and pl[0].isupper() and not pl[0].isdigit()):
                    break
                steps.append(pl)
                k2 += 1
            if ingredients:
                recipes.append({"title": title or f"Recipe {len(recipes)+1}",
                                "servings": servings,
                                "ingredients": _split_ingredients(ingredients),
                                "steps": steps[:20], "line": i})
            i = k2
            continue
        i += 1
    return recipes


def extract_japanese(pages: list[str]) -> list[dict]:
    """Formato giapponese: tabella NAME / INGREDIENTS / PROCESS.

    Struttura: 'NAME' 'INGREDIENTS' 'PROCESS' / <nome> / <makes N ...> /
    <ingredienti> / <processo>
    """
    text = "\n".join(pages)
    lines = text.splitlines()
    recipes: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i].strip()
        if l == "NAME" and i + 2 < n and "INGREDIENTS" in lines[i + 1] and "PROCESS" in lines[i + 2]:
            # nome ricetta: righe dopo la tabella
            j = i + 3
            while j < n and not lines[j].strip():
                j += 1
            name_lines = []
            while j < n:
                lj = lines[j].strip()
                if not lj:
                    j += 1
                    continue
                if re.match(r"^makes\s", lj, re.I):
                    break
                name_lines.append(lj)
                j += 1
            title = _clean_title(" ".join(name_lines)) or f"Recipe {len(recipes)+1}"
            # salta righe vuote dopo il nome, poi 'makes N'
            while j < n and not lines[j].strip():
                j += 1
            servings = 4
            m = re.search(r"makes\s+([\d⅓½¼¾⅔⅛]+)\s*\w*", lines[j].strip(), re.I) if j < n else None
            if m:
                q = m.group(1)
                total = 0.0
                for u, v in _FRAC.items():
                    if u in q:
                        total += float(v)
                        q = q.replace(u, "")
                if q:
                    try:
                        total += float(q)
                    except ValueError:
                        total = 4.0
                servings = max(1, int(total)) if total else 4
            # ingredienti: righe dopo makes
            ingredients: list[str] = []
            k = j + 1
            while k < n:
                il = lines[k].strip()
                if not il:
                    k += 1
                    continue
                if QTY_RX.match(il) or il[0].isdigit():
                    ingredients.append(il)
                    k += 1
                else:
                    break
            # processo: righe dopo gli ingredienti
            steps: list[str] = []
            k2 = k
            while k2 < n:
                pl = lines[k2].strip()
                if not pl:
                    k2 += 1
                    continue
                if pl == "NAME" or (pl.isupper() and len(pl) > 3):
                    break
                steps.append(pl)
                k2 += 1
            if ingredients:
                recipes.append({"title": title, "servings": servings,
                                "ingredients": _split_ingredients(ingredients),
                                "steps": steps[:20], "line": i})
            i = k2
            continue
        i += 1
    return recipes


def extract_escoffier(pages: list[str]) -> list[dict]:
    """Formato Escoffier: numero ricetta su riga sola + titolo + prosa.

    Struttura: '1104' / 'Beignets Pignatelli' / <ingredienti+procedura in prosa>.
    La prosa contiene quantita' (125 g, 2 tbs) e istruzioni.
    """
    text = "\n".join(pages)
    lines = text.splitlines()
    recipes: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        l = lines[i].strip()
        if re.fullmatch(r"\d{3,4}", l):
            # titolo: riga successiva non vuota
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            title = _clean_title(lines[j].strip()) if j < n else ""
            # ingredienti: righe con quantita' nella prosa seguente
            ingredients: list[str] = []
            steps: list[str] = []
            k = j + 1
            while k < n and k < j + 12:
                pl = lines[k].strip()
                if not pl:
                    k += 1
                    continue
                if re.fullmatch(r"\d{3,4}", pl):
                    break
                if QTY_RX.match(pl):
                    ingredients.append(pl)
                else:
                    steps.append(pl)
                k += 1
            if title and ingredients:
                recipes.append({"title": title, "servings": 4,
                                "ingredients": _split_ingredients(ingredients),
                                "steps": steps[:15], "line": i})
            i = k
            continue
        i += 1
    return recipes


EXTRACTORS = {
    "marchesi": extract_marchesi,
    "english": extract_english,
    "japanese": extract_japanese,
    "escoffier": extract_escoffier,
}


def detect_format(book_name: str, pages: list[str]) -> str:
    text = "\n".join(pages[:60])
    if "INGREDIENTI PER" in text:
        return "marchesi"
    if "NAME" in text and "INGREDIENTS" in text and "PROCESS" in text:
        return "japanese"
    if "escoffier" in book_name:
        return "escoffier"
    return "english"


async def load_book(book_dir: pathlib.Path, pack, client: Neo4jClient, principal: Principal,
                    prefix: str, limit: int | None = None) -> dict:
    pages = sorted(book_dir.glob("p*.md"))
    page_texts = [p.read_text(encoding="utf-8", errors="replace") for p in pages]
    # mappa riga->pagina
    line_page: dict[int, int] = {}
    offset = 0
    for p in pages:
        m = re.search(r"p(\d+)\.md", p.name)
        n = int(m.group(1)) if m else 0
        txt = p.read_text(encoding="utf-8", errors="replace")
        for k in range(len(txt.splitlines())):
            line_page[offset + k] = n
        offset += len(txt.splitlines())

    fmt = detect_format(book_dir.name, page_texts)
    extractor = EXTRACTORS.get(fmt, extract_english)
    recipes = extractor(page_texts)
    if limit:
        recipes = recipes[:limit]
    lang = "it" if fmt == "marchesi" else "en"
    mds = [_to_md(r["title"], r["servings"], r["ingredients"], r["steps"],
                  prefix, i + 1, line_page.get(r["line"], 0), lang)
           for i, r in enumerate(recipes)]
    # il fake LLM traduce solo i documenti nel corpus: passiamo tutti i md
    # (solo per libri IT; i libri EN non passano dalla traduzione)
    llm = build_fake_llm(pack, {f"r{i}": md for i, md in enumerate(mds)} if lang == "it" else {})
    loaded = 0
    errors = 0
    for i, (r, md) in enumerate(zip(recipes, mds)):
        try:
            if lang == "it":
                parsed = parse_source_md(
                    md,
                    known_units=pack.known_units(),
                    countable_units=pack.countable_units(),
                )
                translated = await translate_document(pack, md, llm)
                canonical = canonicalize(pack, translated.translated_md)
            else:
                # libri EN: md gia' in formato translated (## Ingredients/## Method)
                parsed = parse_translated_md(
                    md,
                    known_units=pack.known_units(),
                    countable_units=pack.countable_units(),
                )
                canonical = canonicalize(pack, md)
            doses = standardize_doses(canonical.canonical_md, pack, servings_target=10)
            ref = {"author": book_dir.name, "book": book_dir.name,
                   "page": str(line_page.get(r["line"], 0)),
                   "position": f"{book_dir.name}#line{r['line']}"}
            extract_document(client, None, f"{prefix}-{i+1:04d}", doses.canonical_md, pack, source_ref=ref)
            loaded += 1
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"    ! errore ricetta {i+1} ({r['title'][:30]}): {e}")
    return {"book": book_dir.name, "format": fmt, "recipes": len(recipes),
            "loaded": loaded, "errors": errors}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=pathlib.Path, required=True, help="dir canon_corpus")
    ap.add_argument("--books", nargs="*", default=None, help="dir libri (default: tutte)")
    ap.add_argument("--limit", type=int, default=None, help="max ricette per libro")
    ap.add_argument("--prefix", default="bk_", help="prefisso id documenti")
    ap.add_argument("--pack", default="domain-packs/ricette", help="dir domain pack")
    args = ap.parse_args()

    pack = load_domain_pack(args.pack)
    client = Neo4jClient.from_env()
    client.verify_connectivity()
    load_pack(client, pathlib.Path(args.pack))
    # indice vettoriale 384d (idempotente)
    with client.session() as s:
        s.run("CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS "
              "FOR (d:Document) ON (d.embedding) OPTIONS "
              "{indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}")
        s.run("CALL db.awaitIndex('document_embedding_vector', 30)")
    principal = Principal("bk_loader", ("admin",), (), "default", "bk_j_loader")

    books = args.books or [d.name for d in sorted(args.root.iterdir()) if d.is_dir() and d.name != "graphify-out"]
    print(f"libri da caricare: {len(books)}")
    total_loaded = 0
    for b in books:
        bdir = args.root / b
        if not bdir.is_dir():
            print(f"  ! libro non trovato: {b}")
            continue
        res = await load_book(bdir, pack, client, principal,
                              args.prefix + re.sub(r"[^a-z0-9]+", "_", b)[:20], args.limit)
        print(f"  {res['book']}: formato={res['format']} ricette={res['recipes']} "
              f"caricate={res['loaded']} errori={res['errors']}")
        total_loaded += res["loaded"]

    populate_embeddings(client, build_embedding_from_graph(client), principal)
    print(f"\nTOTALE: {total_loaded} ricette caricate nel grafo+vettore")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())