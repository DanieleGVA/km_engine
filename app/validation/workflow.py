"""Workflow di ricerca e validazione ricette (branch validate-recipe).

Flusso (come richiesto):
1. l'utente indica il file contenente le ricette da validare
2. il sistema legge le ricette, verifica lingua e formato, decide come
   trasformarle (standardizzazione: ingredienti, dosi, procedure, x10 persone);
   le ricette composte vengono separate in sub-recipe
3. le ricette standardizzate vengono scritte in formato standardizzato
4. le ricette vengono cercate nel knowledge per:
   a) impronta ingredienti standard con dosi
   b) procedura standardizzata
   c) nome della ricetta
5. se presente: confronto e validazione con correzioni su ingredienti, dosi,
   procedura; se assente: "non presente"
6. la ricetta analizzata viene salvata con le note
7. report globale di validazione
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from app.auth import Principal
from app.domain.pack import DomainPackBundle
from app.storage.client import Neo4jClient
from app.validation.ingest import (
    RawRecipe,
    StandardizedRecipe,
    read_recipes,
    split_subrecipes,
    standardize_recipe,
)
from app.validation.search import RecipeMatch, search_recipe


@dataclass
class RecipeValidationResult:
    """Risultato della validazione di una singola ricetta."""

    recipe: StandardizedRecipe
    match: RecipeMatch
    corrections: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_file: str | None = None

    @property
    def status(self) -> str:
        return "VALIDATA" if self.match.found else "NON PRESENTE"


@dataclass
class ValidationWorkflowReport:
    """Report globale di validazione."""

    input_file: str
    total: int
    found: int
    not_found: int
    sub_recipes: int
    results: list[RecipeValidationResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "input_file": self.input_file,
            "total": self.total,
            "found": self.found,
            "not_found": self.not_found,
            "sub_recipes": self.sub_recipes,
            "results": [
                {
                    "recipe": r.recipe.raw.name,
                    "code": r.recipe.raw.code,
                    "status": r.status,
                    "match_document": r.match.document_id,
                    "match_title": r.match.title,
                    "score": round(r.match.score, 3),
                    "ingredient_score": round(r.match.ingredient_score, 3),
                    "procedure_score": round(r.match.procedure_score, 3),
                    "name_score": round(r.match.name_score, 3),
                    "matched_ingredients": r.match.matched_ingredients[:10],
                    "missing_ingredients": r.match.missing_ingredients[:10],
                    "corrections": r.corrections,
                    "notes": r.notes,
                    "output_file": r.output_file,
                }
                for r in self.results
            ],
        }


def _compare_ingredients(query_fp, doc_fp) -> list[str]:
    """Correzioni ingredienti/dosi: differenze tra la ricetta da validare e il doc trovato."""
    corrections: list[str] = []
    q = {item: qty for item, qty in query_fp}
    d = {item: qty for item, qty in doc_fp}
    for item, qty in q.items():
        if item in d:
            if abs(qty - d[item]) / max(d[item], 1e-9) > 0.05:
                corrections.append(f"dose {item}: {qty:g} -> {d[item]:g} (knowledge)")
        else:
            corrections.append(f"ingrediente {item} non presente nel knowledge")
    for item in d:
        if item not in q:
            corrections.append(f"ingrediente extra nel knowledge: {item}")
    return corrections


async def run_validation_workflow(
    input_path: pathlib.Path,
    pack: DomainPackBundle,
    client: Neo4jClient,
    principal: Principal,
    out_dir: pathlib.Path,
    servings_target: int = 10,
    limit: int | None = None,
) -> ValidationWorkflowReport:
    """Esegue il workflow completo di ricerca e validazione."""
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_recipes = read_recipes(input_path, limit=limit)
    # indice impronte del knowledge costruito UNA volta (6k doc, non per ricetta)
    from app.validation.search import DocumentIndex
    index = DocumentIndex(client, pack)
    # normalizzatore ingredienti CalcMenu (deterministico + LLM nei casi dubbi)
    from app.validation.calcmenu import CalcMenuNormalizer, load_canonical_vocab
    normalizer = _build_normalizer(out_dir)
    results: list[RecipeValidationResult] = []
    n_subs = 0

    for raw in raw_recipes:
        # 2) separazione sub-recipe
        main, subs = split_subrecipes(raw)
        n_subs += len(subs)

        # 2) standardizzazione (con gestione errori: una ricetta malformata
        #    non deve interrompere l'intera validazione)
        try:
            std = await standardize_recipe(main, pack, servings_target=servings_target,
                                           normalizer=normalizer)
        except Exception as e:
            results.append(RecipeValidationResult(
                recipe=StandardizedRecipe(raw=main, canonical_md="", servings_target=servings_target,
                                          scale_factor=0.0, notes=[f"ERRORE standardizzazione: {e}"]),
                match=RecipeMatch(found=False),
                corrections=[], notes=[f"ERRORE standardizzazione: {e}"],
            ))
            continue
        std.notes.append(f"formato: {raw.format}, lingua: {raw.language}")
        if subs:
            std.notes.append(f"{len(subs)} sub-recipe separate: {', '.join(s.name for s in subs)}")

        # 3) scrittura in formato standardizzato
        safe = re.sub(r"[^a-z0-9]+", "_", std.raw.name.lower())[:40]
        out_file = out_dir / f"{safe}.md"
        out_file.write_text(std.canonical_md, encoding="utf-8")

        # 4) ricerca nel knowledge (impronta + procedura + nome)
        try:
            match = search_recipe(client, pack, principal, std.canonical_md, std.raw.name, index=index)
        except Exception as e:
            results.append(RecipeValidationResult(
                recipe=std, match=RecipeMatch(found=False),
                corrections=[], notes=std.notes + [f"ERRORE ricerca: {e}"],
            ))
            continue

        # 5) validazione/correzioni
        corrections: list[str] = []
        try:
            if match.found and match.document_id:
                from app.validation.search import _recompose, ingredient_fingerprint
                doc_md = _recompose(client, match.document_id)
                if doc_md:
                    corrections = _compare_ingredients(
                        ingredient_fingerprint(std.canonical_md, pack.known_units()),
                        ingredient_fingerprint(doc_md, pack.known_units()),
                    )
                std.notes.append(f"trovata nel knowledge: {match.document_id} ({match.title})")
            else:
                std.notes.append("NON PRESENTE nel knowledge")
        except Exception as e:
            std.notes.append(f"ERRORE confronto: {e}")

        # 6) salvataggio con note
        try:
            note_file = out_dir / f"{safe}.notes.json"
            note_file.write_text(json.dumps({
                "recipe": std.raw.name, "code": std.raw.code, "status": "VALIDATA" if match.found else "NON PRESENTE",
                "match": match.document_id, "score": round(match.score, 3),
                "corrections": corrections, "notes": std.notes,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            std.notes.append(f"ERRORE salvataggio note: {e}")

        results.append(RecipeValidationResult(
            recipe=std, match=match, corrections=corrections,
            notes=std.notes, output_file=str(out_file),
        ))

    report = ValidationWorkflowReport(
        input_file=str(input_path),
        total=len(results),
        found=sum(1 for r in results if r.match.found),
        not_found=sum(1 for r in results if not r.match.found),
        sub_recipes=n_subs,
        results=results,
    )
    (out_dir / "validation_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return report


def _build_normalizer(out_dir: pathlib.Path):
    """Normalizzatore CalcMenu: deterministico + LLM (se configurato) + cache."""
    from app.validation.calcmenu import CalcMenuNormalizer, load_canonical_vocab
    llm = None
    try:
        from app.domain.config import get_llm_settings
        from app.domain.llm import HttpLLMClient
        settings = get_llm_settings()
        if settings.llm_endpoint and settings.llm_model:
            llm = HttpLLMClient(settings)
    except Exception:
        llm = None
    return CalcMenuNormalizer(
        load_canonical_vocab(), llm=llm,
        cache_path=out_dir / "calcmenu_normalizer_cache.json",
    )


import re  # noqa: E402
