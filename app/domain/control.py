"""Passo 15 PROGRAMMA-UNICO: campione di controllo (30 non segnalate).

Misura i falsi negativi dell'intera catena su ricette che nessuna regola ha
segnalato. 30 estratte dalle non-flaggate con seed registrato (riproducibile);
baseline riportata senza soglia al primo giro. Il campione non contiene
ricette gia' nel golden o gia' flaggate.
"""
from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

SAMPLE_SIZE = 30


@dataclass
class ControlSample:
    """Campione di controllo riproducibile."""

    seed: str
    recipe_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"seed": self.seed, "recipe_ids": self.recipe_ids}


def draw_control_sample(
    all_recipe_ids: list[str],
    flagged_ids: set[str],
    golden_ids: set[str],
    seed: str,
    size: int = SAMPLE_SIZE,
) -> ControlSample:
    """Estrae ``size`` ricette dalle non-flaggate e non-golden.

    Seed registrato => riproducibile. Mai ricette gia' flaggate o golden.
    """
    eligible = [
        rid for rid in all_recipe_ids
        if rid not in flagged_ids and rid not in golden_ids
    ]
    rng = random.Random(hashlib.sha256(seed.encode()).digest())
    rng.shuffle(eligible)
    return ControlSample(seed=seed, recipe_ids=eligible[:size])


def report_baseline(sample: ControlSample, flagged_in_sample: int) -> dict:
    """Baseline falsi negativi: riportata senza soglia al primo giro."""
    n = len(sample.recipe_ids)
    return {
        "seed": sample.seed,
        "sample_size": n,
        "flagged_in_sample": flagged_in_sample,
        "false_negative_rate": round(flagged_in_sample / n, 3) if n else 0.0,
        "threshold": None,  # nessuna soglia al primo giro
    }
