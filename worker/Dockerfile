# Worker image: Claude Agent SDK Python + Claude CLI (Node) + rg + git.
# Build context = REPO ROOT (needs packages/):
#   docker build -f worker/Dockerfile -t zagent-worker:0.1.0 .
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Claude CLI is the SDK's runtime dependency — installed, never baked with credentials.
RUN npm install -g @anthropic-ai/claude-code@2.0.0 || npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY packages/contracts /app/packages/contracts
COPY worker /app/worker

RUN pip install --no-cache-dir /app/packages/contracts
RUN pip install --no-cache-dir "claude-agent-sdk==0.2.128" "websockets>=13,<16" "redis>=5,<7"

# API keys arrive via env at container start (ANTHROPIC_BASE_URL -> LiteLLM gateway,
# ANTHROPIC_AUTH_TOKEN -> per-lane virtual key, FLEET_PAT -> git credential helper).
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "worker.main"]
