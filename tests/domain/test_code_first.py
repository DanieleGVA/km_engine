"""Passo 7 PROGRAMMA-UNICO: applicazione code-first in canonicalize.

Obiettivo: l'identita' (item code) prevale sulla stringa; ogni riscrittura e'
spiegata nel canon-log con la versione del dizionario; un miss non inventa.
Verifiche: code noto -> item riscritto + entry MAP-<code>@v; code ignoto ->
fallback literal; senza msc_mapping.yaml il comportamento torna identico.
"""
from __future__ import annotations

import pathlib

import yaml

from app.domain import canonicalize, load_domain_pack

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"

MD = """---
title: Test
id: T-7
lang: en
source_lang: en
servings: 10
---
## Ingredients
- 10 g SALT TABLE {code: CM00591}
- 5 g PEPPERCORN BLACK GROUND {code: CM00878}
- 20 g UNKNOWN ITEM {code: ZZ99999}
- 30 g plain sugar
## Method
1. Mix.
"""


def _pack_with_mapping(mapping: dict[str, str]):
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    shutil.copytree(PACK_DIR, tmp / "ricette", dirs_exist_ok=True)
    (tmp / "ricette" / "msc_mapping.yaml").write_text(
        yaml.safe_dump(mapping), encoding="utf-8")
    return load_domain_pack(str(tmp / "ricette"))


def test_code_first_resolves_known_code() -> None:
    pack = _pack_with_mapping({"CM00591": "salt", "CM00878": "black peppercorns"})
    doc = canonicalize(pack, MD)
    assert "- 10 g salt {code: CM00591}" in doc.canonical_md
    assert "- 5 g black peppercorns {code: CM00878}" in doc.canonical_md


def test_canon_log_has_map_rule() -> None:
    pack = _pack_with_mapping({"CM00591": "salt"})
    doc = canonicalize(pack, MD)
    map_entries = [e for e in doc.log_entries if e.rule_id.startswith("MAP-")]
    assert any(e.rule_id == f"MAP-CM00591@{pack.pack.version}" for e in map_entries)
    assert map_entries[0].before_text == "SALT TABLE"
    assert map_entries[0].after_text == "salt"


def test_unknown_code_falls_back_to_literal() -> None:
    """Code ignoto -> fallback literal (lookup stringa, mai inventato)."""
    pack = _pack_with_mapping({"CM00591": "salt"})
    doc = canonicalize(pack, MD)
    # ZZ99999 non nel mapping: resta il nome grezzo (irrisolto, non inventato)
    assert "- 20 g UNKNOWN ITEM {code: ZZ99999}" in doc.canonical_md
    assert "UNKNOWN ITEM" in doc.unresolved_terms


def test_without_mapping_backward_compatible() -> None:
    """Senza msc_mapping.yaml il comportamento torna identico a prima."""
    import shutil
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    shutil.copytree(PACK_DIR, tmp / "ricette", dirs_exist_ok=True)
    (tmp / "ricette" / "msc_mapping.yaml").unlink()  # rimuovi il mapping
    pack = load_domain_pack(str(tmp / "ricette"))
    assert pack.msc_mapping() == {}
    doc = canonicalize(pack, MD)
    # nessuna riscrittura per code: la stringa resta (irrisolta)
    assert "- 10 g SALT TABLE {code: CM00591}" in doc.canonical_md
    assert not any(e.rule_id.startswith("MAP-") for e in doc.log_entries)
