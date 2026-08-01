from app.core import deps
from app.core.security import admin_user, current_user


def test_deps_reexports():
    assert deps.current_user is current_user
    assert deps.admin_user is admin_user
    assert deps.__all__ == ["current_user", "admin_user"]
