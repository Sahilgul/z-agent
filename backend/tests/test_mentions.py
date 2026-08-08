"""@ repo mention parser: extraction and resolution against the fleet.

The composer wraps a picked repo as `@RepoName` (backtick-quoted) inline in
the user's message. Only backtick-wrapped tokens are scope directives; a
bare @word in prose is conversation, not a mount request.
"""

from app.db.models.repo import Repo
from app.services.mentions import MENTION_RE, extract_mentions, resolve_run_repos

# ----------------------------------------------------------- extract_mentions


def test_extract_mentions_finds_backtick_wrapped_tokens():
    text = "look at `@ServerApp` and `@ClientApp` for me"
    assert extract_mentions(text) == ["ServerApp", "ClientApp"]


def test_extract_mentions_dedupes_in_first_appearance_order():
    text = "`@ServerApp` then `@ClientApp` then `@ServerApp` again"
    assert extract_mentions(text) == ["ServerApp", "ClientApp"]


def test_extract_mentions_ignores_bare_at_word_in_prose():
    # A bare @word (no backticks) is conversation, not a scope directive.
    assert extract_mentions("ping @sahil about the bug") == []


def test_extract_mentions_ignores_unwrapped_at_at_start_of_line():
    # Markdown-style @-mentions without backticks don't count.
    assert extract_mentions("@channel heads up") == []


def test_extract_mentions_handles_none_and_empty():
    assert extract_mentions(None) == []
    assert extract_mentions("") == []


def test_extract_mentions_accepts_dots_dashes_digits_in_names():
    text = "`@billing-engine.v2` and `@ClientApp3`"
    assert extract_mentions(text) == ["billing-engine.v2", "ClientApp3"]


def test_mention_re_rejects_names_starting_with_dot_or_dash():
    # Names must start with [A-Za-z0-9] — `.foo` or `-bar` won't match.
    assert MENTION_RE.findall("`@.hidden`") == []
    assert MENTION_RE.findall("`@-dashy`") == []


# -------------------------------------------------------- resolve_run_repos


def _seed_repos(session):
    session.add_all([
        Repo(name="ServerApp", integration_branch="main"),
        Repo(name="ClientApp", integration_branch="main"),
        Repo(name="Billing-Engine", integration_branch="pg-main"),
    ])
    session.commit()


def test_resolve_explicit_repo_wins_and_heads_context(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo="ServerApp",
        task_text="investigate the auth flow")
    assert target.name == "ServerApp"
    assert [r.name for r in context] == ["ServerApp"]
    assert unknown == []


def test_resolve_mentions_become_context_in_order(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo=None,
        task_text="compare `@ClientApp` and `@Billing-Engine`")
    assert target.name == "ClientApp"  # first mention is the target
    assert [r.name for r in context] == ["ClientApp", "Billing-Engine"]
    assert unknown == []


def test_resolve_explicit_repo_prepended_if_not_in_mentions(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo="ServerApp",
        task_text="also check `@ClientApp`")
    # explicit repo heads the context even when not mentioned
    assert target.name == "ServerApp"
    assert [r.name for r in context] == ["ServerApp", "ClientApp"]


def test_resolve_explicit_repo_not_duplicated_when_also_mentioned(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo="ServerApp",
        task_text="look at `@ServerApp` and `@ClientApp`")
    assert [r.name for r in context] == ["ServerApp", "ClientApp"]


def test_resolve_collects_unknown_mentions(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo=None,
        task_text="check `@ServerApp` and `@GhostRepo`")
    assert target.name == "ServerApp"
    assert [r.name for r in context] == ["ServerApp"]
    assert unknown == ["GhostRepo"]


def test_resolve_no_repo_no_mention_returns_empty_target(session):
    _seed_repos(session)
    target, context, unknown = resolve_run_repos(
        session, explicit_repo=None, task_text="just a question")
    assert target is None
    assert context == []
    assert unknown == []
