"""Passo 6 PROGRAMMA-UNICO: publish del dizionario approvato.

Legge le adjudications kind='dictionary' APPROVATE e produce gli artefatti di
pack versionati:
- glossari/ingredienti.yaml v2 (nuove voci dal dizionario approvato)
- msc_mapping.yaml (item code MSC -> termine canonico)
- bump di versione in pack.yaml

Regole (gate passo 6):
- publish con zero approvazioni produce zero modifiche (no-op)
- ogni decisione ha riga di audit (gia' in adjudications/audit_log)
- il diff corrisponde alle sole voci approvate
- una voce rejected non compare in nessun artefatto
- riproducibile: stesso stato -> stesso output

Uso:
    uv run python scripts/publish_dictionary.py --pack domain-packs/ricette
"""
from __future__ import annotations

import argparse
import json
import pathlib

import psycopg
import yaml

from app.domain.verify import list_adjudications


def _bump_version(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pack", required=True, type=pathlib.Path)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    dsn = args.dsn or "postgresql://km:km_dev_password@localhost:5432/km_engine"
    conn = psycopg.connect(dsn, autocommit=True)

    approved = [
        a for a in list_adjudications(conn, status="approved")
        if a.get("kind") == "dictionary" and a.get("verdict_json")
    ]
    # gia' pubblicate (canon_adjudication_log): no-op senza nuove decisioni
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT document_id FROM canon_adjudication_log "
            "WHERE kind = 'dictionary'"
        )
        published_keys = {row[0] for row in cur.fetchall()}
    approved = [a for a in approved if a["document_id"] not in published_keys]
    if not approved:
        print(json.dumps({"approved": 0, "published": 0, "noop": True}))
        conn.close()
        return 0

    # artefatti
    glossary_path = args.pack / "glossari" / "ingredienti.yaml"
    mapping_path = args.pack / "msc_mapping.yaml"
    pack_yaml_path = args.pack / "pack.yaml"

    glossary = yaml.safe_load(glossary_path.read_text(encoding="utf-8"))
    existing_ids = {e["id"] for e in glossary["entries"]}
    mapping: dict[str, str] = {}
    if mapping_path.exists():
        mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}

    new_entries = []
    for a in approved:
        v = a["verdict_json"]
        key = a["document_id"]
        # voce rejected mai negli artefatti (qui solo approved)
        if v["corpus"] == "msc":
            mapping[key] = v["canonical_name_en"]
        entry_id = f"ING-DICT-{len(existing_ids) + len(new_entries) + 1:04d}"
        entry = {
            "id": entry_id,
            "labels_en": v["canonical_name_en"],
            "labels_it": v["canonical_name_en"],
            "aliases": v.get("aliases", []),
            "definition": f"Standardizzato da dizionario ({v['corpus']}, key={key}).",
        }
        if v.get("class"):
            entry["class"] = v["class"]
        if v.get("allergen_tags"):
            entry["allergen_tags"] = v["allergen_tags"]
        if v.get("unit_weight_g") is not None:
            entry["unit_weight_g"] = v["unit_weight_g"]
        if v.get("countable_unit"):
            entry["countable_unit"] = v["countable_unit"]
            entry["count_policy"] = v.get("count_policy", "exact")
        if v.get("density_g_per_ml") is not None:
            entry["density_g_per_ml"] = v["density_g_per_ml"]
        new_entries.append(entry)

    glossary["entries"].extend(new_entries)
    glossary_path.write_text(
        yaml.safe_dump(glossary, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    mapping_path.write_text(
        yaml.safe_dump(mapping, allow_unicode=True, sort_keys=True),
        encoding="utf-8",
    )

    # bump versione pack
    pack_yaml = yaml.safe_load(pack_yaml_path.read_text(encoding="utf-8"))
    old_version = pack_yaml["version"]
    pack_yaml["version"] = _bump_version(old_version)
    pack_yaml_path.write_text(
        yaml.safe_dump(pack_yaml, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    # registro di pubblicazione (reversibilita', passo 16)
    with conn.cursor() as cur:
        for a in approved:
            cur.execute(
                """
                INSERT INTO canon_adjudication_log
                    (document_id, kind, verdict_json, llm_model, llm_confidence)
                VALUES (%s, 'dictionary', %s, %s, %s)
                """,
                (a["document_id"], json.dumps(a["verdict_json"]),
                 a.get("llm_model"), a.get("llm_confidence")),
            )

    print(json.dumps({
        "approved": len(approved),
        "published": len(new_entries),
        "mapping_entries": len(mapping),
        "version": f"{old_version} -> {pack_yaml['version']}",
        "noop": False,
    }, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
