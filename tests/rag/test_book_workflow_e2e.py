"""Workflow completo EMENDATO libro -> grafo -> RAG (richiesta committente).

Per ogni ricetta REALE del libro Marchesi (fixture grezza in
tests/fixtures/book_recipes/marchesi_raw.json), il test verifica TUTTI gli step:

1. LIBRO -> MD: testo grezzo del libro convertito nel template markdown
   (frontmatter + Ingredienti + Procedimento) con adattamenti documentati.
2. STANDARDIZZAZIONE ingredienti/procedure/unita': translate_document (P2-safe)
   + canonicalize (unita' da units.yaml con rule_id, termini dal glossario,
   canon-log completo).
3. DOSI MKS PER 10 PERSONE: standardize_doses converte le unita' culinarie in
   MKS (g/ml/l/°C/min/h) e scala proporzionalmente a 10 persone
   (fattore = 10 / servings del libro, es. 4 -> x2.5). Verifica: servings=10,
   unita' MKS, quantita' scalate col fattore atteso.
4. VERIFICA vs LIBRO ORIGINALE: (a) ingredienti del libro presenti nel md;
   (b) procedure (step) presenti; (c) DOSI coerenti: qty_scalata == qty_libro
   * (10/servings_libro) entro tolleranza; (d) invariante P2 source<->translated;
   (e) canon-log bidirezionale.
5. CARICAMENTO nel grafo e nel vector: extract_document (Document/Entity/Fact +
   canonical_hash + RIFERIMENTI autore/libro/pagina/posizione) +
   populate_embeddings (indice vettoriale 384d).
6. RAG: rag_query con query naturale -> la ricetta normalizzata ESATTA
   (hash match, canonical_md byte-identico) CON TUTTI I RIFERIMENTI:
   autore, libro, pagina (vs libro originale), posizione (vs file md tradotto).

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
from app.domain.doses import MKS_FACTORS, MKS_NATIVE, standardize_doses
from app.domain.extract import extract_document
from app.rag.rag import build_embedding_from_graph, populate_embeddings, rag_query
from app.storage.client import Neo4jClient
from scripts.load_domain_pack import load_pack
from tests.domain.fake_llm import build_fake_llm

PREFIX = "ibw_"
REPO = pathlib.Path(__file__).resolve().parents[2]
BOOK_RAW = REPO / "tests" / "fixtures" / "book_recipes" / "marchesi_raw.json"

# (chiave fixture, id atteso, servings libro, [query naturali])
WORKFLOW_CASES = [
    (
        "asparagi-al-burro",
        "RIC-101",
        4,
        ["asparagi al burro", "ricetta asparagi al burro", "recipe with asparagus and butter"],
    ),
    (
        "fregola-con-le-vongole",
        "RIC-102",
        4,
        ["fregola con le vongole", "recipe with clams and fregola"],
    ),
    (
        "amaretti",
        "RIC-103",
        4,
        ["amaretti", "ricetta amaretti", "amaretti with almonds and sugar"],
    ),
]

# Riferimenti del libro (autore, titolo, pagina/capitolo, posizione nel file md).
BOOK_REF = {
    "author": "Gualtiero Marchesi",
    "book": "La cucina italiana. Il grande ricettario",
    "page": "part0016 (Verdura) / part0008 (Primi di mare) / part0017 (Dolci)",
    "position": "tests/fixtures/book_recipes/marchesi_raw.json",
}

_DIFF = {"*": "facile", "**": "medio", "***": "difficile"}

# Unita' del libro (italiane) -> fattore MKS (per la verifica dosi).
_BOOK_MKS = {
    "cucchiaio": 15.0, "cucchiai": 15.0, "cucchiaino": 5.0, "cucchiaini": 5.0,
    "tazza": 250.0, "tazze": 250.0, "pizzico": 0.5, "spicchio": 5.0, "spicchi": 5.0,
    "foglie": 1.0, "foglia": 1.0, "rametti": 1.0, "rametto": 1.0, "bustina": 7.0,
    "mazzetto": 50.0, "fette": 30.0, "fetta": 30.0, "fili": 1.0, "filo": 1.0,
    "gocce": 0.05, "goccia": 0.05, "etto": 100.0, "etti": 100.0, "noci": 10.0,
    "noce": 10.0, "chicchi": 0.1, "chicco": 0.1, "coste": 20.0, "costa": 20.0,
    "ciuffi": 5.0, "ciuffo": 5.0, "presa": 0.5, "scorza": 1.0,
}
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
    # "4 o 5 albumi" -> "4 albumi" (adattamento documentato: si prende il primo valore)
    rest = re.sub(r"^o\s+\d+\s+", "", rest, flags=re.IGNORECASE)
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
    lines_after = tail.split("\n", 1)[1].splitlines() if "\n" in tail else []
    ing_txt, idx = "", 0
    for idx, ln in enumerate(lines_after):
        s = ln.strip()
        if not s:
            continue
        ing_txt += " " + s
        if not s.endswith(","):
            break
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
    """Tutti gli step del workflow emendato: libro->md->standardizzazione->dosi MKS x10->verifica->grafo+vettore->RAG con riferimenti."""
    _recreate_vector_index(client)
    load_pack(client, pack_dir)
    raw = json.loads(BOOK_RAW.read_text(encoding="utf-8"))
    book_sources = {
        key: book_to_source_md(raw[key], expected)
        for key, expected, _, _ in WORKFLOW_CASES
    }
    llm = build_fake_llm(pack, book_sources)

    doc_id_by_expected: dict[str, str] = {}
    try:
        for key, expected_doc, book_servings, queries in WORKFLOW_CASES:
            # ---- STEP 1: LIBRO -> MD ----
            source_md = book_to_source_md(raw[key], expected_doc)
            parsed = parse_source_md(source_md, known_units=pack.known_units())
            assert parsed.ingredients, f"[{key}] nessun ingrediente estratto"
            assert parsed.steps, f"[{key}] nessuno step di procedura"

            # ---- STEP 2: STANDARDIZZAZIONE ingredienti/procedure/unita' ----
            translated = await translate_document(pack, source_md, llm)
            l1 = verify_l1(source_md, translated.translated_md, pack=pack)
            assert l1.passed, f"[{key}] L1: {l1.issues}"
            canonical = canonicalize(pack, translated.translated_md)
            ok_log = verify_canon_log(pack, translated.translated_md, canonical.canonical_md, canonical.log_entries)
            assert ok_log, f"[{key}] canon-log incompleto"

            # ---- STEP 3: DOSI MKS PER 10 PERSONE ----
            doses = standardize_doses(canonical.canonical_md, pack, servings_target=10)
            assert doses.servings == 10, f"[{key}] servings != 10"
            expected_factor = 10 / book_servings
            assert abs(doses.scale_factor - expected_factor) < 1e-9, (
                f"[{key}] fattore {doses.scale_factor} != atteso {expected_factor}"
            )
            # passo 8: unita' naturali intoccabili (conteggi ammessi), il resto MKS
            from app.domain.doses import COUNT_UNITS as _COUNT_UNITS
            for line in doses.canonical_md.splitlines():
                m = re.match(r"^- (\S+) (\S+) (.+)$", line)
                if m:
                    unit = m.group(2).lower()
                    assert unit in MKS_NATIVE or unit in MKS_FACTORS or unit in _COUNT_UNITS, (
                        f"[{key}] unita' non MKS: {unit}"
                    )
            # dose-log presente (conversioni + scaling)
            assert any(e.rule_id.startswith("DOSE-") for e in doses.log_entries), f"[{key}] dose-log vuoto"

            # ---- STEP 4: VERIFICA vs LIBRO ORIGINALE ----
            # (a) ingredienti del libro nel md
            book_ing = [i[2] for i in _book_ingredients(raw[key])]
            md_ing = [i.item for i in parsed.ingredients]
            for bi in book_ing:
                assert any(bi in mi for mi in md_ing), f"[{key}] ingrediente libro mancante: {bi}"
            # (b) procedure: step presenti
            assert len(parsed.steps) >= 2, f"[{key}] procedure troppo corte"
            # (c) DOSI coerenti: qty_scalata == qty_libro * (10/servings), per indice
            #     (l'ordine degli ingredienti e' preservato da traduzione+canonicalizzazione)
            book_ings = _book_ingredients(raw[key])
            dose_lines = [
                m for m in (re.match(r"^- (\S+) (\S+) (.+)$", l) for l in doses.canonical_md.splitlines())
                if m
            ]
            assert len(dose_lines) == len(book_ings), (
                f"[{key}] n. ingredienti dose ({len(dose_lines)}) != libro ({len(book_ings)})"
            )
            from app.domain.doses import COUNT_UNITS as _DOSE_COUNT
            unit_rules = pack.unit_rules_by_from()
            for i, (bi_qty, bi_unit, bi_item) in enumerate(book_ings):
                if bi_qty is None:
                    continue
                # passo 8: unita' naturali (conteggio) scalate sulla quantita'
                # naturale; unita' di misura vere convertite in MKS. L'unita'
                # del libro viene risolta tramite le regole del pack.
                bu = (bi_unit or "").lower()
                canonical_unit = unit_rules.get(bu).to_unit if bu in unit_rules else bu
                if canonical_unit in _DOSE_COUNT:
                    expected_scaled = bi_qty * expected_factor
                else:
                    mks_factor = _BOOK_MKS.get(bu, 1.0)
                    expected_scaled = bi_qty * expected_factor * mks_factor
                got = float(dose_lines[i].group(1))
                assert abs(got - expected_scaled) / max(expected_scaled, 1e-9) < 0.05, (
                    f"[{key}] dose[{i}] {bi_item}: attesa {expected_scaled:.2f} "
                    f"(libro {bi_qty} x {expected_factor:.2f}), trovata {got}"
                )
            # (d) P2 esatto source<->translated
            assert extract_numbers(source_md) == extract_numbers(translated.translated_md), f"[{key}] P2 violato"

            # ---- STEP 5: CARICAMENTO grafo + vettore (con riferimenti) ----
            doc_id = f"{PREFIX}{canonical.document_id}"
            doc_id_by_expected[expected_doc] = doc_id
            ref = {
                "author": BOOK_REF["author"],
                "book": BOOK_REF["book"],
                "page": BOOK_REF["page"],
                "position": f"{BOOK_REF['position']}#{key}",
            }
            extract_document(client, None, doc_id, doses.canonical_md, pack, source_ref=ref)
            with client.session() as session:
                session.run(
                    "MATCH (d:Document {id: $id}) SET d.source_title = $title",
                    id=doc_id,
                    title=parsed.title,
                )

        embedding = build_embedding_from_graph(client, pack)
        populated = populate_embeddings(client, embedding)
        assert populated == len(WORKFLOW_CASES), f"popolati {populated}"

        # ---- STEP 6: RAG recupera la ricetta normalizzata CON I RIFERIMENTI ----
        admin = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")
        found = 0
        for key, expected_doc, _, queries in WORKFLOW_CASES:
            for query in queries:
                hits = rag_query(client, admin, query, lang="it", limit=5, embedding=embedding)
                top_ids = [h.document_id for h in hits]
                matched = next((h for h in hits if h.document_id == expected_doc), None)
                assert expected_doc in top_ids, f"[{key}] query {query!r}: atteso {expected_doc}, top5={top_ids}"
                # ricetta normalizzata ESATTA (hash match)
                expected_md = recompose_document(client, doc_id_by_expected[expected_doc])
                assert matched.canonical_md == expected_md, f"[{key}] md non identico per {query!r}"
                # TUTTI I RIFERIMENTI: autore, libro, pagina, posizione
                assert matched.source_ref, f"[{key}] source_ref assente per {query!r}"
                assert matched.source_ref.get("source_author") == BOOK_REF["author"], f"[{key}] autore mancante"
                assert matched.source_ref.get("source_book") == BOOK_REF["book"], f"[{key}] libro mancante"
                assert matched.source_ref.get("source_page"), f"[{key}] pagina mancante"
                assert key in matched.source_ref.get("source_position", ""), f"[{key}] posizione mancante"
                found += 1
        print(f"\n[ibw] workflow emendato libro->RAG: {found}/{sum(len(q) for _, _, _, q in WORKFLOW_CASES)} query trovate con riferimenti")
    finally:
        _cleanup(client)


def _book_ingredients(raw: str) -> list[tuple[float | None, str | None, str]]:
    """Ingredienti del libro (qty, unit, item) per la verifica dosi."""
    tail = raw.split("INGREDIENTI PER", 1)[1] if "INGREDIENTI PER" in raw else raw
    lines_after = tail.split("\n", 1)[1].splitlines() if "\n" in tail else []
    ing_txt = ""
    for ln in lines_after:
        s = ln.strip()
        if not s:
            continue
        ing_txt += " " + s
        if not s.endswith(","):
            break
    out = []
    for piece in re.split(r",\s+", ing_txt.strip()):
        qty, unit, item = _parse_ingredient(piece)
        if item and len(item) >= 2 and "PERSONE" not in item and "INGREDIENTI" not in item:
            out.append((qty, unit, item))
    return out


def recompose_document(client: Neo4jClient, doc_id: str) -> str:
    from app.domain.recompose import recompose_document as _r
    return _r(client, doc_id)
