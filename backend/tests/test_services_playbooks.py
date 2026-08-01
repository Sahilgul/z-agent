from pathlib import Path

import pytest

from app.db.models.knowledge import Playbook
from app.services import playbooks


VALID = """---
name: plan/fleet-scoping
mode: plan
version: 2
description: Scope from the fleet graph.
trigger: blast radius non-empty
---

# Fleet-scoping
## Steps
1. Read the blast radius.
"""


def test_parse_skill_md_extracts_frontmatter_and_body():
    doc = playbooks.parse_skill_md(VALID)
    assert doc.name == "plan/fleet-scoping"
    assert doc.mode == "plan"
    assert doc.version == 2
    assert doc.description == "Scope from the fleet graph."
    assert doc.trigger == "blast radius non-empty"
    assert "Fleet-scoping" in doc.body


def test_parse_skill_md_preserves_extra_fields():
    """Hand-parsed frontmatter (stdlib only): extra values stay scalar strings."""
    text = "---\nname: x\nmode: plan\nowner: crew-a\ntags: [fleet, plan]\n---\n\nbody\n"
    doc = playbooks.parse_skill_md(text)
    assert doc.extra == {"owner": "crew-a", "tags": "[fleet, plan]"}


def test_parse_skill_md_value_may_contain_colons():
    text = "---\nname: x\nmode: plan\ndescription: see src/x.ts:5 for the fix\n---\n\nbody\n"
    doc = playbooks.parse_skill_md(text)
    assert doc.description == "see src/x.ts:5 for the fix"


def test_parse_skill_md_missing_frontmatter_raises():
    with pytest.raises(ValueError, match="frontmatter"):
        playbooks.parse_skill_md("just a body, no fences")


def test_parse_skill_md_missing_name_raises():
    text = "---\nmode: plan\n---\n\nbody\n"
    with pytest.raises(ValueError, match="name and mode"):
        playbooks.parse_skill_md(text)


def test_parse_skill_md_non_mapping_frontmatter_raises():
    text = "---\n- just\n- a\n- list\n---\n\nbody\n"
    with pytest.raises(ValueError, match="not a mapping"):
        playbooks.parse_skill_md(text)


def test_as_skill_md_round_trips():
    doc = playbooks.parse_skill_md(VALID)
    reparsed = playbooks.parse_skill_md(doc.as_skill_md())
    assert reparsed.name == doc.name
    assert reparsed.mode == doc.mode
    assert reparsed.version == doc.version
    assert reparsed.body.strip() == doc.body.strip()


def test_load_playbook_file(tmp_path):
    p = tmp_path / "x.md"
    p.write_text(VALID, encoding="utf-8")
    doc = playbooks.load_playbook_file(p)
    assert doc.name == "plan/fleet-scoping"


def test_discover_playbooks_walks_dir(tmp_path):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "fleet-scoping.md").write_text(VALID, encoding="utf-8")
    (tmp_path / "plan" / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    docs = playbooks.discover_playbooks(tmp_path)
    assert len(docs) == 1
    assert docs[0].name == "plan/fleet-scoping"


def test_discover_playbooks_missing_dir_returns_empty(tmp_path):
    assert playbooks.discover_playbooks(tmp_path / "nope") == []


def test_seed_playbooks_inserts_rows(session, tmp_path, monkeypatch):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "fleet-scoping.md").write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(playbooks, "get_settings",
                        lambda: _Settings(playbooks_dir=tmp_path))
    count = playbooks.seed_playbooks(session)
    assert count == 1
    rows = session.query(Playbook).all()
    assert len(rows) == 1
    assert rows[0].name == "plan/fleet-scoping"
    assert rows[0].version == 2


def test_seed_playbooks_is_idempotent(session, tmp_path, monkeypatch):
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "fleet-scoping.md").write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(playbooks, "get_settings",
                        lambda: _Settings(playbooks_dir=tmp_path))
    playbooks.seed_playbooks(session)
    count = playbooks.seed_playbooks(session)
    assert count == 0  # unchanged body -> no upsert
    assert len(session.query(Playbook).all()) == 1


def test_seed_playbooks_bumps_version_on_change(session, tmp_path, monkeypatch):
    (tmp_path / "plan").mkdir()
    p = tmp_path / "plan" / "fleet-scoping.md"
    p.write_text(VALID, encoding="utf-8")
    monkeypatch.setattr(playbooks, "get_settings",
                        lambda: _Settings(playbooks_dir=tmp_path))
    playbooks.seed_playbooks(session)
    p.write_text(VALID.replace("Scope from the fleet graph.", "Scope from the graph + CI."), encoding="utf-8")
    count = playbooks.seed_playbooks(session)
    assert count == 1
    row = session.query(Playbook).filter_by(name="plan/fleet-scoping").one()
    assert row.version == 3  # 2 -> 3 after a body change


class _Settings:
    def __init__(self, playbooks_dir):
        self.playbooks_dir = playbooks_dir


# --------------------------------------------------------------- prompt injection (WU6)
def _seed_mode_with_playbooks(session, playbook_ids=("development/serverapp-areas",)):
    from app.db.models.mode import Mode
    mode = Mode(name="development", persona_prompt="You ship.", permission_mode="acceptEdits",
                topology="development", playbook_ids=list(playbook_ids))
    session.add(mode)
    session.commit()
    return mode


def test_playbooks_prompt_for_mode_injects_bodies(session):
    _seed_mode_with_playbooks(session)
    session.add(Playbook(name="development/serverapp-areas", skill_md=VALID, version=1))
    session.commit()
    block = playbooks.playbooks_prompt_for_mode("development")
    assert "--- Playbook: plan/fleet-scoping" in block  # doc.name from the skill_md
    assert "Fleet-scoping" in block


def test_playbooks_prompt_for_mode_empty_when_no_ids(session):
    from app.db.models.mode import Mode
    session.add(Mode(name="debug", persona_prompt="p", playbook_ids=[]))
    session.commit()
    assert playbooks.playbooks_prompt_for_mode("debug") == ""
    assert playbooks.playbooks_prompt_for_mode("ghost-mode") == ""


def test_playbooks_prompt_for_mode_skips_unseeded_ids(session):
    _seed_mode_with_playbooks(session, playbook_ids=("development/missing", "development/serverapp-areas"))
    session.add(Playbook(name="development/serverapp-areas", skill_md=VALID, version=1))
    session.commit()
    block = playbooks.playbooks_prompt_for_mode("development")
    assert "development/missing" not in block
    assert "Fleet-scoping" in block


def test_playbooks_prompt_preserves_mode_row_order(session):
    _seed_mode_with_playbooks(session, playbook_ids=("b/two", "a/one"))
    other = VALID.replace("plan/fleet-scoping", "b/two").replace("# Fleet-scoping", "# Two")
    session.add_all([
        Playbook(name="a/one", skill_md=VALID.replace("plan/fleet-scoping", "a/one"), version=1),
        Playbook(name="b/two", skill_md=other, version=1),
    ])
    session.commit()
    block = playbooks.playbooks_prompt_for_mode("development")
    assert block.index("b/two") < block.index("a/one")
