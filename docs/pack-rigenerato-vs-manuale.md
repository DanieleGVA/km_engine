# Pack rigenerato vs pack manuale (Iterazione C, WP-C1..C4)

Pipeline deterministica eseguita sul PILOT (15 ricette validate).

## Esito gate

- Round-trip: **15/15** (100.0%) — gate: PASS
- Copertura glossario draft: **98.9%** (92/93 mention risolte)
- Copertura glossario manuale: **96.8%** (90/93)
- Copertura relativa (draft/manuale): **102.2%** — soglia >= 90%: PASS
- Gate complessivo: **PASS**

## Normalizzazione vs golden pilot A

- Precision: **97.8%**
- Recall: **100.0%**
- Match: 90 (draft risolte 92, golden risolte 90)

## Codegen (suite-tipo A sul draft)

- P2 invarianti: 3/3
- Canon-log completo: 3/3
- Round-trip campione: 3/3

## Struttura del brief

- Entità candidate: 62
- Vocabolari: ingredienti (54), tecnica (6), stati (2)
- Unità rilevate: 14
- Ambiguità: 3
- Ontologie candidate: 2

## Draft generato

- Staging dir: `/Users/daniele.buonaiuto/km_engine/domain-packs/ricette-agents-draft`
- Entry glossario: 62
- Regole unità: 18

## Artefatti

- Brief: `docs/domain-briefs/ricette-v1.json`
- Golden pilot A: `docs/domain-briefs/ricette-golden-pilot-a.json`
- Gate report: `docs/domain-briefs/ricette-gate-report.json`
