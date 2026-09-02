"""Chiave di normalizzazione simmetrica per il lookup di glossario (WP-F1).

Il difetto D2: ``canonical._strip_item_connectors`` toglieva ``di``/``e``
dall'item ma le chiavi di glossario li conservavano, cosi' ``olio extravergine
di oliva`` diventava ``olio extravergine oliva`` e non trovava piu' la voce che
lo descrive. La cura non e' aggiungere alias: e' applicare **la stessa**
trasformazione ai due lati del confronto.

``normalize_key`` e' pura e non conosce il pack. E' l'unica normalizzazione
ammessa per il lookup: nessun'altra funzione in ``app/domain`` deve fare
casefold/strip su un termine per cercarlo nel glossario.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.quantities import QTY_RANGE_RE, QuantityError, parse_quantity

if TYPE_CHECKING:  # pragma: no cover - solo per i tipi
    from app.domain.pack import DomainPackBundle

# Apostrofi tipografici -> ASCII: il corpus usa U+2019, i glossari l'ASCII.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})

# Elisioni italiane: "d'aglio" -> "aglio", "l'uovo" -> "uovo". La lista e'
# chiusa di proposito: "sott'olio" e "all'agro" non sono elisioni di un
# articolo ma parte del termine, quindi "sott'" non compare qui.
_ELISION_RE = re.compile(
    r"\b(?:d|dell|degl|all|nell|sull|un|l)'\s*", re.IGNORECASE
)

# Connettore in testa: residuo della segmentazione "2 spicchi | di aglio".
# Solo in testa: i connettori INTERNI fanno parte del termine
# ("olio extravergine di oliva" resta intero).
_LEADING_CONNECTOR_RE = re.compile(
    r"^(?:di|del|della|dello|dei|degli|delle|e|ed)\s+", re.IGNORECASE
)

_WS_RE = re.compile(r"\s+")

# Articolo o partitivo in testa: residuo della segmentazione, mai parte del
# nome ("il succo di 1 limone"). Usato anche da ``verify._parse_ingredient``.
_LEADING_ARTICLE_RE = re.compile(
    r"^(?:(?:il|lo|la|i|gli|le|un|uno|una|dei|degli|delle)\s+"
    r"|(?:l|un|dell|degl|all|nell)['\u2019]\s*)",
    re.IGNORECASE,
)
LEADING_ARTICLE_RE = _LEADING_ARTICLE_RE


def normalize_text(text: str) -> str:
    """Normalizzazione a livello di *testo*: apostrofi, casefold, elisioni.

    Conserva la spaziatura (quindi anche gli a capo): e' la forma usata da
    ``verify.normalize_terms`` per sostituire i termini dentro una sezione,
    dove collassare gli spazi distruggerebbe la struttura del markdown.
    ``normalize_key`` e' questa stessa trasformazione piu' i passi che hanno
    senso solo su un termine isolato.
    """
    if not text:
        return ""
    return _ELISION_RE.sub("", text.translate(_APOSTROPHES).casefold())


def normalize_key(text: str) -> str:
    """Chiave di lookup deterministica, applicata a item e glossario.

    1. apostrofi tipografici -> ASCII;
    2. casefold;
    3. elisioni rimosse (``d'aglio`` -> ``aglio``);
    4. connettori iniziali rimossi (ripetutamente: la funzione e' idempotente);
    5. spazi collassati e bordi ripuliti.

    I connettori interni restano: ``olio extravergine di oliva`` e
    ``sale e pepe`` sono termini, non due termini uniti da una congiunzione.
    """
    if not text:
        return ""
    out = normalize_text(text)
    while True:
        stripped = _LEADING_CONNECTOR_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    return _WS_RE.sub(" ", out).strip()


# ---------------------------------------------------------------------------
# WP-F4 — risoluzione a livelli
# ---------------------------------------------------------------------------

RULE_EXACT = "GLOSS-EXACT"
RULE_ALIAS = "GLOSS-ALIAS"
RULE_HEAD = "GLOSS-HEAD"
RULE_FUZZY = "GLOSS-FUZZY"
RULE_UNRESOLVED = "GLOSS-UNRESOLVED"

DEFAULT_FUZZY_THRESHOLD = 0.92
DEFAULT_FUZZY_MARGIN = 0.05

_HEAD_MIN_TOKENS = 1
_HEAD_MAX_PREFIX_TOKENS = 3


@dataclass(frozen=True)
class Resolution:
    """Esito della risoluzione di un item contro il glossario.

    ``label_en`` e' ``None`` quando nulla ha risolto: in quel caso l'item NON
    viene riscritto (T10, nessuna risoluzione inventata) e ``candidates``
    porta i termini piu' vicini per la coda di proposte.

    ``qty`` e' la quantita' trovata DENTRO l'item dopo un prefisso di
    preparazione ("succo di 1 limone" -> prep=succo, qty=1, item=limone): e'
    la dose della riga, finita nell'item perche' il libro la scrive li'.
    """

    label_en: str | None
    glossary_id: str | None
    rule_id: str
    states: tuple[str, ...] = ()
    prep: str | None = None
    qty: str | None = None
    candidates: tuple[tuple[str, float], ...] = ()
    dropped: tuple[str, ...] = ()
    needs_review: bool = False

    @property
    def resolved(self) -> bool:
        return self.label_en is not None


class Resolver:
    """Risolve un item ingrediente contro le voci del pack, per livelli.

    L'ordine e' fisso e il primo livello che risolve vince:

    ``L0 EXACT``   la chiave normalizzata e' un ``labels_en``/``labels_it``;
    ``L1 ALIAS``   la chiave normalizzata e' un alias;
    ``L2 HEAD``    staccando SOLO i modificatori dichiarati in
                   ``regole/normalizzazione.yaml`` resta una testa nota;
    ``L3 FUZZY``   trigram Jaccard sopra soglia e con margine sul secondo;
    ``L4 UNRESOLVED`` nessuna riscrittura, solo candidati.

    La lista dei modificatori e' chiusa: e' l'unica cosa che impedisce a
    ``funghi porcini`` di diventare ``funghi``. Un aggettivo che discrimina
    (colore, varieta', provenienza) non e' un modificatore e blocca L2, che e'
    il comportamento voluto: meglio irrisolto che risolto male.
    """

    def __init__(
        self,
        pack: DomainPackBundle,
        *,
        fuzzy_threshold: float | None = None,
        fuzzy_margin: float | None = None,
    ) -> None:
        from app.domain.coverage import TrigramIndex

        self.pack = pack
        rules = pack.rules.get("normalizzazione") or {}
        self.fuzzy_threshold = (
            fuzzy_threshold
            if fuzzy_threshold is not None
            else float(rules.get("fuzzy_threshold", DEFAULT_FUZZY_THRESHOLD))
        )
        self.fuzzy_margin = (
            fuzzy_margin
            if fuzzy_margin is not None
            else float(rules.get("fuzzy_margin", DEFAULT_FUZZY_MARGIN))
        )

        self._labels: dict[str, tuple[str, str]] = {}
        self._aliases: dict[str, tuple[str, str]] = {}
        for entry in pack.glossary_entries():
            for term in (entry.labels_en, entry.labels_it):
                key = normalize_key(term)
                if key:
                    self._labels.setdefault(key, (entry.labels_en, entry.id))
            for term in entry.aliases:
                key = normalize_key(term)
                if key and key not in self._labels:
                    self._aliases.setdefault(key, (entry.labels_en, entry.id))

        # Stati: solo il glossario "stati" decide se un modificatore staccato
        # viene conservato come stato o scartato. Non decide MAI se un termine
        # si risolve: quello lo decide la lista chiusa dei modificatori.
        self._states: dict[str, str] = {}
        for entry in pack.glossaries.stati.entries:
            for term in (entry.labels_it, entry.labels_en, *entry.aliases):
                key = normalize_key(term)
                if key:
                    self._states.setdefault(key, entry.labels_en)

        self._modifiers: list[str] = sorted(
            {
                key
                for key in (
                    normalize_key(modifier) for modifier in rules.get("modifiers", [])
                )
                if key
            },
            key=len,
            reverse=True,
        )
        # prep_prefixes: mappa "prefisso italiano" -> "etichetta inglese".
        # Accetta anche una lista semplice (il prefisso vale come etichetta),
        # cosi' un pack piu' vecchio continua a caricarsi.
        raw_prefixes = rules.get("prep_prefixes") or {}
        if isinstance(raw_prefixes, list):
            raw_prefixes = {prefix: prefix for prefix in raw_prefixes}
        self._prep_prefixes: dict[str, str] = {}
        for prefix, label in raw_prefixes.items():
            key = normalize_key(prefix)
            if key:
                self._prep_prefixes.setdefault(key, str(label).strip() or key)
        self._prep_order: list[str] = sorted(
            self._prep_prefixes, key=len, reverse=True
        )
        self._index = TrigramIndex(sorted(self._labels) + sorted(self._aliases))

    # -- livelli ----------------------------------------------------------

    def _lookup(self, key: str) -> tuple[str, str, str] | None:
        hit = self._labels.get(key)
        if hit is not None:
            return hit[0], hit[1], RULE_EXACT
        hit = self._aliases.get(key)
        if hit is not None:
            return hit[0], hit[1], RULE_ALIAS
        return None

    def _split_prep(self, key: str) -> tuple[str | None, str, str | None]:
        """``"succo di 1 limone"`` -> ``("succo", "limone", "1")``.

        Restituisce ``(prep, resto, quantita' trovata nel resto)``. La
        quantita' e' quella che il libro scrive dentro l'item: e' la dose
        della riga, non parte del nome dell'ingrediente.
        """
        for prefix in self._prep_order:
            if not key.startswith(prefix + " "):
                continue
            prep = self._prep_prefixes[prefix]
            rest = key[len(prefix) + 1:].strip()
            qty: str | None = None
            match = QTY_RANGE_RE.match(rest)
            if match:
                try:
                    qty = parse_quantity(match.group(1))
                except QuantityError:
                    qty = None
                else:
                    rest = rest[match.end():].strip()
            return (prep or None), rest, qty
        return None, key, None

    def _strip_modifiers(self, key: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        """Toglie i modificatori dichiarati; restituisce ``(testa, stati, scartati)``."""
        remaining = key
        states: list[str] = []
        dropped: list[str] = []
        changed = True
        while changed:
            changed = False
            for modifier in self._modifiers:
                for candidate in (f" {modifier}", f"{modifier} "):
                    if candidate.startswith(" ") and remaining.endswith(candidate):
                        remaining = remaining[: -len(candidate)].strip()
                    elif candidate.endswith(" ") and remaining.startswith(candidate):
                        remaining = remaining[len(candidate):].strip()
                    else:
                        continue
                    state = self._states.get(modifier)
                    if state is not None:
                        states.append(state)
                    else:
                        dropped.append(modifier)
                    changed = True
                    break
                if changed:
                    break
        return remaining, tuple(states), tuple(dropped)

    def _fuzzy(self, key: str) -> tuple[str, str, tuple[tuple[str, float], ...]] | None:
        ranked = self._index.top(key, 3)
        if not ranked:
            return None
        best_key, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        if best_score < self.fuzzy_threshold:
            return None
        if best_score - second_score < self.fuzzy_margin:
            # due candidati quasi equivalenti: nessuna scelta arbitraria
            return None
        hit = self._labels.get(best_key) or self._aliases.get(best_key)
        if hit is None:
            return None
        return hit[0], hit[1], tuple(ranked)

    # -- API --------------------------------------------------------------

    def resolve(self, item: str) -> Resolution:
        """Risolve ``item``; non riscrive mai un termine che non ha risolto."""
        key = normalize_key(item)
        # L'articolo in testa e' gia' tolto dal parser; qui si difende dal
        # caso in cui il resolver venga chiamato su testo grezzo.
        key = normalize_key(_LEADING_ARTICLE_RE.sub("", key, count=1))
        if not key:
            return Resolution(None, None, RULE_UNRESOLVED)

        hit = self._lookup(key)
        if hit is not None:
            label, glossary_id, rule_id = hit
            return Resolution(label, glossary_id, rule_id)

        prep, without_prep, inner_qty = self._split_prep(key)
        if prep is not None:
            hit = self._lookup(without_prep)
            if hit is not None:
                label, glossary_id, _ = hit
                return Resolution(
                    label, glossary_id, RULE_HEAD, prep=prep, qty=inner_qty
                )

        head, states, dropped = self._strip_modifiers(without_prep)
        if head != without_prep and head:
            hit = self._lookup(head)
            if hit is not None:
                label, glossary_id, _ = hit
                return Resolution(
                    label, glossary_id, RULE_HEAD,
                    states=states, prep=prep, qty=inner_qty, dropped=dropped,
                )

        fuzzy = self._fuzzy(without_prep if prep else key)
        if fuzzy is not None:
            label, glossary_id, candidates = fuzzy
            return Resolution(
                label, glossary_id, RULE_FUZZY,
                states=states, prep=prep, qty=inner_qty, candidates=candidates,
                dropped=dropped, needs_review=True,
            )

        # T10: nessuna riscrittura. Gli stati staccati restano comunque
        # disponibili al chiamante, che li conserva sulla riga.
        return Resolution(
            None, None, RULE_UNRESOLVED,
            states=states, prep=prep, qty=inner_qty,
            candidates=tuple(self._index.top(key, 3)),
            dropped=dropped,
        )
