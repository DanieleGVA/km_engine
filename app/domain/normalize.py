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
