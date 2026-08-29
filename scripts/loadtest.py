#!/usr/bin/env python3
"""Load test 100 utenti concorrenti (WP7, gate G8 — NFR2).

asyncio + httpx: ogni utente esegue un **login reale** (POST /auth/login) e poi
richieste GET /api/v1/entities e GET /api/v1/search. L'header X-Forwarded-For
per-utente simula IP distinti (come dietro nginx), cosi' il rate limiter in-app
per-IP non interferisce con la misura.

Uso:
    uv run python scripts/loadtest.py --base-url http://localhost:8000 --users 100
    uv run python scripts/loadtest.py --base-url http://localhost --users 100   # via nginx

Setup automatico (default --create-users):
    - crea N utenti ``wp7_load_*`` in Postgres (KM_PG_DSN)
    - crea N entita' ``wp7_load_*`` in Neo4j (KM_NEO4J_URI) per dare risultati
      a /search
Cleanup automatico a fine test (--no-cleanup per conservare i dati).

Report: p50/p95/p99 per endpoint + errori; stampato a video e, con --report,
appeso a docs/benchmark-report.md.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import time
from dataclasses import dataclass, field

import httpx

PREFIX = "wp7_load_"
PASSWORD = "wp7-load-password-123"  # >= 12 char (ADR-002 D5)


# --------------------------------------------------------------------------- setup
def create_users(n: int) -> None:
    """Crea n utenti wp7_load_* in Postgres con hash reale (idempotente).

    Usa create_user del layer auth: hash argon2id (ADR-002 D5) e ruoli viewer,
    cosi' il login reale del load test funziona.
    """
    import psycopg

    from app.auth.users import create_user

    dsn = os.environ.get("KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine")
    with psycopg.connect(dsn, autocommit=True) as conn:
        for i in range(n):
            create_user(
                conn,
                f"{PREFIX}u{i:03d}",
                f"{PREFIX}u{i:03d}@test.local",
                PASSWORD,
                roles=("viewer",),
            )


def create_entities(n: int) -> None:
    """Crea n entita' wp7_load_* in Neo4j (idempotente)."""
    from app.storage.client import Neo4jClient

    client = Neo4jClient.from_env()
    try:
        with client.session() as session:
            for i in range(n):
                eid = f"{PREFIX}entity_{i:03d}"
                session.run(
                    "MERGE (e:Entity {id: $id}) SET e.label = $label, e.type = 'loadtest'",
                    id=eid, label=f"{PREFIX}entity_{i:03d}",
                )
    finally:
        client.close()


