# PROGRAMMA UNICO — Dal corpus al canone validato su km_engine

**Versione:** 1.3 — 2026-08-31 · **due passaggi di verifica contro il codebase
`DanieleGVA/km_engine@main`**: il primo su innesti, schemi, call-site e
migrazioni; il secondo (avversariale) su traduzione, template e ingestione —
da cui il passo 0 (convertitore MSC, bypass stage-1) e i vincoli frontmatter
**Questo documento sostituisce e assorbe:** `spec-iterazione-F-canon-adjudication.md`,
`logica-standardizzazione-ingredienti-dosi.md`, `mappa-implementazione-WP-F0.md`.
Da qui in poi si usa **solo questo file**. È organizzato così:

- **§1** — il flusso logico in una pagina (leggi solo questo per decidere);
- **§2** — Fase 0: standardizzazione (il prerequisito);
- **§3** — Fase 1: giudice LLM (l'adiudicazione);
- **§4** — la sequenza di implementazione unica, numerata, con i gate:
  **è la risposta a "cosa uso prima e cosa dopo"**;
- **§4-bis** — per ogni passo, l'obiettivo (l'invariante) e le verifiche con
  cui l'agente controlla che la modifica sia davvero corretta, non solo
  formalmente conforme;
- **§5** — a cosa servono i materiali già prodotti (report Pareto, script);
- **§6** — cosa consegnare a Claude Code e in che ordine.

---

## 1. IL FLUSSO LOGICO IN UNA PAGINA

**Obiettivo finale:** ogni ricetta MSC viene giudicata contro il canone
(libri + house rules) da un giudice LLM di livello corporate chef, con
verdetto citato, gate umano e log completo.

**Perché due fasi, in quest'ordine:** il giudice confronta la ricetta con i
candidati di canone. Il confronto funziona solo se i due parlano la stessa
lingua di dati — stessi nomi ingrediente, stesse classi, dosi comparabili per
porzione. Oggi non è così ("TOMATOES ROMA FRESH" vs "pomodori ramati";
"0 KG" che significa "a piacere"; rese sbagliate in entrambe le direzioni).
**Quindi: prima si standardizza (Fase 0), poi si giudica (Fase 1).**
Un giudice su dati non standard produce verdetti non affidabili: la Fase 0
non è preparazione, è il pavimento.

**La pipeline a regime** (● = deterministico, ◆ = LLM, ■ = umano):

```
card MSC / ricetta libro
  ● parsing → markdown canonico (con item code e sfrido nell'IR)
  ● risoluzione ingredienti per DIZIONARIO (lookup, mai inventata)   ← Fase 0
  ● dosi: tipizzazione + doppia rappresentazione + scala a 10        ← Fase 0
  ● screen intra-documento (6 famiglie di contraddizioni)
  ● retrieval candidati di canone (RAG esistente, per componente)
  ◆ decomposizione in componenti con ruoli                            ← Fase 1
  ◆ giudice di canone per componente (closed-book, cita o si astiene) ← Fase 1
  ◆ giudice di ricomposizione a livello piatto                        ← Fase 1
  ◆ critico avversariale sul verdetto                                 ← Fase 1
  ● verifica citazioni + validazione numeri + routing per accordo k=3
  ■ executive chef: approva / corregge (unica autorità)
  ● log (canon-log, dose-log, canon_adjudication_log) + grafo
```

**Regola di divisione del lavoro** (vale ovunque): deterministico dove il
problema è chiuso e verificabile; LLM solo dove serve giudizio; umano dove
serve autorità. Ogni chiamata LLM è incorniciata da deterministico prima
(input normalizzati) e dopo (citazioni verificate, numeri ri-validati).

---

## 2. FASE 0 — STANDARDIZZAZIONE (prerequisito dei giudici)

**Principio:** l'LLM standardizza il **dizionario** una volta (~3.600 voci:
2.029 item code MSC + 1.476 termini libro), con gate umano; l'applicazione
alle ~30.000 righe è **lookup deterministico per sempre**. Mai
normalizzazione inventata a runtime: stesso input ⇒ stesso output, con log.

