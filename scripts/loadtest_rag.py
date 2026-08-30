#!/usr/bin/env python3
"""Load test RAG retrieval — 100 utenti concorrenti (WP-B5, gate GB5, NFR1).

Mix per utente (10 richieste dopo login reale, come ``scripts/loadtest.py``):

- 50% ``POST /api/v1/rag/query`` (query dal golden pilot, 120 coppie)
- 30% ``GET  /api/v1/entities``
- 20% ``GET  /api/v1/glossary/query``

Setup automatico (default ``--create-data``):

- 100 utenti ``ib5_load_*`` in Postgres (hash argon2id reale, ruolo viewer)
- 70 documenti ``ib5_load_*`` in Neo4j (titoli reali dal corpus pilota) con
  embedding deterministici popolati, entita' collegate e termini glossario
  (per dare risultati a /glossary/query)

Cleanup automatico a fine test (``--no-cleanup`` per conservare i dati).

Report: p50/p95/p99 per endpoint + errori; stampato a video e, con
``--report``, appeso a ``docs/benchmark-report.md`` (sezione Iterazione B).

Uso:
    uv run python scripts/loadtest_rag.py --base-url http://localhost:8000 --users 100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PREFIX = "ib5_load_"
PASSWORD = "ib5-load-password-123"  # >= 12 char (ADR-002 D5)
REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_ricette"
GOLDEN_PATH = REPO_ROOT / "tests" / "fixtures" / "rag_golden.json"
DOCS = 70
GLOSSARY_TERMS = ("garlic", "tomato", "olive oil", "basil", "parmesan", "flour")


# --------------------------------------------------------------------------- setup
def create_users(n: int) -> None:
    """Crea n utenti ib5_load_* in Postgres con hash reale (idempotente)."""
    import psycopg

    from app.auth.users import create_user

    dsn = os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        for i in range(n):
            create_user(
                conn,
                f"{PREFIX}u{i:03d}",
                f"{PREFIX}u{i:03d}@test.local",
                PASSWORD,
                roles=("viewer",),
            )


def _corpus_titles() -> list[str]:
    """Titoli reali dei primi DOCS file del corpus pilota (deterministico)."""
    titles: list[str] = []
    for path in sorted(CORPUS_DIR.glob("ric-*.md"))[:DOCS]:
        text = path.read_text(encoding="utf-8")
        match = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
        titles.append(match.group(1).strip() if match else path.stem)
    return titles


def create_documents() -> None:
    """Crea DOCS documenti ib5_load_* + entita' + termini glossario + embedding."""
    from app.rag.rag import build_embedding_from_graph, populate_embeddings
    from app.storage.client import Neo4jClient

    client = Neo4jClient.from_env()
    try:
        with client.session() as session:
            # Termini glossario pubblici (per /glossary/query).
            for i, label in enumerate(GLOSSARY_TERMS):
                term_id = f"ingredienti:IB5-{label.upper().replace(' ', '-')}"
                session.run(
                    """
                    MERGE (t:CanonicalTerm {id: $id})
                    SET t.namespace = 'ingredienti',
                        t.term_id = $id,
                        t.label_en = $label,
                        t.label_it = $label,
                        t.is_public = true,
                        t.roles = [],
                        t.teams = []
                    """,
                    id=term_id,
                    label=label,
                )

            titles = _corpus_titles()
            for i, title in enumerate(titles):
                doc_id = f"{PREFIX}doc_{i:03d}"
                session.run(
                    """
                    MERGE (d:Document {id: $id})
                    SET d.document_id = $id,
                        d.title = $title,
                        d.source_title = $title,
                        d.lang = 'en',
                        d.source_lang = 'it',
                        d.source_language = 'it',
                        d.canonical_hash = $hash,
                        d.verification_level = 'L1',
                        d.translation_state = 'translated',
                        d.is_public = true,
                        d.roles = [],
                        d.teams = []
                    REMOVE d.embedding
                    """,
                    id=doc_id,
                    title=title,
                    hash=f"hash-{doc_id}",
                )
                # Un'entita' ingrediente per documento, collegata a un termine
                # glossario (ciclico) per dare risultati a /glossary/query.
                term_label = GLOSSARY_TERMS[i % len(GLOSSARY_TERMS)]
                term_id = f"ingredienti:IB5-{term_label.upper().replace(' ', '-')}"
                entity_id = f"{doc_id}:ing:0"
                session.run(
                    """
                    MERGE (e:Entity {id: $entity_id})
                    SET e.label = $label, e.type = 'ingredient',
                        e.is_public = true, e.roles = [], e.teams = []
                    WITH e
                    MATCH (d:Document {id: $doc_id})
                    MERGE (e)-[:PART_OF_DOC]->(d)
                    WITH e
                    MATCH (t:CanonicalTerm {id: $term_id})
                    MERGE (e)-[:NORMALIZED_TO]->(t)
                    """,
                    entity_id=entity_id,
                    label=term_label,
                    doc_id=doc_id,
                    term_id=term_id,
                )

        embedding = build_embedding_from_graph(client)
        populated = populate_embeddings(client, embedding)
        print(f"[setup] embedding popolati: {populated}/{DOCS}")
    finally:
        client.close()


