"""Normalizzazione ingredienti CalcMenu -> termini canonici del glossario.

Strategia (come richiesto):
1. passaggio DETERMINISTICO: rimozione qualificatori industriali
   (fresh/frz/ground/sliced/table/5kg/...), pattern speciali
   (herb X fresh -> X, oil X -> X oil), match sul vocabolario canonico
   del glossario (labels_en)
2. fallback LLM nei casi DUBBI: l'LLM mappa il nome industriale al termine
   canonico piu' vicino (vocabolario chiuso, temperature 0)
3. cache su file JSON: ogni nome unico viene chiesto all'LLM una sola volta

La normalizzazione e' applicata PRIMA della canonicalizzazione del dominio:
i nomi industriali diventano termini canonici, cosi' l'impronta ingredienti
delle ricette da validare combacia con quella del knowledge.
"""
from __future__ import annotations

import json
import pathlib
import re

# Qualificatori industriali CalcMenu da rimuovere (ordine: piu' specifici prima)
QUALIFIERS = [
    r"\bgluten free\b", r"\bfor pastry\b", r"\blong life\b", r"\bextra virgin\b",
    r"\bunsalted\b", r"\bseedless\b", r"\bpowdered?\b", r"\bgranulated\b",
    r"\bpastorized\b", r"\bpeeled\b", r"\bsliced\b", r"\bground\b",
    r"\bfrozen\b", r"\bfresh\b", r"\bdried\b", r"\bwhole\b", r"\bcanned\b",
    r"\bchilled\b", r"\braw\b", r"\bcooked\b", r"\bboiled\b", r"\btoasted\b",
    r"\broasted\b", r"\bgrilled\b", r"\bminced\b", r"\bchopped\b", r"\bdiced\b",
    r"\bcrushed\b", r"\bfinely\b", r"\bcoarsely\b", r"\broughly\b",
    r"\btable\b", r"\bbunch\b", r"\bleaves?\b", r"\broot\b", r"\bstalk\b",
    r"\bseeds?\b", r"\bpowder\b", r"\bflakes?\b", r"\bpieces?\b",
    r"\bhalves?\b", r"\bquarters?\b", r"\bwedges?\b", r"\bjuice\b",
    r"\bpeeled\b", r"\btrimmed\b", r"\bcleaned\b", r"\bwashed\b",
    r"\b5kg\b", r"\b82%\b", r"\b36%\b", r"\b3,5%\b", r"\b150/170g\b",
    r"\b7-8oz\b", r"\b1for1\b", r"\b1/1\b", r"\bpet\b", r"\buht\b",
    r"\bglass\b", r"\bdispenser\b", r"\bcup\b", r"\bfor frying\b",
    r"\bfor cooking\b", r"\bfor salad\b", r"\bfor dessert\b",
    r"\bculinary\b", r"\bword order\b", r"\bno derived figure\b",
]

# Pattern speciali: (regex, sostituzione)
SPECIAL_PATTERNS = [
    (re.compile(r"^herbs?\s+(.+?)\s+fresh$"), r"\1"),          # herb thyme fresh -> thyme
    (re.compile(r"^herbs?\s+(.+?)\s+dried$"), r"\1"),          # herb bay leaves dried -> bay leaves
    (re.compile(r"^peppercorn\s+(.+?)\s+ground$"), r"\1 peppercorn"),  # peppercorn black ground -> black peppercorn
    (re.compile(r"^oil\s+(.+)$"), r"\1 oil"),                   # oil olive -> olive oil
    (re.compile(r"^tomatoes?\s+(.+?)\s+fresh$"), r"\1 tomatoes"),  # tomatoes cherry red fresh -> cherry red tomatoes
    (re.compile(r"^onion\s+(.+?)\s+fresh$"), r"\1 onion"),     # onion yellow fresh -> yellow onion
    (re.compile(r"^peppers?\s+(.+?)\s+fresh$"), r"\1 peppers"),  # peppers bell red fresh -> bell red peppers
    (re.compile(r"^milk\s+(.+)$"), r"\1 milk"),                  # milk whole 3,5% fat -> milk
    (re.compile(r"^cream\s+(.+)$"), r"\1 cream"),                # cream heavy cooking -> cream
    (re.compile(r"^butter\s+(.+)$"), r"butter"),                  # butter unsalted 5kg -> butter
    (re.compile(r"^eggs?\s+(.+)$"), r"eggs"),                     # eggs whole pastorized -> eggs
    (re.compile(r"^wine\s+(.+)$"), r"\1 wine"),                   # wine white -> white wine
    (re.compile(r"^juice\s+(.+)$"), r"\1 juice"),                 # juice lemon -> lemon juice
    (re.compile(r"^stock\s+(.+)$"), r"\1 stock"),                 # stock vegetable -> vegetable stock
    (re.compile(r"^base\s+(.+)$"), r"\1 base"),                   # base holland sauce -> holland sauce base
    (re.compile(r"^dressing\s+(.+)$"), r"\1 dressing"),           # dressing caesar salad -> caesar salad dressing
]