### 2.0 Ingestione MSC: convertitore CalcMenu → markdown — ● deterministico
**Passo mancante emerso dalla verifica sul codice** (secondo passaggio):
1. Lo stage-1 di traduzione è mono-corpus: `parse_source_md` ha le sezioni
   italiane cablate e `translate_document` chiama sempre l'LLM. Le card MSC,
   EN-native, **entrano a valle della traduzione**, direttamente nella forma
   `translated.md` (`## Ingredients`/`## Method`, `lang=en`,
   `source_lang=en`) — bypass dello stage-1, stesso principio del dominio
   `code` (già EN) dell'iterazione D. Conseguenza benefica: il suffisso
   metadati **non attraversa mai l'LLM** (i libri non hanno codici), quindi
   nessuna collisione con i placeholder `{Nk}`.
2. Il convertitore (`scripts/msc_to_md.py`, porting del parser Pareto già
   riconciliato 1.653/19.500) normalizza ciò che i regex del template
   rifiutano o corrompono: **numeri senza separatore di migliaia**
   (`_INGREDIENT_RE` leggerebbe "1,500 g" come qty=1 — corruzione
   silenziosa); **procedure rinumerate** in passi `N.` strettamente
   sequenziali (`_STEP_RE` impone la sequenza); **yield → `servings` int**
   ("24 serve", "10 [_]", "100 pax"; mancante o senza unità ⇒ coda errori,
   mai default); **righe-sezione della distinta** ("— CRUMBLE —") che il
   parser rifiuterebbe ⇒ diventano metadato di componente sulle righe
   successive (`{component: crumble}` nel suffisso — utile poi alla
   decomposizione della Fase 1).
3. Frontmatter: `time_min` e `difficulty` sono oggi **obbligatori**
   (int/enum) e le card MSC non li hanno ⇒ decisione richiesta (proposta:
   renderli opzionali via flag di pack quando `source_lang == lang`, per non
   inquinare i dati con placeholder).

### 2.1 Estrazione e dedup — ● deterministico
MSC per item code (chiave = codice, mai la stringa); libri per stringa
normalizzata (NFKC, casefold, apostrofi ASCII). Per ogni voce: frequenza,
forme viste, unità viste, 3 contesti d'uso. Nessuna fusione cross-corpus qui.
→ `scripts/build_term_dictionary.py` (nuovo) → `term_dictionary.jsonl`.

### 2.2 Standardizzazione del dizionario — ◆ LLM (batch)
Batch 40–60 voci, temperatura 0, JSON-mode, few-shot fissi, ordine
deterministico. Schema per voce: `canonical_name_en` (ordine culinario,
singolare), `ingredient_core`, `states[]`, `pack_format`, `class` (enum
chiuso), `aliases[]`, `allergen_tags[]` (EU-FIC 14), `is_food`,
`unit_weight_g` + `countable_unit` + `count_policy` (integer|exact) per i
contabili, `density_g_per_ml` per i liquidi, `confidence`, `ambiguous`.
Divieti nel prompt: mai fondere item d'acquisto distinti; mai inventare la
specie ("CHEESE GRATED" resta `cheese` + ambiguo); identità solo dal contesto
fornito; stato e formato fuori dal nome canonico.
→ `app/domain/standardize.py` + `scripts/standardize_terms.py` (nuovi),
sopra la primitiva `judge()` di §3.0.

### 2.3 Consolidazione — ● deterministico
Validazione schema/enum (un retry, poi coda manuale); collisioni
(core+stato+formato uguali) ⇒ proposta di merge, decide l'umano; coerenza
(stesso core ⇒ stessa classe e stessi allergeni ovunque); **allineamento
cross-corpus**: stesse (core, stato) MSC↔libro ⇒ proposta `SAME_AS` — è il
ponte che permetterà al giudice di confrontare card e libro senza
attraversare le lingue a runtime; sanity range su pesi e densità.

### 2.4 Gate umano — ■ (P5, stratificato)
100%: top-500 per frequenza (=85,5% delle righe MSC) + ambigue + collisioni +
`is_food=false` + SAME_AS. Campione: 200 voci di coda ⇒ tasso d'errore
misurato; >3% ⇒ estensione della revisione. Firma ⇒ artefatti di pack
**versionati**: `glossari/ingredienti.yaml` v2, `msc_mapping.yaml`,
`regole/plausibilita.yaml`, seed allergeni.
→ coda `adjudications` con `kind='dictionary'` (migrazione §3.0) + scheda UI.

