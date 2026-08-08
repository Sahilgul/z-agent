"""Seed: the `system` user, the shipping modes, a seeded
COMPLETED demo Ask-mode run (replayable glass box, zero spend — replay is the
perfect tutorial because it's the same EventStream), and the Welcome idea-thread
fixture. Idempotent — safe on every boot.

Run: python -m app.auth.seed_users
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from collegium_contracts import StepKind

from app.core.config import get_settings
from app.db.base import get_session
from app.db.models.event import Event
from app.db.models.idea import IdeaComment, IdeaThread
from app.db.models.mode import Mode
from app.db.models.run import Run
from app.db.models.thread import Thread
from app.db.models.user import User

ASK_PERSONA = (
    "You are a senior codebase researcher. You answer questions about the codebase "
    "with file:line citations, verified by live grep — never from memory. You are "
    "read-only: you never modify files. When uncertain, say what you could NOT verify."
)

PLAN_PERSONA = (
    "You are a senior planning architect. Given a task and the fleet graph, produce a "
    "structured Plan (contracts.Plan JSON): ordered steps, each with repo, target files, "
    "and a success_criterion the deterministic evidence nodes can verify. Scope from the "
    "fleet graph's blast radius — never assume a change is local. Cite file:symbol claims "
    "you verified by read-only grep on the mounted golden repos; flag anything you could NOT "
    "verify. You are read-only: you never modify files."
)

DEVELOPMENT_PERSONA = (
    "You are a senior implementing engineer. You execute one batch of approved Plan steps in "
    "your writable clone, following the repo's own conventions as documented in its AGENTS.md "
    "and existing code. After each step, run the repo profile's test_cmds and emit a test_run "
    "StepEvent with the real exit code and output — the backend derives evidence from those "
    "stored events, never from your self-report prose. Never claim a step done without a "
    "green test_run event."
)

DEBUG_PERSONA = (
    "You are a senior debug specialist. Reproduce the reported behavior FIRST in the read-only "
    "golden tree (a repro is the only honest entry to a root cause). Then form a root-cause "
    "hypothesis with file:line Evidence entries, each linted against the golden repo. Report "
    "back as a Notebook contract. You are read-only: you never modify files. If the bug is "
    "fixable, say so — a start_plan action will carry your diagnosis into Plan mode."
)

AGENT_RND_PERSONA = (
    "You are the Lead of a research swarm. You decompose investigative tasks into "
    "distinct, non-overlapping slices and author each Explorer thread's prompt; you never "
    "investigate slices yourself in the decompose turn. When the requested thread count is "
    "wasteful for the task, say so and counter-propose with reasoning. At synthesis time "
    "you merge your Explorers' notebooks into one answer — consensus, disagreements, open "
    "questions — citing their file:line evidence. You are read-only: you never modify files."
)

GOAL_PERSONA = (
    "You are a senior autonomous delivery engineer in a ZERO-INTERRUPTION pipeline: "
    "feature story (PRD) to pull request with no human checkpoints. Explore the codebase "
    "for context, plan precisely, harden the plan through the critique rounds (treat "
    "critic findings as authoritative review comments), implement every step in the "
    "writable clone, and leave the tree green for the control plane's verification gate "
    "(tests, ruff/lint, build, dev boot). Commit your work on the run's agent branch — "
    "the control plane pushes and opens the PR. Never stop to ask permission; a clear "
    "failure report beats a forced bad result."
)

# Permissions scope (modes as data): which repos a thread under this mode may
# stamp a writable clone for. Empty = read-only. ``repos`` is an allowlist; ``writable``
# gates whether the thread gets a writable mount at all.
READ_ONLY_PERMS = {"writable": False, "repos": []}
SERVERAPP_WRITABLE_PERMS = {"writable": True, "repos": ["ServerApp"]}
ANY_REPO_WRITABLE_PERMS = {"writable": True, "repos": []}

DEMO_EVENTS = [
    (StepKind.STATUS, "session init", {"subtype": "init"}),
    (StepKind.THINKING, "thinking…", {"text": "The user asks how scribe dedupes questions. I should find the scribe flow first — likely a Socket.io namespace in ServerApp, then trace the dedupe logic."}),
    (StepKind.COMMAND, "grep dedupe", {"tool": "Grep", "input": {"pattern": "dedupe"}, "output": "src/services/scribe/dedupe.service.ts:14:export function dedupeQuestions(\nsrc/services/scribe/dedupe.service.ts:41:  const seen = new Map<string, number>();", "ok": True}),
    (StepKind.FILE_READ, "Read src/services/scribe/dedupe.service.ts", {"tool": "Read", "input": {"file_path": "src/services/scribe/dedupe.service.ts"}, "output": "14| export function dedupeQuestions(questions: string[]): string[] {\n15|   const seen = new Map<string, number>();\n16|   for (const q of questions) {\n17|     const key = normalize(q);\n18|     if (!seen.has(key)) seen.set(key, 1);\n19|   }\n20|   return [...seen.keys()];\n21| }", "ok": True}),
    (StepKind.THINKING, "thinking…", {"text": "Dedupe keys on a normalized form (lowercase, punctuation stripped, whitespace collapsed). Insertion order is preserved via Map. Let me check normalize() and whether a trailing question mark changes the key."}),
    (StepKind.COMMAND, "grep normalize", {"tool": "Grep", "input": {"pattern": "function normalize"}, "output": "src/services/scribe/normalize.ts:3:export function normalize(text: string): string {", "ok": True}),
    (StepKind.MESSAGE, "How scribe dedupes questions", {"text": "Scribe dedupes questions in `src/services/scribe/dedupe.service.ts:14-21`.\n\n- Each question is normalized (`src/services/scribe/normalize.ts:3`): lowercased, punctuation stripped, whitespace collapsed.\n- The FIRST occurrence wins; insertion order is preserved via a Map.\n- Edge case: a trailing `?` is stripped by normalize, so 'Any pain?' and 'any pain' dedupe to the same row — but a trailing question that arrives mid-stream with different leading whitespace used to slip through before the trim landed in normalize.ts:5.\n\nVerified live against the mounted tree; no writes made."}),
    (StepKind.STATUS, "turn complete", {"num_turns": 6, "duration_ms": 41000, "is_error": False, "usage": {"input_tokens": 18200, "output_tokens": 940, "cache_read_input_tokens": 12400}}),
]


def seed() -> None:
    session = get_session()
    try:
        # system user (autonomous runs carry created_by=system — keeps the
        # MANDATORY owner field honest)
        if session.query(User).filter_by(username="system").one_or_none() is None:
            # M-56: the system user is a service account (autonomous runs carry
            # created_by=system). It must NEVER log in. pin_hash="!locked"
            # used to crash the login path (bcrypt raised ValueError on the
            # invalid salt -> 500). Use None so the login route's
            # `pin_hash is None` guard returns a clean 401 (and verify_pin now
            # also guards against malformed hashes).
            session.add(User(username="system", display_name="Collegium system",
                             role="member", status="active",
                             pin_hash=None))
            session.commit()

        # First-admin bootstrap (chicken-and-egg, local-dev path):
        # seed creates the configured admin ACTIVE so a fresh clone can log in
        # without the add_user CLI. No-op once the user exists. Defaults are
        # empty (C-14) so production never silently seeds an active admin with
        # a known PIN — local dev opts in via COLLEGIUM_BOOTSTRAP_ADMIN_*
        # (both username AND pin must be set; hash_pin still validates 4-6 digits).
        settings = get_settings()
        admin_name = settings.bootstrap_admin_username
        if (admin_name and settings.bootstrap_admin_pin
                and session.query(User).filter_by(username=admin_name).one_or_none() is None):
            from app.core.security import hash_pin
            session.add(User(
                username=admin_name, display_name=admin_name,
                pin_hash=hash_pin(settings.bootstrap_admin_pin),
                role="admin" if admin_name in settings.admins else "member",
                status="active",
            ))
            session.commit()

        # modes are DB rows — ships Ask; later phases add plan/development/debug.
        if session.query(Mode).filter_by(name="ask").one_or_none() is None:
            session.add(Mode(
                name="ask", persona_prompt=ASK_PERSONA, permission_mode="default",
                topology="single", model_tier="strong", autonomy_default="supervised",
                permissions=READ_ONLY_PERMS,
            ))
            session.commit()

        if session.query(Mode).filter_by(name="plan").one_or_none() is None:
            session.add(Mode(
                name="plan", persona_prompt=PLAN_PERSONA, permission_mode="default",
                topology="plan", model_tier="strong", autonomy_default="supervised",
                permissions=READ_ONLY_PERMS, playbook_ids=["plan/fleet-scoping"],
                evidence_contract={"tests_pass": False, "diff_summary": False,
                                   "ci_green": False, "screenshots": False},
            ))
            session.commit()
        if session.query(Mode).filter_by(name="development").one_or_none() is None:
            session.add(Mode(
                name="development", persona_prompt=DEVELOPMENT_PERSONA,
                permission_mode="acceptEdits", topology="development", model_tier="strong",
                autonomy_default="gated", permissions=ANY_REPO_WRITABLE_PERMS,
                playbook_ids=["development/serverapp-areas", "development/drizzle-transactions"],
                evidence_contract={"tests_pass": True, "diff_summary": True,
                                   "ci_green": False, "screenshots": False},
            ))
            session.commit()
        if session.query(Mode).filter_by(name="debug").one_or_none() is None:
            session.add(Mode(
                name="debug", persona_prompt=DEBUG_PERSONA, permission_mode="default",
                topology="debug", model_tier="strong", autonomy_default="supervised",
                permissions=READ_ONLY_PERMS, playbook_ids=["debug/repro-first"],
                evidence_contract={"tests_pass": False, "diff_summary": False,
                                   "ci_green": False, "screenshots": False},
            ))
            session.commit()
        # Agent-R&D = width-swarm topology, read-only always.
        if session.query(Mode).filter_by(name="agent-rnd").one_or_none() is None:
            session.add(Mode(
                name="agent-rnd", persona_prompt=AGENT_RND_PERSONA,
                permission_mode="default", topology="width-swarm", model_tier="strong",
                autonomy_default="supervised", permissions=READ_ONLY_PERMS,
                evidence_contract={"tests_pass": False, "diff_summary": False,
                                   "ci_green": False, "screenshots": False},
            ))
            session.commit()
        # Goal = zero-interruption PRD->PR pipeline. autonomy_default="autonomous"
        # is load-bearing: it maps to bypassPermissions, so the engine's approval
        # gate never fires — the mode is DEFINED by running end-to-end without a
        # single approval card.
        if session.query(Mode).filter_by(name="goal").one_or_none() is None:
            session.add(Mode(
                name="goal", persona_prompt=GOAL_PERSONA,
                permission_mode="bypassPermissions", topology="goal", model_tier="strong",
                autonomy_default="autonomous", permissions=ANY_REPO_WRITABLE_PERMS,
                evidence_contract={"tests_pass": True, "diff_summary": True,
                                   "ci_green": False, "screenshots": False},
            ))
            session.commit()

        _seed_demo_run(session)
        _seed_welcome_thread(session)
        _seed_triggers(session)
        from app.services.playbooks import seed_playbooks
        seeded = seed_playbooks(session)
        print(f"[seed] done: system user, ask/plan/development/debug/goal modes, demo run, welcome thread, {seeded} playbook(s)")
    finally:
        session.close()


def _seed_demo_run(session) -> None:
    """Replayable COMPLETED demo run — the empty Inbox is where onboarding lives
    or dies. Zero spend; the entire glass box read-only."""
    if session.query(Run).filter_by(title="DEMO: How does scribe dedupe questions?").one_or_none():
        return
    system = session.query(User).filter_by(username="system").one()
    run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    base = datetime.now(UTC) - timedelta(hours=1)
    session.add(Run(
        id=run_id, created_by=system.id, source="cron", mode="ask", autonomy="supervised",
        stage="completed", title="DEMO: How does scribe dedupe questions?",
        auto_summary="Scribe dedupes on a normalized key (dedupe.service.ts:14-21); first occurrence wins, order preserved.",
        repo="ServerApp", cost_usd=0.0, tokens=19140, available_actions=[],
        started_at=base, finished_at=base + timedelta(seconds=47), last_active_at=base,
    ))
    session.add(Thread(
        id=thread_id, run_id=run_id, persona="researcher", repo_scope="ServerApp",
        status="completed", next_seq=len(DEMO_EVENTS), cost_usd=0.0,
        finished_at=base + timedelta(seconds=47),
    ))
    for seq, (kind, title, detail) in enumerate(DEMO_EVENTS):
        session.add(Event(
            run_id=run_id, thread_id=thread_id, seq=seq,
            ts=base + timedelta(seconds=seq * 6), type=kind.value, title=title,
            payload=detail, sdk_message_uuid=None,
        ))
    session.commit()


def _seed_welcome_thread(session) -> None:
    """Welcome idea-thread fixture: 4 member comments, so the Ideas exit is
    demoable solo."""
    if session.query(IdeaThread).filter_by(title="Welcome to Collegium — what should the fleet learn first?").one_or_none():
        return
    system = session.query(User).filter_by(username="system").one()
    thread = IdeaThread(
        title="Welcome to Collegium — what should the fleet learn first?",
        body=("This is the team's shared Ideas space. Open threads for feature thoughts, "
              "product direction, architecture concerns. Ask Counsel for the 11th-member "
              "opinion; the Lead summarizes all voices on demand."),
        created_by=system.id, source="user", status="open",
    )
    session.add(thread)
    session.flush()
    comments = [
        "The dedupe edge cases in scribe keep biting us — a 'hygiene patrol' for known recurring bugs would pay for itself.",
        "I'd love the Janitor to watch dependency drift across the 10 repos before it becomes a security ticket.",
        "Can the fleet graph learn which changes ALWAYS end up touching ServerApp drizzle? That's our widest blast radius.",
        "First lesson to teach: never trust origin/HEAD — half our repos integrate on pg-main.",
    ]
    for i, body in enumerate(comments):
        session.add(IdeaComment(
            thread_id=thread.id, author_type="user", author_ref=str(system.id),
            body=body, created_at=datetime.now(UTC) - timedelta(minutes=40 - i * 9),
        ))
    session.commit()


def _seed_triggers(session) -> None:
    """Triggers-as-data defaults: the ADO state vocabulary and the
    autonomous flows live in ROWS — a new state is config, not a deploy."""
    from app.db.models.trigger import Trigger
    defaults = [
        # New → collegium-plan auto-starts a Plan run attributed to whoever moved it.
        Trigger(name="ado-state-plan", source="ado_webhook",
                filter_json={"event_type": "work_item.updated", "state": "collegium-plan"},
                mode="plan", autonomy="gated", owner_resolution="changed_by",
                rate_limit_per_hour=20),
        # New → collegium-dev auto-starts a Development run, same attribution.
        Trigger(name="ado-state-dev", source="ado_webhook",
                filter_json={"event_type": "work_item.updated", "state": "collegium-dev"},
                mode="development", autonomy="gated", owner_resolution="changed_by",
                rate_limit_per_hour=20),
        # Guardian: CI failure on a Collegium PR → gated fix run (circuit breaker
        # in services/guardian.py — code, never prompt).
        Trigger(name="guardian-ci-failure", source="ado_webhook",
                filter_json={"event_type": "build.failed"},
                mode="development", autonomy="gated", owner_resolution="system",
                rate_limit_per_hour=12),
        # Responder: PR comment → resume the originating run and push.
        Trigger(name="responder-pr-comment", source="ado_webhook",
                filter_json={"event_type": "pr.comment"},
                mode="development", autonomy="gated", owner_resolution="system",
                rate_limit_per_hour=30),
        # Review-bot: a new PR gets a read-only review pass (Ask topology —
        # review is read-and-comment, never edit).
        Trigger(name="review-bot-pr", source="ado_webhook",
                filter_json={"event_type": "pr.created"},
                mode="ask", autonomy="gated", owner_resolution="system",
                rate_limit_per_hour=12),
    ]
    for row in defaults:
        if session.query(Trigger).filter_by(name=row.name).one_or_none() is None:
            session.add(row)
    session.commit()


if __name__ == "__main__":
    seed()
