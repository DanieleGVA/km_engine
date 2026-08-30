# Spec Iterazione A — Domain Knowledge Layer (ricette)

**Versione:** 1.0 — 2026-08-30
**Riferimenti:** `roadmap-iterazioni-e-test.md` (sez. 1) · `architecture.md` v1.0 (MVP) · `docs/requirements.md` (FR9)
**Obiettivo:** costruire il **Domain Knowledge Layer** manuale ("ricette") sopra il prototipo km_engine:
IR markdown a due stadi, sotto-grafo canonico, verifica a 3 livelli, round-trip garantito.

---

## 0. Principi vincolanti (P1–P7)

- **P1** Due stadi IR: `translated.md` (traduzione fedele EN, invariante sui numeri) →
  `canonical.md` (rappresentazione canonica deterministica, normalizzata).
- **P2** LLM mai sui numeri: quantità, temperature, tempi non passano mai dal modello
  senza verifica; invariante: nessun numero alterato fuori da una regola tracciata.
- **P3** Normalizzazione mai distruttiva: il testo originale è sempre recuperabile
  (provenance + source_language + canon-log).
- **P4** Glossario nel grafo: i termini canonici sono nodi `:CanonicalTerm` nel grafo,
  versionati e visibili solo agli autorizzati (default-deny).
- **P5** Gate umano sull'ontologia: modifica di glossari/template = PR (fuori repo o
  via API di adjudication con audit). Nessuna normalizzazione inventata.
- **P6** Riuso interfacce km_engine: storage/auth/query/api esistenti; estensioni additive.
- **P7** Standard esterni prima di canoni proprietari: unità SI + FoodOn/DBpedia
  (riferimenti URI) quando disponibili.

## 1. Struttura del Domain Pack

```
domain-packs/ricette/
├── pack.yaml                  # meta: nome, lingua, ontologie esterne, versioni
├── template.md                # template IR (frontmatter + strutture inline)
├── glossari/
│   ├── tecnica.yaml           # TECNICHE: id → label EN/IT, alias, definizione
│   ├── ingredienti.yaml       # INGREDIENTI: id FoodOn → label EN/IT, alias, unità default
│   └── stati.yaml             # STATI (cottura, consistenza): id → label EN/IT, alias
├── units.yaml                 # regole conversione unità (da→a, fattore, arrotondamento)
└── regole/
    ├── normalizzazione.yaml   # regole deterministiche di riscrittura canonica
    └── verifica.yaml          # soglie e regole dei 3 livelli di verifica
```

**pack.yaml (schema pydantic `DomainPack` in `app/domain/pack.py`):**
```yaml
name: ricette
language: it           # lingua del corpus sorgente
canonical_language: en # lingua canonica di output (FR9.1)
version: 1.0.0
ontologies:            # P7: standard esterni di riferimento
  - { prefix: foodon, uri: http://purl.obolibrary.org/obo/FOODON_ }
  - { prefix: dbpedia, uri: http://dbpedia.org/resource/ }
units_source: units.yaml
glossaries: [tecnica, ingredienti, stati]
```

**template.md:** frontmatter YAML (`title`, `id`, `lang`, `source`, `servings`,
`time_min`, `difficulty`) + sezioni `## Meta`, `## Ingredienti` (lista `- N unit ingredient`),
`## Procedimento` (step numerati), con struttura inline per quantità:
`{qty: 200, unit: g, item: farina}`.

## 2. Stadio 1 — Traduzione (`translated.md`)

- Input: markdown sorgente (IT) → output `translated.md` fedele in EN.
- **P2**: estrazione preventiva dei numeri del sorgente (regex su quantità/temp/tempo);
  il traduttore LLM traduce con numeri come segnaposto `{N1}`, `{N2}`; fase di
  re-iniezione verifica l'uguaglianza multiset col sorgente originale (invariante).
- **LLMSemanticService reale (chiude FR9.2)**: config `KM_LLM_API_KEY/ENDPOINT/MODEL`;
  client httpx; prompt con istruzioni di fedeltà; per i test: `FakeLLMClient`
  deterministico che "traduce" secondo fixture (nessuna rete nei test).
