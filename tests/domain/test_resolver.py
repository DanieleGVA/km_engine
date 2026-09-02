"""WP-F4 — risoluzione a livelli: un test per livello, e i casi che NON devono risolvere.

Il valore di questo WP non e' la copertura in piu': e' che ogni riga risolta
dica con quale regola. Un livello che risolve troppo (``funghi porcini`` ->
``funghi``) fa salire il numero e peggiora il dato.
"""
from __future__ import annotations

import pytest

from app.domain.normalize import (
    RULE_ALIAS,
    RULE_EXACT,
    RULE_HEAD,
    RULE_UNRESOLVED,
    Resolver,
)


@pytest.fixture(scope="module")
def resolver(pack) -> Resolver:
    return Resolver(pack)


# ---------------------------------------------------------------------------
# Un caso per livello
# ---------------------------------------------------------------------------

def test_f4_level_exact(resolver) -> None:
    resolution = resolver.resolve("garlic")
    assert resolution.rule_id == RULE_EXACT
    assert resolution.label_en == "garlic"
    assert resolution.glossary_id


def test_f4_level_exact_italian_label(resolver) -> None:
    resolution = resolver.resolve("olio extravergine di oliva")
    assert resolution.rule_id == RULE_EXACT
    assert resolution.label_en == "extra virgin olive oil"


def test_f4_level_alias(resolver) -> None:
    resolution = resolver.resolve("olio evo")
    assert resolution.rule_id == RULE_ALIAS
    assert resolution.label_en == "extra virgin olive oil"


def test_f4_level_head_with_state(resolver) -> None:
    """La testa si risolve e il modificatore staccato resta come stato."""
    resolution = resolver.resolve("mandorle dolci sbucciate")
    assert resolution.rule_id == RULE_HEAD
    assert resolution.label_en == "sweet almonds"
    assert resolution.states == ("peeled",)


def test_f4_level_head_with_prep(resolver) -> None:
    """"il succo di 1 limone" e' un limone: prep juice, quantita' 1.

    L'etichetta della preparazione e' in inglese come il resto del canonico:
    la mappa italiano -> inglese sta in ``regole/normalizzazione.yaml``.
    """
    resolution = resolver.resolve("il succo di 1 limone")
    assert resolution.rule_id == RULE_HEAD
    assert resolution.label_en == "lemon"
    assert resolution.prep == "juice"
    assert resolution.qty == "1"


def test_f4_level_unresolved_carries_candidates(resolver) -> None:
    resolution = resolver.resolve("brodo di carne")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.label_en is None
    assert resolution.candidates
    keys = [key for key, _ in resolution.candidates]
    assert any("brodo" in key for key in keys)


# ---------------------------------------------------------------------------
# Cio' che NON deve risolvere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "item",
    [
        "funghi porcini",     # porcini discrimina la specie
        "riso originario",    # originario e' una varieta'
        "pasta sfoglia",      # sfoglia non e' un modificatore
        "farina di mais",     # il mais cambia l'ingrediente
        "brodo di carne",     # carne vs pesce vs vegetale
        "peperone rosso",     # il colore discrimina
    ],
)
def test_f4_head_does_not_overgeneralize(resolver, item) -> None:
    """La lista chiusa dei modificatori e' l'unica difesa: verifichiamola."""
    resolution = resolver.resolve(item)
    assert resolution.rule_id == RULE_UNRESOLVED, (
        f"{item!r} risolto a {resolution.label_en!r} con {resolution.rule_id}: "
        "il livello HEAD sta generalizzando su un aggettivo discriminante"
    )


def test_f4_no_invention_on_unresolved(resolver) -> None:
    """T10: un termine non risolto non porta ne' label ne' glossary_id."""
    resolution = resolver.resolve("xilofono ripieno")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.label_en is None
    assert resolution.glossary_id is None


def test_f4_states_survive_an_unresolved_head(resolver) -> None:
    """Anche quando la testa non risolve, lo stato staccato non si perde."""
    resolution = resolver.resolve("capperi sotto sale")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.states == ("salted",)


def test_f4_generic_modifiers_are_dropped_not_kept(resolver) -> None:
    """"grosso" non e' uno stato: viene staccato e registrato, non conservato."""
    resolution = resolver.resolve("aglio grosso")
    assert resolution.rule_id == RULE_HEAD
    assert resolution.label_en == "garlic"
    assert resolution.states == ()
    assert "grosso" in resolution.dropped


# ---------------------------------------------------------------------------
# Livello fuzzy: soglia e margine
# ---------------------------------------------------------------------------

def test_f4_fuzzy_threshold_is_conservative(pack) -> None:
    """A soglia 0.92 un refuso di una lettera NON basta: meglio irrisolto.

    E' una constatazione, non un difetto: il Jaccard sui trigrammi e' severo
    (``parmigiano regiano`` vs ``parmigiano reggiano`` vale 0.82). Il livello
    esiste e non risolve nulla su questo corpus; abbassare la soglia e'
    vietato dal piano perche' aprirebbe a risoluzioni sbagliate.
    """
    resolver = Resolver(pack)
    assert resolver.fuzzy_threshold >= 0.92
    assert resolver.fuzzy_margin >= 0.05
    assert resolver.resolve("parmigiano regiano").rule_id == RULE_UNRESOLVED


def test_f4_fuzzy_margin_blocks_ambiguous_pairs(pack) -> None:
    """Con due candidati vicini non si sceglie: si lascia irrisolto."""
    resolver = Resolver(pack, fuzzy_threshold=0.3, fuzzy_margin=0.05)
    # "brodo" e' quasi equidistante da "brodo di pesce" e "brodo vegetale"
    assert resolver.resolve("brodo").rule_id == RULE_UNRESOLVED


def test_f4_fuzzy_resolves_when_one_candidate_is_clearly_closer(pack) -> None:
    """Sotto soglia abbassata e con margine, il livello fuzzy funziona."""
    resolver = Resolver(pack, fuzzy_threshold=0.6, fuzzy_margin=0.05)
    resolution = resolver.resolve("parmigiano regiano")
    assert resolution.rule_id == "GLOSS-FUZZY"
    assert resolution.label_en
    assert resolution.needs_review is True


def test_f4_resolver_reads_thresholds_from_the_pack(pack) -> None:
    """Soglia e margine sono dati del pack, non costanti nel codice."""
    rules = pack.rules["normalizzazione"]
    resolver = Resolver(pack)
    assert resolver.fuzzy_threshold == rules["fuzzy_threshold"]
    assert resolver.fuzzy_margin == rules["fuzzy_margin"]
    assert rules["modifiers"]
    assert rules["prep_prefixes"]


def test_f4_states_only_decide_preservation_not_resolution(pack) -> None:
    """Il glossario "stati" non aggiunge copertura: la aggiunge la lista chiusa.

    Se un modificatore non e' nella lista di ``normalizzazione.yaml``, avere
    la voce in ``stati`` non lo rende staccabile.
    """
    resolver = Resolver(pack)
    state_labels = {
        entry.labels_it for entry in pack.glossaries.stati.entries
    }
    modifiers = {m.casefold() for m in pack.rules["normalizzazione"]["modifiers"]}
    # "al dente" e' uno stato ma non un modificatore di ingrediente
    assert "al dente" in state_labels
    assert "al dente" not in modifiers
    assert resolver.resolve("pasta al dente").rule_id == RULE_UNRESOLVED
