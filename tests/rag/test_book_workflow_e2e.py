"""Workflow completo libro -> grafo -> RAG (richiesta committente).

Per ogni ricetta REALE del libro Marchesi (fixture grezza in
tests/fixtures/book_recipes/marchesi_raw.json):

1. LIBRO -> MD: il testo grezzo del libro viene convertito nel template
   markdown (frontmatter + Ingredienti + Procedimento) con adattamenti
   editoriali documentati (virgole decimali -> punto, "SALE" -> "1 pizzico",
   tempi PREPARAZIONE+COTTURA -> time_min, prosa -> step numerati).
2. STANDARDIZZAZIONE: translate_document (P2-safe, segnaposto numerici) +
   canonicalize (unita' da units.yaml con rule_id, termini dal glossario,
   canon-log completo).
3. VERIFICA VS LIBRO ORIGINALE: (a) le quantita' degli ingredienti e i numeri
   del procedimento del libro compaiono nel source md; (b) invariante P2
   esatto source<->translated (nessun numero alterato); (c) canon-log
   bidirezionale (verify_canon_log: il diff e' interamente spiegato).
4. GRAFO + VETTORE: extract_document (Document/Entity/Fact + canonical_hash)
   + populate_embeddings (indice vettoriale 384d).
5. RAG: rag_query con query naturale -> il documento canonico ESATTO
   (hash match, canonical_md byte-identico).

Prefisso dati: ibw_ (pulizia dedicata, indice vettoriale ricreato).
"""
from __future__ import annotations

import json
import pathlib
import re

from app.auth import Principal
from app.domain import (
    canonicalize,
    extract_numbers,
    parse_source_md,
    translate_document,
    verify_canon_log,
    verify_l1,
)
from app.domain.extract import extract_document
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

PREFIX = "ibw_"
REPO = pathlib.Path(__file__).resolve().parents[2]
BOOK_RAW = REPO / "tests" / "fixtures" / "book_recipes" / "marchesi_raw.json"

# (chiave fixture, id atteso, [query naturali])
WORKFLOW_CASES = [
    (
        "asparagi-al-burro",
        "RIC-101",
        ["asparagi al burro", "ricetta asparagi al burro", "recipe with asparagus and butter"],
    ),
    (
        "fregola-con-le-vongole",
        "RIC-102",
        ["fregola con le vongole", "recipe with clams and fregola"],
    ),
    (
        "amaretti",
        "RIC-103",
        ["amaretti", "ricetta amaretti", "recipe with almonds and sugar"],
    ),
]

_DIFF = {"*": "facile", "**": "medio", "***": "difficile"}
_KNOWN_UNITS = {
    "g", "kg", "ml", "l", "dl", "cl", "°c", "min", "h", "ora", "ore",
    "cucchiaio", "cucchiai", "cucchiaino", "cucchiaini", "tazza", "tazze",
    "pizzico", "spicchio", "spicchi", "foglie", "foglia", "rametti", "rametto",
    "bustina", "mazzetto", "fette", "fetta", "fili", "filo", "coste", "costa",
    "ciuffo", "ciuffi", "presa", "gocce", "goccia", "etto", "etti", "noci",
    "noce", "chicchi", "chicco", "scorza",
}


def _minutes(s: str) -> int:
    s = s.upper()
    m = re.search(r"(\d+)\s*ORA", s)
    mi = re.findall(r"(\d+)\s*MINUT", s)
    total = 0
    if m:
        total += int(m.group(1)) * 60
    if mi:
        total += max(int(x) for x in mi)
    return total if total > 0 else 30