def cleanup() -> None:
    """Rimuove utenti wp7_load_* da Postgres e entita' wp7_load_* da Neo4j."""
    import psycopg

    dsn = os.environ.get("KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine")
    with psycopg.connect(dsn, autocommit=True) as conn, conn.transaction():
        conn.execute("DELETE FROM users WHERE username LIKE %s", (f"{PREFIX}%",))

    from app.storage.client import Neo4jClient

    client = Neo4jClient.from_env()
    try:
        with client.session() as session:
            session.run(
                "MATCH (n) WHERE (n:Entity OR n:Fact OR n:Source OR n:Version) "
                "AND n.id STARTS WITH $prefix DETACH DELETE n",
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

    def add(self, endpoint: str, seconds: float, ok: bool, status: int | None = None) -> None:
        self.latencies.setdefault(endpoint, []).append(seconds)
        if not ok:
            self.errors[endpoint] = self.errors.get(endpoint, 0) + 1
        if status is not None and status >= 400:
            self.http_errors[endpoint] = self.http_errors.get(endpoint, 0) + 1


async def worker(
    client: httpx.AsyncClient,
    user_id: int,
    requests_per_user: int,
    stats: Stats,
) -> None:
    """Login reale + richieste entities/search per un utente."""
    username = f"{PREFIX}u{user_id:03d}"
    # IP simulato per-utente (come dietro nginx): evita il rate limiter in-app
    headers = {"X-Forwarded-For": f"10.99.{user_id // 250}.{user_id % 250}"}

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
        if i % 2 == 0:
            endpoint, url = "entities", "/api/v1/entities"
        else:
            endpoint, url = "search", f"/api/v1/search?q={PREFIX}"
        t0 = time.perf_counter()
        try:
            r = await client.get(url, headers=auth)
            ok = r.status_code == 200
            stats.add(endpoint, time.perf_counter() - t0, ok, r.status_code)
        except httpx.HTTPError:
            stats.add(endpoint, time.perf_counter() - t0, False)


# --------------------------------------------------------------------------- report
def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    return statistics.quantiles(sorted(values), n=100, method="inclusive")[int(p) - 1]


def render_markdown(stats: Stats, args: argparse.Namespace, total_s: float) -> str:
    lines = [
        "## Load test — 100 utenti concorrenti (WP7, gate G8)",
        "",
        f"- Data: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Target: `{args.base_url}` — utenti: {args.users} — richieste/utente: {args.requests_per_user}",
        f"- Durata totale: {total_s:.1f}s — richieste totali: {sum(len(v) for v in stats.latencies.values())}",
        "",
        "| Endpoint | Richieste | p50 (ms) | p95 (ms) | p99 (ms) | Errori | HTTP>=400 |",
        "|---|---|---|---|---|---|---|",
    ]
    for ep in sorted(stats.latencies):
        lat = stats.latencies[ep]
        lines.append(
            f"| {ep} | {len(lat)} | {pct(lat, 50)*1000:.0f} | {pct(lat, 95)*1000:.0f} | "
            f"{pct(lat, 99)*1000:.0f} | {stats.errors.get(ep, 0)} | {stats.http_errors.get(ep, 0)} |"
        )
    lines.append("")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000", help="base URL API")
    parser.add_argument("--users", type=int, default=100, help="utenti concorrenti (default 100)")
    parser.add_argument("--requests-per-user", type=int, default=10, help="richieste per utente dopo il login")
    parser.add_argument("--timeout", type=float, default=30.0, help="timeout httpx (s)")
    parser.add_argument("--create-users", action="store_true", default=True, help="crea utenti/entita' di test")
    parser.add_argument("--no-create-users", dest="create_users", action="store_false")
    parser.add_argument("--cleanup", action="store_true", default=True, help="cleanup dati di test a fine run")
    parser.add_argument("--no-cleanup", dest="cleanup", action="store_false")
    parser.add_argument("--report", default="docs/benchmark-report.md", help="file markdown report")
    args = parser.parse_args()

    if args.create_users:
        print(f"[setup] creo {args.users} utenti e {args.users} entita' {PREFIX}*...")
        create_users(args.users)
        create_entities(args.users)

    stats = Stats()
    total_t0 = time.perf_counter()
    limits = httpx.Limits(max_connections=args.users + 10, max_keepalive_connections=args.users)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=args.timeout, limits=limits) as client:
        await asyncio.gather(
            *[worker(client, i, args.requests_per_user, stats) for i in range(args.users)]
        )
    total_s = time.perf_counter() - total_t0

    # --- report console ---
    print(f"\n=== Load test completato in {total_s:.1f}s ===")
    for ep in sorted(stats.latencies):
        lat = stats.latencies[ep]
        print(
            f"{ep:10s} n={len(lat):4d}  p50={pct(lat, 50)*1000:6.0f}ms  "
            f"p95={pct(lat, 95)*1000:6.0f}ms  p99={pct(lat, 99)*1000:6.0f}ms  "
            f"errori={stats.errors.get(ep, 0)}  http>=400={stats.http_errors.get(ep, 0)}"
        )

    md = render_markdown(stats, args, total_s)
    if args.report:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.report)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:  # noqa: ASYNC230 - scrittura report una tantum a fine test
            f.write(md)
        print(f"[report] sezione appesa a {path}")

    if args.cleanup:
        print("[cleanup] rimuovo dati di test...")
        cleanup()

    # exit code: 0 se nessun errore
    total_errors = sum(stats.errors.values())
    raise SystemExit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
