# Piano di sviluppo — Ripristino riconoscimento ricette (km_engine)

**Esecutore:** Claude Code (Opus 5) · **Repo:** `DanieleGVA/km_engine` · **Branch:** `fix/recipe-normalization` · **Data:** 2026-09-02
**Riferimento:** `spec-normalizzazione-ricette-fix.md` (diagnosi D1–D8, gate GF0–GF6). Questo documento è il piano operativo: cosa toccare, come, con quali test e quali comandi.

---

## 0. Regole operative per l'agente

1. **Un WP = un commit (o PR) con gate misurato.** Il messaggio di commit termina con la riga `coverage: <prima>% -> <dopo>%` presa da `scripts/measure_coverage.py`. Non iniziare il WP successivo se il gate non è verde.
2. **Misura prima, misura dopo.** Prima di ogni modifica esegui `python scripts/measure_coverage.py --corpus tests/fixtures/corpus_marchesi_full --pack domain-packs/ricette --out /tmp/before.json`; dopo, `--out /tmp/after.json`; riporta il delta.
3. **Ambiente test.** I test in `tests/domain` richiedono Postgres e Neo4j (`tests/domain/conftest.py`, DSN da `KM_TEST_PG_DSN`). Avviarli con `docker compose up -d neo4j postgres` dalla root e applicare `db/postgres/*.sql`, `db/neo4j/*.cypher` come da `docs/runbook.md`. Comando test: `pytest tests/domain -q`. I test che non usano fixture DB (`test_parser.py`, `test_numbers.py`, `test_pack.py`) girano anche senza DB.
4. **Perimetro.** File modificabili: `app/domain/{verify,canonical,pack,extract,numbers,recompose,translate}.py`, nuovo `app/domain/normalize.py`, `app/agents/evaluator.py`, `domain-packs/ricette/**`, `scripts/measure_coverage.py`, `scripts/build_corpus_fixtures.py`, `tests/domain/**`, `tests/fixtures/corpus_marchesi_full/**`, `docs/coverage/**`. **Non toccare** `app/auth`, `app/storage`, `app/rag`, `app/api`, `app/conflict`, `code_domain/**`, `domain-packs/code/**`. Se un fix richiede di uscire dal perimetro, fermati e chiedi.
5. **Non ampliare il glossario nei WP F1–F4.** L'aumento di coverage in quei WP deve venire solo dal codice. Aggiungere alias per far passare un gate è vietato: maschera il difetto.
6. **Nessuna risoluzione inventata.** Il principio T10 resta: un termine non risolto non viene mai riscritto nel markdown canonico. Cambia solo *come* si prova a risolverlo.
7. **Round-trip T9 sempre verde.** Dopo ogni WP: `pytest tests/domain/test_a6_roundtrip.py tests/domain/test_canonical.py -q`. Se T9 si rompe, il WP non è finito.
8. **Punti di decisione umana** (fermarsi e chiedere): F3 se il raw completo del corpus non è reperibile; F4 sulla serializzazione degli stati nel template; F5 su ogni lotto di glossario.
9. **Report finale** in `docs/report-fix-normalizzazione.md` con la tabella gate e i delta effettivi (formato in §3).

---

## 1. Fatti verificati che guidano il piano

Misurati sul corpus `tests/fixtures/corpus_marchesi_full` (154 ricette, 10.892 righe ingrediente) con il codice attuale:

- Coverage reale **44,3%** (4.820 righe). Il gate report dichiara 96,8% ma su 93 termini del pilota.
- `canonical._strip_item_connectors` toglie `di`/`e` dall'item ma le chiavi glossario li conservano → `olio extravergine di oliva` (823 righe), `sale e pepe` (570) non matchano; `d'aglio` (292) non gestito. Simulazione fix: **60,5%**.
- `units.yaml` ha `rametti`/`fette`/`fili` solo al plurale; mancano `cucchiaino, filetti, fettine, presa, noce, bicchiere, costa, grani, bacche, chiodi, fogli, ciuffo`. Tabelle plurali duplicate in `verify.DEFAULT_KNOWN_UNITS`, `pack._UNIT_PLURALS`, `canonical._ITALIAN_PLURALS/_ENGLISH_PLURALS`. Simulazione: **62,0%**.
- **2.160 righe** (19,8%) con `1 pizzico` iniettato a monte per soddisfare `verify._INGREDIENT_RE = ^(\d+(?:\.\d+)?)\s+(.*)$`. Simulazione bonifica: **64,2%**.
- Decomposizione testa+modificatori: **68,9%**. Residuo 3.382 righe / 1.008 termini; top-50 = 44% del residuo → **~83%** con Fase 0.
- `tests/domain/fake_llm.py` traduce con `normalize_terms(pack.it_to_en_terms())`: i test dello stadio 2 sono circolari.
- `extract._glossary_index` collega `NORMALIZED_TO` solo su `labels_en` esatto.

---

## 2. Work packages

### WP-F0 — Strumento di misura

**Obiettivo:** una sola funzione di misura, riusata da script e da `evaluator.py`.

**Nuovo** `app/domain/coverage.py`
```python
@dataclass(frozen=True)
class UnresolvedTerm:
    term: str            # chiave normalizzata
    count: int
    examples: list[str]  # max 3 item raw
    candidates: list[tuple[str, float]]  # top-3 (chiave glossario, score trigram)

