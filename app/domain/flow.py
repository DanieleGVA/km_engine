"""Passo 8 PROGRAMMA-UNICO: orchestratore del flusso documento.

``process_document`` e' l'UNICO punto in cui la sequenza
translate -> L1 -> L2 -> canonicalize -> doses -> issues e' cablata.
Adottato dai call-site esistenti di ``canonicalize`` negli agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.domain import (
    CanonicalDocument,
    L1Report,
    L2Report,
    TranslatedDocument,
    canonicalize,
    parse_source_md,
    parse_translated_md,
    translate_document,
    verify_l1,
    verify_l2,
)
from app.domain.doses import DoseStandardizedDocument, standardize_doses
from app.domain.llm import LLMClient
from app.domain.pack import DomainPackBundle


@dataclass
class ProcessedDocument:
    """Esito completo del flusso su un documento."""

    source_md: str
    translated: TranslatedDocument
    l1: L1Report
    l2: L2Report | None
    canonical: CanonicalDocument
    doses: DoseStandardizedDocument
    issues: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.l1.passed and not self.issues


async def process_document(
    pack: DomainPackBundle,
    source_md: str,
    llm: LLMClient,
    *,
    servings_target: int = 10,
    run_l2: bool = True,
) -> ProcessedDocument:
    """Esegue il flusso completo su un documento sorgente.

    ``run_l2``: L2 richiede il pack per la normalizzazione glossario; per i
    documenti nativi EN (card MSC) il confronto L2 non ha senso (source ==
    translated) e si puo' saltare.
    """
    translated = await translate_document(pack, source_md, llm)
    l1 = verify_l1(source_md, translated.translated_md, pack=pack)

    l2: L2Report | None = None
    if run_l2:
        source_parsed = parse_source_md(
            source_md, known_units=pack.known_units(),
            countable_units=pack.countable_units(),
        )
        translated_parsed = parse_translated_md(
            translated.translated_md, known_units=pack.known_units(),
            optional_when_native=tuple(pack.frontmatter_optional_when_native),
            countable_units=pack.countable_units(),
        )
        l2 = verify_l2(source_parsed, translated_parsed, pack=pack)

    canonical = canonicalize(pack, translated.translated_md)
    doses = standardize_doses(
        canonical.canonical_md, pack, servings_target=servings_target
    )

    issues: list[str] = []
    issues.extend(i.message for i in l1.issues)
    if l2 is not None:
        issues.extend(i.message for i in l2.escalations)
    issues.extend(doses.issues)

    return ProcessedDocument(
        source_md=source_md,
        translated=translated,
        l1=l1,
        l2=l2,
        canonical=canonical,
        doses=doses,
        issues=issues,
    )
