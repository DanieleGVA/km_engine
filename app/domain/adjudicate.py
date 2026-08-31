"""Passo 12 PROGRAMMA-UNICO: giudice semantico sulle escalation L2.

L2 (token overlap) resta come trigger economico ma smette di decidere: le
escalation passano dal giudice che chiude i falsi allarmi con motivazione,
diagnostica i veri con suggerimento per riga, marca UNSURE. Solo veri e
UNSURE arrivano all'umano.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.llm import LLMClient

L2_SYSTEM_PROMPT = (
    "You are a culinary translation verifier. A source recipe section and its "
    "translation were flagged by a token-overlap check. Decide whether the "
    "divergence is REAL (the translation changed the meaning) or a FALSE ALARM "
    "(the overlap check is too strict for legitimate rewording).\n"
    "Rules:\n"
    "- If the translation preserves the meaning (synonyms, reordering, "
    "grammar) -> overall 'ok' with motivation.\n"
    "- If the translation drops/adds/changes content -> overall 'divergent' "
    "with a per-line suggestion.\n"
    "- If you cannot decide -> overall 'unsure'.\n"
    "Respond with a single JSON object: {\"overall\": \"ok\"|\"divergent\"|"
    "\"unsure\", \"motivation\": string, \"lines\": [{\"line\": int, "
    "\"verdict\": \"ok\"|\"divergent\"|\"unsure\", \"motivation\": "
    "string, \"suggestion\": string}]}."
)


class L2LineVerdict(BaseModel):
    """Verdetto per riga."""

    line: int
    verdict: str = Field(pattern="^(ok|divergent|unsure)$")
    motivation: str = ""
    suggestion: str = ""


class L2Verdict(BaseModel):
    """Verdetto complessivo su una sezione L2."""

    overall: str = Field(pattern="^(ok|divergent|unsure)$")
    motivation: str = ""
    lines: list[L2LineVerdict] = Field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        """Solo veri e UNSURE arrivano all'umano."""
        return self.overall in ("divergent", "unsure")


def build_l2_prompt(section: str, source_text: str, translated_text: str) -> str:
    return (
        f"Section: {section}\n"
        f"SOURCE:\n{source_text}\n"
        f"TRANSLATED:\n{translated_text}"
    )


async def adjudicate_l2(
    judge: LLMClient,
    section: str,
    source_text: str,
    translated_text: str,
) -> L2Verdict:
    """Giudica una escalation L2 (falso allarme / divergenza reale / unsure)."""
    result = await judge.judge(
        L2_SYSTEM_PROMPT,
        build_l2_prompt(section, source_text, translated_text),
        L2Verdict,
    )
    return L2Verdict.model_validate(result)