@dataclass
class CoverageReport:
    pack_id: str
    corpus_dir: str
    lines_total: int
    lines_resolved: int
    coverage: float
    by_rule: dict[str, int]          # rule_id -> righe (da F4; prima solo GLOSS-EXACT)
    unresolved: list[UnresolvedTerm]  # ordinati per count desc
    def to_json(self) -> dict

def measure_coverage(pack: DomainPackBundle, corpus_dir: Path,
                     *, stage: Literal["source", "translated"] = "source",
                     top_candidates: int = 3) -> CoverageReport
```
- `stage="source"`: parse con `parse_source_md`, lookup dell'item contro il term map (alias IT inclusi). `stage="translated"`: `parse_translated_md` (usato in F6).
- Il lookup usa **la stessa funzione** che userà `canonicalize` (in F0 è ancora `_strip_item_connectors` + `_build_term_map`; da F1 diventa `normalize.resolve`). Importarla, non copiarla.
- Score candidati: trigram Jaccard su `normalize_key` (implementazione locale 20 righe, no dipendenze nuove).

**Nuovo** `scripts/measure_coverage.py`
```
python scripts/measure_coverage.py --corpus DIR --pack DIR [--stage source|translated] [--out FILE] [--top 50]
```
Stampa tabella riassuntiva + top-N irrisolti con candidati; scrive JSON.

**Modifica** `app/agents/evaluator.py`: sostituire il calcolo `manual_coverage`/`draft_coverage` (basato sui termini del brief) con `measure_coverage(...).coverage` sul corpus passato. Mantenere le vecchie chiavi nel JSON con suffisso `_terms` per confronto storico. Aggiornare `tests/agents/test_ic_evaluator.py` di conseguenza.

**Test** `tests/domain/test_coverage_tool.py`
- `test_coverage_baseline_corpus_marchesi`: `coverage` in `[0.438, 0.448]` sul corpus attuale (vincola il baseline; **da aggiornare a ogni WP** con il nuovo valore atteso ±0,5).
- `test_coverage_unresolved_sorted_with_candidates`: primo irrisolto = `olio extravergine oliva`, count 823, candidati non vuoti.

**Gate GF0:** baseline riprodotto; `docs/coverage/00-baseline.json` committato.
Commit: `F0: coverage measurement tool + evaluator uses corpus coverage · coverage: 44.3% -> 44.3%`

---

### WP-F1 — Normalizzazione simmetrica

**Nuovo** `app/domain/normalize.py`
```python
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "`": "'"})
_ELISION_RE = re.compile(r"\b(?:d|dell|all|nell|sull|un|l)'\s*", re.IGNORECASE)
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:di|del|della|dello|dei|degli|delle|e|ed)\s+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

def normalize_key(text: str) -> str:
    """Chiave di lookup deterministica, applicata IDENTICAMENTE a item e glossario.
    1. apostrofi -> ASCII; 2. casefold; 3. elisioni rimosse (d'aglio -> aglio);
    4. connettore iniziale rimosso; 5. spazi collassati. I connettori INTERNI
    restano ('olio extravergine di oliva' invariato)."""