### 2.5 Applicazione alle righe — ● deterministico, per sempre
MSC per code, libri per stringa; ogni mappatura ⇒ riga canon-log con
`rule_id = term_id@versione`; non risolta ⇒ `glossary_proposals` (coda
esistente). → estensione di `canonicalize()` (code-first, fallback literal).

### 2.6 Dosi — ● deterministico (rappresentazione doppia)
**L'unità naturale non si riscrive mai:** "2 uova" restano 2 uova, "3 foglie
di alloro" restano 3 foglie. I grammi equivalenti esistono sempre ma vivono
solo nel dose-log e nel grafo (mass balance, costing, plausibilità).
1. Tipizzazione: misurata / contabile / a-piacere (TT o 0 su classi
   condimento) / zero anomalo ⇒ coda / senza unità o mg sospetti ⇒ coda.
2. Contabili: riga invariata; `mass_g = qty × unit_weight_g` solo a log
   (peso per-ingrediente del dizionario > fattore generico > assente ⇒ issue
   dichiarata, mai stima silenziosa). Liquidi via densità, solo a log.
3. Conversione MKS **solo sulle unità di misura vere** (cucchiai, cl, KG,
   LT…); scala della resa sulla quantità naturale (2 uova ×2,5 ⇒ 5 uova;
   arrotondamento da `count_policy`: integer ⇒ mezzo su, min 1).
   **Resa mancante ⇒ errore, mai default** (rimozione del default 4).
4. Gate di plausibilità per classe per porzione sui grammi equivalenti
   (i 220 KG e i 100 mg falliscono per costruzione).
→ rifattorizzazione `doses.py` (MKS_MEASURE vs COUNT_UNITS) e **cablaggio in
un nuovo orchestratore `app/domain/flow.py::process_document()`**
(translate → L1 → L2 → canonicalize → doses → issues), adottato dai
call-site esistenti di `canonicalize` in `app/agents/{analyst,codegen,
evaluator,curator}.py`. Verificato sul repo: `app/ingest/` è il percorso
legacy graphify e **non** tocca il flusso domain; `standardize_doses` non è
chiamata da nessun punto dell'app — l'orchestratore è il pezzo mancante.

### 2.7 Screen intra-documento — ● deterministico
Porting dello screen validato sul Pareto (6 famiglie: citato-non-costato,
titolo vs distinta, mass balance, temperature/USPH, integrità unità,
duplicati) come `verify_intra()`; con le classi del dizionario le regole
diventano esatte. I finding alimentano il giudice della Fase 1.

---

## 3. FASE 1 — GIUDICE LLM (adiudicazione a livello corporate chef)

**Tre fonti esplicite per il giudice, tutte citabili:** candidati di canone
(libri, via RAG esistente), `house-rules.yaml` (porzioni MSC, vincoli
USPH/VSP, regole di classe — versionato e firmato: se non lo scrivi,
il modello inventa la policy aziendale), e la card standardizzata.

**Tre principi nuovi** (si aggiungono a P1–P7):
- **P8 closed-book:** ogni verdetto cita un candidato fornito
  (document_id + posizione) o si astiene con `CANON_GAP` — l'astensione mappa
  i buchi del corpus canone, è un output di valore.
- **P9 numeri validati:** l'LLM emette verdetti JSON riferiti a righe; ogni
  quantità proposta ripassa dal validatore deterministico prima di toccare
  qualunque documento. L'LLM non riscrive mai markdown.
- **P10 adattamento dichiarato ≠ errore:** scostamento giustificato dai tag
  (NSA, VG, classe) è adattamento da registrare, non difetto da correggere.

### 3.0 Primitiva di giudizio — condivisa con la Fase 0
`judge(system, user, schema)` su `LLMClient` (Http JSON-mode temperatura 0 +
Fake su fixture per i test); modello del giudice separato da quello di
traduzione (`KM_JUDGE_*`). Migrazione unica `004`: `adjudications` +
`kind ('translation'|'canon'|'dictionary')`, `verdict_json JSONB`,
`llm_model`, `llm_confidence`, `candidate_ids[]`; tabella
`canon_adjudication_log` speculare a `canon_log`.

