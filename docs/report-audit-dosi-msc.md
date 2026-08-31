# Audit dosi — ricettario MSC (Pareto_Recipe_Cards_v001.pdf)

**Data:** 2026-08-31 · **Blocco 2** del piano PROGRAMMA-UNICO · **Solo report, nessuna modifica al codice**
**Input:** `Pareto_Recipe_Cards_v001.pdf` (1.653 card, 19.500 righe ingredienti) vs output standardizzati in `deploy/validation_out/` (1.315 file .md)

---

## Verdetto in una riga

**Lo scaling aritmetico (qty × 10/yield) è corretto; il problema vero è che ~920 righe (5,9%) hanno unità che il parser del dominio non riconosce (lt, ea, cl, serving, pz, mg) e che finiscono dentro il nome ingrediente — senza conversione MKS e con item inquinati che non matchano il knowledge.**

---

## 1. Copertura del parsing

| Metrica | Valore | Note |
|---|---|---|
| Card nel PDF | 1.653 | |
| Card parse dal workflow | 1.382 | **271 card perse (16%)** |
| Card con output standardizzato | 1.315 | 67 parse ma senza output (errori) |
| Righe ingredienti nel PDF | 19.500 | |
| Righe parse con qty>0 e unità ammesse | 15.589 | ~3.900 righe droppate |

## 2. Yield: 262 card (16%) droppate per formato non riconosciuto

Il parser accetta solo `Yield N serve/serving/servings`. Formati presenti nel PDF e **non** riconosciuti:

| Formato yield | Card | Esempio |
|---|---|---|
| `N [_]` (unità ignota) | 120 | "10 [_]", "1 [_]", "100 [_]", "10,000 [_]" |
| `N pz` (pezzi) | 47 | "1 pz", "24 pz", "50 pz" |
| `N pax` | 41 | "100 pax", "50 pax", "400 pax" |
| `N recipe` | 11 | "1 recipe", "10 recipe" |
| `N subrecipe` | 15 | "1 subrecipe", "450 subrecipe" |
| `N rect.60x40` (teglia) | 8 | "1 rect.60x40", "30 rect.60x40" |
| `N KG` / `N LT` (resa in peso/volume) | 4 | "10 KG", "2.50 KG", "10 LT" |
| `N loaf` / `N cake24cm` / `N pizza` / `N tray` | 8 | "4 loaf", "6 cake24cm", "1 pizza", "1 tray" |
| `N Portion` | 2 | "100 Portion" |
| `N,NNN serve` (migliaia con virgola) | 5 | "6,000 serve", "1,120 serving" |
| `1 nan` | 1 | dato sporco |

**Impatto:** 262 card escluse silenziosamente dalla validazione. Il piano (passo 0) prevede esattamente questo: yield → `servings` int con coda errori per i formati non risolvibili, mai default.

## 3. Unità: 922 righe (5,9%) con unità assenti dal pack → item inquinati

Il parser del dominio (`parse_translated_md` con `pack.known_units()`) non riconosce queste unità, che finiscono **dentro il nome ingrediente**:

| Unità | Righe | Nel pack? | Effetto attuale |
|---|---|---|---|
| `lt` (litri) | 364 | ❌ | "3 LT green peppercorn sauce" → item = "LT green peppercorn sauce", nessuna conversione |
| `ea` (each) | 212 | ❌ | item inquinato |
| `cl` (centilitri) | 158 | ❌ | "150 cl oil" → item = "cl oil", nessuna conversione cl→ml |
| `serving` / `servings` | 81 | ❌ | "10 serving cherry tomatoes" → item = "serving cherry tomatoes" |
| `pz` (pezzi) | 72 | ❌ | item inquinato |
| `mg` (milligrammi) | 35 | ❌ | "10 mg thyme" → item = "mg thyme", nessuna conversione mg→g |

**Conseguenze misurate:**
- **547 righe con unità diversa** tra atteso e output (3,6% delle 15.175 confrontate)
- **193 righe con quantità diversa** (1,3%) — quasi tutte lo stesso fenomeno: la conversione MKS (cl→ml, lt→l, mg→g) non avviene perché l'unità non è estratta
- **Impatto sul matching:** gli item inquinati ("LT green peppercorn sauce", "cl oil") non combaciano con i termini canonici del knowledge → contribuiscono al match rate ~0,6%

**Nota:** `MKS_FACTORS` in `doses.py` contiene già cl/dl/mg/lt, ma il parser non li estrae mai come unità perché non sono in `known_units()` del pack. Il piano (passo 2) prevede di aggiungerli come regole identità in `units.yaml` con le forme esatte per case (KG, LT, EA, TT, pz).

## 4. Quantità zero e assenti (a piacere / sezioni)

| Caso | Righe | Trattamento attuale |
|---|---|---|
| qty `0` (a piacere: "SALT TABLE 0 K", "PEPPERCORN BLACK GROUND 0 T") | 893 | Droppate dal parser (filtro q>0) — nessuna traccia |
| qty `—` (righe-sezione: "CRUMBLE", "COMPOSITION", "GARNISH & SERVING") | 545 | Droppate — il piano (passo 0) le vuole come metadato `{component: ...}` |
| Unità non ammesse / artefatti di parsing | 287 | Droppate o parse errate |

**Impatto:** le righe "a piacere" (0) sono ingredienti reali (sale, pepe, prezzemolo) che oggi spariscono dalla distinta → il confronto ingredienti perde componenti. Il piano (passo 9, `verify_intra`) distingue "0 KG SALT TABLE" (a piacere, non flaggato) da "0 KG ONION" (anomalo, flaggato).

## 5. Lo scaling è corretto dove l'unità è riconosciuta

Sulle righe con unità riconosciute (g, kg, ml, dl — 94% del totale), il confronto posizionale qty×10/yield vs output **non trova discrepanze** oltre ai 193 casi cl/lt/mg. Il fattore di scala nei notes (0.10, 0.42, 2.50…) è coerente con `10/yield` del PDF.

## 6. Raccomandazioni (per i passi del piano, non patch ad hoc)

1. **Passo 0 (convertitore MSC):** gestire i formati yield mancanti ([_], pax, pz, recipe, subrecipe, KG/LT, migliaia con virgola) → coda errori, mai default. Recupero stimato: +262 card (16%).
2. **Passo 2 (schema pack + unità):** aggiungere a `units.yaml` le regole identità per `lt`, `ea`, `cl`, `mg`, `serving(s)`, `pz` (e forme esatte `LT`, `EA`, `KG`, `TT`) → il parser le estrae, `canonicalize` non le tocca, `standardize_doses` converte cl→ml / lt→l / mg→g e lascia i contabili invariati. Recupero stimato: 922 righe (5,9%) con item puliti e dosi MKS corrette.
3. **Passo 0/9:** righe qty 0 → metadato "a piacere" (non droppate); righe-sezione → `{component: ...}`.
4. **Verifica finale:** dopo i passi 0+2, ri-eseguire questo audit: atteso 0 mismatch di unità/quantità e 1.653 card processate.

---
*Report generato da `scripts/audit_doses.py` (confronto posizionale riga-per-riga, 15.175 righe, 1.315 card).*