- Interfaccia: `translate_document(pack, source_md, llm) -> TranslatedDocument`.

## 3. Motore di verifica a 3 livelli (`app/domain/verify.py`)

- **L1 — Struttura/numeri (deterministico, no LLM):** parser del template; controlla
  frontmatter completo, sezioni presenti, unità riconosciute, integrità numerica
  (P2 invariants: multiset numeri source == translated), ingredienti presenti.
- **L2 — Sezioni semantiche (LLM opzionale, stub nei test):** confronto per sezione
  originale↔tradotta; divergenza localizzata alla sezione; se oltre soglia → escalation L3.
- **L3 — Coda adjudication umana:** righe in Postgres `adjudications` (documento,
  sezione, motivo, stato pending→approved/rejected, risolto da/date, suggestion);
  API approve/reject; stato riflesso su `:Source` e frontmatter `verification_level`.

## 4. Estensioni Neo4j (`db/neo4j/002_domain_schema.cypher`)

```
(:Document {id, title, lang, source_lang, canonical_hash, verification_level,
            translation_state, source_language})
(:CanonicalTerm {id, namespace, term_id, label_en, label_it, definition,
                 ontology_uri, is_public, roles, teams})
(:DomainPack {id, name, version, language, canonical_language})
(:Document)-[:PART_OF_PACK]->(:DomainPack)
(:Document)-[:NORMALIZED_TO]->(:CanonicalTerm)   -- ogni entità normalizzata
(:Entity)-[:NORMALIZED_TO]->(:CanonicalTerm)
(:Entity)-[:PART_OF_DOC]->(:Document)
(+ vincoli unicità :Document.id, :CanonicalTerm.id, :DomainPack.id;
   indice fulltext Document.title; INDICE VETTORIALE Document.embedding
   se supportato da Community 5.26 — verificare, altrimenti documentare fallback)
```

**Visibilità (P4):** `:Document` e `:CanonicalTerm` ereditano default-deny;
`principal_visibility_context` esteso nel query engine per i nuovi tipi.

## 5. Stadio 2 — Canonicalizzazione (`app/domain/canonical.py`)

- `canonicalize(pack, translated_md, conn) -> CanonicalResult`
- Regole deterministiche (NON LLM), ordine fisso:
  1. **Unità** (`units.yaml`): conversione esatta con `rule_id` in canon-log
     (es. `100 g → 100 g`; `1 tazza → 250 ml` con arrotondamento dichiarato).
  2. **Termini** (glossari): alias → id canonico (`farina 00 → ING-WHEAT-FLOUR`);
     termini irrisolti → **coda proposte** (Postgres `glossary_proposals`),
     mai normalizzazioni inventate nel md (test dedicato).
  3. **Riscrittura strutturale**: template canonico con quantità/unità/termini normalizzati.
- **canon-log** (`canon_log` su Postgres oppure `:CanonicalLogEntry` nel grafo — scelta
  implementativa documentata): per ogni differenza translated↔canonical una riga
  (documento, campo, prima, dopo, regola_id). Invariante: **il diff spiega il 100%
  del log e viceversa**.

## 6. Estrattore md→grafo e ricompositore (round-trip)

- **Estrattore** `extract_document(doc_id, canonical_md, conn)`: crea/aggiorna
  `:Document` (canonical_hash = sha256 del md canonico) + `:Entity` per ingredienti/
  tecniche/stati + `:Fact` (quantità, unità, tempo, temperatura) con `PART_OF_DOC` e
  `NORMALIZED_TO`; idempotente (MERGE sul canonical_hash).
- **Ricompositore** `recompose_document(doc_id, conn) -> canonical_md`: dal grafo
  ricostruisce il md canonico (frontmatter + sezioni) — inversa esatta dell'estrattore.
- **Round-trip invariante**: `recompose(extract(md)) == md` byte-identico (o con
  normalizzazione canonica definita) al 100% del corpus.

## 7. Pipeline orchestrata (`scripts/dag_pipeline.py` o CLI)