### 3.1 Giudice semantico sulle escalation L2 — ◆
L2 (token overlap) resta come trigger economico ma smette di decidere: le
escalation passano dal giudice che chiude i falsi allarmi con motivazione,
diagnostica i veri con suggerimento per riga, marca UNSURE. Solo veri e
UNSURE arrivano all'umano. (Fix contestuale al deterministico: il
denominatore `min()` dell'overlap è cieco alle aggiunte — contenimento
bidirezionale.)

### 3.2 Decomposizione in componenti — ◆
Il piatto si scompone in componenti con ruoli (proteina, salsa, contorno,
bagna, crust…): tutti i difetti fini del Pareto erano a livello componente.
Retrieval dei candidati **per componente** (il canone stesso va decomposto
nel grafo in `CanonComponent`, una tantum).

### 3.3 Giudice di canone per componente — ◆ (il cuore)
Prompt = i 5 passi: dosi già per 10 porzioni (Fase 0) ⇒ confronto diretto;
identifica il referente tra i candidati o `CANON_GAP`; scegli la **forma del
benchmark** (rapporto / grammature assolute / rapporto interno) prima di
confrontare i numeri; classifica errore vs adattamento dichiarato (P10);
rileva le assenze di procedura — sempre con citazione (P8).
Output: `LineVerdict` per riga (`ok|correct|add|delete|flag`, proposta,
motivo, severità, citazione, base) + `procedure_absences` + `overall` +
confidenza.

### 3.4 Ricomposizione a livello piatto — ◆
Ciò che i componenti non vedono: architettura della porzione (massa totale,
equilibrio proteina/amido/verdura), coerenza classe/dieta, fattibilità di
servizio in linea, sicurezza trasversale.

### 3.5 Critico avversariale — ◆
Secondo passaggio con mandato opposto: attacca il verdetto — citazioni che
non sostengono l'affermazione, severità gonfiate, adattamenti bocciati a
torto, conflitti con le house rules. Genera-critica-rivedi batte il
passaggio singolo in modo affidabile.

### 3.6 Routing, gate umano, apprendimento — ● + ■
Confidenza per **accordo**, mai auto-dichiarata: k=3 esecuzioni con ordine
dei candidati permutato; convergenza ⇒ batch-approve dei meccanici;
divergenza ⇒ coda umana; `CANON_GAP` ⇒ coda dedicata. Verifica deterministica
delle citazioni e ri-validazione dei numeri (P9) prima della coda.
L'executive chef decide (P5); ogni decisione alimenta il golden set e la
banca di few-shot **per famiglia di piatto** (il sistema converge sul gusto
della casa senza fine-tuning). Cosa NON fare: debate multi-agente teatrale,
catene lunghe di agenti, fine-tuning ora, confidenza auto-riportata.

---

## 4. SEQUENZA DI IMPLEMENTAZIONE UNICA — cosa prima, cosa dopo

Un solo ordine, dependency-safe. Ogni passo ha il suo gate: non si passa al
successivo senza il gate verde (metodo delle iterazioni A–E).