def cleanup() -> None:
    """Rimuove utenti ib5_load_* da Postgres e nodi ib5_load_* da Neo4j."""
    import psycopg

    dsn = os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    )
    with psycopg.connect(dsn, autocommit=True) as conn, conn.transaction():
        conn.execute("DELETE FROM users WHERE username LIKE %s", (f"{PREFIX}%",))

    from app.storage.client import Neo4jClient

    client = Neo4jClient.from_env()
    try:
        with client.session() as session:
            session.run(
                "MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n",
                prefix=PREFIX,
            )
    finally:
        client.close()


# --------------------------------------------------------------------------- worker
@dataclass
class Stats:
    """Latenze per endpoint + contatori errori."""

    latencies: dict[str, list[float]] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)
    http_errors: dict[str, int] = field(default_factory=dict)

    def add(
        self,
        endpoint: str,
        seconds: float,
        ok: bool,
        status: int | None = None,
    ) -> None:
        self.latencies.setdefault(endpoint, []).append(seconds)
        if not ok:
            self.errors[endpoint] = self.errors.get(endpoint, 0) + 1
        if status is not None and status >= 400:
            self.http_errors[endpoint] = self.http_errors.get(endpoint, 0) + 1


def _load_golden_queries() -> list[tuple[str, str | None]]:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return [(pair["query"], pair.get("lang")) for pair in golden["pairs"]]


async def _mixed_request(
    client: httpx.AsyncClient,
    auth: dict[str, str],
    user_id: int,
    request_index: int,
    stats: Stats,
    queries: list[tuple[str, str | None]],
) -> None:
    """Una richiesta del mix 50% rag / 30% entities / 20% glossary."""
    slot = request_index % 10
    if slot < 5:  # 50% RAG
        query, lang = queries[(user_id * 10 + request_index) % len(queries)]
        endpoint, method, url, payload = (
            "rag",
            "post",
            "/api/v1/rag/query",
            {"query": query, "lang": lang, "limit": 5},
        )
    elif slot < 8:  # 30% entities
        endpoint, method, url, payload = "entities", "get", "/api/v1/entities", None
    else:  # 20% glossary
        term = GLOSSARY_TERMS[(user_id * 10 + request_index) % len(GLOSSARY_TERMS)]
        endpoint, method, url, payload = (
            "glossary",
            "get",
            f"/api/v1/glossary/query?ingredient={term}",
            None,
        )
    t0 = time.perf_counter()
    try:
        if method == "post":
            r = await client.post(url, json=payload, headers=auth)
        else:
            r = await client.get(url, headers=auth)
        ok = r.status_code == 200
        stats.add(endpoint, time.perf_counter() - t0, ok, r.status_code)
    except httpx.HTTPError:
        stats.add(endpoint, time.perf_counter() - t0, False)