def _parse_ingredient(raw: str) -> tuple[float | None, str | None, str]:
    raw = re.sub(r"\s+", " ", raw.strip().rstrip(",")).strip()
    raw = re.sub(r"\s*\(.*?\)\s*", " ", raw)
    m = re.match(r"^(\d[\d,\.]*)\s+(.*)$", raw)
    if m:
        qty = float(m.group(1).replace(",", "."))
        rest = m.group(2).strip()
    else:
        qty, rest = None, raw
    rest = re.sub(r"^(?:di\s+)+", "", rest, flags=re.IGNORECASE)
    tokens = rest.split()
    unit = None
    for k in (2, 1):
        cand = " ".join(tokens[:k]).lower().rstrip(".")
        if cand in _KNOWN_UNITS or cand.rstrip("s") in _KNOWN_UNITS:
            unit = cand
            rest = " ".join(tokens[k:])
            break
    return qty, unit, rest.strip().lower()


def book_to_source_md(raw: str, doc_id: str) -> str:
    """Libro (testo grezzo) -> source md nel template (adattamenti documentati)."""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    title = lines[0].title()
    body = "\n".join(lines[1:])
    prep = _minutes(re.search(r"PREPARAZIONE\s*:?\s*([^\n]+)", body).group(1))
    cook = _minutes(re.search(r"COTTURA\s*:?\s*([^\n]+)", body).group(1))
    diff = re.search(r"DIFFICOLTÀ\s*:?\s*([^\n]+)", body).group(1).strip()
    serv = int(re.search(r"INGREDIENTI PER\s+(\d+)", body).group(1))
    tail = body[body.find("INGREDIENTI PER"):]
    # Blocco ingredienti: righe dopo il marcatore finche' una riga non finisce con virgola.
    lines_after = tail.split("\n", 1)[1].splitlines() if "\n" in tail else []
    ing_txt, idx = "", 0
    for idx, ln in enumerate(lines_after):
        s = ln.strip()
        if not s:
            continue
        ing_txt += " " + s
        if not s.endswith(","):
            break
    # Split su ", " (virgola+spazio): la virgola decimale "1,5" non ha spazio dopo.
    ingredients = []
    for piece in re.split(r",\s+", ing_txt.strip()):
        qty, unit, item = _parse_ingredient(piece)
        if item and len(item) >= 2 and "PERSONE" not in item and "INGREDIENTI" not in item:
            ingredients.append((qty, unit, item))
    method_text = re.sub(r"\s*\n\s*", " ", " ".join(lines_after[idx + 1:]).strip())
    steps = [s.strip() for s in re.split(r"(?<=[.!?])\s+", method_text) if len(s.strip()) > 15][:12]

    md = (
        f"---\ntitle: {title}\nid: {doc_id}\nlang: it\nservings: {serv}\n"
        f"time_min: {prep + cook}\ndifficulty: {_DIFF.get(diff, 'facile')}\n---\n## Ingredienti\n"
    )
    for qty, unit, item in ingredients:
        if qty is None:
            md += f"- 1 pizzico {item}\n"
        elif unit is None:
            md += f"- {qty:g} {item}\n"
        else:
            md += f"- {qty:g} {unit} {item}\n"
    md += "\n## Procedimento\n"
    for j, st in enumerate(steps, 1):
        md += f"{j}. {st}\n"
    return md


def _book_content_numbers(raw: str) -> set[str]:
    """Numeri di contenuto del libro (ingredienti + procedimento, esclusi i metadati)."""
    body = raw.split("INGREDIENTI PER", 1)[1] if "INGREDIENTI PER" in raw else raw
    return {
        t.replace(",", ".")
        for t in re.findall(r"\b\d+[,\.]?\d*\b", body)
        if t not in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10")
    }


def _recreate_vector_index(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run("DROP INDEX document_embedding_vector IF EXISTS")
        session.run(
            """
            CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS
            FOR (d:Document) ON (d.embedding)
            OPTIONS {indexConfig: {
                `vector.dimensions`: 384,
                `vector.similarity_function`: 'cosine'
            }}
            """
        )


def _cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )


