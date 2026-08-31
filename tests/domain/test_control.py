"""Passo 15 PROGRAMMA-UNICO: campione di controllo (30 non segnalate).

Obiettivo: misurare i falsi negativi su ricette che nessuna regola ha
segnalato; campione riproducibile; mai golden o gia' flaggate.
"""
from __future__ import annotations

from app.domain.control import (
    SAMPLE_SIZE,
    draw_control_sample,
    report_baseline,
)

ALL = [f"ric-{i:04d}" for i in range(200)]
FLAGGED = {"ric-0001", "ric-0002", "ric-0003"}
GOLDEN = {"ric-0100", "ric-0101"}


def test_sample_size_and_exclusions() -> None:
    s = draw_control_sample(ALL, FLAGGED, GOLDEN, seed="seed-1")
    assert len(s.recipe_ids) == SAMPLE_SIZE
    assert not (set(s.recipe_ids) & FLAGGED)
    assert not (set(s.recipe_ids) & GOLDEN)


def test_reproducible_with_seed() -> None:
    s1 = draw_control_sample(ALL, FLAGGED, GOLDEN, seed="seed-1")
    s2 = draw_control_sample(ALL, FLAGGED, GOLDEN, seed="seed-1")
    assert s1.recipe_ids == s2.recipe_ids
    # seed diverso => campione diverso
    s3 = draw_control_sample(ALL, FLAGGED, GOLDEN, seed="seed-2")
    assert s1.recipe_ids != s3.recipe_ids


def test_baseline_reported_without_threshold() -> None:
    s = draw_control_sample(ALL, FLAGGED, GOLDEN, seed="seed-1")
    report = report_baseline(s, flagged_in_sample=2)
    assert report["false_negative_rate"] == round(2 / SAMPLE_SIZE, 3)
    assert report["threshold"] is None  # nessuna soglia al primo giro
