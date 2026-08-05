# Day-1 tracer bullet runner — builds the worker image and runs the spike INSIDE
# the container on this workstation (the Docker-Desktop-under-Cortex-XDR leg,
# plan §10). Prereqs: Docker Desktop running; infra/.env filled (gateway up).
param(
  [ValidateSet("ask","structured","soak","interrupt","cache","all")]
  [string]$Check = "all",
  [string]$GoldenDir = "./golden/repos",
  [string]$Repo = "ServerApp",
  [string]$Branch = "main"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent

docker build -f "$root/worker/Dockerfile" -t zagent-worker:0.1.0 $root

# Gateway runs on the compose network; host.docker.internal reaches it from the container.
docker run --rm -it `
  -e ANTHROPIC_BASE_URL="http://host.docker.internal:4000" `
  -e ANTHROPIC_AUTH_TOKEN="$env:SPIKE_GATEWAY_KEY" `
  -e SPIKE_RESULTS_DIR="/spike-results" `
  -v "${GoldenDir}:/golden/repos:ro" `
  -v "${root}/spike-results:/spike-results" `
  -p 8765:8765 -p 8766:8766 `
  zagent-worker:0.1.0 `
  python -m spike.tracer $Check --golden /golden/repos --repo $Repo --branch $Branch

Write-Host "Open http://localhost:8766 to watch live."
Write-Host "Decision matrix: $root/spike-results/DECISION_MATRIX.md"
