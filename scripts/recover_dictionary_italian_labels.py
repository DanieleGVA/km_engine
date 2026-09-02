#!/usr/bin/env python3
"""Recupera il termine italiano delle voci di dizionario (WP-F7).

``scripts/publish_dictionary.py`` ha pubblicato 930 voci ``ING-DICT-*`` con
``labels_it`` uguale a ``labels_en``: l'italiano non e' mai stato riempito. Il
termine sorgente non e' perso — sta nel testo della ``definition``:

    labels_en:  sage
    labels_it:  sage                                          <- campo sbagliato
    definition: Standardizzato da dizionario (book, key=salvia)   <- il dato e' qui

Cosi' com'e', una voce gia' approvata dallo chef e' irraggiungibile dal lato
italiano: ``salvia`` resta irrisolto pur avendo la sua voce. Questo script
sposta il dato dove serve. Non inventa niente, non aggiunge voci, non tocca
``labels_en``: e' una migrazione deterministica di un dato gia' giudicato.

Il termine nella ``definition`` e' pero' la RIGA GREZZA del ricettario, non un
termine pulito: accanto a ``salvia`` e ``patate`` ci sono
``teaspoon salt, or to taste`` e ``garlic, crushed in the garlic-press``.
Scriverli in ``labels_it`` riempirebbe il glossario di spazzatura, e alias
speculativi possono produrre risoluzioni sbagliate.

Quindi il criterio e' stretto: **si aggiunge un alias solo se quel termine
compare davvero nel corpus come item irrisolto**. Ogni alias aggiunto e'
giustificato da occorrenze reali e il guadagno e' misurabile riga per riga;
niente entra "per ogni evenienza". ``labels_it`` non viene toccata: e'
l'etichetta ufficiale della voce e non si deduce da una riga di ricettario.

Cosa fa, per ogni voce ``ING-DICT-*`` con ``corpus=book``:
  1. estrae il termine sorgente dalla ``definition``;
  2. lo aggiunge agli ``aliases`` SOLO se e' fra i termini irrisolti misurati
     sul corpus;
  3. salta la voce se la chiave normalizzata appartiene gia' a un'ALTRA voce
     (non si creano ambiguita' nuove: il conflitto viene riportato).

Le voci ``corpus=msc`` sono saltate: la loro chiave e' un item code, non un
termine italiano.

Uso:
  uv run python scripts/recover_dictionary_italian_labels.py            # anteprima
  uv run python scripts/recover_dictionary_italian_labels.py --apply
"""
from __future__ import annotations

import argparse
import pathlib
import re

import yaml

from app.domain.coverage import measure_coverage
from app.domain.normalize import normalize_key
from app.domain.pack import load_domain_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PACK = REPO_ROOT / "domain-packs" / "ricette"
DEFAULT_CORPUS = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"

DEFINITION_RE = re.compile(r"\((?P<corpus>msc|book), key=(?P<key>.+)\)\.?\s*$", re.DOTALL)
# Il termine sorgente porta spesso la coda della riga di ricettario: il
# connettore iniziale e i rimandi fra parentesi non fanno parte del nome.
TRAILING_NOTE_RE = re.compile(r"\s*\((?:vedi|see)[^)]*\)\s*$", re.IGNORECASE)


def source_term(definition: str | None) -> tuple[str, str] | None:
    """``(corpus, termine sorgente)`` dalla definition, o ``None``."""
    match = DEFINITION_RE.search((definition or "").strip())
    if match is None:
        return None
    term = TRAILING_NOTE_RE.sub("", match.group("key")).strip()
    return match.group("corpus"), term


def plan(pack, unresolved: dict[str, int]) -> tuple[list[dict], list[str]]:
    """``(migrazioni, conflitti)`` senza toccare nulla.

    ``unresolved``: termine irrisolto -> righe di corpus, dalla misura di
    copertura. E' il filtro che tiene fuori gli alias speculativi.
    """
    owner: dict[str, str] = {}
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            if key:
                owner.setdefault(key, entry.id)

    migrations: list[dict] = []
    conflicts: list[str] = []
    for entry in pack.glossaries.ingredienti.entries:
        if not entry.id.startswith("ING-DICT"):
            continue
        parsed = source_term(entry.definition)
        if parsed is None:
            continue
        corpus, term = parsed
        if corpus != "book" or not term:
            continue
        key = normalize_key(term)
        english = normalize_key(entry.labels_en)
        if not key or key == english:
            continue
        lines = unresolved.get(key)
        if not lines:
            # nessuna occorrenza reale: non si aggiungono alias speculativi
            continue
        holder = owner.get(key)
        if holder is not None and holder != entry.id:
            conflicts.append(
                f"{entry.id} ({entry.labels_en!r}): il termine {term!r} "
                f"appartiene gia' a {holder!r} — non migrato"
            )
            continue
        migrations.append(
            {
                "id": entry.id,
                "labels_en": entry.labels_en,
                "term": term,
                "lines": lines,
            }
        )
        owner[key] = entry.id
    migrations.sort(key=lambda m: -m["lines"])
    return migrations, conflicts


def apply(pack_dir: pathlib.Path, migrations: list[dict]) -> pathlib.Path:
    path = pack_dir / "glossari" / "ingredienti.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    by_id = {entry["id"]: entry for entry in payload["entries"]}
    for migration in migrations:
        entry = by_id[migration["id"]]
        aliases = entry.setdefault("aliases", [])
        if migration["term"] not in aliases:
            aliases.append(migration["term"])
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", default=str(DEFAULT_PACK))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--show", type=int, default=25)
    args = parser.parse_args(argv)

    pack_dir = pathlib.Path(args.pack)
    pack = load_domain_pack(pack_dir)
    before = measure_coverage(pack, args.corpus)

    unresolved = {term.term: term.count for term in before.unresolved}
    migrations, conflicts = plan(pack, unresolved)
    recovered = sum(migration["lines"] for migration in migrations)
    residual = before.lines_total - before.lines_resolved
    print(f"alias recuperati (solo termini con occorrenze reali): {len(migrations)}")
    print(
        f"  righe di corpus coperte: {recovered} su {residual} di residuo "
        f"({recovered / residual:.1%})"
    )
    if conflicts:
        print(f"conflitti (saltati, nessuna ambiguita' creata): {len(conflicts)}")
        for conflict in conflicts[:10]:
            print(f"  ! {conflict}")

    print(f"\nprime {min(args.show, len(migrations))} migrazioni (per righe coperte):")
    for migration in migrations[: args.show]:
        print(
            f"  {migration['lines']:>4} righe  {migration['id']:<16} "
            f"{migration['term']!r} -> {migration['labels_en']!r}"
        )

    if not args.apply:
        print("\n(anteprima: glossario non scritto — usa --apply)")
        return 0

    path = apply(pack_dir, migrations)
    after = measure_coverage(load_domain_pack(pack_dir), args.corpus)
    print(f"\nglossario: {path}")
    print(
        f"coverage: {before.coverage:.2%} -> {after.coverage:.2%} "
        f"(+{after.lines_resolved - before.lines_resolved} righe)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
