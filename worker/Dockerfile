# Worker image: custom LangGraph engine (default) + legacy Claude Agent SDK
# fallback (ENGINE=sdk, kept through the RE soak) + Claude CLI + rg + git.
# Build context = REPO ROOT (needs packages/):
#   docker build -f worker/Dockerfile -t zagent-worker:0.2.0 .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep curl ca-certificates gnupg \
 # libpq: psycopg (Postgres checkpointer) needs the client library at runtime
 && apt-get install -y --no-install-recommends libpq5 \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Claude CLI is the SDK fallback's runtime dependency — installed, never baked
# with credentials. Dropped when the seam is cut (RE).
RUN npm install -g @anthropic-ai/claude-code@2.0.0 || npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY packages/contracts /app/packages/contracts
COPY worker/pyproject.toml /app/worker-project/pyproject.toml
# The importable package is worker/worker — copying the project dir instead puts
# main.py at /app/worker/worker/main.py, where `python -m worker.main` can't see it.
COPY worker/worker /app/worker-project/worker
RUN pip install --no-cache-dir /app/packages/contracts
# The langgraph stack (engine) + the cas extra (sdk fallback through RE).
RUN pip install --no-cache-dir "/app/worker-project[cas]"
# `python -m worker.*` resolves against /app/worker-project on sys.path.
ENV PYTHONPATH=/app/worker-project

# API keys arrive via env at container start (LITELLM_BASE_URL / LITELLM_API_KEY
# -> LiteLLM gateway + per-thread virtual key, FLEET_PAT -> git credential
# helper, DATABASE_URL -> Postgres checkpointer).
# ENGINE=custom (default) boots the LangGraph engine runner via worker.main's
# dispatch; ENGINE=sdk keeps the legacy CAS fallback alive through RE.
ENV PYTHONUNBUFFERED=1 ENGINE=custom
CMD ["python", "-m", "worker.main"]
