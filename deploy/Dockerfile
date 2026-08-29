# syntax=docker/dockerfile:1
# ============================================================================
# km-api — immagine dell'app layer stateless (ADR-003 D3, WP7/G8)
#
# Build (context = repo root, include il workspace member graphify/):
#   docker build -f deploy/Dockerfile -t km-api:0.1.0 .
#
# Scelta package manager: **uv** (lockfile riproducibile, uv.lock gia' nel repo,
# workspace member graphifyy). Multi-stage: il builder installa le dipendenze
# con `uv sync --frozen`; il runtime copia solo .venv + app/ (immagine slim,
# nessun tool di build, nessuna dipendenza dev).
#
# L'app e' stateless (ADR-003 D3): 1 worker uvicorn per container, scaling
# orizzontale via `docker compose up -d --scale km-api=N` (nginx upstream DNS).
# ============================================================================

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- builder: uv + dipendenze dal lockfile ---
FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

FROM base AS builder
COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY graphify/ ./graphify/
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime ---
FROM base AS runtime
RUN groupadd --system km && useradd --system --gid km --create-home km
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app/ ./app/
# l'app scrive la cache di ingestione in CWD (.km_ingest_cache): /app deve
# essere scrivibile dall'utente non-root km
RUN chown -R km:km /app
USER km
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
