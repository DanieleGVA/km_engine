"""Grammatica delle quantita' (WP-F3): frazioni, range, "q.b.".

Un solo posto sa leggere e scrivere una quantita'. Il parser del template, la
misura P2 e i renderer (canonico, tradotto, ricomposto) passano tutti da qui:
se ``½`` fosse un numero per il parser e non per P2, l'invariante sui numeri
fallirebbe su ogni riga con una frazione.
"""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Frazioni tipografiche presenti nei ricettari (il corpus usa ½; le altre
# sono accettate per non dover tornare qui alla prima ricetta che le usa).
VULGAR_FRACTIONS: dict[str, tuple[int, int]] = {
    "½": (1, 2), "⅓": (1, 3), "⅔": (2, 3), "¼": (1, 4), "¾": (3, 4),
    "⅕": (1, 5), "⅖": (2, 5), "⅗": (3, 5), "⅘": (4, 5),
    "⅙": (1, 6), "⅚": (5, 6), "⅐": (1, 7), "⅛": (1, 8), "⅜": (3, 8),
    "⅝": (5, 8), "⅞": (7, 8), "⅑": (1, 9), "⅒": (1, 10),
}
_FRACTION_CLASS = "".join(VULGAR_FRACTIONS)

# Un atomo di quantita': "2", "1.5", "1,5", "1/2", "½", "1 ½".
QTY_ATOM = (
    rf"(?:\d+\s*/\s*\d+"
    rf"|\d+(?:[.,]\d+)?(?:\s*[{_FRACTION_CLASS}])?"
    rf"|[{_FRACTION_CLASS}])"
)

# Range: "2-3", "2–3" (trattino o en dash).
QTY_RANGE_RE = re.compile(
    rf"^({QTY_ATOM})(?:\s*[-–—]\s*({QTY_ATOM}))?(?=\s|$)"
)

_MIXED_RE = re.compile(rf"^(\d+(?:[.,]\d+)?)\s*([{_FRACTION_CLASS}])$")
_SLASH_RE = re.compile(r"^(\d+)\s*/\s*(\d+)$")

# "q.b." e come lo rende un traduttore: la riga non ha una dose numerica.
# ``as needed``/``as required`` non sono ipotesi: sono le forme che il modello
# di traduzione produce davvero al posto di "q.b." (viste nel golden reale).
_TO_TASTE_ALT = (
    r"q\.?\s?b\.?|qb|a piacere|quanto basta"
    r"|to taste|as needed|as required|as desired"
)
TO_TASTE_HEAD_RE = re.compile(
    rf"^(?:{_TO_TASTE_ALT})(?=[\s,]|$)[\s,]*", re.IGNORECASE
)
TO_TASTE_TAIL_RE = re.compile(
    rf"(?:^|[\s,])(?:{_TO_TASTE_ALT})\s*$", re.IGNORECASE
)

TO_TASTE_IT = "q.b."
TO_TASTE_EN = "to taste"


class QuantityError(ValueError):
    """Il testo non e' una quantita' riconoscibile."""


def format_decimal(value: Decimal) -> str:
    """Serializza una quantita' (intero senza .0, massimo 3 decimali)."""
    if value == value.to_integral_value():
        return str(int(value))
    quantized = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f").rstrip("0").rstrip(".")
    return text or "0"


def parse_quantity(token: str) -> str:
    """``"½"`` -> ``"0.5"``, ``"1 ½"`` -> ``"1.5"``, ``"1,5"`` -> ``"1.5"``.

    Restituisce sempre una stringa decimale: il confronto P2 fra italiano e
    inglese avviene sui valori, non sui glifi.
    """
    text = token.strip()
    if not text:
        raise QuantityError("empty quantity")

    if len(text) == 1 and text in VULGAR_FRACTIONS:
        numerator, denominator = VULGAR_FRACTIONS[text]
        return format_decimal(Decimal(numerator) / Decimal(denominator))

    mixed = _MIXED_RE.match(text)
    if mixed:
        whole = Decimal(mixed.group(1).replace(",", "."))
        numerator, denominator = VULGAR_FRACTIONS[mixed.group(2)]
        return format_decimal(whole + Decimal(numerator) / Decimal(denominator))

    slash = _SLASH_RE.match(text)
    if slash:
        denominator = Decimal(slash.group(2))
        if denominator == 0:
            raise QuantityError(f"division by zero in {token!r}")
        return format_decimal(Decimal(slash.group(1)) / denominator)

    try:
        return format_decimal(Decimal(text.replace(",", ".")))
    except InvalidOperation as exc:
        raise QuantityError(f"not a quantity: {token!r}") from exc


def is_quantity(token: str) -> bool:
    """True se ``token`` e' un atomo di quantita' completo."""
    try:
        parse_quantity(token)
    except QuantityError:
        return False
    return True
