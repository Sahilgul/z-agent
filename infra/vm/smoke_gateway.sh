#!/usr/bin/env bash
# Gateway smoke test — run from infra/vm:  ./smoke_gateway.sh
#
# The gateway port is not published, so curl runs in a throwaway container on the
# compose network rather than from the host. Checks what a lane needs, in the
# order things fail: reachability, chat/completions, the Anthropic surface the
# Agent SDK speaks, and the reasoning parameter the SDK actually sends.
set -uo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
source .env

NET="${GATEWAY_NETWORK:-zagent_internal}"
MODEL="${GATEWAY_MODEL:-kimi-foundry}"
CURL_IMAGE="curlimages/curl:8.11.1"
MSG='{"role":"user","content":"say ok"}'

# One curl invocation: label, then any number of curl args.
run_curl() {
  local label="$1"; shift
  echo "=== ${label}"
  docker run --rm --network "${NET}" "${CURL_IMAGE}" \
    -s -S -w '\nHTTP %{http_code}\n' "$@" | head -c 600
  echo
  echo
}

run_curl "1. liveliness" \
  "http://gateway:4000/health/liveliness"

run_curl "2. chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -d "{\"model\":\"${MODEL}\",\"max_tokens\":200,\"messages\":[${MSG}]}" \
  "http://gateway:4000/v1/chat/completions"

run_curl "3. anthropic /v1/messages (what the Agent SDK speaks)" \
  -H "Content-Type: application/json" \
  -H "x-api-key: ${LITELLM_MASTER_KEY}" \
  -H "anthropic-version: 2023-06-01" \
  -d "{\"model\":\"${MODEL}\",\"max_tokens\":200,\"messages\":[${MSG}]}" \
  "http://gateway:4000/v1/messages"

run_curl "4. nested reasoning param — 400s unless additional_drop_params covers it" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -d "{\"model\":\"${MODEL}\",\"max_tokens\":200,\"reasoning\":{\"effort\":\"medium\"},\"messages\":[${MSG}]}" \
  "http://gateway:4000/v1/chat/completions"

run_curl "5. flat reasoning_effort param" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
  -d "{\"model\":\"${MODEL}\",\"max_tokens\":200,\"reasoning_effort\":\"medium\",\"messages\":[${MSG}]}" \
  "http://gateway:4000/v1/chat/completions"
