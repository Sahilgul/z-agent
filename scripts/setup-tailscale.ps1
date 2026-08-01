# Zagent — Tailscale tunnel setup (plan §1: watch a run from a phone browser).
#
# What this does:
#   1. Verifies Docker Desktop is up and the zagent stack is running.
#   2. Brings up the `tailscale` compose profile (sidecar on the internal network).
#   3. Prints the tailnet URL for the console (https://zagent.<tailnet>.ts.net).
#
# One-time prerequisites (not automatable from here):
#   - A tailnet you control (tailscale.com) with HTTPS certificates enabled
#     (DNS page -> "Enable HTTPS").
#   - EITHER set TS_AUTHKEY in infra/.env (reusable auth key, tagged, ephemeral=false)
#     OR run the container once and complete the interactive login it logs.
#
# Phone access: install the Tailscale app, sign into the same tailnet, open the URL.
# PWA: "Add to Home Screen" gives the console a standalone shell.

$ErrorActionPreference = "Stop"
$InfraDir = Join-Path $PSScriptRoot "..\infra"

docker info | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running." }

Push-Location $InfraDir
try {
    docker compose --profile tailscale up -d
    if ($LASTEXITCODE -ne 0) { throw "compose up failed — check infra/.env (TS_AUTHKEY optional)" }

    Start-Sleep -Seconds 5
    $status = docker compose exec -T tailscale tailscale status --json | ConvertFrom-Json
    if (-not $status.BackendState -or $status.BackendState -ne "Running") {
        Write-Host ""
        Write-Host "Tailscale is not logged in yet. Complete the one-time login:"
        docker compose logs tailscale | Select-String -Pattern "https://login.tailscale.com" | Select-Object -First 1
        Write-Host "Open that URL in a browser, authorize the node, then re-run this script."
        exit 1
    }

    $dns = $status.Self.DNSName.TrimEnd('.')
    Write-Host ""
    Write-Host "Console is live on your tailnet:"
    Write-Host "  https://$dns"
    Write-Host ""
    Write-Host "Verify serve config:"
    docker compose exec -T tailscale tailscale serve status
} finally {
    Pop-Location
}
