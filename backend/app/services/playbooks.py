"""Playbook service (WU6): SKILL.md playbook format + loader + seeder.

A playbook is a SKILL.md file: YAML frontmatter (name, mode, version,
description, trigger) delimited by ``---`` fences, then a markdown body. The
mode row's ``playbook_ids`` (e.g. ``plan/fleet-scoping``) resolve to Playbook
rows seeded from these files. The loader is pure code (no LLM); the seeder is
idempotent (upsert by name, bump version when the body changes).

Format::

    ---
    name: plan/fleet-scoping
    mode: plan
    version: 1
    description: Scope every plan from the fleet graph's blast radius.
    trigger: task touches a repo with a non-empty blast radius
    ---

    # Fleet-scoping
    ## When to use ...
    ## Steps 1. ...

The parser is permissive: missing frontmatter fields fall back to sane
defaults so a malformed playbook never crashes a boot. The body is everything
after the closing ``---`` fence. Frontmatter is hand-parsed ``key: value``
(stdlib only, per the WU6 brief — NO pyyaml dependency); values are scalars.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(service="playbooks")

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
REQUIRED_FIELDS = ("name", "mode")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Hand-parsed ``key: value`` frontmatter. Splits on the FIRST colon only,
    so values may contain colons. A line without a colon means the block isn't
    a mapping (e.g. a YAML list) and is rejected."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            raise ValueError("playbook frontmatter is not a mapping")
        out[key.strip()] = value.strip()
    return out


@dataclass
class PlaybookDoc:
    """Parsed SKILL.md: frontmatter + body. ``extra`` holds any other keys."""
    name: str
    mode: str
    version: int = 1
    description: str = ""
    trigger: str = ""
    body: str = ""
    extra: dict = field(default_factory=dict)

    def as_skill_md(self) -> str:
        """Render back to SKILL.md text (frontmatter + body)."""
        lines = [f"name: {self.name}", f"mode: {self.mode}", f"version: {self.version}",
                 f"description: {self.description}", f"trigger: {self.trigger}"]
        lines += [f"{k}: {v}" for k, v in self.extra.items()]
        front = "\n".join(line.rstrip() for line in lines)
        return f"---\n{front}\n---\n\n{self.body.strip()}\n"


def parse_skill_md(text: str) -> PlaybookDoc:
    """Parse a SKILL.md string into a PlaybookDoc. Raises ValueError when the
    frontmatter is missing the required name/mode fields."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("playbook missing YAML frontmatter (--- ... ---)")
    front = _parse_frontmatter(match.group(1))
    body = match.group(2).strip()
    name = front.pop("name", None)
    mode = front.pop("mode", None)
    if not name or not mode:
        raise ValueError("playbook frontmatter requires name and mode")
    version = int(front.pop("version", "1") or "1")
    description = front.pop("description", "")
    trigger = front.pop("trigger", "")
    return PlaybookDoc(name=str(name), mode=str(mode), version=version,
                       description=description, trigger=trigger, body=body, extra=front)


def load_playbook_file(path: Path) -> PlaybookDoc:
    """Read + parse a single SKILL.md file."""
    return parse_skill_md(path.read_text(encoding="utf-8"))


def discover_playbooks(playbooks_dir: Path | None = None) -> list[PlaybookDoc]:
    """Walk the playbooks dir for ``*.md`` files and parse each. Files whose
    frontmatter is malformed are skipped with a warning (never crash a boot)."""
    root = playbooks_dir or get_settings().playbooks_dir
    out: list[PlaybookDoc] = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*.md")):
        try:
            out.append(load_playbook_file(path))
        except Exception as exc:
            log.warning("skipping malformed playbook", path=str(path), error=str(exc)[:160])
    return out


def seed_playbooks(session) -> int:
    """Upsert discovered playbooks into the Playbook table by name. Bumps
    ``version`` when the skill_md body changes. Idempotent. Returns the count
    of playbooks upserted."""
    from app.db.models.knowledge import Playbook
    docs = discover_playbooks()
    count = 0
    for doc in docs:
        row = session.query(Playbook).filter_by(name=doc.name).one_or_none()
        if row is None:
            session.add(Playbook(name=doc.name, skill_md=doc.as_skill_md(), version=doc.version))
            count += 1
        elif row.skill_md != doc.as_skill_md():
            row.skill_md = doc.as_skill_md()
            row.version = (row.version or 0) + 1
            count += 1
    session.commit()
    return count


def playbooks_prompt_for_mode(mode_name: str) -> str:
    """Render a mode's playbook bodies for persona-prompt injection (WU6: the
    blueprint hydrates inject matching playbooks, matched by mode via the mode
    row's ``playbook_ids``). Returns ``""`` when none — prompt composition stays
    unconditional. Order follows the mode row's playbook_ids."""
    from app.db.base import get_session
    from app.db.models.knowledge import Playbook
    from app.db.models.mode import Mode
    session = get_session()
    try:
        mode = session.query(Mode).filter_by(name=mode_name).one_or_none()
        ids = list(mode.playbook_ids) if mode and mode.playbook_ids else []
        if not ids:
            return ""
        rows = session.query(Playbook).filter(Playbook.name.in_(ids)).all()
        by_name = {r.name: r for r in rows}
        chunks: list[str] = []
        for pid in ids:
            row = by_name.get(pid)
            if row is None:
                log.info("playbook referenced by mode not seeded", mode=mode_name, playbook=pid)
                continue
            try:
                doc = parse_skill_md(row.skill_md)
                when = doc.trigger or doc.description
                header = f"--- Playbook: {doc.name}" + (f" (when: {when})" if when else "") + " ---"
                chunks.append(f"{header}\n{doc.body}")
            except Exception:  # a malformed row still reaches the lane verbatim
                chunks.append(f"--- Playbook: {row.name} ---\n{row.skill_md}")
        return "\n\n".join(chunks)
    finally:
        session.close()