`bootstrap_pack` → `translate` → `verify` → `canonicalize` → `ingest` → `query` →
`recompose`. Stati persistiti (Postgres `domain_jobs` opzionale; riuso ingest_jobs dove possibile).

## 8. Test (criteri del roadmap sez. 1 — vincolanti)

| # | Test | Criterio |
|---|---|---|
| T1 | Parser template (frontmatter, strutture, malformati) | 100% casi + errori espliciti |
| T2 | Conversioni unità (tutte le righe, limiti, arrotondamento) | aritmetica esatta, rule_id presente |
| T3 | Invarianti P2 (numeri source vs translated) | nessun numero alterato |
| T4 | Verifica L1 (corruzioni sintetiche) | ogni corruzione intercettata |
| T5 | Verifica L2 (sezioni riscritte) | divergenza localizzata, escalation L3 |
| T6 | Coda L3 + adjudication | workflow completo + audit + stato su Source/frontmatter |
| T7 | Bootstrap pack idempotente | nessun duplicato, MERGE stabile |
| T8 | Visibilità Document/CanonicalTerm | default-deny, admin bypass |
| T9 | Canon-log completo | 100% diff spiegati |
| T10 | Termini irrisolti | coda proposte, mai inventati nel md |
| T11 | **E2E round-trip** | `recompose(ingest(canonical.md)) == canonical.md` su 100% corpus |
| T12 | E2E flusso completo | bootstrap→load→translate→verify→canonicalize→ingest→query→recompose |
| T13 | Qualità | copertura ≥90%, ruff pulito, 207 test MVP verdi |

## 9. Gate

GA1 pack+templates · GA2 traduzione P2-safe · GA3 verifica L1/L2/L3 ·
GA4 schema grafo+visibilità · GA5 canonicalizzazione+canon-log · GA6 round-trip E2E.
Uscita: **ADR-004**, aggiornamento `architecture.md`/`runbook.md`, report iterazione A.


---

## Appendice A — Formato `canonical.md` (contratto vincolante per round-trip)

File generato dallo stadio 2 e usato dallo stadio 3 (ingest). **Il round-trip
`recompose(ingest(md)) == md` è garantito solo se extractor e ricompositore usano
ESATTAMENTE questo template** (stessi separatori, ordine delle chiavi, formattazione).

```markdown
---
title: <title EN>
id: <id identico al source>
lang: en                    # lingua canonica (FR9.1)
source_lang: it             # lingua originale (FR9.4)
servings: <int>
time_min: <int>
difficulty: easy            # facile|medio|difficile -> easy|medium|hard
verification_level: L1|L2|L3
canonical_version: 1
---
## Ingredients
- <qty> <unit> <CANONICAL-TERM-LABEL>
## Method
1. <step EN con numeri invariati>
```

**Grammatica riga ingrediente:** `- ` + `<qty>` (int o float, punto decimale) + ` `
+ `<unit>` (da `units.yaml`; se assente: `unit: <unit>` non viene toccata e il
termine va in coda proposte) + ` ` + `<term>` (label EN del `:CanonicalTerm`;
se il termine non è nel glossario, label = traduzione EN letterale della fonte e
il termine va in coda proposte: **mai inventare un id**).

**Regole di serializzazione:**
- qty: intero senza zeri superflui (`200`, non `200.0`); float con massimo 3 decimali
  (es. `0.5`).
- unit: sempre singolare canonica (`cucchiai → tablespoon`, `tazze → cup`,
  `g`, `kg`, `ml`, `l`, `°C`, `min`, `h`).
- frontmatter: chiavi nell'ordine esatto sopra; YAML scalari senza apici.
- Sezioni: `## Ingredients` poi `## Method`; step numerati `1. `, `2. ` ...
- Il file termina con un singolo `\n` (nessuna riga vuota finale extra).

**canon-log:** ogni trasformazione md→canonical produce una riga di log
(documento, campo, prima, dopo, rule_id). L'invariante T9: il diff testuale
translated↔canonical deve essere interamente spiegato dalle righe di log.
