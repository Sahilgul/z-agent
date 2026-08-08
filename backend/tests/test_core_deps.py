from app.core import deps
from app.core.security import admin_user, current_user


def test_deps_reexports():
    assert deps.current_user is current_user
    assert deps.admin_user is admin_user
    assert sorted(deps.__all__) == ["admin_user", "current_user"]
