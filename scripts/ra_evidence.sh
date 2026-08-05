#!/usr/bin/env bash
# Exit evidence: run the spine contract suite against a REAL Postgres
# checkpointer in a throwaway container (DONE = WIRED + EVIDENCED).
#
# Usage: scripts/ra_evidence.sh
set -euo pipefail

CONTAINER="ra-evidence-pg"
PORT="${RA_PG_PORT:-55432}"
DB="zagent"
export DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:${PORT}/${DB}"

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
docker run -d --name "${CONTAINER}" \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB="${DB}" \
  -p "${PORT}:5432" postgres:16-alpine >/dev/null

for _ in $(seq 1 30); do
  if docker exec "${CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
[ -x "${PY}" ] || PY="python3"

cd "${ROOT}/worker"
"${PY}" -m pytest tests/test_spine_contract.py -q
echo "RA evidence: spine suite green against Postgres at ${DATABASE_URL}"
