"""Web UI minima di adjudication (WP-E6, GE6).

Serve una pagina HTML statica (``app/api/templates/adjudication.html``) e i
POST di approve/reject per le tre code:

- code L3 (``adjudications``)
- proposte glossario (``glossary_proposals``)
- conflitti (``conflicts``)

L'accesso richiede ruolo ``admin`` o ``editor``. Le azioni riusano i servizi
esistenti (``app.domain.verify`` e ``app.conflict``): la UI non introduce
logica di business, solo un'interfaccia umana.
"""
from __future__ import annotations

import html
import threading
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.config import AuthSettings, get_auth_settings
from app.auth.db import connect
from app.auth.deps import Principal, auth_required
from app.conflict import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    ConflictResolutionError,
    InvalidChoiceError,
    approve_conflict,
    list_conflicts,
    reject_conflict,
)
from app.domain.errors import (
    AdjudicationAlreadyResolvedError,
    AdjudicationNotFoundError,
    GlossaryProposalAlreadyResolvedError,
    GlossaryProposalNotFoundError,
)
from app.domain.verify import (
    decide_adjudication,
    decide_glossary_proposal,
    list_adjudications,
    list_glossary_proposals,
)
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

router = APIRouter(prefix="/ui", tags=["ui"])

TEMPLATE_PATH = Path(__file__).parent / "templates" / "adjudication.html"

_neo4j_client: Neo4jClient | None = None
_neo4j_lock = threading.Lock()


def _get_neo4j_client() -> Neo4jClient:
    """Singleton Neo4j client for the UI (same pattern as app.api.app)."""
    global _neo4j_client
    if _neo4j_client is None:
        with _neo4j_lock:
            if _neo4j_client is None:
                _neo4j_client = Neo4jClient.from_env()
    return _neo4j_client


async def _require_adjudicator(request: Request) -> Principal:
    """Body-free auth dependency: admin o editor."""
    principal = await auth_required(request)
    if not {"admin", "editor"}.intersection(principal.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accesso negato: richiesto ruolo admin o editor.",
        )
    return principal


def _esc(value: object) -> str:
    return html.escape(str(value or ""))


def _render_table(rows: list[str]) -> str:
    if not rows:
        return '<p class="empty">Nessun elemento pending.</p>'
    return (
        "<table><tr><th>ID</th><th>Dettaglio</th><th>Azioni</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def _adjudication_rows(items: list[dict]) -> list[str]:
    rows: list[str] = []
    for item in items:
        if item["status"] != "pending":
            continue
        if item.get("kind") == "dictionary":
            # scheda dizionario (passo 6): mostra la proposta standardizzata
            v = item.get("verdict_json") or {}
            detail = (
                f"<b>dizionario</b> · key={_esc(item['document_id'])}<br>"
                f"canonical={_esc(v.get('canonical_name_en', ''))} · "
                f"core={_esc(v.get('ingredient_core', ''))} · "
                f"class={_esc(v.get('class', ''))}<br>"
                f"aliases={_esc(', '.join(v.get('aliases', [])))} · "
                f"allergens={_esc(', '.join(v.get('allergen_tags', [])))} · "
                f"conf={_esc(v.get('confidence'))}"
            )
        else:
            detail = (
                f"document={_esc(item['document_id'])} · section={_esc(item['section'])}<br>"
                f"reason={_esc(item['reason'])}<br>suggestion={_esc(item['suggestion'])}"
            )
        actions = (
            f'<form method="post" action="/ui/adjudications/{item["id"]}/approve">'
            f'<button class="approve">Approve</button></form>'
            f'<form method="post" action="/ui/adjudications/{item["id"]}/reject">'
            f'<button class="reject">Reject</button></form>'
        )
        rows.append(f"<tr><td>{item['id']}</td><td>{detail}</td><td>{actions}</td></tr>")
    return rows


def _proposal_rows(items: list[dict]) -> list[str]:
    rows: list[str] = []
    for item in items:
        if item["status"] != "pending":
            continue
        detail = f"term={_esc(item['term'])}<br>context={_esc(item['context'])}"
        actions = (
            f'<form method="post" action="/ui/glossary-proposals/{item["id"]}/approve">'
            f'<button class="approve">Approve</button></form>'
            f'<form method="post" action="/ui/glossary-proposals/{item["id"]}/reject">'
            f'<button class="reject">Reject</button></form>'
        )
        rows.append(f"<tr><td>{item['id']}</td><td>{detail}</td><td>{actions}</td></tr>")
    return rows


def _conflict_rows(items: list[dict]) -> list[str]:
    rows: list[str] = []
    for item in items:
        if item["status"] != "pending":
            continue
        detail = (
            f"entity={_esc(item['entity_id'])} · property={_esc(item['property'])}<br>"
            f"A: {_esc(item['value_a'])} (source {_esc(item['source_a'])})<br>"
            f"B: {_esc(item['value_b'])} (source {_esc(item['source_b'])})<br>"
            f"suggestion={_esc(item['suggestion'])}"
        )
        actions = (
            f'<form method="post" action="/ui/conflicts/{item["id"]}/approve">'
            f'<select name="choice"><option value="a">A</option><option value="b">B</option></select>'
            f'<button class="approve">Approve</button></form>'
            f'<form method="post" action="/ui/conflicts/{item["id"]}/reject">'
            f'<button class="reject">Reject</button></form>'
        )
        rows.append(f"<tr><td>{item['id']}</td><td>{detail}</td><td>{actions}</td></tr>")
    return rows


def _render_page(
    *,
    adjudications: list[dict],
    proposals: list[dict],
    conflicts: list[dict],
    error: str | None = None,
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{ERROR}}", f'<div class="error">{_esc(error)}</div>' if error else "")
        .replace("{{ADJUDICATIONS}}", _render_table(_adjudication_rows(adjudications)))
        .replace("{{PROPOSALS}}", _render_table(_proposal_rows(proposals)))
        .replace("{{CONFLICTS}}", _render_table(_conflict_rows(conflicts)))
    )


