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


def aware_utc(dt: datetime) -> datetime:
    """Coerce a model-column datetime for arithmetic against aware ``now`` —
    the columns are tz-naive UTC, so a Postgres round-trip returns tz-less
    values and ``aware - naive`` raises."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
