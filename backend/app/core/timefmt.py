"""API timestamp formatting.

Model columns are tz-naive UTC. Emitting a suffix-less ``.isoformat()`` makes
browsers parse the value as LOCAL time — east of UTC, heartbeat ages went
negative and the stale-thread watchdog never fired (W-H9). Every timestamp
that crosses an API boundary goes through ``iso_z`` so the wire always
carries an explicit UTC offset.
"""

from datetime import UTC, datetime


def iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()