def _strip_qualifiers(name: str) -> str:
    for q in QUALIFIERS:
        name = re.sub(q, " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_deterministic(name: str) -> str:
    """Normalizzazione deterministica: parentesi, qualificatori, pattern."""
    n = name.lower().strip()
    n = re.sub(r"\([^)]*\)", " ", n)          # (VEGAN), (YC&MDR 2024)
    n = re.sub(r"[^a-z0-9%\s/'-]", " ", n)      # punteggiatura
    n = re.sub(r"\s+", " ", n).strip()
    for pat, repl in SPECIAL_PATTERNS:
        m = pat.match(n)
        if m:
            n = re.sub(pat, repl, n)
            break
    n = _strip_qualifiers(n)
    return n


def match_glossary(normalized: str, canonical_labels: set[str]) -> str | None:
    """Match sul vocabolario canonico: esatto, poi Jaccard con preferenza
    per le label piu' corte (termine canonico piu' specifico)."""
    if normalized in canonical_labels:
        return normalized
    toks = set(normalized.split())
    if not toks:
        return None
    best, best_score, best_len = None, 0.0, 10**9
    for label in canonical_labels:
        ltoks = set(label.split())
        inter = len(toks & ltoks)
        union = len(toks | ltoks)
        score = inter / union if union else 0.0
        # preferisce: punteggio piu' alto, poi label piu' corta
        if score > best_score or (score == best_score and len(ltoks) < best_len):
            best, best_score, best_len = label, score, len(ltoks)
    # soglia: almeno il 50% dei token del nome normalizzato deve combaciare
    if best_score >= 0.5 and best is not None:
        return best
    return None


VOCAB_PATH = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette" / "calcmenu_vocab.json"


def load_canonical_vocab() -> set[str]:
    """Vocabolario canonico: glossario + item knowledge (file calcmenu_vocab.json)."""
    if VOCAB_PATH.exists():
        try:
            return set(json.loads(VOCAB_PATH.read_text()))
        except Exception:
            pass
    return set()


class CalcMenuNormalizer:
    """Normalizza nomi ingredienti CalcMenu verso il vocabolario canonico.

    Deterministico prima; LLM nei casi dubbi (nessun match confidente).
    Cache su file JSON: ogni nome unico chiesto all'LLM una sola volta.
    """

    def __init__(self, canonical_labels: set[str] | None = None, llm=None, cache_path: pathlib.Path | None = None):
        self.canonical_labels = canonical_labels if canonical_labels is not None else load_canonical_vocab()
        self.canonical_labels = canonical_labels
        self.llm = llm
        self.cache_path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path and cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except Exception:
                self.cache = {}

    def normalize(self, name: str) -> tuple[str, str]:
        """Ritorna (termine canonico, metodo: 'deterministic'|'llm'|'identity')."""
        n = normalize_deterministic(name)
        if not n:
            return name.lower(), "identity"
        hit = match_glossary(n, self.canonical_labels)
        if hit:
            return hit, "deterministic"
        # fallback LLM (con cache)
        if name in self.cache:
            return self.cache[name], "llm-cache"
        if self.llm is not None:
            mapped = self._ask_llm(name, n)
            if mapped:
                self.cache[name] = mapped
                self._save_cache()
                return mapped, "llm"
        return n, "identity"

    def _ask_llm(self, original: str, normalized: str) -> str | None:
        vocab = ", ".join(sorted(self.canonical_labels))
        prompt = (
            "You are a culinary ingredient normalizer for a recipe knowledge base.\n"
            f"Map the industrial ingredient name to the closest canonical culinary term.\n"
            f"Industrial name: {original!r} (normalized: {normalized!r})\n"
            "Choose ONLY from this vocabulary (return exactly one term, no explanation):\n"
            f"{vocab}\n"
            "If nothing fits, return the closest term anyway."
        )
        try:
            import asyncio
            text = asyncio.run(self.llm.translate(prompt, source_lang="en", target_lang="en"))
            text = text.strip().strip("\"'.,").lower()
            # valida: deve essere nel vocabolario o molto vicino
            if text in self.canonical_labels:
                return text
            hit = match_glossary(text, self.canonical_labels)
            return hit or text
        except Exception:
            return None

    def _save_cache(self) -> None:
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1))
            except Exception:
                pass