| # | Fase | Passo | File principali | Gate di uscita |
|---|---|---|---|---|
| 0 | 0 | **Convertitore MSC** CalcMenu → `translated.md` EN-native (bypass stage-1): numeri senza migliaia, step rinumerati, yield→servings con coda errori, sezioni-componente nel suffisso; decisione frontmatter (`time_min`/`difficulty` opzionali via flag di pack) | `scripts/msc_to_md.py`, `verify.py` (flag), `domain-packs/ricette/pack.yaml` | riconciliazione 1.653 card / 19.500 righe / 1.591 procedure; zero righe corrotte da "1,500"; L1 verde sul convertito |
| 1 | 0 | Parser IR con `{code, waste, component}` retro-compatibile — il suffisso è **strutturale**: escluso dai token L2 e (hardening) da `numbers.py`; grazie al bypass del passo 0 **non attraversa mai l'LLM**; emesso simmetricamente da `_render_canonical_md` (canonical.py) e da `recompose.py` | `verify.py`, `numbers.py`, `canonical.py`, `recompose.py`, `extract.py` | round-trip T7-bis verde con e senza metadati; multiset P2 invariato |
| 2 | 0 | Schema pack esteso (classe, allergeni, pesi, countable) + `plausibilita.yaml`; **unità di conteggio come regole identità in `units.yaml`** (from=to, factor 1.0 — pattern UNIT-G esistente: entrano in `known_units()` per il parser e `canonicalize` non le tocca), con forme esatte per case (`KG`, `LT`, `EA`, `TT`, `pz` — il parser è case-sensitive) e plurali come regole separate (`egg`/`eggs`) | `pack.py`, `domain-packs/ricette/` | pack `code` (iter. D) carica invariato; parser riconosce le unità di conteggio |
| 3 | 0 | Dizionario di input dai due corpora | `scripts/build_term_dictionary.py` | conteggi riconciliati (2.029 / 1.476) |
| 4 | 0+1 | Primitiva `judge()` + migrazione 004 | `llm.py`, `db/postgres/004_*.sql` | unit verdi; zero regressioni su translate |
| 5 | 0 | Batch LLM di standardizzazione + consolidazione | `standardize.py`, `scripts/standardize_terms.py` | fake: 100% schema-valid; live su 100 voci: error-rate riportato |
| 6 | 0 | Coda dizionario + UI + publish | `ui.py`, `scripts/enqueue_*`, `scripts/publish_*` | flusso e2e dizionario chiuso; artefatti v2 firmati |
| 7 | 0 | Applicazione code-first in `canonicalize` | `canonical.py` | ≥99% righe MSC risolte per code; canon-log versionato |
| 8 | 0 | Dosi: tipizzazione + doppia rappresentazione + **nuovo orchestratore** `app/domain/flow.py` adottato dai call-site negli agents | `doses.py`, `app/domain/flow.py`, `app/agents/*` | "2 uova"/"3 foglie" invariate nel canonico; 220 KG e 100 mg bloccati; resa mancante ⇒ errore |
| 9 | 0 | `verify_intra` (screen 6 famiglie) | `verify.py` | famiglie F5/F1 in calo sul corpus senza ammorbidire le regole |
| 10 | 0 | Grafo: proprietà CanonicalTerm + `SAME_AS` | `load_domain_pack.py`, cypher 004 | retrieval cross-corpus dimostrato su 5 coppie |
| 11 | 1 | Decomposizione canone in `CanonComponent` (una tantum) | script dedicato | componenti citabili con fonte |
| 12 | 1 | Giudice semantico su escalation L2 | `adjudicate.py` (parte 1) | ≥60% escalation chiuse motivate; corrotti sintetici L1/L2 ancora tutti intercettati |
| 13 | 1 | Giudice canone per componente + ricomposizione + critico | `adjudicate.py`, `regole/adjudication.yaml`, `house-rules.yaml` | golden 20 card: precision ≥0,80 / recall ≥0,70 per riga; 100% citazioni risolvibili; CANON_GAP corretto sui casi seminati |
| 14 | 1 | Routing k=3 + UI verdetti + batch-approve | `adjudicate.py`, `ui.py` | accordo/disaccordo instrada correttamente su casi costruiti |
| 15 | 1 | Campione di controllo (30 non segnalate) | harness | tasso falsi negativi misurato (baseline) |
| 16 | 1 | End-to-end su batch Pareto reale con gate chef | tutto | `canon_adjudication_log` completo e reversibile; report con metriche e costi |

**Fix immediati fuori sequenza** (piccoli, subito): default `servings=4` in
`doses.py` ⇒ errore; contenimento bidirezionale in `_overlap` di L2.

---

## 4-bis. OBIETTIVI E VERIFICHE PER INTERVENTO (per gli agenti)

**Come usare questa sezione.** Ogni passo della tabella §4 ha qui il suo
*obiettivo* — l'invariante che deve risultare vero a fine intervento — e le
*verifiche* con cui l'agente controlla di averlo rispettato. Regola: un
gate superato alla lettera ma in violazione dell'obiettivo **non è
superato**. Le verifiche marcate ✗ sono negative: descrivono ciò che deve
fallire o non accadere — sono quelle che smascherano le implementazioni
sbagliate ma verdi.

