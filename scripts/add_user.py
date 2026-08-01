"""add_user CLI — BOOTSTRAP PATH ONLY (plan §1b): chicken-and-egg — the Team
settings UI needs an admin to exist, so you run this ONCE for yourself; every
teammate after that is born in the UI.

  python add_user.py --username sahil --pin 4821 --display-name "Sahil" [--ado-email s@x.com]

Identity binding: --ado-email resolves the ADO descriptor via the Graph API at
provisioning (fail-loud on 0 or 2+ matches — the two-Alis rule).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_pin  # noqa: E402
from app.db.base import get_session  # noqa: E402
from app.db.models.user import User  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", required=True)
    parser.add_argument("--pin", required=True, help="4-6 digits")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--ado-email", default="")
    args = parser.parse_args()

    descriptor = None
    if args.ado_email:
        from app.ado.client import AdoClient
        identity = await AdoClient(pat=get_settings().fetch_pat).resolve_identity(args.ado_email)
        descriptor = identity.descriptor
        print(f"[add_user] bound ADO identity: {identity.display_name} ({descriptor[:24]}...)")

    settings = get_settings()
    session = get_session()
    try:
        if session.query(User).filter_by(username=args.username).one_or_none():
            raise SystemExit(f"[add_user] username '{args.username}' already exists")
        role = "admin" if args.username in settings.admins else "member"
        session.add(User(
            username=args.username, pin_hash=hash_pin(args.pin),
            display_name=args.display_name or args.username, role=role,
            status="active", ado_email=args.ado_email or None,
            ado_descriptor=descriptor,
        ))
        session.commit()
        print(f"[add_user] {args.username} created (role={role}). Everyone else: Team settings UI.")
    finally:
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
