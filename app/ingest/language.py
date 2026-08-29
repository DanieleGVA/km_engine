"""FR9 language normalization.

The canonical internal language is English. ``normalize_language`` detects the
source language with a small deterministic heuristic (no external service) and
marks non-English content as needing translation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Function words per language. The detector is deliberately small and
# deterministic: it is a testable MVP heuristic, not a statistical language
# model. WP5 can replace it with a real detector without changing the
# LanguageInfo contract.
_LANG_MARKERS: dict[str, frozenset[str]] = {
    "en": frozenset({
        "the", "and", "is", "are", "of", "to", "in", "for", "with",
        "this", "that", "from", "on", "at", "by", "a", "an", "be",
    }),
    "fr": frozenset({
        "le", "la", "les", "et", "est", "sont", "de", "des", "du",
        "une", "un", "pour", "avec", "ce", "cette", "dans", "que",
        "qui", "sur", "pas", "au", "aux",
    }),
    "de": frozenset({
        "der", "die", "das", "und", "ist", "sind", "von", "zu", "in",
        "für", "mit", "dies", "diese", "ein", "eine", "auf", "den",
        "dem", "nicht",
    }),
    "it": frozenset({
        "il", "lo", "la", "e", "è", "sono", "di", "del", "della",
        "per", "con", "questo", "questa", "un", "una", "che", "non",
        "nel", "nella",
    }),
    "es": frozenset({
        "el", "la", "los", "las", "y", "es", "son", "de", "del",
        "para", "con", "este", "esta", "un", "una", "que", "no",
        "en", "por",
    }),
}

# Strong character signals for languages that share many short function words.
_ACCENT_BONUS: dict[str, str] = {
    "fr": "éèêëàâçôîïùûœ",
    "de": "äöüß",
    "es": "ñ¿¡",
    "it": "ìòù",
}

_WORD_RE = re.compile(r"[^\W\d_]+", flags=re.UNICODE)


@dataclass(frozen=True)
class LanguageInfo:
    """Result of language normalization.

    ``detected`` is the source language; ``canonical`` is always ``en``
    (FR9.1). ``translation_state`` is ``pending`` for non-English content and
    ``native`` for content already in the canonical language.
    """

    detected: str
    canonical: str = "en"
    needs_translation: bool = False
    translation_state: str = "native"

    @property
    def source_language(self) -> str | None:
        """The original language when translation is required, else None."""
        return self.detected if self.needs_translation else None


def normalize_language(text: str | None) -> LanguageInfo:
    """Detect the language of ``text`` and return the FR9 normalization.

    Empty/None text is treated as English (canonical). The heuristic scores
    function words and adds a bonus for language-specific accented characters.
    Ties fall back to English.
    """
    if not text or not text.strip():
        return LanguageInfo(detected="en")

    words = _WORD_RE.findall(text.casefold())
    scores = {lang: 0 for lang in _LANG_MARKERS}
    for word in words:
        for lang, markers in _LANG_MARKERS.items():
            if word in markers:
                scores[lang] += 1

    lowered = text.casefold()
    for lang, chars in _ACCENT_BONUS.items():
        scores[lang] += sum(1 for ch in chars if ch in lowered)

    if not any(scores.values()):
        return LanguageInfo(detected="en")

    # Highest score wins; ties resolve to English (canonical default).
    best_score = max(scores.values())
    if best_score == 0:
        return LanguageInfo(detected="en")
    winners = [lang for lang, score in scores.items() if score == best_score]
    detected = "en" if "en" in winners else winners[0]
    needs_translation = detected != "en"
    return LanguageInfo(
        detected=detected,
        needs_translation=needs_translation,
        translation_state="pending" if needs_translation else "native",
    )
