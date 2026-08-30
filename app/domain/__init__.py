"""Domain knowledge layer (Iteration A): pack, translation, verification.

Public API:
- :func:`load_domain_pack` / :func:`validate_pack` — WP-A1 Domain Pack.
- :class:`LLMClient` / :class:`HttpLLMClient` / :class:`FakeLLMClient` — WP-A2.
- :func:`translate_document` — WP-A2 P2-safe stage-1 translation.
- :func:`parse_source_md` / :func:`parse_translated_md` — WP-A3 parser.
- :func:`verify_l1` / :func:`verify_l2` — WP-A3 verification levels.
- :func:`create_adjudication` / :func:`list_adjudications` /
  :func:`decide_adjudication` — WP-A3 L3 queue.
"""
from __future__ import annotations

from app.domain.canonical import (
    CanonicalDocument,
    CanonicalizationError,
    CanonLogEntry,
    CanonLogVerificationError,
    canonicalize,
    generate_canon_log,
    verify_canon_log,
    write_canon_log,
)
from app.domain.config import LLMSettings, get_llm_settings
from app.domain.errors import (
    AdjudicationAlreadyResolvedError,
    AdjudicationError,
    AdjudicationNotFoundError,
    DomainError,
    DomainPackValidationError,
    GlossaryProposalAlreadyResolvedError,
    GlossaryProposalNotFoundError,
    NumberInvariantError,
    ParseError,
    TranslationError,
    VerificationError,
)
from app.domain.llm import FakeLLMClient, HttpLLMClient, LLMClient
from app.domain.numbers import (
    extract_numbers,
    mask_numbers,
    numbers_multiset_equal,
    reinject_numbers,
)
from app.domain.pack import (
    DomainPack,
    DomainPackBundle,
    Glossaries,
    Glossary,
    GlossaryEntry,
    OntologyRef,
    PackPaths,
    UnitRule,
    format_quantity,
    load_domain_pack,
    validate_pack,
)
from app.domain.translate import (
    TranslatedDocument,
    build_translation_input,
    render_translated_document,
    translate_document,
)
from app.domain.verify import (
    DIFFICULTY_MAP,
    IngredientLine,
    L1Report,
    L2Report,
    ParsedDoc,
    SectionComparison,
    VerificationIssue,
    create_adjudication,
    create_glossary_proposal,
    decide_adjudication,
    decide_glossary_proposal,
    get_adjudication,
    list_adjudications,
    list_glossary_proposals,
    normalize_terms,
    parse_source_md,
    parse_translated_md,
    update_document_verification_level,
    verify_l1,
    verify_l2,
)

__all__ = [
    "DIFFICULTY_MAP",
    "AdjudicationAlreadyResolvedError",
    "AdjudicationError",
    "AdjudicationNotFoundError",
    "CanonLogEntry",
    "CanonLogVerificationError",
    "CanonicalDocument",
    "CanonicalizationError",
    "DomainError",
    "DomainPack",
    "DomainPackBundle",
    "DomainPackValidationError",
    "FakeLLMClient",
    "Glossaries",
    "Glossary",
    "GlossaryEntry",
    "GlossaryProposalAlreadyResolvedError",
    "GlossaryProposalNotFoundError",
    "HttpLLMClient",
    "IngredientLine",
    "L1Report",
    "L2Report",
    "LLMClient",
    "LLMSettings",
    "NumberInvariantError",
    "OntologyRef",
    "PackPaths",
    "ParseError",
    "ParsedDoc",
    "SectionComparison",
    "TranslatedDocument",
    "TranslationError",
    "UnitRule",
    "VerificationError",
    "VerificationIssue",
    "build_translation_input",
    "canonicalize",
    "create_adjudication",
    "create_glossary_proposal",
    "decide_adjudication",
    "decide_glossary_proposal",
    "extract_numbers",
    "format_quantity",
    "generate_canon_log",
    "get_adjudication",
    "get_llm_settings",
    "list_adjudications",
    "list_glossary_proposals",
    "load_domain_pack",
    "mask_numbers",
    "normalize_terms",
    "numbers_multiset_equal",
    "parse_source_md",
    "parse_translated_md",
    "reinject_numbers",
    "render_translated_document",
    "translate_document",
    "update_document_verification_level",
    "validate_pack",
    "verify_canon_log",
    "verify_l1",
    "verify_l2",
    "write_canon_log",
]
