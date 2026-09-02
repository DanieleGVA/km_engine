#!/usr/bin/env python3
"""Genera il golden di traduzione REALE con l'LLM (WP-F6).

I test dello stadio 2 sono circolari: il traduttore finto sostituisce i
termini usando lo stesso glossario che lo stadio 2 usera' per risolverli,
quindi non puo' mai mancare un termine e il gate misura se' stesso (D7).
Questo script rompe il cerchio: chiama l'LLM vero una volta, salva le
traduzioni e le committa; da li' in poi i test leggono quelle.

Va eseguito da una persona: costa e richiede credenziali
(``KM_LLM_ENDPOINT`` / ``KM_LLM_MODEL`` / ``KM_LLM_API_KEY``).

  uv run python scripts/build_translated_golden.py --limit 154
  uv run python scripts/build_translated_golden.py           # tutto il corpus

Il ``manifest.json`` registra modello, hash del prompt e data: se il prompt
di traduzione cambia, l'hash cambia e si sa che il golden va rifatto.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import sys

from app.domain import (
    FakeLLMClient,
    build_translation_input,
    mask_numbers,
    parse_source_md,
)
from app.domain.config import get_llm_settings
from app.domain.errors import DomainError
from app.domain.llm import HttpLLMClient, translation_prompt_sha256
from app.domain.pack import load_domain_pack
from app.domain.translate import glossary_labels, translate_document

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"
DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_translated"


async def _build(
    pack,
    corpus: dict[str, str],
    out_dir: pathlib.Path,
    concurrency: int,
    *,
    resume: bool = True,
) -> list[dict]:
    llm = HttpLLMClient()
    semaphore = asyncio.Semaphore(concurrency)
    known_units = pack.known_units()
    countable_units = pack.countable_units()

    manifest_path = out_dir / "manifest.json"
    existing: dict[str, dict] = {}
    if resume and manifest_path.is_file():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing = {
            entry["source"]: entry
            for entry in previous.get("documents", [])
            if "masked_output" in entry
        }

    async def one(name: str, source_md: str) -> dict | None:
        path = out_dir / f"{pathlib.Path(name).stem}.md"
        try:
            parsed = parse_source_md(
                source_md, known_units=known_units, countable_units=countable_units
            )
            masked_input, _ = mask_numbers(build_translation_input(parsed))
        except DomainError as exc:
            print(f"  ! {name}: {exc}", file=sys.stderr)
            return None

        # Resume: un batch lungo non deve ripagare le traduzioni gia' fatte.
        cached = existing.get(name)
        if resume and cached is not None and path.is_file():
            return cached

        async with semaphore:
            try:
                # Si salva la risposta GREZZA del modello (il corpo mascherato
                # tradotto), non il documento finale: e' quello che il
                # FakeLLMClient deve restituire ai test perche' la pipeline
                # faccia davvero la re-iniezione dei numeri e la verifica P2.
                # Salvare il documento gia' composto farebbe saltare quei
                # passi e il golden mentirebbe su cosa e' stato verificato.
                masked_output = await llm.translate(
                    masked_input,
                    source_lang=pack.language,
                    target_lang=pack.canonical_language,
                    glossary=glossary_labels(pack),
                )
                replay = FakeLLMClient({masked_input: masked_output})
                translated = await translate_document(pack, source_md, replay)
            except DomainError as exc:
                # Una ricetta che il modello traduce male (tipicamente P2: un
                # numero aggiunto o perso) viene saltata, non fa cadere il
                # batch: il golden e' un campione, non un tutto-o-niente.
                print(f"  ! {name}: {exc}", file=sys.stderr)
                return None
            except (ValueError, RuntimeError) as exc:
                print(f"  ! {name}: {exc}", file=sys.stderr)
                return None
            path.write_text(translated.translated_md, encoding="utf-8")
            return {
                "source": name,
                "file": path.name,
                "document_id": translated.document_id,
                "masked_input": masked_input,
                "masked_output": masked_output,
            }

    results = await asyncio.gather(
        *(one(name, corpus[name]) for name in sorted(corpus))
    )
    return [row for row in results if row is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="ritraduce anche le ricette gia' presenti in --out",
    )
    args = parser.parse_args(argv)

    pack = load_domain_pack(args.pack)
    corpus_dir = pathlib.Path(args.corpus)
    files = sorted(corpus_dir.glob("*.md"))
    if args.limit:
        files = files[: args.limit]
    corpus = {path.name: path.read_text(encoding="utf-8") for path in files}
    if not corpus:
        print(f"nessuna ricetta in {corpus_dir}", file=sys.stderr)
        return 1

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"traduzione reale di {len(corpus)} ricette -> {out_dir}")
    documents = asyncio.run(
        _build(
            pack, corpus, out_dir, args.concurrency, resume=not args.no_resume
        )
    )

    manifest = {
        "version": "1.0",
        "generated_by": "scripts/build_translated_golden.py",
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "pack": f"{pack.pack.name}:{pack.pack.version}",
        "model": get_llm_settings().llm_model,
        "prompt_sha256": translation_prompt_sha256(
            pack.language, pack.canonical_language, glossary_labels(pack)
        ),
        "documents": documents,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"golden: {len(documents)}/{len(corpus)} ricette, manifest scritto")
    if len(documents) < len(corpus):
        print("alcune ricette non sono state tradotte (vedi sopra)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
