from app.core.security import verify_pin


def test_smoke(session, make_user):
    u = make_user("smoke")
    assert verify_pin("1234", u.pin_hash)
    assert not verify_pin("9999", u.pin_hash)
