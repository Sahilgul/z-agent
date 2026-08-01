"""Autonomy dial <-> SDK permission_mode mapping (plan §6, verified against SDK
source). WARNING: the SDK SHADOWS can_use_tool under auto-approving modes —
Supervised must stay on 'default' or approvals silently vanish.

The auto-allow FLOOR lives worker-side (worker/approvals.py AUTO_ALLOW_TOOLS):
read/grep/glob always pass without a card; without the floor the dial's bottom
setting is decorative.
"""

from __future__ import annotations

SUPERVISED = "supervised"
GATED = "gated"
AUTONOMOUS = "autonomous"

PERMISSION_MODE_BY_AUTONOMY = {
    SUPERVISED: "default",           # every non-floor tool bridged through can_use_tool
    GATED: "acceptEdits",            # file edits auto-approved BY DESIGN; bash/git/MCP bridged
    AUTONOMOUS: "bypassPermissions", # nothing bridged, everything logged to the event stream
}


def permission_mode_for(autonomy: str) -> str:
    return PERMISSION_MODE_BY_AUTONOMY.get(autonomy, "default")
