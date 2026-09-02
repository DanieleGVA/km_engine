# Pack rigenerato vs pack manuale (Iterazione C, WP-C1..C4)

Pipeline deterministica eseguita sul PILOT (15 ricette validate).

## Esito gate

- Round-trip: **15/15** (100.0%) — gate: PASS
- Copertura glossario draft: **100.0%** (93/93 mention risolte)
- Copertura glossario manuale: **97.9%** (91/93)
- Copertura relativa (draft/manuale): **102.2%** — soglia >= 90%: PASS
- Gate complessivo: **PASS**

## Normalizzazione vs golden pilot A

- Precision: **94.6%**
- Recall: **96.7%**
- Match: 88 (draft risolte 93, golden risolte 91)

## Codegen (suite-tipo A sul draft)

- P2 invarianti: 3/3
- Canon-log completo: 3/3
- Round-trip campione: 3/3

## Struttura del brief

- Entità candidate: 62
- Vocabolari: ingredienti (54), tecnica (6), stati (2)
- Unità rilevate: 15
- Ambiguità: 3
- Ontologie candidate: 2

## Draft generato

- Staging dir: `/Users/daniele.buonaiuto/km_engine/domain-packs/ricette-agents-draft`
- Entry glossario: 62
- Regole unità: 27

## Artefatti

- Brief: `docs/domain-briefs/ricette-v1.json`
- Golden pilot A: `docs/domain-briefs/ricette-golden-pilot-a.json`
- Gate report: `docs/domain-briefs/ricette-gate-report.json`

## Curator/Documenter (Iterazione C, WP-C5/C6)

### Curator loop (WP-C5)

- `app/agents/curator.py`: `mine_issues` (fatti AMBIGUOUS, conflitti pending,
  flag untranslated, coda `glossary_proposals`, ambiguità dal brief) →
  `propose_extension` (alias/entry glossario + definizione + `ontology_uri` P7,
  mai applicata direttamente) → `apply_approved` (solo proposte `approved`,
  gate umano non aggirabile).
- Gate umano verificato con test negativi: `apply_approved` su proposta
  `pending`/`rejected` o su riga Postgres non `approved` → `CuratorGateError`;
  il pack manuale `domain-packs/ricette` non è mai scritto.
- Ri-canonicalizzazione incrementale verificata: solo i documenti toccati
  vengono ri-estratti; gli altri mantengono `canonical_hash` invariato.
- Storico bitemporale intatto: nessun DELETE; le versioni precedenti dei fatti
  restano nella catena `VERSION_OF` (test white-box su cambio valore).

### E2E Curator (ambiguità iniettate)

- Corpus sintetico con 5 termini modificatori (`sbucciate`, `a cubetti`,
  `aromatizzato`, `tritato`, `fresco`).
- Ambiguità iniziali: **5** → finali dopo N=3 cicli con adjudication simulata:
  **0**.
- Riduzione ambiguità: **100%** (soglia >= 80%: PASS).

### Documenter (WP-C6)

- `app/agents/documenter.py`: `generate_decision_records` (da `canon_log` +
  `adjudications` approvate: chi, quando, perché, `rule_id`) e
  `generate_pack_changelog` (diff tra versioni pack).
- Artefatti: `docs/domain-briefs/decision-records.json` e
  `docs/domain-briefs/pack-changelog.md`.
- Test: >=1 decision record per mappatura adjudicata; changelog non vuoto e
  coerente (ADDED/CHANGED/REMOVED con versione pack).

### Punti aperti per l'iterazione D

- Le euristiche di ambiguità restano da raffinare con adjudication reale
  (oggi l'adjudication è simulata nei test).
- `ontology_uri` delle nuove entry è un URI DBpedia deterministico (P7); la
  validazione FoodOn puntuale resta al gate umano.
- Il ricalcolo automatico dei fatti derivati (Q12) dopo invalidazione resta
  parziale: il Curator versiona i fatti toccati, ma la propagazione ai
  dipendenti `INFERRED` è ancora `under_review` (eredità WP6).
