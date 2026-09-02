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
    normalize_key,
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
    resolution = resolver.resolve("concentrato di pomodoro")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.label_en is None
    assert resolution.candidates
    keys = [key for key, _ in resolution.candidates]
    assert any("pomodoro" in key for key in keys)


# ---------------------------------------------------------------------------
# Cio' che NON deve risolvere
# ---------------------------------------------------------------------------

# (termine specifico, testa generica che NON deve assorbirlo)
DISCRIMINANT_CASES = [
    ("funghi porcini", "funghi"),      # porcini discrimina la specie
    ("riso originario", "riso"),       # originario e' una varieta'
    ("pasta sfoglia", "pasta"),        # sfoglia non e' un modificatore
    ("farina di mais", "farina"),      # il mais cambia l'ingrediente
    ("brodo di carne", "brodo"),       # carne vs pesce vs vegetale
    ("peperone rosso", "peperone"),    # il colore discrimina
]


@pytest.mark.parametrize(("item", "generic"), DISCRIMINANT_CASES)
def test_f4_head_does_not_overgeneralize(resolver, item, generic) -> None:
    """Un aggettivo discriminante non deve far collassare il termine sulla testa.

    L'invariante non e' "resta irrisolto": e' "non diventa il termine generico".
    Quando il glossario ha la voce specifica (``funghi porcini`` ->
    ``porcini mushroom``) risolvere e' giusto; quello che non deve succedere e'
    che il livello HEAD stacchi ``porcini`` e restituisca ``funghi``.
    """
    specific = resolver.resolve(item)
    generic_resolution = resolver.resolve(generic)

    if specific.rule_id == RULE_UNRESOLVED:
        return  # nessuna voce: irrisolto e' l'esito corretto
    assert specific.rule_id != RULE_HEAD or not generic_resolution.resolved, (
        f"{item!r} risolto per HEAD mentre {generic!r} e' un termine noto: "
        "il modificatore discriminante e' stato staccato"
    )
    if generic_resolution.resolved:
        assert specific.label_en != generic_resolution.label_en, (
            f"{item!r} e {generic!r} risolvono entrambi a "
            f"{specific.label_en!r}: la distinzione e' persa"
        )


def test_f4_no_invention_on_unresolved(resolver) -> None:
    """T10: un termine non risolto non porta ne' label ne' glossary_id."""
    resolution = resolver.resolve("xilofono ripieno")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.label_en is None
    assert resolution.glossary_id is None


def test_f4_states_survive_an_unresolved_head(resolver) -> None:
    """Anche quando la testa non risolve, lo stato staccato non si perde."""
    resolution = resolver.resolve("farina di mais secca")
    assert resolution.rule_id == RULE_UNRESOLVED
    assert resolution.states == ("dried",)


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
    # "ginepro" e' quasi equidistante da "ginger" e "ground ginger"
    assert resolver.resolve("ginepro").rule_id == RULE_UNRESOLVED


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


# ---------------------------------------------------------------------------
# WP-F7 — i modificatori valgono anche nella forma inglese
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("translated_item", "head", "state"),
    [
        ("butter clarified", "butter", "clarified"),
        ("tomato vine-ripened", "tomato", "vine-ripened"),
        ("bread stale", "bread", "stale"),
        ("pea shelled", "pea", "shelled"),
        ("zucchini new", "zucchini", "new"),
    ],
)
def test_f7_modifiers_work_on_the_translated_item(
    resolver, translated_item, head, state
) -> None:
    """Il resolver gira sul documento TRADOTTO, dove i modificatori sono inglesi.

    La lista in ``normalizzazione.yaml`` e' dichiarata in italiano; senza la
    forma inglese il livello HEAD non staccava nulla e la testa non si
    risolveva. Era D2 che ricompariva a un terzo livello: una tabella che
    conosce un solo lato del confronto.
    """
    resolution = resolver.resolve(translated_item)
    assert resolution.rule_id == RULE_HEAD, translated_item
    assert resolution.label_en == head
    assert state in resolution.states


def test_f7_english_forms_do_not_widen_the_closed_list(pack) -> None:
    """Si aggiunge solo la traduzione di modificatori GIA' ammessi.

    La difesa contro la generalizzazione resta: un aggettivo discriminante non
    diventa staccabile solo perche' e' inglese.
    """
    resolver = Resolver(pack)
    declared = {
        normalize_key(m) for m in pack.rules["normalizzazione"]["modifiers"]
    }
    states_by_it = {}
    for entry in pack.glossaries.stati.entries:
        for term in (entry.labels_it, *entry.aliases):
            states_by_it[normalize_key(term)] = normalize_key(entry.labels_en)
    expected = declared | {
        states_by_it[key] for key in declared if key in states_by_it
    }
    assert set(resolver._modifiers) == {key for key in expected if key}

    # nessun discriminante e' entrato dalla porta di servizio
    for discriminant in ("porcini", "sfoglia", "rosso", "di mais"):
        assert normalize_key(discriminant) not in resolver._modifiers