**Passo 0 — Convertitore MSC.**
*Obiettivo:* ogni card CalcMenu diventa un `translated.md` valido senza
perdita né invenzione: ciò che non si può convertire con certezza finisce in
coda errori con motivo, mai riempito con un default.
*Verifiche:* riconciliazione esatta 1.653 card / 19.500 righe / 1.591
procedure; "1,500 g" produce qty **1500**, mai 1; procedura non sequenziale
rinumerata preservando il testo parola per parola; riga-sezione "— CRUMBLE —"
diventa `{component: crumble}` sulle righe seguenti e nessuna riga viene
persa. ✗ yield "10 [_]" o assente NON produce un `servings`: produce una
riga in coda errori. ✗ card senza `time_min` NON riceve un valore inventato.

**Passo 1 — Parser IR con metadati.**
*Obiettivo:* code, sfrido e componente viaggiano nell'IR come metadati
strutturali: invisibili a P2 e a L2, simmetrici tra serializer e
ricompositore, e **assenza di suffisso = comportamento identico a oggi**.
*Verifiche:* parse→render idempotente con e senza suffisso; token L2 di una
riga identici con e senza suffisso; `extract_numbers` restituisce lo stesso
multiset con e senza suffisso; round-trip T7-bis verde nei due casi.
✗ un documento libro esistente ri-processato dopo la modifica produce output
**byte-identico** a prima (golden di regressione). ✗ il suffisso non compare
mai nell'`item` parsato.

**Passo 2 — Schema pack e unità di conteggio.**
*Obiettivo:* estendere senza rompere: campi tutti opzionali, dominio `code`
intatto; le unità di conteggio sono riconosciute dal parser ma nessuno
stadio le converte o riscrive.
*Verifiche:* il pack `code` carica invariato e i suoi test restano verdi;
"- 2 egg yolk" parsa `unit=egg`; `canonicalize` su quella riga non produce
alcuna entry di canon-log (no-op reale); "KG", "EA", "pz", "TT" riconosciuti
nella forma esatta del corpus. ✗ nessuna regola identità ha `factor ≠ 1.0`.
✗ validator: `countable_unit` senza `unit_weight_g` o senza `count_policy`
viene rifiutato.

**Passo 3 — Dizionario di input.**
*Obiettivo:* rappresentazione completa e riproducibile del vocabolario: ogni
voce ha frequenza, forme e contesti reali; niente perso, niente duplicato.
*Verifiche:* conteggi 2.029 (MSC) / 1.476 (libri); somma delle frequenze =
righe totali dei corpora; ogni voce ha ≥1 contesto. ✗ due esecuzioni
producono file **hash-identici** (ordine deterministico).

**Passo 4 — Primitiva `judge()`.**
*Obiettivo:* un solo modo per chiedere giudizio strutturato all'LLM: output
sempre schema-valido o errore esplicito; la traduzione non cambia di un bit.
*Verifiche:* output non conforme ⇒ un retry con l'errore in coda, poi
`JudgeOutputError`; `FakeLLMClient.judge` deterministico su fixture; tutti i
test esistenti di `translate` verdi senza modifica. ✗ nessun percorso in cui
un output judge non validato raggiunge chiamanti a valle.

**Passo 5 — Batch di standardizzazione + consolidazione.**
*Obiettivo:* una proposta tracciabile per ogni voce, costruita solo dal
contesto fornito; i divieti (mai fondere item, mai inventare specie) valgono
più della completezza.
*Verifiche:* caso seminato "CHEESE GRATED" ⇒ `ingredient_core=cheese`,
`ambiguous=true`; due item burro con pack diverso ⇒ due voci con stesso core;
collisioni e SAME_AS compaiono come **proposte**, mai applicati; stesso input
+ stesso fake ⇒ stesso output. ✗ nessuna voce con `class` fuori enum
sopravvive alla consolidazione. ✗ una voce con specie inventata nei casi
seminati fa fallire il test, anche se plausibile.

