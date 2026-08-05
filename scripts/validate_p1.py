"""Validation smoke: app factory + seed + services import."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.main import create_app  # noqa: E402
app = create_app()
routes = [getattr(r, "path", str(r)) for r in app.routes]
print("routes:")
for r in sorted(set(routes)):
    print(" ", r)

from app.auth.seed_users import seed  # noqa: E402
seed()

from app.db.base import get_session  # noqa: E402
from app.db.models.run import Run  # noqa: E402
from app.db.models.event import Event  # noqa: E402
from app.db.models.user import User  # noqa: E402
from app.db.models.mode import Mode  # noqa: E402
from app.db.models.idea import IdeaThread, IdeaComment  # noqa: E402

s = get_session()
print("users:", [u.username for u in s.query(User).all()])
print("modes:", [m.name for m in s.query(Mode).all()])
demo = s.query(Run).filter(Run.title.like("DEMO%")).one()
events = s.query(Event).filter_by(run_id=demo.id).order_by(Event.seq).all()
print(f"demo run stage={demo.stage}, events={len(events)}, first={events[0].title!r}, last={events[-1].title!r}")
thread = s.query(IdeaThread).first()
print("welcome thread:", thread.title, "| comments:", s.query(IdeaComment).filter_by(thread_id=thread.id).count())

from app.services.runs import compute_available_actions  # noqa: E402
from zagent_contracts import RunStage  # noqa: E402
demo.stage = RunStage.INTERRUPTED.value
print("interrupted actions:", compute_available_actions(demo))
s.close()
