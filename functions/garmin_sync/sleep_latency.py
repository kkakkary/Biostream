"""Deep-sleep latency from Garmin's sleepLevels stage timeline.

Garmin's full get_sleep_data() response carries a "sleepLevels" array — the
night chopped into stage epochs, each {startGMT, endGMT, activityLevel} with
activityLevel coding the stage: 0=deep, 1=light, 2=REM, 3=awake. "Deep-sleep
latency" is the time from sleep onset (dailySleepDTO.sleepStartTimestampGMT)
to the start of the first deep epoch — i.e. how long it took to reach deep
sleep after falling asleep.

Kept in its own stdlib-only module (no garminconnect / google-cloud imports)
so both the garmin_sync Cloud Function and the local backfill script can use
it, and the test suite can import it without the function's dependencies.
"""

from __future__ import annotations

import datetime as dt

DEEP = 0  # Garmin activityLevel code for deep sleep (arrives as 0.0)


def _epoch_ms(ts: str | None) -> float | None:
    """Parse a sleepLevels timestamp ('2026-06-10T05:43:10.0', GMT) to epoch
    milliseconds. [:19] drops the '.0' fractional tail strptime won't take."""
    if not isinstance(ts, str):
        return None
    try:
        parsed = dt.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc).timestamp() * 1000


def deep_sleep_latency_seconds(sleep: dict, levels: list) -> int | None:
    """Seconds from sleep onset to the first deep-sleep epoch.

    None when it can't be computed: no sleep window recorded, no stage data,
    or a night that never reached deep sleep at all (indistinguishable from
    missing stage data, so it is left NULL rather than stored as a fake
    huge latency). Also None if the first deep epoch precedes the recorded
    sleep start — that's inconsistent data, not a zero latency.
    """
    start_ms = (sleep or {}).get("sleepStartTimestampGMT")
    if not isinstance(start_ms, (int, float)):
        return None
    deep_starts = [ms for lvl in (levels or []) if isinstance(lvl, dict)
                   and lvl.get("activityLevel") == DEEP
                   and (ms := _epoch_ms(lvl.get("startGMT"))) is not None]
    if not deep_starts:
        return None
    latency_s = (min(deep_starts) - start_ms) / 1000
    return int(round(latency_s)) if latency_s >= 0 else None