**Passo 6 — Coda dizionario, UI, publish.**
*Obiettivo:* nessuna proposta diventa artefatto di pack senza decisione umana
registrata; la pubblicazione è riproducibile e versionata.
*Verifiche:* `publish` con zero approvazioni produce zero modifiche; ogni
decisione ha riga di audit; publish incrementa la versione e il diff
corrisponde alle sole voci approvate; secondo publish senza nuove decisioni
è un no-op. ✗ una voce `rejected` non compare in nessun artefatto.

**Passo 7 — Risoluzione code-first.**
*Obiettivo:* l'identità (item code) prevale sulla stringa; ogni riscrittura è
spiegata nel canon-log con la versione del dizionario; un miss non inventa
mai.
*Verifiche:* code noto ⇒ item riscritto + entry `MAP-<code>@v`; code ignoto ⇒
fallback literal ⇒ se irrisolto, proposta con il code nel context; ≥99% delle
righe MSC risolte per code. ✗ rimuovendo `msc_mapping.yaml` il comportamento
torna **identico** a prima dell'intervento (retro-compatibilità dimostrata,
non dichiarata).

**Passo 8 — Dosi e orchestratore `flow.py`.**
*Obiettivo:* le unità naturali sono intoccabili ("2 uova" restano 2 uova); i
grammi equivalenti esistono solo nei log; nessuna stima silenziosa; la resa
non si inventa mai; `flow.py` è l'unico punto in cui la sequenza
translate→verify→canonicalize→doses è cablata.
*Verifiche:* canonico con "2 egg" e "3 leaf bay laurel" invariati dopo dosi e
round-trip; dose-log contiene `mass_g` con la priorità giusta (peso da
dizionario > fattore generico marcato > issue dichiarata); scala ×1,25 su
policy `integer` ⇒ arrotondamento su, minimo 1, entrambe le forme a log;
gli agents passano da `flow.py`. ✗ `servings` assente ⇒ `ParseError`, mai 4.
✗ 220 KG e 100 mg producono issue di plausibilità, non documenti puliti.
✗ contabile senza peso ⇒ issue, mai un grammo stimato nel log.

**Passo 9 — `verify_intra`.**
*Obiettivo:* le contraddizioni interne alla card (procedura vs distinta,
titolo vs distinta, bilanci, temperature, unità) emergono con regole esatte
basate sulle classi; le convenzioni legittime non sono difetti.
*Verifiche:* casi seminati per ciascuna delle 6 famiglie intercettati;
finding con riferimento alla riga/sezione. ✗ "0 KG SALT TABLE" NON è
flaggato (a piacere); "0 KG ONION" È flaggato. ✗ ingrediente citato ma
plausibilmente dentro una sotto-ricetta ⇒ severità cap `medium` con nota,
mai `high`.

**Passo 10 — Grafo.**
*Obiettivo:* il dizionario è interrogabile nel grafo; i legami cross-corpus
esistono solo se approvati.
*Verifiche:* proprietà (classe, allergeni, pesi) presenti sui
`CanonicalTerm`; retrieval cross-corpus su 5 coppie note; doppia esecuzione
del load ⇒ stesso conteggio nodi/relazioni (MERGE idempotente). ✗ nessuna
`SAME_AS` senza `approved_by`.

**Passo 11 — Decomposizione del canone in componenti.**
*Obiettivo:* il canone diventa interrogabile per componente, e ogni
componente resta ancorato alla sua fonte.
*Verifiche:* ogni `CanonComponent` cita il documento d'origine; la
ricomposizione dei componenti di una ricetta restituisce esattamente i suoi
ingredienti (nessun orfano, nessuna aggiunta). ✗ nessun componente con
ingredienti che non esistono nel documento d'origine.

**Passo 12 — Giudice semantico su L2.**
*Obiettivo:* la coda umana riceve solo divergenze reali, con un suggerimento
azionabile; la capacità di intercettare le corruzioni non diminuisce mai.
*Verifiche:* ≥60% delle escalation storiche chiuse con motivazione
registrata; sui divergenti veri `suggestion` non è mai vuota. ✗ **tutti** i
documenti corrotti sintetici dei test L1/L2 esistenti ancora intercettati —
qualunque miglioramento che ne perde anche uno è una regressione, non un
progresso.

