"""Deep-sleep latency: minutes from falling asleep to the first deep-sleep stage.

Garmin's get_sleep_data(date) response carries two things we combine here:
  dailySleepDTO.sleepStartTimestampGMT — when sleep began (epoch milliseconds)
  sleepLevels — the stage timeline: one {startGMT, endGMT, activityLevel}
                interval per stretch of a single stage, in GMT text timestamps.
                activityLevel codes: 0 = deep, 1 = light, 2 = REM, 3 = awake.

"Latency to deep sleep" = first activityLevel-0 interval's start minus sleep
start. It's a standing sleep-quality question (does an active day buy you a
faster descent into deep sleep?), which is why the sync stores it per night.

This module is deliberately pure stdlib — no Google Cloud, no Garmin client —
so tests/test_sleep_latency.py can exercise it with hand-built dicts and the
CI test job (which only installs the dashboard's requirements) can import it.
"""

from __future__ import annotations

import datetime as dt

# Garmin encodes the stage of each sleepLevels interval as a number; deep
# sleep is 0. Values arrive as floats (0.0) but == 0 matches both int and float.
DEEP_STAGE = 0


def _parse_gmt(text: str | None) -> dt.datetime | None:
    """Parse a sleepLevels timestamp ('2026-08-26T06:12:00.0', GMT) into a
    naive datetime. text[:19] keeps 'YYYY-MM-DDTHH:MM:SS' and drops the '.0'
    fractional tail that strptime's format doesn't expect — the same trick
    main.py's _reading_ts uses on HRV timestamps."""
    if not text:
        return None
    try:
        return dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def deep_sleep_latency_min(sleep: dict | None) -> int | None:
    """Minutes from sleep onset to the night's first deep-sleep interval.

    `sleep` is the FULL get_sleep_data payload (not just dailySleepDTO).
    Returns None whenever the answer can't be computed honestly: no sleep
    record, no stage timeline (older watches / nights Garmin didn't stage),
    or a night with no deep sleep at all.
    """
    sleep = sleep or {}
    dto = sleep.get("dailySleepDTO") or {}

    start_ms = dto.get("sleepStartTimestampGMT")
    if not isinstance(start_ms, (int, float)):
        return None
    # Epoch ms -> naive GMT datetime, matching _parse_gmt's naive output.
    # (fromtimestamp with an explicit UTC zone, then drop the zone — the
    # deprecated utcfromtimestamp shortcut spelled out.)
    start = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).replace(tzinfo=None)

    # Collect every deep interval's start; min() below tolerates an unsorted
    # timeline rather than trusting Garmin to order the list.
    deep_starts = [t for lvl in (sleep.get("sleepLevels") or [])
                   if lvl.get("activityLevel") == DEEP_STAGE
                   and (t := _parse_gmt(lvl.get("startGMT"))) is not None]
    if not deep_starts:
        return None

    latency = (min(deep_starts) - start).total_seconds() / 60
    # Clamp at 0: Garmin occasionally stamps the first stage a hair before
    # the official sleep start; a negative latency is noise, not a finding.
    return max(0, round(latency))
