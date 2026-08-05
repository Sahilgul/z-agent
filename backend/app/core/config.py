"""pydantic-settings config. DB URL, Redis, gateway, ADO, dirs — all from env.
Dialect-neutrality (plan §7): SQLite local era, Postgres at the VM move = URL change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ZAGENT_", env_file=".env", extra="ignore")

    db_url: str = "sqlite:///./data/zagent.db"
    # "memory://0" = in-process fakeredis (local dev without Docker/Redis).
    # Every real deployment sets ZAGENT_REDIS_URL explicitly (k8s backend.yaml).
    redis_url: str = "memory://0"

    gateway_url: str = "http://localhost:4000"
    litellm_master_key: str = ""
    # The gateway's public model alias (infra/litellm/config.yaml model_name).
    # Thread virtual keys are scoped to it, so every caller must use this exact
    # string — asking for anything else is rejected by the gateway.
    gateway_model: str = "kimi-foundry"
    # URLs as seen FROM WORKER CONTAINERS (compose network / host.docker.internal)
    worker_redis_url: str = "redis://redis:6379/0"
    worker_gateway_url: str = "http://gateway:4000"
    worker_image: str = "zagent-worker:0.2.0"
    # Engine cutover (plan §20/RB): custom LangGraph engine by default; "sdk"
    # keeps the legacy CAS fallback alive through the RE soak.
    engine_runtime: str = "custom"
    # Canary mode: read-only production threads on the custom engine before the
    # flag flip — ask-mode tools only, supervised autonomy (engine-enforced).
    engine_canary: bool = False
    engine_default_mode: str = "development"
    # Postgres checkpointer DSN reachable FROM WORKER CONTAINERS. Empty in the
    # local-era stack (app Postgres arrives at the VM move) — the engine falls
    # back to MemorySaver with a loud warning.
    engine_database_url: str = ""
    # Egress-locked compose network for thread containers (plan §10): internal-only,
    # no internet route — threads reach gateway + redis and nothing else.
    worker_network: str = "zagent_thread"
    # Package-registry egress (Phase 2): allowlisting squid on the thread network.
    # Empty string disables proxy injection (Phase 1 read-only threads).
    package_proxy_url: str = ""
    pip_cache_volume: str = "zagent_pip-cache"
    npm_cache_volume: str = "zagent_npm-cache"

    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_seconds: int = 60 * 60 * 24 * 14
    admin_usernames: str = "sahil"
    # First-admin bootstrap (plan §1b chicken-and-egg): seed creates this ACTIVE
    # admin if the username doesn't exist yet. Local-dev convenience only —
    # teammates are still born in the Team UI via one-time setup codes.
    bootstrap_admin_username: str = "sahil"
    bootstrap_admin_pin: str = "4545"

    ado_org: str = ""
    ado_project: str = ""
    fetch_pat: str = ""
    fleet_pat: str = ""
    # Merge-identity lock (plan §9): True = service account may NOT bypass
    # policies on complete; the merge tap deep-links to ADO's native UI instead.
    merge_native_ui: bool = False

    golden_dir: Path = Path("./golden/repos")
    sessions_dir: Path = Path("./sessions")
    # Flat JSONL mirror of each run's event stream. Deliberately NOT under
    # sessions_dir: that tree is purged at session_retention_days, while a
    # transcript should live as long as the events rows (events_ttl_months).
    transcripts_dir: Path = Path("./transcripts")
    workspaces_dir: Path = Path("./workspaces")
    evidence_dir: Path = Path("./evidence")  # Playwright screenshots + run evidence artifacts
    fleet_config_dir: Path = Path("./fleet-config")
    playbooks_dir: Path = Path("./playbooks")  # SKILL.md playbooks (mode.playbook_ids resolve here)
    scripts_dir: Path = Path("./scripts")  # git-credential-zagent lives here

    fetch_interval_seconds: int = 300
    session_retention_days: int = 30
    events_ttl_months: int = 12

    # Tool-permission cards. The worker's BLPOP gives up after this long and
    # denies deterministically (plan §10), so the backend has to expire the row
    # on the same clock or the console keeps offering a dead button.
    approval_timeout_seconds: int = 900

    global_thread_cap: int = 12
    default_thread_budget_usd: float = 5.0

    # PREWARM_POOL (documented-not-implemented, plan §2): semantics live in
    # orchestrator/thread_manager.py; prewarm_status() reports {"enabled": false}.
    prewarm_pool_enabled: bool = False
    prewarm_pool_size: int = 2

    # Knowledge flywheel retrieval (plan §3 G-1 fix): cheap-model rerank of
    # trigger_descriptions at run start. Any gateway failure falls back to
    # deterministic lexical ranking — retrieval must never fail a run.
    knowledge_rerank_model: str = "kimi-foundry"
    knowledge_top_k: int = 8
    knowledge_rerank_timeout_seconds: float = 10.0

    # Ideas space (plan §6): Counsel + Lead synthesis completions via the gateway.
    ideas_model: str = "kimi-foundry"

    # BYO-PAT (plan §1b Phase 3): local-era at-rest encryption key; Key Vault
    # takes over at the VM move. Empty disables BYO-PAT storage.
    byo_pat_encryption_key: str = "dev-only-byo-pat-key"

    # Triggers engine (plan §6 Phase 4): webhook HMAC secret (empty = ingress
    # rejects everything, fail-closed), the service account's OWN ADO descriptor
    # (guardrail 1 loop prevention), and the state-flapping coalesce window.
    ado_webhook_secret: str = ""
    service_account_descriptor: str = ""
    trigger_flap_window_minutes: int = 10
    # Guardian circuit breaker (Phase 4): max fix runs per PR per 24h before
    # halt; a repeated failure signature halts immediately regardless.
    guardian_max_attempts: int = 3
    # Improvement Inbox (Phase 4): accepted proposals spend real money with no
    # human initiating each one — the weekly ceiling is enforced in code.
    proposals_weekly_ceiling_usd: float = 25.0
    # PWA push (Phase 4): VAPID identity for web push; generated per deploy,
    # empty = push disabled (sends are skipped, subscriptions still stored).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@zagentharness.ai"
    # Autonomy promotion (Phase 4): evidence thresholds — completed runs at the
    # level below before the dial unlocks one notch. Failures reset nothing;
    # they just don't count.
    autonomy_promote_gated_after: int = 3
    autonomy_promote_autonomous_after: int = 8
    # Cross-host session store (Phase 5): Azure Blob connection for the session
    # mirror. Empty = single-host era, uploads skip (bind-mount is the path).
    session_store_connection: str = ""

    @property
    def admins(self) -> set[str]:
        return {u.strip() for u in self.admin_usernames.split(",") if u.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
