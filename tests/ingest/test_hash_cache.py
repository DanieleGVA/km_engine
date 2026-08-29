"""Tests for the persistent content-hash cache (FR1.4)."""

from __future__ import annotations

import json

import pytest

from app.ingest.hash_cache import HashCache


def test_get_set_remove_roundtrip(tmp_path) -> None:
    cache = HashCache(tmp_path / "cache.json")
    assert cache.get("missing") is None
    cache.set("a.py", "hash1")
    cache.set("b.py", "hash2")
    assert cache.get("a.py") == "hash1"
    assert cache.get("b.py") == "hash2"
    cache.remove("a.py")
    assert cache.get("a.py") is None
    assert cache.get("b.py") == "hash2"
    # remove di una chiave inesistente non deve sollevare
    cache.remove("nope")


def test_flush_persists_and_reloads(tmp_path) -> None:
    path = tmp_path / "cache.json"
    cache = HashCache(path)
    cache.set("k1", "d1")
    cache.set("k2", "d2")
    cache.flush()
    assert path.exists()

    reloaded = HashCache(path)
    assert reloaded.as_dict() == {"k1": "d1", "k2": "d2"}


def test_flush_creates_parent_directories(tmp_path) -> None:
    path = tmp_path / "nested" / "deep" / "cache.json"
    cache = HashCache(path)
    cache.set("k", "d")
    cache.flush()
    assert path.exists()


def test_corrupted_json_loads_as_empty(tmp_path) -> None:
    path = tmp_path / "cache.json"
    path.write_text("{not valid json", encoding="utf-8")
    cache = HashCache(path)
    assert cache.as_dict() == {}


def test_non_dict_json_loads_as_empty(tmp_path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    cache = HashCache(path)
    assert cache.as_dict() == {}


def test_flush_is_atomic_and_keeps_old_file_on_error(tmp_path) -> None:
    path = tmp_path / "cache.json"
    cache = HashCache(path)
    cache.set("k", "d")
    cache.flush()
    original = path.read_text(encoding="utf-8")

    # Un valore non serializzabile fa fallire json.dump: il file originale
    # deve restare intatto e il tmp deve essere rimosso.
    cache._data["bad"] = object()  # type: ignore[assignment]
    with pytest.raises(TypeError):
        cache.flush()
    assert path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("cache.json.*.tmp"))