async def worker(
    client: httpx.AsyncClient,
    user_id: int,
    requests_per_user: int,
    stats: Stats,
    queries: list[tuple[str, str | None]],
) -> None:
    """Login reale + mix rag/entities/glossary per un utente (fase storm)."""
    username = f"{PREFIX}u{user_id:03d}"
    # IP simulato per-utente (come dietro nginx): evita il rate limiter in-app.
    headers = {"X-Forwarded-For": f"10.98.{user_id // 250}.{user_id % 250}"}

    t0 = time.perf_counter()
    try:
        r = await client.post(
            "/auth/login",
            json={"username": username, "password": PASSWORD},
            headers=headers,
        )
        ok = r.status_code == 200
        stats.add("login", time.perf_counter() - t0, ok, r.status_code)
        if not ok:
            return
        token = r.json()["access_token"]
    except httpx.HTTPError:
        stats.add("login", time.perf_counter() - t0, False)
        return

    auth = {"Authorization": f"Bearer {token}", **headers}
    for i in range(requests_per_user):
        await _mixed_request(client, auth, user_id, i, stats, queries)


async def steady_state_phase(
    client: httpx.AsyncClient,
    users: int,
    requests_per_user: int,
    stats: Stats,
    queries: list[tuple[str, str | None]],
    think_time: float = 0.1,
) -> None:
    """Fase steady-state: login sequenziali (non misurati), poi il mix di
    richieste con 100 utenti concorrenti e un piccolo think time tra le
    richieste (comportamento utente realistico). Isola la latenza delle
    query dalla saturazione CPU del login storm argon2id (artefatto dev
    single-worker) e dal burst simultaneo di tutte le richieste."""
    tokens: list[dict[str, str]] = []
    for i in range(users):
        username = f"{PREFIX}u{i:03d}"
        headers = {"X-Forwarded-For": f"10.97.{i // 250}.{i % 250}"}
        try:
            r = await client.post(
                "/auth/login",
                json={"username": username, "password": PASSWORD},
                headers=headers,
            )
            if r.status_code != 200:
                print(f"[steady-state] login fallito per {username}: {r.status_code}")
                return
            tokens.append({"Authorization": f"Bearer {r.json()['access_token']}", **headers})
        except httpx.HTTPError as exc:
            print(f"[steady-state] errore login {username}: {exc}")
            return

    async def user_loop(i: int) -> None:
        # Staggered start: gli utenti non arrivano tutti nello stesso istante
        # (arrivo realistico), evitando il burst iniziale di 100 richieste.
        await asyncio.sleep(i * 0.1)
        for j in range(requests_per_user):
            await _mixed_request(client, tokens[i], i, j, stats, queries)
            if think_time > 0:
                await asyncio.sleep(think_time)

    await asyncio.gather(*[user_loop(i) for i in range(users)])


# --------------------------------------------------------------------------- report
def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    return statistics.quantiles(sorted(values), n=100, method="inclusive")[int(p) - 1]


def _render_table(title: str, stats: Stats) -> list[str]:
    lines = [f"### {title}", "", "| Endpoint | Richieste | p50 (ms) | p95 (ms) | p99 (ms) | Errori | HTTP>=400 |", "|---|---|---|---|---|---|---|"]
    for ep in sorted(stats.latencies):
        lat = stats.latencies[ep]
        lines.append(
            f"| {ep} | {len(lat)} | {pct(lat, 50) * 1000:.0f} | {pct(lat, 95) * 1000:.0f} | "
            f"{pct(lat, 99) * 1000:.0f} | {stats.errors.get(ep, 0)} | {stats.http_errors.get(ep, 0)} |"
        )
    lines.append("")
    return lines


