"""P2 number utilities: extraction, masking and re-injection.

P2 (spec A section 0): the LLM never sees numbers. ``mask_numbers`` replaces
every *content* number with a ``{Nk}`` placeholder; ``reinject_numbers``
restores the original values; ``extract_numbers`` is the deterministic
invariant used by stage-1 translation and L1 verification.

Content numbers are quantities, temperatures and times. Structural step
numbers (``1. `` at the start of a Method/Procedimento line) are NOT content
numbers and are left untouched.
"""
from __future__ import annotations

import re
from collections import Counter

# A content number: integer or decimal with a dot (Italian corpus convention).
# The lookarounds keep it out of identifiers such as ``RIC-001``.
NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)
_STEP_RE = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")


def strip_frontmatter(text: str) -> str:
    """Return the body of a markdown document (frontmatter removed)."""
    match = _FRONTMATTER_RE.match(text)
    return text[match.end():] if match else text


def _find_numbers(text: str) -> list[str]:
    # Skip all-zero tokens such as the "00" in "farina 00" (a flour type,
    # not a quantity). Real recipe quantities are never written as "00".
    return [
        token for token in NUMBER_RE.findall(text)
        if not re.fullmatch(r"0+", token)
    ]


def extract_numbers(text: str) -> list[str]:
    """Return content numbers in document order (frontmatter excluded).

    Step numbers are structural and are skipped. Frontmatter numeric fields
    (servings/time_min) are checked separately by L1, not by this function.
    """
    body = strip_frontmatter(text)
    numbers: list[str] = []
    for line in body.splitlines():
        match = _STEP_RE.match(line)
        if match:
            numbers.extend(_find_numbers(match.group(3)))
        else:
            numbers.extend(_find_numbers(line))
    return numbers


def mask_numbers(text: str) -> tuple[str, list[str]]:
    """Replace content numbers with ``{N1}``.. placeholders.

    Returns ``(masked_text, numbers)``. Step numbers are preserved so the
    document structure stays intact for the translator.
    """
    numbers: list[str] = []
    out: list[str] = []

    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        # Allineato a _find_numbers: i token all-zero (es. "00" di "farina 00")
        # non sono quantità e non vanno mascherati (P2: nessun numero alterato,
        # ma solo numeri di contenuto reali).
        if re.fullmatch(r"0+", token):
            return token
        numbers.append(token)
        return f"{{N{len(numbers)}}}"

    for line in text.splitlines():
        match = _STEP_RE.match(line)
        if match:
            indent, step_no, rest = match.groups()
            masked_rest = NUMBER_RE.sub(repl, rest)
            out.append(f"{indent}{step_no}. {masked_rest}")
        else:
            out.append(NUMBER_RE.sub(repl, line))
    return "\n".join(out), numbers


def reinject_numbers(text: str, numbers: list[str]) -> str:
    """Replace ``{N1}``.. placeholders with the original numbers.

    The placeholder sequence must be exactly ``{N1}, {N2}, ...`` in order.
    A missing, duplicated or reordered placeholder raises ``ValueError`` so a
    translation that drops or swaps numbers fails closed instead of silently
    producing wrong quantities.
    """
    expected = [f"{{N{i}}}" for i in range(1, len(numbers) + 1)]
    found = re.findall(r"\{N\d+\}", text)
    if found != expected:
        raise ValueError(
            f"placeholder sequence mismatch: expected {expected}, got {found}"
        )
    result = text
    for i, number in enumerate(numbers, start=1):
        result = result.replace(f"{{N{i}}}", number)
    return result


def numbers_multiset_equal(left: list[str], right: list[str]) -> bool:
    """Return True when the two number lists are equal as multisets."""
    return Counter(left) == Counter(right)