def _pg_conn(settings: AuthSettings | None = None) -> psycopg.Connection:
    return connect(settings or get_auth_settings())


@router.get("/", response_class=HTMLResponse)
async def ui_home(
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> HTMLResponse:
    """Pagina principale con le tre code pending."""
    del principal
    with _pg_conn() as conn:
        adjudications = list_adjudications(conn, status="pending")
        proposals = list_glossary_proposals(conn, status="pending")
        conflicts = list_conflicts(conn, status="pending")
    return HTMLResponse(_render_page(adjudications=adjudications, proposals=proposals, conflicts=conflicts))


@router.post("/adjudications/{item_id}/approve")
async def ui_adjudication_approve(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    try:
        with _pg_conn() as conn:
            decide_adjudication(conn, item_id, "approved", principal.user_id)
    except AdjudicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdjudicationAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)


@router.post("/adjudications/{item_id}/reject")
async def ui_adjudication_reject(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    try:
        with _pg_conn() as conn:
            decide_adjudication(conn, item_id, "rejected", principal.user_id)
    except AdjudicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AdjudicationAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)


@router.post("/glossary-proposals/{item_id}/approve")
async def ui_proposal_approve(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    try:
        with _pg_conn() as conn:
            decide_glossary_proposal(conn, item_id, "approved", principal.user_id)
    except GlossaryProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GlossaryProposalAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)


@router.post("/glossary-proposals/{item_id}/reject")
async def ui_proposal_reject(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    try:
        with _pg_conn() as conn:
            decide_glossary_proposal(conn, item_id, "rejected", principal.user_id)
    except GlossaryProposalNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GlossaryProposalAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)


@router.post("/conflicts/{item_id}/approve")
async def ui_conflict_approve(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    form = await request.form()
    choice = str(form.get("choice", "a"))
    if choice not in ("a", "b"):
        raise HTTPException(status_code=422, detail="choice must be 'a' or 'b'")
    repo = GraphRepository(_get_neo4j_client())
    try:
        with _pg_conn() as conn:
            approve_conflict(repo, conn, item_id, choice, principal.user_id)
    except ConflictNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (InvalidChoiceError, ConflictResolutionError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)


@router.post("/conflicts/{item_id}/reject")
async def ui_conflict_reject(
    item_id: int,
    request: Request,
    principal: Annotated[Principal, Depends(_require_adjudicator)],
) -> RedirectResponse:
    try:
        with _pg_conn() as conn:
            reject_conflict(conn, item_id, principal.user_id)
    except ConflictNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictAlreadyResolvedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/ui/", status_code=303)
