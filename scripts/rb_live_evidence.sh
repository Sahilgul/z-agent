#!/usr/bin/env bash
# RB live evidence: bring up the local stack (redis + mock gateway + Postgres
# checkpointer + backend) and drive a LIVE API-created ask thread on the custom
# engine, asserting StepEvents reach the console feed endpoint.
#
# Prerequisites: docker, the worker image built
#   docker build -f worker/Dockerfile -t zagent-worker:0.2.0 .
# and the repo venv (.venv) with backend deps.
#
# Usage: scripts/rb_live_evidence.sh [--keep]   (--keep leaves the stack up)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
DATA="/tmp/rb-evidence"
NET="zagent_thread"
PG_PORT="${RA_PG_PORT:-55432}"
KEEP="${1:-}"

cleanup() {
  [ "${KEEP}" = "--keep" ] && return 0
  docker rm -f rb-redis rb-gateway rb-evidence-pg >/dev/null 2>&1 || true
  pkill -f "uvicorn app.main:app --port 8000" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# --- infra -------------------------------------------------------------------
docker network inspect "${NET}" >/dev/null 2>&1 || docker network create "${NET}" >/dev/null
docker rm -f rb-redis rb-gateway rb-evidence-pg >/dev/null 2>&1 || true
docker run -d --name rb-redis --network "${NET}" --network-alias redis -p 6380:6379 redis:7-alpine >/dev/null
docker run -d --name rb-evidence-pg --network "${NET}" --network-alias pg \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=zagent -p "${PG_PORT}:5432" \
  postgres:16-alpine >/dev/null
docker run -d --name rb-gateway --network "${NET}" --network-alias gateway -p 4099:4000 \
  -v "${ROOT}/scripts/mock_gateway.py:/app/mock_gateway.py:ro" \
  python:3.12-slim python /app/mock_gateway.py >/dev/null

for c in rb-evidence-pg; do
  for _ in $(seq 1 30); do
    docker exec "${c}" pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1
  done
done
for _ in $(seq 1 30); do curl -sf http://localhost:4099/health/liveliness >/dev/null 2>&1 && break; sleep 1; done

# --- backend ------------------------------------------------------------------
mkdir -p "${DATA}"/{golden/repos,sessions,workspaces,transcripts,evidence,playbooks}
export ZAGENT_DB_URL="sqlite:///${DATA}/zagent.db" \
  ZAGENT_REDIS_URL="redis://localhost:6380/0" \
  ZAGENT_GATEWAY_URL="http://localhost:4099" \
  ZAGENT_LITELLM_MASTER_KEY="sk-mock-master" \
  ZAGENT_WORKER_IMAGE="zagent-worker:0.2.0" \
  ZAGENT_WORKER_NETWORK="${NET}" \
  ZAGENT_ENGINE_DATABASE_URL="postgresql://postgres:postgres@pg:5432/zagent" \
  ZAGENT_GOLDEN_DIR="${DATA}/golden/repos" \
  ZAGENT_SESSIONS_DIR="${DATA}/sessions" \
  ZAGENT_WORKSPACES_DIR="${DATA}/workspaces" \
  ZAGENT_TRANSCRIPTS_DIR="${DATA}/transcripts" \
  ZAGENT_EVIDENCE_DIR="${DATA}/evidence" \
  ZAGENT_PLAYBOOKS_DIR="${DATA}/playbooks"

cd "${ROOT}/backend"
rm -f "${DATA}/zagent.db"*
"${PY}" -m alembic upgrade head >/dev/null
"${PY}" -m app.auth.seed_users >/dev/null
"${PY}" - <<'PYEOF'
import sqlite3
from datetime import datetime, timezone
conn = sqlite3.connect('/tmp/rb-evidence/zagent.db')
conn.execute(
    "INSERT INTO repos (name, remote_url, integration_branch, status, status_detail, created_at)"
    " VALUES ('ServerApp', '', 'main', 'ready-no-map', '', ?)",
    (datetime.now(timezone.utc).isoformat(),))
conn.commit()
PYEOF

pkill -f "uvicorn app.main:app --port 8000" >/dev/null 2>&1 || true
"${PY}" -m uvicorn app.main:app --port 8000 &
BACKEND_PID=$!
for _ in $(seq 1 30); do curl -sf http://localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done

# --- live thread ---------------------------------------------------------------
curl -sf -c "${DATA}/cookies.txt" -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"sahil","pin":"4545"}' >/dev/null
RUN_ID=$(curl -sf -b "${DATA}/cookies.txt" -X POST http://localhost:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"mode":"ask","task":"Which engine runtime is serving this thread, and how do steps reach this feed?"}' \
  | "${PY}" -c "import json,sys; print(json.load(sys.stdin)['id'])")
echo "run: ${RUN_ID}"

# Wait for the feed to fill (worker boot + turn + ingest).
FEED=""
for _ in $(seq 1 24); do
  FEED=$(curl -sf -b "${DATA}/cookies.txt" "http://localhost:8000/runs/${RUN_ID}/events")
  [ "$("${PY}" -c "import json; print(len(json.loads('''${FEED}''')))" 2>/dev/null || echo 0)" -ge 3 ] && break
  sleep 5
done

echo "${FEED}" | "${PY}" -c "
import json, sys
events = json.load(sys.stdin)
kinds = [e.get('kind') for e in events]
titles = [e.get('title', '') for e in events]
assert 'message' in kinds, f'no assistant message in feed: {titles}'
assert any('CUSTOM LangGraph engine' in t for t in titles), 'feed is not from the custom engine'
assert 'status' in kinds and any('turn complete' in t for t in titles), 'no turn boundary'
print(f'RB LIVE EVIDENCE OK: {len(events)} StepEvents in the console feed (kinds: {sorted(set(kinds))})')
"

CONTAINER=$(docker ps -a --filter name=zagent-thread --format '{{.Names}}' | head -1)
docker logs "${CONTAINER}" 2>&1 | grep -m1 "ENGINE=custom" \
  && echo "RB BOOT EVIDENCE OK: worker container booted the engine runner"
docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true

docker exec rb-evidence-pg psql -U postgres -d zagent -tAc \
  "SELECT count(*) FROM checkpoints" | { read -r n; [ "${n}" -gt 0 ] \
  && echo "RB CHECKPOINTER EVIDENCE OK: ${n} checkpoint rows in Postgres"; }

[ "${KEEP}" != "--keep" ] && kill "${BACKEND_PID}" 2>/dev/null || true
echo "RB evidence complete."
