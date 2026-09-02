#!/usr/bin/env python3
"""Fonde un lotto di proposte APPROVATE nel glossario del pack (WP-F5).

Il gate umano e' qui e non e' aggirabile: il file di proposte prodotto da
``scripts/propose_glossary_entries.py`` nasce con ``status:
pending_human_review`` e questo script rifiuta di procedere finche' una
persona non lo porta a ``status: approved``.

Cosa fa, in ordine:
  1. rifiuta il file se non e' approvato;
  2. ``duplicate_of`` -> il termine diventa un ALIAS della voce indicata, non
     una voce nuova (e' il caso piu' frequente e il piu' facile da sbagliare);
  3. rifiuta una voce la cui chiave normalizzata esiste gia' (due voci con la
     stessa chiave renderebbero il lookup non deterministico);
  4. rifiuta un ``broader_than`` che punta a un id inesistente;
  5. scrive il glossario e rimisura la copertura.

Uso:
  uv run python scripts/merge_glossary_batch.py --approved <file> [--apply]
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import yaml

from app.domain.coverage import measure_coverage
from app.domain.normalize import normalize_key
from app.domain.pack import load_domain_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"

APPROVED_STATUS = "approved"
ENTRY_FIELDS = (
    "id", "labels_en", "labels_it", "aliases", "definition",
    "ontology_uri", "broader_than",
)


class MergeRefused(RuntimeError):
    """Il lotto non e' fondibile: il messaggio dice perche'."""


def _load_proposals(path: pathlib.Path) -> list[dict]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    status = str(payload.get("status", "")).strip().lower()
    if status != APPROVED_STATUS:
        raise MergeRefused(
            f"{path}: status e' {status!r}, serve {APPROVED_STATUS!r}. "
            "Il lotto va rivisto da una persona prima di entrare nel pack."
        )
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise MergeRefused(f"{path}: nessuna voce da fondere")
    return entries


def plan_merge(pack, proposals: list[dict]) -> tuple[list[dict], list[tuple[str, str]], list[str]]:
    """Restituisce ``(voci nuove, alias da aggiungere, rifiuti motivati)``."""
    glossary = pack.glossaries.ingredienti
    by_id = {entry.id: entry for entry in pack.glossary_entries()}
    keys: dict[str, str] = {}
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            if key:
                keys.setdefault(key, entry.id)

    new_entries: list[dict] = []
    new_aliases: list[tuple[str, str]] = []
    refused: list[str] = []

    for proposal in proposals:
        source = str(proposal.get("source_term") or proposal.get("labels_it") or "")
        duplicate_of = proposal.get("duplicate_of")
        if duplicate_of:
            if duplicate_of not in by_id:
                refused.append(
                    f"{source!r}: duplicate_of {duplicate_of!r} non esiste"
                )
                continue
            new_aliases.append((duplicate_of, source))
            keys.setdefault(normalize_key(source), duplicate_of)
            continue

        entry = {field: proposal.get(field) for field in ENTRY_FIELDS}
        entry["aliases"] = list(entry.get("aliases") or [])
        if source and source not in entry["aliases"]:
            entry["aliases"].append(source)
        if not entry["id"] or not entry["labels_en"] or not entry["labels_it"]:
            refused.append(f"{source!r}: id/labels_en/labels_it mancanti")
            continue
        if entry["id"] in by_id:
            refused.append(f"{source!r}: id {entry['id']!r} gia' usato")
            continue
        if entry["broader_than"] and entry["broader_than"] not in by_id:
            refused.append(
                f"{source!r}: broader_than {entry['broader_than']!r} non esiste"
            )
            continue

        clash = next(
            (
                (term, keys[normalize_key(term)])
                for term in (entry["labels_en"], entry["labels_it"], *entry["aliases"])
                if normalize_key(term) in keys
            ),
            None,
        )
        if clash is not None:
            term, owner_id = clash
            refused.append(
                f"{source!r}: il termine {term!r} appartiene gia' alla voce "
                f"{owner_id!r} (va trattato come alias, non come voce nuova)"
            )
            continue

        for term in (entry["labels_en"], entry["labels_it"], *entry["aliases"]):
            key = normalize_key(term)
            if key:
                keys[key] = entry["id"]
        by_id[entry["id"]] = entry
        new_entries.append(entry)

    del glossary
    return new_entries, new_aliases, refused


def apply_merge(
    pack_dir: pathlib.Path, new_entries: list[dict], new_aliases: list[tuple[str, str]]
) -> pathlib.Path:
    path = pack_dir / "glossari" / "ingredienti.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = payload["entries"]
    by_id = {entry["id"]: entry for entry in entries}

    for entry_id, alias in new_aliases:
        target = by_id.get(entry_id)
        if target is None:
            continue
        aliases = target.setdefault("aliases", [])
        if alias not in aliases:
            aliases.append(alias)

    entries.extend(
        {key: value for key, value in entry.items() if value is not None or key == "ontology_uri"}
        for entry in new_entries
    )
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved", required=True)
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument(
        "--apply", action="store_true", help="scrive il glossario (default: anteprima)"
    )
    args = parser.parse_args(argv)

    pack_dir = pathlib.Path(args.pack)
    pack = load_domain_pack(pack_dir)
    before = measure_coverage(pack, args.corpus)

    try:
        proposals = _load_proposals(pathlib.Path(args.approved))
    except MergeRefused as exc:
        print(exc, file=sys.stderr)
        return 1

    new_entries, new_aliases, refused = plan_merge(pack, proposals)
    print(f"voci nuove : {len(new_entries)}")
    for entry in new_entries:
        print(f"  + {entry['id']:<28} {entry['labels_it']} -> {entry['labels_en']}")
    print(f"alias nuovi: {len(new_aliases)}")
    for entry_id, alias in new_aliases:
        print(f"  ~ {entry_id:<28} += {alias!r}")
    if refused:
        print(f"rifiutate  : {len(refused)}")
        for reason in refused:
            print(f"  ! {reason}")

    if not args.apply:
        print("\n(anteprima: glossario non scritto — usa --apply)")
        return 0

    path = apply_merge(pack_dir, new_entries, new_aliases)
    after = measure_coverage(load_domain_pack(pack_dir), args.corpus)
    print(f"\nglossario: {path}")
    print(
        f"coverage: {before.coverage:.2%} -> {after.coverage:.2%} "
        f"(+{after.lines_resolved - before.lines_resolved} righe)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
