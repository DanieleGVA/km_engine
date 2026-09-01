"""Passo 16 PROGRAMMA-UNICO: end-to-end su batch reale con gate chef.

La catena completa e' spiegata e reversibile: per ogni modifica applicata
esiste la riga di log che la giustifica e la via del ritorno. Nessuna
modifica applicata risale a un verdetto non approvato.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import psycopg

from app.domain.canon_judge import ComponentVerdict
from app.domain.llm import LLMClient
from app.domain.pack import DomainPackBundle
from app.domain.routing import K3Result, route_k3


@dataclass
class E2EResult:
    """Esito della run end-to-end su un batch."""

    processed: int = 0
    components: int = 0
    skipped: int = 0
    batch_approved: int = 0
    human: int = 0
    canon_gap: int = 0
    log_entries: int = 0
    errors: list[str] = field(default_factory=list)
    coverage_problems: list[str] = field(default_factory=list)
    rollback_ok: bool = True
    report: dict = field(default_factory=dict)


def _card_components(card_md: str) -> list[tuple[str, list[str]]]:
    """Componenti di una card: (nome, righe) dal canonical md."""
    components: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in card_md.splitlines():
        if line.startswith("- "):
            if current is None:
                current = ("main", [])
            current[1].append(line)
        elif line.startswith("## "):
            if current is not None:
                components.append(current)
            current = None
    if current is not None:
        components.append(current)
    return components or [("main", [])]


def verify_log_coverage(
    input_md: str, output_md: str, log_entries: list[dict[str, Any]]
) -> list[str]:
    """Il diff(input, output) e' interamente coperto dai log (nessuna
    differenza orfana)."""
    problems: list[str] = []
    in_lines = set(input_md.splitlines())
    out_lines = set(output_md.splitlines())
    changed = (in_lines - out_lines) | (out_lines - in_lines)
    logged = set()
    for e in log_entries:
        logged.add(e.get("before_text", ""))
        logged.add(e.get("after_text", ""))
    orphans = changed - logged
    if orphans:
        problems.append(f"differenze orfane: {sorted(orphans)[:5]}")
    return problems


def rollback_from_log(
    log_entries: list[dict[str, Any]], output_md: str
) -> str:
    """Rollback da log: applica le entry in ordine inverso per ricostruire
    l'originale."""
    md = output_md
    for e in reversed(log_entries):
        before = e.get("before_text", "")
        after = e.get("after_text", "")
        if after and after in md:
            md = md.replace(after, before, 1)
    return md


async def run_e2e_batch(
    judge: LLMClient,
    cards: list[dict[str, Any]],
    pack: DomainPackBundle,
    conn: psycopg.Connection,
    *,
    limit: int | None = None,
) -> E2EResult:
    """Esegue la catena su un batch di card e scrive il log dei verdetti
    approvati (canon_adjudication_log)."""
    result = E2EResult()
    batch = cards[:limit] if limit else cards
    for card in batch:
        result.processed += 1
        card_md = card.get("canonical_md", "")
        # Componenti espliciti (nome + righe + candidati propri) se forniti
        # dal chiamante; altrimenti fallback: card intera come unico
        # componente "main" con i candidati della card.
        components = card.get("components") or [
            {"name": name, "lines": lines, "candidates": card.get("candidates", [])}
            for name, lines in _card_components(card_md)
        ]
        for comp in components:
            result.components += 1
            comp_name = comp["name"]
            comp_lines = comp["lines"]
            candidates = comp.get("candidates", card.get("candidates", []))
            section = f"component:{comp_name}"
            # Resume: componente gia' giudicato in una run precedente
            # (coda umana o log) => salta, non duplica.
            if _already_judged(conn, card["id"], section):
                result.skipped += 1
                continue
            try:
                k3: K3Result = await route_k3(
                    judge, comp_name, comp_lines, candidates
                )
            except Exception as exc:  # noqa: BLE001 - una card non deve uccidere il batch
                result.errors.append(f"{card['id']}:{comp_name}: {exc!r}")
                continue
            if k3.route == "batch_approve":
                result.batch_approved += 1
                verdict = k3.runs[0]
                _write_log(conn, card["id"], comp_name, verdict, k3)
                result.log_entries += 1
            elif k3.route == "canon_gap":
                result.canon_gap += 1
                _enqueue_human(conn, card["id"], comp_name, k3, "canon_gap")
            else:
                result.human += 1
                _enqueue_human(conn, card["id"], comp_name, k3, "divergent")
    result.report = {
        "processed": result.processed,
        "components": result.components,
        "skipped": result.skipped,
        "batch_approved": result.batch_approved,
        "human": result.human,
        "canon_gap": result.canon_gap,
        "log_entries": result.log_entries,
        "errors": result.errors[:20],
    }
    return result


def _enqueue_human(
    conn: psycopg.Connection,
    document_id: str,
    component: str,
    k3: K3Result,
    reason: str,
) -> None:
    """Accoda il verdetto non approvato alla coda umana (kind='canon')."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO adjudications
                (document_id, section, reason, suggestion, kind, verdict_json,
                 llm_model, llm_confidence, candidate_ids)
            VALUES (%s, %s, %s, %s, 'canon', %s, %s, %s, %s)
            """,
            (
                document_id,
                f"component:{component}",
                f"verdetto {reason} (k=3)",
                k3.runs[0].motivation if k3.runs else None,
                json.dumps(k3.runs[0].model_dump()) if k3.runs else None,
                "judge",
                k3.runs[0].confidence if k3.runs else None,
                k3.permutations[0] if k3.permutations else [],
            ),
        )


def _write_log(
    conn: psycopg.Connection,
    document_id: str,
    component: str,
    verdict: ComponentVerdict,
    k3: K3Result,
) -> None:
    """Scrive il verdetto approvato in canon_adjudication_log (reversibile)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO canon_adjudication_log
                (document_id, section, kind, verdict_json, llm_model,
                 llm_confidence, candidate_ids)
            VALUES (%s, %s, 'canon', %s, %s, %s, %s)
            """,
            (
                document_id,
                f"component:{component}",
                json.dumps(verdict.model_dump()),
                "judge",
                verdict.confidence,
                k3.permutations[0] if k3.permutations else [],
            ),
        )


def _already_judged(
    conn: psycopg.Connection, document_id: str, section: str
) -> bool:
    """Resume: il componente e' gia' stato giudicato (coda umana o log)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM adjudications
                WHERE document_id = %s AND section = %s AND kind = 'canon'
                UNION ALL
                SELECT 1 FROM canon_adjudication_log
                WHERE document_id = %s AND section = %s
            )
            """,
            (document_id, section, document_id, section),
        )
        return bool(cur.fetchone()[0])
