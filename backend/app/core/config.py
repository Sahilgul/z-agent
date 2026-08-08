"""pydantic-settings config. DB URL, Redis, gateway, ADO, dirs — all from env.
Dialect-neutrality: SQLite local era, Postgres at the VM move = URL change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.models import DEFAULT_MODELS, ModelOption

# Known-insecure shipped secrets — rejected in any non-dev deployment.
_INSECURE_JWT_SECRETS = frozenset({"", "dev-only-change-me"})
_INSECURE_PAT_KEYS = frozenset({"", "dev-only-byo-pat-key"})


def _contracts_version() -> str:
    """Installed collegium-contracts version (importlib metadata — the
    package does not export __version__)."""
    try:
        from importlib.metadata import version
        return version("collegium-contracts")
    except Exception:
        return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COLLEGIUM_", env_file=".env", extra="ignore")

    db_url: str = "sqlite:///./data/collegium.db"
    # "memory://0" = in-process fakeredis (local dev without Docker/Redis).
    # Every real deployment sets COLLEGIUM_REDIS_URL explicitly (k8s backend.yaml).
    redis_url: str = "memory://0"

    gateway_url: str = "http://localhost:4000"
    # PUBLIC URL of the LiteLLM proxy UI, as reachable from an admin's browser
    # (the VM's host:port, e.g. https://vm.example.com:4000/ui). The internal
    # gateway_url is a compose-network address the browser can't resolve, so
    # the Usage button uses this. Empty = derive from gateway_url (fine for
    # local dev where localhost means the same thing to both).
    gateway_ui_url: str = ""
    litellm_master_key: str = ""
    # The gateway's public model alias (infra/litellm/config.yaml model_name).
    # Thread virtual keys are scoped to it, so every caller must use this exact
    # string — asking for anything else is rejected by the gateway.
    gateway_model: str = "kimi-k2.6"
    # The user-selectable model fleet (composer dropdown). Aliases must match
    # gateway routes; gateway_model must be one of them (it is the default
    # when a run doesn't choose). Override with COLLEGIUM_AVAILABLE_MODELS
    # as a JSON array of ModelOption objects.
    available_models: list[ModelOption] = Field(
        default_factory=lambda: list(DEFAULT_MODELS))
    # URLs as seen FROM WORKER CONTAINERS (compose network / host.docker.internal)
    worker_redis_url: str = "redis://redis:6379/0"
    worker_gateway_url: str = "http://gateway:4000"
    worker_image: str = "collegium-worker:0.1.0"
    # Engine cutover: custom LangGraph engine by default; "sdk"
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
    # Egress-locked compose network for thread containers: internal-only,
    # no internet route — threads reach gateway + redis and nothing else.
    worker_network: str = "collegium_thread"
    # Package-registry egress: allowlisting squid on the thread network.
    # Empty string disables proxy injection (read-only threads).
    package_proxy_url: str = ""
    pip_cache_volume: str = "collegium_pip-cache"
    npm_cache_volume: str = "collegium_npm-cache"

    # ---- Remediation feature flags (all default OFF; flip per rollout wave).
    # Wave 3: DB-backed capacity reservations / repo write locks.
    feature_db_concurrency: bool = False
    # Wave 3: worker spawn tools become backend-orchestrated spawn requests.
    feature_backend_swarm: bool = False
    # Wave 1: acked control delivery for interrupt/kill/spawn_done.
    feature_control_acks: bool = False
    # Wave 2: enforce durable engine storage before ENGINE=custom serves.
    feature_durable_resume: bool = False

    jwt_secret: str = ""  # no shipped default — set COLLEGIUM_JWT_SECRET (C-13)
    jwt_ttl_seconds: int = 60 * 60 * 24 * 14
    admin_usernames: str = "sahil"
    # First-admin bootstrap (chicken-and-egg): seed creates this ACTIVE
    # admin if the username AND pin are configured. Defaults are EMPTY so a
    # production deploy never silently seeds an active admin with a known
    # PIN — local dev opts in via COLLEGIUM_BOOTSTRAP_ADMIN_USERNAME /
    # COLLEGIUM_BOOTSTRAP_ADMIN_PIN in .env (C-14).
    bootstrap_admin_username: str = ""
    bootstrap_admin_pin: str = ""
    # Dev opt-in for the insecure shipped defaults (jwt_secret etc.). False in
    # every real deploy — the secret validator fails fast without it (C-13).
    dev_insecure_defaults: bool = False

    ado_org: str = ""
    ado_project: str = ""
    fetch_pat: str = ""
    fleet_pat: str = ""
    # Merge-identity lock: True = service account may NOT bypass
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
    # Anchored to the backend package root, not the process CWD — the shipped
    # playbooks live in backend/playbooks and must seed regardless of where
    # uvicorn/pytest was launched from.
    playbooks_dir: Path = Path(__file__).resolve().parents[2] / "playbooks"
    scripts_dir: Path = Path("./scripts")  # git-credential-collegium lives here

    fetch_interval_seconds: int = 300
    session_retention_days: int = 30
    events_ttl_months: int = 12

    # Tool-permission cards. The worker's BLPOP gives up after this long and
    # denies deterministically, so the backend has to expire the row
    # on the same clock or the console keeps offering a dead button.
    approval_timeout_seconds: int = 900
    # The worker engine's idle linger (seconds) before a thread completes.
    # Passed to the container verbatim so the backend and worker clocks agree (C8).
    idle_ttl_seconds: int = 900
    # Container env has a hard ceiling (Docker/ECS ~ most platforms fail or
    # truncate past a few hundred KB total). Oversize TASK_PROMPT /
    # PERSONA_PROMPT payloads must fail with a clear error instead of a
    # mangled container start (C6).
    max_env_payload_bytes: int = 128 * 1024

    # H3: ONE authoritative capacity number, owned here. The worker's
    # SWARM_MAX_SLICES is a request-batching hint, not a capacity source.
    global_thread_cap: int = 100
    default_thread_budget_usd: float = 5.0
    # LiteLLM virtual-key TTL (LiteLLM duration syntax, e.g. "24h"). Backstop
    # for orphaned keys — see GatewayClient.mint_key.
    gateway_key_ttl: str = "24h"
    # F4: the worker engine's local cost ESTIMATE (reminder thresholds) must
    # price tokens the way the gateway does, or the 50%/80% reminders drift
    # from real spend. USD per 1M (input, output) tokens; injected into the
    # container env and read by worker/engine/llm.py.
    worker_price_in_per_mtok: float = 2.0
    worker_price_out_per_mtok: float = 6.0

    # PREWARM_POOL (documented-not-implemented): semantics live in
    # orchestrator/thread_manager.py; prewarm_status() reports {"enabled": false}.
    prewarm_pool_enabled: bool = False
    prewarm_pool_size: int = 2

    # Knowledge flywheel retrieval: cheap-model rerank of
    # trigger_descriptions at run start. Any gateway failure falls back to
    # deterministic lexical ranking — retrieval must never fail a run.
    knowledge_rerank_model: str = "kimi-k2.6"
    knowledge_top_k: int = 8
    knowledge_rerank_timeout_seconds: float = 10.0

    # Ideas space: Counsel + Lead synthesis completions via the gateway.
    ideas_model: str = "kimi-k2.6"

    # BYO-PAT: local-era at-rest encryption key; Key Vault
    # takes over at the VM move. Empty disables BYO-PAT storage.
    # H-44: no shipped default — the old "dev-only-byo-pat-key" let anyone
    # with DB access decrypt every stored PAT. Set
    # COLLEGIUM_BYO_PAT_ENCRYPTION_KEY in any real deploy (enforced below).
    byo_pat_encryption_key: str = ""

    # Triggers engine: webhook HMAC secret (empty = ingress
    # rejects everything, fail-closed), the service account's OWN ADO descriptor
    # (guardrail 1 loop prevention), and the state-flapping coalesce window.
    ado_webhook_secret: str = ""
    service_account_descriptor: str = ""
    trigger_flap_window_minutes: int = 10
    # Guardian circuit breaker: max fix runs per PR per 24h before
    # halt; a repeated failure signature halts immediately regardless.
    guardian_max_attempts: int = 3
    # Improvement Inbox: accepted proposals spend real money with no
    # human initiating each one — the weekly ceiling is enforced in code.
    proposals_weekly_ceiling_usd: float = 25.0
    # PWA push: VAPID identity for web push; generated per deploy,
    # empty = push disabled (sends are skipped, subscriptions still stored).
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@collegiumlabs.com"
    # Autonomy promotion: evidence thresholds — completed runs at the
    # level below before the dial unlocks one notch. Failures reset nothing;
    # they just don't count.
    autonomy_promote_gated_after: int = 3
    autonomy_promote_autonomous_after: int = 8
    # Cross-host session store: Azure Blob connection for the session
    # mirror. Empty = single-host era, uploads skip (bind-mount is the path).
    session_store_connection: str = ""

    @property
    def admins(self) -> set[str]:
        return {u.strip() for u in self.admin_usernames.split(",") if u.strip()}

    def model_option(self, alias: str) -> ModelOption | None:
        """Registry lookup by gateway alias. None = not selectable — callers
        fail closed (never substitute another model)."""
        return next((m for m in self.available_models if m.alias == alias), None)

    @model_validator(mode="after")
    def _enforce_secret_defaults(self) -> Settings:
        # C-13: a shipped/empty jwt_secret lets anyone forge a JWT and bypass
        # PIN auth. Fail fast at startup unless the dev opt-in is set, so a
        # production deploy that forgot COLLEGIUM_JWT_SECRET refuses to boot
        # rather than silently running with a known secret.
        if not self.dev_insecure_defaults and self.jwt_secret in _INSECURE_JWT_SECRETS:
            raise ValueError(
                "COLLEGIUM_JWT_SECRET must be set to a non-default secret "
                "(it is currently empty or the shipped 'dev-only-change-me'). "
                "Set COLLEGIUM_JWT_SECRET in the environment/.env, or set "
                "COLLEGIUM_DEV_INSECURE_DEFAULTS=1 only for local dev."
            )
        # H-44: a shipped/empty byo_pat_encryption_key lets anyone with DB
        # access decrypt every stored PAT. Fail fast at startup in any
        # non-dev deploy that forgot COLLEGIUM_BYO_PAT_ENCRYPTION_KEY.
        if not self.dev_insecure_defaults and self.byo_pat_encryption_key in _INSECURE_PAT_KEYS:
            raise ValueError(
                "COLLEGIUM_BYO_PAT_ENCRYPTION_KEY must be set to a non-default key "
                "(it is currently empty or the shipped 'dev-only-byo-pat-key'). "
                "Set COLLEGIUM_BYO_PAT_ENCRYPTION_KEY in the environment/.env, or set "
                "COLLEGIUM_DEV_INSECURE_DEFAULTS=1 only for local dev."
            )
        # M1: a real deploy that forgot COLLEGIUM_REDIS_URL would silently run
        # the in-process fakeredis — streams, control channels, and heartbeats
        # all evaporate on restart. Fail fast outside dev.
        if not self.dev_insecure_defaults and self.redis_url.startswith("memory://"):
            raise ValueError(
                "COLLEGIUM_REDIS_URL is unset (memory://0 in-process fake). "
                "Real deployments must point at a Redis instance; set "
                "COLLEGIUM_REDIS_URL, or COLLEGIUM_DEV_INSECURE_DEFAULTS=1 "
                "for local dev."
            )
        # Wave 2 gate: durable resume requires durable engine storage — an
        # empty engine_database_url means MemorySaver, so "resume" would be a
        # DB-only fiction. Only enforced once the rollout flag is on.
        if (
            self.feature_durable_resume
            and self.engine_runtime == "custom"
            and not self.engine_database_url
        ):
            raise ValueError(
                "COLLEGIUM_FEATURE_DURABLE_RESUME=1 with ENGINE=custom requires "
                "COLLEGIUM_ENGINE_DATABASE_URL (durable checkpointer DSN). "
                "Without it the engine falls back to in-memory checkpoints and "
                "resume cannot survive container replacement."
            )
        # Contract/image agreement: a semver-tagged worker image must match the
        # installed contracts package version, or backend and worker speak
        # different schemas. Non-semver tags (test/dev images) skip the check.
        if not self.dev_insecure_defaults and self.engine_runtime == "custom":
            tag = self.worker_image.rsplit(":", 1)[-1] if ":" in self.worker_image else ""
            if tag and tag[0].isdigit() and tag != _contracts_version():
                raise ValueError(
                    f"Worker image tag '{tag}' does not match installed "
                    f"collegium-contracts version '{_contracts_version()}'. "
                    "Rebuild the worker image against the pinned contracts "
                    "package so backend and worker share one schema."
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
