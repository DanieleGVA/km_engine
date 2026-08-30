"""WP-C2 designer tests: valid draft pack + non-bypassable human gate."""
from __future__ import annotations

import pytest

from app.agents import DesignError, design_pack
from app.agents.designer import _safe_write
from app.agents.models import DomainBrief
from app.domain import load_domain_pack
from tests.agents.conftest import PACK_DIR


def test_ic_designer_produces_valid_pack(tmp_path, pilot_brief: DomainBrief) -> None:
    """The draft pack validates against the app.domain.pack pydantic schema."""
    result = design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    bundle = load_domain_pack(result.staging_dir)
    assert bundle.pack.name == "ricette"
    assert bundle.pack.glossaries == ["tecnica", "ingredienti", "stati"]
    assert len(bundle.glossary_entries()) == result.glossary_entries
    assert len(bundle.units) == result.unit_rules


def test_ic_designer_writes_only_inside_staging(tmp_path, pilot_brief: DomainBrief) -> None:
    """Every generated file lives under the staging directory."""
    staging = tmp_path / "draft"
    result = design_pack(pilot_brief, staging_dir=staging)
    for path in result.files:
        assert path.is_relative_to(staging.resolve())


def test_ic_designer_does_not_touch_manual_pack(tmp_path, pilot_brief: DomainBrief) -> None:
    """The production pack is never written by the designer."""
    before = (PACK_DIR / "pack.yaml").read_text(encoding="utf-8")
    design_pack(pilot_brief, staging_dir=tmp_path / "draft")
    after = (PACK_DIR / "pack.yaml").read_text(encoding="utf-8")
    assert before == after


def test_ic_designer_rejects_production_dir(pilot_brief: DomainBrief) -> None:
    """The human gate refuses the production pack dir as a staging target."""
    with pytest.raises(DesignError):
        design_pack(pilot_brief, staging_dir=PACK_DIR)


def test_ic_designer_rejects_path_escape(tmp_path) -> None:
    """A path traversal write is refused (negative test)."""
    root = tmp_path / "draft"
    root.mkdir()
    with pytest.raises(DesignError):
        _safe_write(root, "../evil.yaml", "x: 1")
    assert not (tmp_path / "evil.yaml").exists()


def test_ic_designer_rejects_symlink_escape(tmp_path) -> None:
    """A symlink inside staging pointing outside is refused (negative test)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = tmp_path / "draft"
    staging.mkdir()
    (staging / "glossari").mkdir()
    (staging / "glossari" / "ingredienti.yaml").symlink_to(outside / "leak.yaml")

    with pytest.raises(DesignError):
        _safe_write(staging, "glossari/ingredienti.yaml", "x: 1")
    assert not (outside / "leak.yaml").exists()


def test_ic_designer_deterministic(tmp_path, pilot_brief: DomainBrief) -> None:
    """Two design runs produce byte-identical pack contents."""
    first = design_pack(pilot_brief, staging_dir=tmp_path / "a")
    second = design_pack(pilot_brief, staging_dir=tmp_path / "b")

    first_files = {p.relative_to(first.staging_dir): p.read_bytes() for p in first.files}
    second_files = {p.relative_to(second.staging_dir): p.read_bytes() for p in second.files}
    assert first_files == second_files


def test_ic_designer_requires_overwrite_for_nonempty(tmp_path, pilot_brief: DomainBrief) -> None:
    """A non-empty staging dir is not silently overwritten."""
    staging = tmp_path / "draft"
    design_pack(pilot_brief, staging_dir=staging)
    with pytest.raises(DesignError):
        design_pack(pilot_brief, staging_dir=staging)
