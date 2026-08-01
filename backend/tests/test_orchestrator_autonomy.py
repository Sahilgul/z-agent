from app.orchestrator.autonomy import (
    AUTONOMOUS, GATED, SUPERVISED, PERMISSION_MODE_BY_AUTONOMY, permission_mode_for,
)


def test_supervised_maps_to_default():
    assert permission_mode_for(SUPERVISED) == "default"


def test_gated_maps_to_acceptEdits():
    assert permission_mode_for(GATED) == "acceptEdits"


def test_autonomous_maps_to_bypass():
    assert permission_mode_for(AUTONOMOUS) == "bypassPermissions"


def test_unknown_autonomy_falls_back_to_default():
    assert permission_mode_for("nonsense") == "default"


def test_mapping_table_completeness():
    assert PERMISSION_MODE_BY_AUTONOMY == {
        SUPERVISED: "default", GATED: "acceptEdits", AUTONOMOUS: "bypassPermissions",
    }