```
Regola: `normalize_key` è **pura, senza pack**; nessuna altra funzione in `app/domain` può fare casefold/strip su termini per il lookup.

**Modifica** `app/domain/canonical.py`
- `_build_term_map`: chiave = `normalize_key(term)`; conserva il longest-first e `setdefault`.
- In `canonicalize`: `lookup = normalize_key(ingredient.item)`; `resolved = term_map.get(lookup)`. Rimuovere `_strip_item_connectors`.
- `unresolved` continua a raccogliere `lookup` (chiave normalizzata) — coerente con il report F0.
- Il canon-log resta invariato (before = item originale, after = `labels_en`).

**Modifica** `app/domain/verify.py::normalize_terms` (usato da L2 e dal fake LLM): applicare `normalize_key` ai termini del term map e al testo prima della sostituzione, così L2 e stadio 2 usano la stessa chiave. Verificare che `test_verify_l2.py` resti verde.

**Test** `tests/domain/test_normalize.py`
| caso | input | atteso |
|---|---|---|
| elisione | `spicchio d'aglio` | `spicchio aglio` |
| apostrofo unicode | `sott’olio` | `sott'olio` → dopo elisione? **No**: `sott'` non è in lista elisioni, resta `sott'olio` |
| connettore iniziale | `di extra virgin olive oil` | `extra virgin olive oil` |
| connettore interno | `olio extravergine di oliva` | invariato |
| congiunzione | `salt e black pepper` | invariato (non risolvibile: è un composto, va a proposte) |
| idempotenza | `normalize_key(normalize_key(x)) == normalize_key(x)` su tutto il glossario | vero |

`tests/domain/test_canonical.py` — aggiungere:
- `test_f1_glossary_symmetry(pack)`: per ogni entry e ogni termine in `(labels_en, labels_it, *aliases)`, `normalize_key(term) in _build_term_map(pack)`.
- `test_f1_regression_d2(pack)`: `canonicalize` su translated con item `di extra virgin olive oil`, `extra virgin olive oil`, `garlic` risolve tutti a `labels_en`.

**Gate GF1:** coverage ≥ 60% (atteso 60,5); `pytest tests/domain -q` verde; T9 154/154.
Commit: `F1: symmetric normalize_key for glossary lookup (fix D2) · coverage: 44.3% -> 60.x%`

---

### WP-F2 — Unità: sorgente unica

**Modifica** `app/domain/pack.py`
```python
class UnitRule(BaseModel):
    rule_id: str
    from_unit: str            # forma canonica IT (singolare)
    to_unit: str              # forma canonica EN (singolare)
    factor: float
    rounding: int | None = None
    note: str | None = None
    from_forms: list[str] = []  # varianti IT accettate (plurali, abbreviazioni)
    to_forms: list[str] = []    # varianti EN accettate
    countable: bool = False     # unità naturale da preservare (2 uova restano 2 uova)
```
- `DomainPackBundle.known_units()` → `{from_unit, to_unit, *from_forms, *to_forms}` per ogni regola. **Eliminare** `_UNIT_PLURALS`.
- Nuovo `DomainPackBundle.unit_rule_for(token) -> UnitRule | None` con indice dict costruito una volta (`@cached_property`).
- Validator: nessun token può comparire in due regole (errore `DomainPackValidationError` esplicito).

**Modifica** `app/domain/canonical.py`: eliminare `_ITALIAN_PLURALS`, `_ENGLISH_PLURALS`, `_known_units`, `_unit_rule_for_token`; usare `pack.known_units()` e `pack.unit_rule_for()`.

**Modifica** `app/domain/verify.py`: eliminare `DEFAULT_KNOWN_UNITS`; `known_units` diventa parametro **obbligatorio** in `parse_source_md`, `parse_translated_md`, `parse_translated_body` (nessun default silenzioso). Aggiornare tutti i call site (elenco: `canonical.py`, `translate.py`, `extract.py`, `doses.py`, `agents/curator.py`, `scripts/generate_golden.py`, `tests/rag/*`, `tests/domain/*`). `grep -rn "parse_source_md\|parse_translated_md\|parse_translated_body" app scripts tests` deve mostrare solo chiamate con `known_units=`.

**Modifica** `domain-packs/ricette/units.yaml` — riscrivere le regole culinarie con `from_forms`/`to_forms`/`countable` e aggiungere:

| rule_id | from_unit | from_forms | to_unit | to_forms | countable |
|---|---|---|---|---|---|
| UNIT-TBSP | cucchiaio | cucchiai | tablespoon | tablespoons, tbsp | |
| UNIT-TSP | cucchiaino | cucchiaini | teaspoon | teaspoons, tsp | |
| UNIT-CUP | tazza | tazze | cup | cups | |
| UNIT-DEMITASSE | tazzina | tazzine | demitasse | | |
| UNIT-GLASS | bicchiere | bicchieri | glass | glasses | |
| UNIT-PINCH | pizzico | pizzichi, presa, prese | pinch | pinches | |
| UNIT-CLOVE | spicchio | spicchi | clove | cloves | ✓ |
| UNIT-LEAF | foglia | foglie | leaf | leaves | ✓ |
| UNIT-SPRIG | rametto | rametti, ciuffo, ciuffi | sprig | sprigs | ✓ |
| UNIT-SACHET | bustina | bustine | sachet | sachets | ✓ |
| UNIT-BUNCH | mazzetto | mazzetti, mazzo, mazzi | bunch | bunches | ✓ |
| UNIT-SLICE | fetta | fette, fettina, fettine | slice | slices | ✓ |
| UNIT-FILLET | filetto | filetti | fillet | fillets | ✓ |
| UNIT-THREAD | filo | fili | thread | threads | ✓ |
| UNIT-KNOB | noce | noci | knob | knobs | ✓ |
| UNIT-STALK | costa | coste, costola, gambo, gambi | stalk | stalks | ✓ |
| UNIT-GRAIN | grano | grani | grain | grains | ✓ |
| UNIT-BERRY | bacca | bacche | berry | berries | ✓ |
| UNIT-CLOVE-SPICE | chiodo | chiodi | clove (spice) | | ✓ — attenzione: `chiodi di garofano` = item, non unità: gestire come alias glossario `ING-CLOVE-SPICE` e **non** creare la regola se crea ambiguità con UNIT-CLOVE; decidere in base al corpus (14 occorrenze) |
| UNIT-SHEET | foglio | fogli | sheet | sheets | ✓ |
| UNIT-HANDFUL | manciata | manciate | handful | handfuls | |
| UNIT-PIECE | pezzo | pezzi | piece | pieces | ✓ |
| UNIT-DROP | goccia | gocce | drop | drops | |

Le regole SI (`g, kg, ml, l, dl, °C, min, h`) restano com'erano. `noce` è ambigua (unità "noce di burro" vs ingrediente "noci"): la regola UNIT-KNOB si applica solo quando il token è in posizione unità (dopo la quantità) e l'item successivo non è vuoto; `noci` come item resta ingrediente. Documentare nel `note`.

**Test**
- `tests/domain/test_pack.py`: aggiornare `test_ia5_t2_all_16_unit_rules` (il numero cambia), `test_ia5_t2_italian_plural_units`, `test_ia5_t2_english_plural_units` per leggere le forme dal pack invece che da tabelle hardcoded; nuovo `test_f2_no_duplicate_unit_tokens`.
- `tests/domain/test_parser.py`: `test_f2_parse_requires_known_units` (chiamata senza `known_units` → `TypeError`).
- Nuovo `tests/domain/test_units_corpus.py`: per il corpus, raccogli il primo token di ogni item con `unit is None`; asserisci che nessun token con ≥ 5 occorrenze appartenga alla lista `SUSPECT_UNIT_TOKENS` (le forme della tabella sopra) — cioè che il parser li abbia consumati come unità.
- `grep -rn "_ITALIAN_PLURALS\|_ENGLISH_PLURALS\|_UNIT_PLURALS\|DEFAULT_KNOWN_UNITS" app tests scripts` → 0 risultati (test `test_f2_single_source_of_units` con `subprocess` + grep, oppure controllo `hasattr`).

**Gate GF2:** coverage ≥ 62%; test verdi; T9 154/154.
Commit: `F2: units.yaml single source with forms/countable; remove hardcoded plural tables (fix D3) · coverage: 60.x% -> 62.x%`

---

### WP-F3 — Parser quantità e bonifica corpus

