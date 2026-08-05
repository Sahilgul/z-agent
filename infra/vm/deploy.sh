#!/usr/bin/env bash
# Zagent VM deploy — the whole stack in two steps (Ubuntu 24.04):
#
#   1. cp .env.example .env   # then edit: passwords, PATs, Foundry key, admin PIN
#   2. ./deploy.sh            # builds images, runs migrations+seed, starts the stack
#
# Prereqs on a fresh VM:  docker + compose plugin, and this repo cloned.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "missing .env — step 1: cp .env.example .env and fill it"; exit 1; }

# Lane containers are siblings: the HOST daemon resolves the bind sources the
# backend hands it, so these dirs must exist on the host at the same paths the
# backend uses. Docker would otherwise auto-create them as empty root-owned
# dirs and lanes would mount empty repos.
DATA_ROOT="$(grep -E '^ZAGENT_DATA_ROOT=' .env | cut -d= -f2- | tr -d '"' || true)"
DATA_ROOT="${DATA_ROOT:-/srv/zagent}"
echo "==> preparing data root at $DATA_ROOT"
sudo mkdir -p "$DATA_ROOT"/{golden,sessions,workspaces,transcripts,evidence}
sudo chown -R "$(id -u):$(id -g)" "$DATA_ROOT"

echo "==> building worker image (lane runtime)"
docker build -f ../../worker/Dockerfile -t zagent-worker:0.1.0 ../..

echo "==> building backend + web images"
docker compose build

echo "==> starting stack (migrations + seed run automatically before backend)"
docker compose up -d

echo
echo "==> zagent is up:"
echo "    local:   http://localhost:8080"
echo "    login:   \$ZAGENT_BOOTSTRAP_ADMIN_USERNAME / \$ZAGENT_BOOTSTRAP_ADMIN_PIN from .env"
echo
echo "    logs:    docker compose logs -f backend"
