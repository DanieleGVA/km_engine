"""Passo 13 PROGRAMMA-UNICO: giudice di canone per componente + critico.

Prompt in 5 passi (spec §3.3): dosi gia' per 10 porzioni => confronto diretto;
identifica il referente tra i candidati o CANON_GAP; scegli la forma del
benchmark (rapporto / grammature assolute / rapporto interno) prima di
confrontare i numeri; classifica errore vs adattamento dichiarato (P10);
rileva le assenze di procedura — sempre con citazione (P8).

P8 closed-book: ogni verdetto cita un candidato fornito o si astiene
(CANON_GAP). P9: ogni quantita' proposta ripassa dal validatore
deterministico prima di toccare qualunque documento.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.llm import LLMClient
from app.domain.pack import DomainPackBundle

CANON_JUDGE_SYSTEM = (
    "You are a corporate-chef-level recipe validator. Compare a recipe "
    "component against canon candidates (book recipes) and produce a "
    "per-line verdict.\n"
    "Steps:\n"
    "1. Doses are already scaled to 10 portions: compare directly.\n"
    "2. Identify the reference among the candidates, or abstain with "
    "CANON_GAP if none fits.\n"
    "3. Choose the benchmark form (ratio / absolute grams / internal ratio) "
    "BEFORE comparing numbers.\n"
    "4. Classify error vs declared adaptation (NSA, VG, class): a declared "
    "adaptation is 'ok' with base 'declared_adaptation', never rejected.\n"
    "5. Detect procedure absences.\n"
    "Rules:\n"
    "- Every verdict line must cite a candidate (document_id + position) or "
    "the whole component abstains with CANON_GAP.\n"
    "- Never invent a citation: cite ONLY the candidates provided.\n"
    "- Proposed quantities must be parseable (number + unit).\n"
    "Respond with a single JSON object matching the schema."
)


class LineVerdict(BaseModel):
    """Verdetto per riga ingrediente."""

    line: int
    verdict: str = Field(pattern="^(ok|correct|add|delete|flag)$")
    proposal: str | None = None
    reason: str = ""
    severity: str = Field(default="low", pattern="^(low|medium|high)$")
    citation: str | None = None
    base: str | None = Field(
        default=None,
        pattern="^(ratio|absolute|internal|declared_adaptation)$",
    )


class ComponentVerdict(BaseModel):
    """Verdetto su un componente."""

    component: str
    overall: str = Field(pattern="^(ok|correct|add|delete|flag|canon_gap)$")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    motivation: str = ""
    lines: list[LineVerdict] = Field(default_factory=list)
    procedure_absences: list[str] = Field(default_factory=list)


class DishVerdict(BaseModel):
    """Ricomposizione a livello piatto (architettura della porzione)."""

    overall: str = Field(pattern="^(ok|flag|canon_gap)$")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    motivation: str = ""
    issues: list[str] = Field(default_factory=list)


def build_component_prompt(
    component: str,
    card_lines: list[str],
    candidates: list[dict[str, Any]],
    house_rules: str = "",
) -> str:
    """Prompt utente: componente della card + candidati di canone."""
    parts = [f"COMPONENT: {component}", "CARD LINES:"]
    parts.extend(f"  {i}. {l}" for i, l in enumerate(card_lines))
    parts.append("CANON CANDIDATES:")
    for c in candidates:
        parts.append(
            f"  [{c['document_id']}] {c['title']} | "
            f"{c.get('component', 'main')} | {c.get('lines', [])}"
        )
    if house_rules:
        parts.append(f"HOUSE RULES:\n{house_rules}")
    return "\n".join(parts)


async def judge_component(
    judge: LLMClient,
    component: str,
    card_lines: list[str],
    candidates: list[dict[str, Any]],
    house_rules: str = "",
) -> ComponentVerdict:
    """Giudica un componente contro i candidati di canone (closed-book)."""
    result = await judge.judge(
        CANON_JUDGE_SYSTEM,
        build_component_prompt(component, card_lines, candidates, house_rules),
        ComponentVerdict,
    )
    return ComponentVerdict.model_validate(result)


async def judge_recomposition(
    judge: LLMClient, component_verdicts: list[ComponentVerdict]
) -> DishVerdict:
    """Ricomposizione a livello piatto: massa, equilibrio, coerenza, servizio."""
    prompt = "COMPONENT VERDICTS:\n" + "\n".join(
        f"  [{v.component}] {v.overall} conf={v.confidence:.2f} | {v.motivation}"
        for v in component_verdicts
    )
    result = await judge.judge(
        "You are a corporate chef reviewing the whole dish. Check portion "
        "architecture (total mass, protein/starch/vegetable balance), "
        "class/diet coherence, line service feasibility, cross-cutting "
        "safety. Respond with a single JSON object matching the schema.",
        prompt,
        DishVerdict,
    )
    return DishVerdict.model_validate(result)


CRITIC_SYSTEM = (
    "You are an adversarial critic. Attack the following recipe verdict: "
    "citations that do not support the claim, inflated severities, declared "
    "adaptations wrongly rejected, conflicts with house rules. Then produce "
    "the REVISED verdict. Respond with a single JSON object matching the "
    "component verdict schema."
)


async def critic_revise(
    judge: LLMClient, verdict: ComponentVerdict
) -> ComponentVerdict:
    """Genera-critica-rivedi: secondo passaggio con mandato opposto."""
    prompt = f"VERDICT TO CRITICIZE:\n{verdict.model_dump_json(indent=1)}"
    result = await judge.judge(CRITIC_SYSTEM, prompt, ComponentVerdict)
    return ComponentVerdict.model_validate(result)


# ---------------------------------------------------------------------------
# Verifiche deterministiche (P8, P9)
# ---------------------------------------------------------------------------

def verify_citations(verdict: ComponentVerdict, candidate_ids: set[str]) -> list[str]:
    """P8: ogni citazione deve riferirsi a un candidato fornito.

    Una citazione a un documento non presente tra i candidati fa fallire il
    verdetto, per quanto convincente.
    """
    problems: list[str] = []
    for line in verdict.lines:
        if line.citation:
            doc_id = line.citation.split(":")[0]
            if doc_id not in candidate_ids:
                problems.append(
                    f"line {line.line}: citazione {line.citation!r} a un "
                    f"documento non tra i candidati"
                )
    return problems


def validate_proposed_quantities(
    verdict: ComponentVerdict, pack: DomainPackBundle
) -> list[str]:
    """P9: una quantita' proposta non parsabile con le unita' del pack viene
    respinta, mai applicata."""
    import re

    problems: list[str] = []
    units = pack.known_units()
    for line in verdict.lines:
        if line.proposal:
            m = re.match(r"^(\d+(?:\.\d+)?)\s+(\S+)$", line.proposal.strip())
            if not m:
                problems.append(f"line {line.line}: proposta non parsabile {line.proposal!r}")
            elif m.group(2).lower() not in units:
                problems.append(
                    f"line {line.line}: unita' {m.group(2)!r} non nel pack"
                )
    return problems