**Modifica** `app/domain/verify.py`
```python
@dataclass(frozen=True)
class IngredientLine:
    raw: str
    qty: str | None          # None = quantità assente / q.b.
    unit: str | None
    item: str
    qty_max: str | None = None   # range "2-3" -> qty="2", qty_max="3"
    to_taste: bool = False       # "q.b.", "a piacere", assente
```
Grammatica in `_parse_ingredient` (sostituisce `_INGREDIENT_RE`), in quest'ordine:
1. `q.b.` / `qb` / `a piacere` / `to taste` all'inizio o alla fine della riga → `to_taste=True, qty=None`.
2. Quantità: `\d+(?:[.,]\d+)?` | frazioni unicode `[½⅓¼⅔¾⅛]` | `\d+/\d+` | misto `\d+\s*[½¼¾]` | range `Q\s*[-–]\s*Q`. Normalizzare a Decimal-string: `½`→`0.5`, `1 ½`→`1.5`, `1/2`→`0.5`, virgola→punto.
3. Riga senza quantità (`- sale`) → `qty=None, to_taste=True` (solo in `parse_source_md`; in `parse_translated_md` accettare `- to taste salt`).
4. Prefissi descrittivi che precedono l'item **dopo** la quantità: `il succo di`, `la scorza grattugiata di`, `la scorza di`, `le foglie di`, `il ripieno di` → non fanno parte dell'item; vengono spostati in un campo `prep` (aggiunto in F4; in F3 restano nell'item ma senza articolo: `succo 1 limone` → qty `1`, item `limone`, prep `succo` è **F4**; in F3 limitarsi a rimuovere l'articolo iniziale `il/la/lo/le/l'/un/una` dall'item).
5. Unità: primo token dell'item in `known_units` (invariato).

Rendering (`canonical._render_canonical_md`, `translate.render_translated_document`, `recompose`): `- {qty} {unit} {item}` con `qty` omesso se `None` e prefisso `to taste` / `q.b.` se `to_taste`; range reso `{qty}-{qty_max}`. Il template `domain-packs/ricette/template.md` va aggiornato con le tre forme ammesse.

**Modifica** `app/domain/numbers.py`: `NUMBER_RE` deve catturare frazioni unicode e `\d+/\d+`; `extract_numbers` le normalizza a stringa decimale (`½`→`0.5`) così il multiset P2 confronta IT e EN su valori, non su glifi. `mask_numbers`/`reinject_numbers` devono reinserire il glifo originale (conservare `(raw, normalized)`).

**Bonifica corpus** — nuovo `scripts/build_corpus_fixtures.py`
- Cercare il generatore originale delle 154 ricette (non è nel repo: `tests/fixtures/book_recipes/marchesi_raw.json` ha solo 4 ricette). Verificare in `~/km_engine`, in `DanieleGVA/rcps` e nella history git (`git log --all --diff-filter=A -- tests/fixtures/corpus_marchesi_full`). **Se il raw completo esiste:** rigenerare da lì con il nuovo parser. **Se non esiste: fermarsi e chiedere.** In assenza di risposta, fallback deterministico e reversibile: trasformazione delle righe `^- 1 pizzico (.+)$` secondo la tabella seguente, con diff committato separatamente per revisione.

| pattern item dopo `1 pizzico` | trasformazione |
|---|---|
| `sale`, `pepe`, `sale e pepe`, `noce moscata`, `moscata`, `zucchero`, `cannella…`, `origano`, `peperoncino` (spezie a pizzico) | resta `1 pizzico X` **solo se** nel testo del procedimento compare "pizzico/presa"; altrimenti `- q.b. X` |
| inizia con `½ ¼ ¾` o numero | `- <frazione> <resto>` (es. `- ½ cipolla`) |
| inizia con `il succo di N limone`, `la scorza grattugiata di N limone` | `- N limone` + annotazione (F4: `prep`) — in F3: `- N limone (succo)` **no**: mantenere `- q.b. succo di limone` per non perdere info; decisione finale in F4 |
| `olio per friggere`, `olio`, `brodo`, `farina`, `pangrattato`, `latte`, `burro` (ingredienti senza dose) | `- q.b. X` |
| altro | `- q.b. X` |

- Test `tests/domain/test_corpus_hygiene.py`: `test_no_injected_pinch` (nessuna riga `- 1 pizzico` il cui item inizi per frazione, articolo, `olio`, `brodo`, `farina`, `latte`); `test_corpus_parses_154`; `test_p2_fraction_roundtrip` (`½` estratto come `0.5`, reinserito come `½`).
- `tests/domain/test_parser.py`: `test_f3_quantity_grammar` parametrizzato su 30 casi (interi, decimali, virgola, `½`, `1 ½`, `1/2`, `2-3`, `2–3`, `q.b.`, riga senza quantità, `to taste salt`, unità dopo frazione `½ cucchiaino di senape`).

**Gate GF3:** coverage ≥ 64%; righe `1 pizzico` ≤ 50 e tutte con item in una allow-list di spezie; P2 verde 154/154; T9 154/154; `pytest tests/domain tests/rag -q` verde (i test RAG rileggono il corpus).
Commit (due): `F3a: quantity grammar (fractions, ranges, q.b.) + P2 on fractions` · `F3b: rebuild corpus_marchesi_full without injected pinch (fix D4) · coverage: 62.x% -> 64.x%`

---

### WP-F4 — Risoluzione a livelli e stati preservati

**⛔ Prima di iniziare: chiedere all'utente** quale delle due opzioni per gli stati nel markdown canonico:
- **(A) inline nel md** — `- 100 g capers [salted]`; il canonical.md resta autosufficiente per lo chef; T9 e template cambiano. *(Raccomandata.)*
- **(B) solo nel grafo** — item = testa canonica, stati come Facts sull'Entity; il md perde l'informazione.
Il piano sotto assume (A).

**Modifica** `app/domain/verify.py::IngredientLine`: aggiungere `prep: str | None = None` (succo, scorza, …) e `state: tuple[str, ...] = ()` (stati canonici EN, es. `("salted",)`). Parser: riconoscere il suffisso `[a, b]` nella riga canonica/tradotta; per il source IT gli stati sono ancora dentro l'item (li estrae il canonicalizer).

**Nuovo** `app/domain/normalize.py` (estensione)
```python
@dataclass(frozen=True)
class Resolution:
    label_en: str | None
    glossary_id: str | None
    rule_id: str            # GLOSS-EXACT | GLOSS-ALIAS | GLOSS-HEAD | GLOSS-FUZZY | GLOSS-UNRESOLVED
    states: tuple[str, ...]  # labels_en degli stati staccati
    prep: str | None
    candidates: tuple[tuple[str, float], ...]  # per UNRESOLVED e FUZZY
    needs_review: bool

class Resolver:
    def __init__(self, pack: DomainPackBundle, *, fuzzy_threshold: float = 0.92): ...
    def resolve(self, item: str) -> Resolution
```
Livelli, in ordine, primo che risolve vince:
- **L0 EXACT**: `normalize_key(item)` in mappa `labels_en`/`labels_it`.
- **L1 ALIAS**: in mappa `aliases`. (Distinguere L0/L1 solo per il `rule_id`: costruire due mappe.)
- **L2 HEAD**: rimuovere dal testo i modificatori riconosciuti — tutte le forme del glossario `stati` (`labels_it`, `aliases`) più la lista `regole/normalizzazione.yaml: modifiers` (nuova chiave; seed: `sotto sale, sott'olio, denocciolate/i, chiarificato, tritato/a/i/e, grattugiato/a, fresco/a/i/he, secco/a/hi/he, in polvere, a fette, a dadini, a pezzi, a cubetti, lessato/a/i/e, sbucciato/a/i/e, affettato/a/i/e, pelati, sgusciati/e, maturi/e, ramati, crudo, cotto, intero/i, per friggere, per la teglia, per spolverare, per servire, piccolo/a/i/e, grande/i, grosso/a/i/e, medio/a/i/e`); ogni modificatore rimosso che sia una voce di `stati` diventa uno `state` (label EN); i modificatori generici (`piccola`, `grosse`) vengono scartati e loggati. Prefissi `succo di`, `scorza di`, `scorza grattugiata di`, `foglie di` → `prep`. Il residuo va in L0/L1; se ancora nulla, tentare il **prefisso più lungo** di 3→2→1 token che sia in mappa, **solo se** i token scartati sono tutti in `modifiers` o `stati` (evita `funghi porcini` → `funghi` quando `porcini` è discriminante: `porcini` non è un modificatore, quindi niente match: giusto).
- **L3 FUZZY**: trigram Jaccard su `normalize_key`; risolve solo se `best ≥ threshold` **e** `best - second ≥ 0.05`. `needs_review=True`.
- **L4 UNRESOLVED**: nessuna riscrittura; `candidates` top-3.

**Modifica** `app/domain/canonical.py`
- Sostituire il lookup con `Resolver.resolve`; `item = label_en` se risolto; `state`/`prep` sull'`IngredientLine` canonica.
- Rendering: `- {qty} {unit} {item}` + ` [{state1}, {state2}]` se stati, + ` ({prep})` se prep. Aggiornare `_render_canonical_md`, `recompose`, `translate.render_translated_document` (stadio 1 non ha stati: rende senza).
- Canon-log: entry per `ingredients[i].item` con `rule_id` della Resolution; entry aggiuntiva `ingredients[i].state` (`before=""`, `after="salted"`) con `rule_id` = id della voce `stati`; `verify_canon_log` deve saper applicare `.state` e `.prep`. **T9 deve restare bidirezionale.**
- `unresolved` → `create_glossary_proposal(conn, term, context=document_id)`: estendere la tabella `glossary_proposals` con colonna `candidates jsonb` (migrazione `db/postgres/004_proposal_candidates.sql`, additiva).

**Modifica** `app/domain/extract.py`
- `NORMALIZED_TO` sul `term_id` risolto dal canonicalizer. Dato che `extract` riparte dal canonical.md, l'item è già `labels_en`: `_glossary_index` resta valido per EXACT, ma deve usare `normalize_key`. Per FUZZY il md contiene `labels_en` quindi il link è deterministico.
- Stati → Facts `state=<label>` sull'Entity ingrediente, con `NORMALIZED_TO` verso il CanonicalTerm dello stato (`stati:<id>`).
- `prep` → Fact `prep=<value>`.

**Modifica** `domain-packs/ricette/regole/normalizzazione.yaml`: aggiungere `fuzzy_threshold: 0.92`, `fuzzy_margin: 0.05`, `modifiers: [...]`, `prep_prefixes: [...]`. `pack.py` li valida (pydantic).

**Test** `tests/domain/test_resolver.py`
- un caso per livello con `rule_id` atteso: `garlic`→EXACT; `olio evo`→ALIAS; `capperi sotto sale`→HEAD (`capers`, state `salted`); `olive nere denocciolate`→HEAD (`black olives`… richiede voce; se assente → UNRESOLVED con state staccato: **verificare che gli stati siano comunque conservati anche in UNRESOLVED**); `parmigiano regiano` (typo)→FUZZY; `brodo di carne`→UNRESOLVED con candidati `[vegetable broth, ...]`.
- `test_f4_no_invention`: UNRESOLVED non cambia `item` nel md (T10).
- `test_f4_head_does_not_overgeneralize`: `funghi porcini` **non** risolve a `funghi`; `riso originario` non risolve a `riso`.
- `test_f4_fuzzy_margin`: due candidati vicini → UNRESOLVED.
- `tests/domain/test_canonical.py`: `test_f4_states_roundtrip` (`recompose(extract(canonical)) == canonical` con stati); `test_f4_canon_log_state_entries`.
- `tests/domain/test_a6_roundtrip.py`: 154/154 con il nuovo rendering.
- Campione manuale: `scripts/measure_coverage.py --stage source --dump-fuzzy /tmp/fuzzy.md` produce le risoluzioni L3; l'agente le allega al PR (max 100) per revisione umana.

**Gate GF4:** coverage ≥ 68%; `by_rule` riportato; L3 ≤ 3% delle righe e tutte allegate al PR; T9 154/154; Neo4j: `MATCH (e:Entity {type:'ingredient'}) OPTIONAL MATCH (e)-[:NORMALIZED_TO]->(t) RETURN count(t)*1.0/count(e)` ≥ 0,68 dopo ingest del corpus.
Commit: `F4: tiered resolver (exact/alias/head/fuzzy/unresolved) with states preserved (fix D5, D8) · coverage: 64.x% -> 68.x%`

---

### WP-F5 — Fase 0 sul residuo (glossario, gate umano)

**Input:** `docs/coverage/04-after-F4.json → unresolved` (≈1.000 termini, count desc).

**Nuovo** `scripts/propose_glossary_entries.py`
```
python scripts/propose_glossary_entries.py --from docs/coverage/04-after-F4.json --batch 50 --offset 0 --out domain-packs/ricette-agents-draft/glossari/ingredienti.proposals.yaml
```
- Per ogni termine chiama l'LLM (client `app/domain/llm.py`) con prompt: contesto (3 righe raw di esempio), glossario esistente (solo `id, labels_en`) per evitare duplicati, richiesta di output YAML `{id, labels_en, labels_it, aliases[], definition, ontology_uri|null, broader_than|null, duplicate_of|null}`. Temperatura 0. Se `duplicate_of` è valorizzato → diventa **alias** della voce esistente, non nuova voce.
- Convenzioni id: `ING-<EN-UPPER-KEBAB>`; gerarchia via `broader_than: ING-BROTH` (nuova chiave opzionale in `GlossaryEntry`, validata da `pack.py`; usata dal RAG in seguito, per ora solo dato).
- Output in **draft**, mai in `domain-packs/ricette`.

**Gate umano:** l'agente presenta il lotto (tabella termine → proposta → count) e attende approvazione; solo dopo esegue `scripts/merge_glossary_batch.py --approved <file>` che fonde nel pack e ri-esegue la misura.

**Test:** `test_f5_glossary_no_duplicate_keys` (nessuna `normalize_key` duplicata tra voci diverse); `test_f5_broader_than_resolves` (ogni `broader_than` punta a un id esistente).

**Gate GF5:** coverage ≥ 82% dopo il lotto 1 (top-50); ≥ 90% dopo tre lotti; ogni voce con `ontology_uri` o `null` motivato in `definition`.
Commit per lotto: `F5-batchN: glossary batch N approved (+K entries, +J aliases) · coverage: … -> …`

---

### WP-F6 — Test non circolari e gate reale

1. **Golden tradotto reale.** `scripts/build_translated_golden.py`: per le 154 ricette chiama `translate_document` con `HttpLLMClient` reale, salva in `tests/fixtures/corpus_marchesi_translated/<id>.md` + `manifest.json` (`model`, `prompt_sha256`, `date`). Eseguito **una volta** dall'utente (costo/credenziali); committato.
2. **Prompt di traduzione vincolato** (`app/domain/llm.py::HttpLLMClient.translate`): iniettare la lista `labels_en` del glossario ingredienti/stati con l'istruzione "usa esattamente questi termini per gli ingredienti quando applicabile; non tradurre i placeholder `{Nk}`". Aggiornare `manifest.json` al cambio di prompt.
3. **Fake LLM** (`tests/domain/fake_llm.py`): `build_fake_llm` legge dal golden se presente (`masked_input → translated_md` per id), altrimenti fallback all'attuale sostituzione **con warning** `pytest.warns`. I test di gate (`test_a6_roundtrip`, `test_b3_coverage`) usano solo il golden.
4. **Nuovo test** `tests/domain/test_canonicalize_real_translation.py`: `measure_coverage(stage="translated")` sul golden ≥ `measure_coverage(stage="source")` − 0,03. Se fallisce, il report elenca i termini EN non mappati (`EVOO`, `canned peeled tomatoes`…) da aggiungere come **alias EN** in F5 (unico caso in cui alias sono ammessi per coverage).
5. **Evaluator/gate:** `app/agents/evaluator.py` → `gate_coverage = coverage_corpus >= 0.85`; rimuovere il gate sui 93 termini; rigenerare `docs/domain-briefs/ricette-gate-report.json`.
6. **RAG E2E:** `tests/rag/test_ingredient_queries.py`: 20 query (`ricette con capperi sotto sale`, `recipes with saffron`, …) da `tests/fixtures/rag_golden_ingredients.json`; Recall@5 ≥ 0,9 con `rag_query` sul corpus intero ingestato.

**Gate GF6:** tutti i test verdi con golden reale; Recall@5 ≥ 0,9; gate report con coverage corpus.
Commit: `F6: real-LLM translated golden, constrained translation prompt, corpus-based gates (fix D7)`

---

## 3. Sequenza e checkpoint

| Ordine | WP | Stima | Checkpoint con l'utente |
|---|---|---|---|
| 1 | F0 | ½ g | — |
| 2 | F1 | 1 g | — |
| 3 | F2 | 1 g | — |
| 4 | F3 | 1–2 g | **se il raw completo non è reperibile** |
| 5 | F4 | 2–3 g | **opzione A/B stati nel md, prima di iniziare**; revisione L3 a fine WP |
| 6 | F5 | 2 g + gate | **ogni lotto** |
| 7 | F6 | 1–2 g | esecuzione del golden reale (credenziali LLM) |

F5 può produrre le proposte in parallelo a F4, ma il merge nel pack avviene solo dopo GF4.

**Report finale** `docs/report-fix-normalizzazione.md`:
```
| Gate | Atteso | Misurato | Test | Note |
| GF0 | 44,3 | … | … | |
| GF1 | ≥60 | … | … | |
…
Residuo irrisolto finale: N righe / M termini (top-20 allegati)
Risoluzioni FUZZY: K (elenco allegato, revisionato: sì/no)
Decisioni prese: template stati (A/B), chiodi di garofano, noce di burro, …
```

## 4. Vietato
- Aggiungere alias/voci al glossario in F1–F4 (eccezione unica: alias EN emersi dal golden reale, in F6→F5).
- Abbassare `fuzzy_threshold` sotto 0,92 o togliere il margine.
- Mantenere `DEFAULT_KNOWN_UNITS` o altre tabelle unità fuori da `units.yaml`.
- Rigenerare il corpus con trasformazioni non riproducibili (ogni bonifica passa da uno script versionato).
- Avviare la Fase 1 (giudice LLM) prima di GF4.
