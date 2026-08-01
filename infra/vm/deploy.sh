#!/usr/bin/env bash
# Zagent VM deploy — the whole stack in three steps (Ubuntu 24.04):
#
#   1. cp .env.example .env   # then edit: passwords, PATs, Foundry key, admin PIN
#   2. ./deploy.sh            # builds images, runs migrations+seed, starts the stack
#   3. ./deploy.sh tailscale  # (optional) bring up tailnet access
#
# Prereqs on a fresh VM:  docker + compose plugin, and this repo cloned.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] || { echo "missing .env — step 1: cp .env.example .env and fill it"; exit 1; }

echo "==> building worker image (lane runtime)"
docker build -f ../../worker/Dockerfile -t zagent-worker:0.1.0 ../..

echo "==> building backend + web images"
docker compose build

echo "==> starting stack (migrations + seed run automatically before backend)"
docker compose up -d

if [ "${1:-}" = "tailscale" ]; then
  echo "==> starting tailscale profile"
  docker compose --profile tailscale up -d
  docker compose --profile tailscale exec tailscale tailscale status || true
fi

echo
echo "==> zagent is up:"
echo "    local:   http://localhost:8080"
echo "    tailnet: https://zagent.<your-tailnet>.ts.net  (after step 3)"
echo "    login:   \$ZAGENT_BOOTSTRAP_ADMIN_USERNAME / \$ZAGENT_BOOTSTRAP_ADMIN_PIN from .env"
echo
echo "    logs:    docker compose logs -f backend"