**Passo 13 — Giudice di canone + ricomposizione + critico.**
*Obiettivo:* ogni verdetto è citato o è un'astensione; gli adattamenti
dichiarati non vengono mai bocciati; nessun numero proposto tocca un
documento senza rivalidazione deterministica.
*Verifiche:* golden 20 card: precision ≥0,80 / recall ≥0,70 per riga; 100%
delle citazioni risolte dal verificatore deterministico contro i candidati
passati; card NSA con dolcificante 1:1 ⇒ `ok` con base
`declared_adaptation`; caso senza referente seminato ⇒ `CANON_GAP`.
✗ una citazione a un documento non presente tra i candidati fa fallire il
verdetto, per quanto convincente. ✗ una quantità proposta non parsabile con
le unità del pack viene respinta, mai applicata.

**Passo 14 — Routing per accordo.**
*Obiettivo:* la confidenza è un fatto misurato (accordo tra esecuzioni), mai
un'autodichiarazione; il disaccordo va sempre all'umano.
*Verifiche:* k=3 con ordine dei candidati realmente permutato (verificabile
dai log); convergenza sui meccanici ⇒ batch-approve; caso costruito
divergente ⇒ coda umana **anche se** ogni singolo run si dichiara sicuro.
✗ nessun percorso in cui `llm_confidence` da sola autorizza
un'applicazione.

**Passo 15 — Campione di controllo.**
*Obiettivo:* misurare ciò che oggi è ignoto — i falsi negativi dell'intera
catena — su ricette che nessuna regola ha segnalato.
*Verifiche:* 30 estratte dalle non-flaggate con seed registrato
(riproducibile); baseline riportata nel report senza soglia al primo giro.
✗ il campione non contiene ricette già nel golden o già flaggate.

**Passo 16 — End-to-end.**
*Obiettivo:* la catena completa è spiegata e reversibile: per ogni modifica
applicata esiste la riga di log che la giustifica e la via del ritorno.
*Verifiche:* diff(input, output) interamente coperto dai log (canon-log,
dose-log, canon_adjudication_log) — nessuna differenza orfana; rollback da
log ricostruisce l'originale; report con metriche e costi per chiamata.
✗ nessuna modifica applicata risale a un verdetto non approvato.

**Fix immediati.**
*`servings` default → errore. Obiettivo:* il fattore di scala non si inventa
mai. ✗ nessun test può più costruire un documento senza servings e ottenere
un risultato. — *`_overlap` bidirezionale. Obiettivo:* una traduzione che
aggiunge contenuto non presente nel sorgente non può più passare con overlap
1.0; verifica con caso sintetico ad aggiunta pura.

---

| Materiale | Ruolo nel programma |
|---|---|
| Report validazione Pareto (docx) + Registro (xlsx) | **Golden set** del passo 13 (le 20 card adiudicate) e coda operativa di correzioni per il chef — indipendente dall'implementazione |
| `parse.py` / `screen.py` consegnati | Seed dei passi 3 e 9 (porting, non riscrittura) |
| Corpus Marchesi nel repo | Corpus canone di sviluppo per i passi 3, 10, 11 |
| I tre documenti precedenti | **Superati da questo file** — non usarli più |

## 6. COSA CONSEGNARE A CLAUDE CODE

Questo file, da solo, è la spec. Consegna in due tranche, coerenti coi tuoi
work-plan con gate:
1. **Tranche 1 = passi 1–10** (Fase 0 completa). Approvazione tua sui passi
   2, 6 (schema pack e flusso dizionario) prima del codice; gate umano al
   passo 6 = revisione del dizionario.
2. **Tranche 2 = passi 11–16** (Fase 1), da avviare **solo a dizionario
   pubblicato** (gate del passo 6 superato): i giudici consumano classi,
   pesi e SAME_AS che prima non esistono.

Decisioni che restano a te (segnaposto nel testo): soglia error-rate coda
(proposta 3%); `kind` dedicato per le issue dosi (proposta: `canon` con
`section='doses'`); `time_min`/`difficulty` obbligatori nel frontmatter ma
assenti sulle card MSC (proposta: opzionali via flag di pack quando
`source_lang == lang`, mai placeholder inventati); contenuto iniziale di
`house-rules.yaml` (porzioni standard, limiti USPH, regole di classe) —
quello lo firma l'organizzazione, non il modello.