async def test_ibw_full_workflow_book_to_rag(client, pack, pack_dir) -> None:
    """Libro -> md -> standardizzazione -> verifica -> grafo+vettore -> RAG."""
    _recreate_vector_index(client)
    load_pack(client, pack_dir)
    raw = json.loads(BOOK_RAW.read_text(encoding="utf-8"))
    # Fake LLM con traduzione glossario-based per QUALSIASI input (non solo il corpus):
    # i source md generati dal libro non sono nel corpus, quindi serve un fallback
    # che traduca sezioni e termini (translate_masked) invece di restituire identita'.
    book_sources = {
        key: book_to_source_md(raw[key], expected)
        for key, expected, _ in WORKFLOW_CASES
    }
    llm = build_fake_llm(pack, book_sources)

    doc_id_by_expected: dict[str, str] = {}
    try:
        for key, expected_doc, queries in WORKFLOW_CASES:
            # 1) LIBRO -> MD
            source_md = book_to_source_md(raw[key], expected_doc)
            parsed = parse_source_md(source_md, known_units=pack.known_units())
            assert parsed.ingredients, f"[{key}] nessun ingrediente estratto"

            # 3a) VERIFICA VS LIBRO: quantita'/numeri del libro nel source md
            book_nums = _book_content_numbers(raw[key])
            md_nums = set(extract_numbers(source_md))
            missing = book_nums - md_nums
            assert not missing, f"[{key}] numeri del libro mancanti nel md: {missing}"

            # 2) STANDARDIZZAZIONE: traduzione P2-safe + canonicalizzazione
            translated = await translate_document(pack, source_md, llm)
            l1 = verify_l1(source_md, translated.translated_md, pack=pack)
            assert l1.passed, f"[{key}] L1: {l1.issues}"
            # 3b) invariante P2 esatto source<->translated
            assert extract_numbers(source_md) == extract_numbers(translated.translated_md), (
                f"[{key}] P2 violato: numeri alterati in traduzione"
            )
            canonical = canonicalize(pack, translated.translated_md)
            # 3c) canon-log bidirezionale (diff interamente spiegato)
            ok_log = verify_canon_log(pack, translated.translated_md, canonical.canonical_md, canonical.log_entries)
            assert ok_log, f"[{key}] canon-log incompleto"

            # 4) GRAFO + VETTORE
            doc_id = f"{PREFIX}{canonical.document_id}"
            doc_id_by_expected[expected_doc] = doc_id
            extract_document(client, None, doc_id, canonical.canonical_md, pack)
            with client.session() as session:
                session.run(
                    "MATCH (d:Document {id: $id}) SET d.source_title = $title",
                    id=doc_id,
                    title=parsed.title,
                )

        embedding = build_embedding_from_graph(client, pack)
        populated = populate_embeddings(client, embedding)
        assert populated == len(WORKFLOW_CASES), f"popolati {populated}"

        # 5) RAG: la ricetta normalizzata viene ritrovata
        admin = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")
        found = 0
        for key, expected_doc, queries in WORKFLOW_CASES:
            for query in queries:
                hits = rag_query(client, admin, query, lang="it", limit=5, embedding=embedding)
                top_ids = [h.document_id for h in hits]
                matched = next((h for h in hits if h.document_id == expected_doc), None)
                assert expected_doc in top_ids, (
                    f"[{key}] query {query!r}: atteso {expected_doc}, top5={top_ids}"
                )
                assert matched is not None and matched.canonical_md == canonical_by_doc_id(expected_doc, client, doc_id_by_expected[expected_doc]), (
                    f"[{key}] hash/md non identico per {query!r}"
                )
                found += 1
        print(f"\n[ibw] workflow libro->RAG: {found}/{sum(len(q) for _, _, q in WORKFLOW_CASES)} query trovate")
    finally:
        _cleanup(client)


def canonical_by_doc_id(expected_doc: str, client: Neo4jClient, doc_id: str) -> str:
    """Ricomponi il canonical.md atteso dal grafo (il doc appena estratto)."""
    from app.domain.recompose import recompose_document
    return recompose_document(client, doc_id)