def render_markdown(
    stats: Stats,
    steady: Stats | None,
    args: argparse.Namespace,
    total_s: float,
    steady_s: float,
) -> str:
    lines = [
        "## 9. Iterazione B — Load test RAG retrieval (WP-B5, gate GB5)",
        "",
        f"- Data: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Target: `{args.base_url}` — utenti: {args.users} — richieste/utente: {args.requests_per_user}",
        "- Mix: 50% POST /api/v1/rag/query (golden pilot) · 30% GET /api/v1/entities · 20% GET /api/v1/glossary/query",
        f"- Dati: {DOCS} documenti `{PREFIX}*` con embedding popolati + termini glossario",
        f"- Durata fase storm: {total_s:.1f}s — richieste: {sum(len(v) for v in stats.latencies.values())}",
    ]
    lines += _render_table("Fase 1 — login storm + mix (esperienza completa)", stats)
    if steady is not None:
        lines += [
            f"- Durata fase steady-state: {steady_s:.1f}s — richieste: {sum(len(v) for v in steady.latencies.values())}",
            "",
        ]
        lines += _render_table(
            "Fase 2 — steady-state (login gia' effettuati, 100 utenti concorrenti)",
            steady,
        )
    lines.append("")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000", help="base URL API")
    parser.add_argument("--users", type=int, default=100, help="utenti concorrenti (default 100)")
    parser.add_argument("--requests-per-user", type=int, default=10, help="richieste per utente dopo il login")
    parser.add_argument("--timeout", type=float, default=30.0, help="timeout httpx (s)")
    parser.add_argument("--create-data", action="store_true", default=True, help="crea utenti/documenti di test")
    parser.add_argument("--no-create-data", dest="create_data", action="store_false")
    parser.add_argument("--cleanup", action="store_true", default=True, help="cleanup dati di test a fine run")
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false")
    parser.add_argument("--steady-state", action="store_true", default=True, help="fase steady-state dopo lo storm (NFR1 pulito)")
    parser.add_argument("--no-steady-state", dest="steady_state", action="store_false")
    parser.add_argument("--think-time", type=float, default=0.5, help="think time (s) tra le richieste nella fase steady-state (default 0.5)")
    parser.add_argument("--report", default="docs/benchmark-report.md", help="file markdown report")
    args = parser.parse_args()

    if args.create_data:
        print(f"[setup] creo {args.users} utenti, {DOCS} documenti e termini glossario {PREFIX}*...")
        create_users(args.users)
        create_documents()

    queries = _load_golden_queries()
    stats = Stats()
    total_t0 = time.perf_counter()
    limits = httpx.Limits(
        max_connections=args.users + 10, max_keepalive_connections=args.users
    )
    steady: Stats | None = None
    steady_s = 0.0
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=args.timeout, limits=limits
    ) as client:
        await asyncio.gather(
            *[
                worker(client, i, args.requests_per_user, stats, queries)
                for i in range(args.users)
            ]
        )
        total_s = time.perf_counter() - total_t0

        # --- fase steady-state (NFR1 pulito, senza login storm) ---
        if args.steady_state:
            print("\n[steady-state] login sequenziali + mix concorrente...")
            steady = Stats()
            steady_t0 = time.perf_counter()
            await steady_state_phase(
                client,
                args.users,
                args.requests_per_user,
                steady,
                queries,
                think_time=args.think_time,
            )
            steady_s = time.perf_counter() - steady_t0

    # --- report console ---
    print(f"\n=== Load test RAG completato in {total_s:.1f}s ===")
    for label, phase in (("storm", stats), ("steady", steady)):
        if phase is None:
            continue
        print(f"--- fase {label} ---")
        for ep in sorted(phase.latencies):
            lat = phase.latencies[ep]
            print(
                f"{ep:9s} n={len(lat):4d}  p50={pct(lat, 50) * 1000:6.0f}ms  "
                f"p95={pct(lat, 95) * 1000:6.0f}ms  p99={pct(lat, 99) * 1000:6.0f}ms  "
                f"errori={phase.errors.get(ep, 0)}  http>=400={phase.http_errors.get(ep, 0)}"
            )

    md = render_markdown(stats, steady, args, total_s, steady_s)
    if args.report:
        path = REPO_ROOT / args.report
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "a") as f:  # noqa: ASYNC230 - scrittura report una tantum
            f.write(md)
        print(f"[report] sezione appesa a {path}")

    if args.cleanup:
        print("[cleanup] rimuovo dati di test...")
        cleanup()

    total_errors = sum(stats.errors.values()) + (
        sum(steady.errors.values()) if steady is not None else 0
    )
    raise SystemExit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
