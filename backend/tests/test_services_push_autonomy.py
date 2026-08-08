"""Push notifications + autonomy promotion tests: subscription
upsert/prune, injected sender tallies, deep links, evidence-based cap ladder,
clamp semantics, review-bot normalizer + trigger task text.
"""

from app.db.models.notification import Notification
from app.db.models.run import Run
from app.services import autonomy, push, triggers


# ------------------------------------------------------------------------ push
def test_subscription_upsert_by_endpoint(session, make_user):
    u = make_user()
    push.save_subscription(u.id, "https://push.example/sub/1", {"p256dh": "a"})
    push.save_subscription(u.id, "https://push.example/sub/1", {"p256dh": "b"})
    rows = session.query(Notification).all()
    assert len(rows) == 1 and rows[0].keys["p256dh"] == "b"


def test_subscription_rejects_non_https(session, make_user):
    u = make_user()
    import pytest
    with pytest.raises(push.PushError):
        push.save_subscription(u.id, "http://insecure/sub", {})


def test_send_tallies_and_prunes_expired(session, make_user):
    u = make_user()
    push.save_subscription(u.id, "https://push.example/good", {})
    push.save_subscription(u.id, "https://push.example/dead", {})

    def fake_sender(endpoint, keys, payload):
        assert payload["url"] == "/app?screen=approvals&run=r1&card=a1"
        return "expired" if "dead" in endpoint else "sent"

    tally = push.send_to_user(u.id, "Approval needed", "tool: do thing",
                              push.approval_deep_link("r1", "a1"), sender=fake_sender)
    assert tally == {"sent": 1, "skipped": 0, "expired": 1}
    remaining = [n.endpoint for n in session.query(Notification).all()]
    assert remaining == ["https://push.example/good"]


def test_send_without_subscriptions_is_a_noop(make_user):
    u = make_user()
    assert push.send_to_user(u.id, "t", "b") == {"sent": 0, "skipped": 0, "expired": 0}


def test_deep_link_points_at_the_card():
    assert push.approval_deep_link("run-9", "card-3") == \
        "/app?screen=approvals&run=run-9&card=card-3"


# -------------------------------------------------------------------- autonomy
def _runs(session, user_id, autonomy, stage="completed", n=1):
    for i in range(n):
        session.add(Run(id=f"{autonomy}-{i}", created_by=user_id, mode="development",
                        autonomy=autonomy, stage=stage))
    session.commit()


def test_cap_starts_supervised(session, make_user):
    u = make_user()
    out = autonomy.cap_for(u.id)
    assert out["cap"] == "supervised"


def test_cap_promotes_on_evidence(session, make_user, monkeypatch):
    monkeypatch.setattr(autonomy.get_settings(), "autonomy_promote_gated_after", 3)
    monkeypatch.setattr(autonomy.get_settings(), "autonomy_promote_autonomous_after", 8)
    u = make_user()
    _runs(session, u.id, "supervised", n=3)
    assert autonomy.cap_for(u.id)["cap"] == "gated"
    _runs(session, u.id, "gated", n=8)
    assert autonomy.cap_for(u.id)["cap"] == "autonomous"


def test_failed_runs_earn_nothing(session, make_user, monkeypatch):
    monkeypatch.setattr(autonomy.get_settings(), "autonomy_promote_gated_after", 3)
    u = make_user()
    _runs(session, u.id, "supervised", stage="failed", n=10)
    assert autonomy.cap_for(u.id)["cap"] == "supervised"


def test_clamp_respects_cap_and_defaults(session, make_user):
    u = make_user()
    assert autonomy.clamp("autonomous", u.id) == "supervised"  # above cap -> down
    assert autonomy.clamp(None, u.id) == "gated"               # product default
    assert autonomy.clamp("nonsense", u.id) == "supervised"
    _runs(session, u.id, "supervised", n=3)
    assert autonomy.clamp("gated", u.id) == "gated"


# ------------------------------------------------------------------ review-bot
def test_normalize_pr_created():
    body = {"resource": {"pullRequestId": 88, "title": "add billing area",
                         "createdBy": {"id": "desc-dev"},
                         "repository": {"name": "ServerApp"},
                         "lastMergeSourceCommit": {"commitId": "abcdef0123456"}}}
    ev = triggers.normalize_ado_pr_created(body)
    assert ev.event_type == "pr.created"
    assert ev.idempotency_key == ("ado_webhook", "88", 0)
    assert ev.payload["repo"] == "ServerApp"
    assert ev.changed_by_descriptor == "desc-dev"


def test_pr_task_text_is_a_review_brief():
    from collegium_contracts.triggers import TriggerEvent, TriggerSource
    ev = TriggerEvent(source=TriggerSource.ADO_WEBHOOK, external_id="88", revision=0,
                      event_type="pr.created",
                      payload={"pr_id": 88, "repo": "ServerApp", "title": "add billing"})
    text = triggers._task_text(ev)
    assert "Review PR 88" in text and "Read-only" in text
