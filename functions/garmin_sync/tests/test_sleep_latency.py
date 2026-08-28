"""Tests for sleep_latency.deep_sleep_latency_seconds — the stage-timeline
math behind garmin_daily.deep_sleep_latency_seconds. The module is stdlib-only
on purpose, so these tests run without the sync function's Garmin/GCP deps.

Fixture shapes mirror the real API: sleepStartTimestampGMT is epoch ms;
sleepLevels entries carry GMT strings with a trailing '.0' and float
activityLevel codes (0=deep, 1=light, 2=REM, 3=awake).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sleep_latency import deep_sleep_latency_seconds  # noqa: E402

# 2026-06-10T05:33:10Z in epoch ms — matches the real payload sanity-checked
# against Garmin Connect (first deep epoch 10 minutes later).
SLEEP_START_MS = 1781069590000
SLEEP = {"sleepStartTimestampGMT": SLEEP_START_MS}


def _level(start: str, code: float) -> dict:
    return {"startGMT": start, "endGMT": start, "activityLevel": code}


def test_latency_to_first_deep_epoch():
    """Light for 10 min, then deep: latency = 600 s. The later deep epoch
    must not win — first deep epoch only."""
    levels = [_level("2026-06-10T05:33:10.0", 1.0),
              _level("2026-06-10T05:43:10.0", 0.0),
              _level("2026-06-10T06:30:00.0", 0.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) == 600


def test_unsorted_levels_still_pick_earliest_deep():
    """The API contract doesn't promise ordering; min() must, not levels[0]."""
    levels = [_level("2026-06-10T06:30:00.0", 0.0),
              _level("2026-06-10T05:43:10.0", 0.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) == 600


def test_integer_activity_level_also_matches():
    """0 == 0.0 — the code must not depend on the float-ness of the payload."""
    levels = [_level("2026-06-10T05:43:10.0", 0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) == 600


def test_night_with_no_deep_sleep_is_none():
    """A night of only light/REM/awake stays NULL — indistinguishable from
    missing data, and a fake huge latency would poison the group means."""
    levels = [_level("2026-06-10T05:43:10.0", 1.0),
              _level("2026-06-10T06:00:00.0", 2.0),
              _level("2026-06-10T06:10:00.0", 3.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) is None


def test_empty_or_missing_inputs_are_none():
    assert deep_sleep_latency_seconds(SLEEP, []) is None
    assert deep_sleep_latency_seconds(SLEEP, None) is None
    assert deep_sleep_latency_seconds({}, [_level("2026-06-10T05:43:10.0", 0.0)]) is None
    assert deep_sleep_latency_seconds(None, [_level("2026-06-10T05:43:10.0", 0.0)]) is None


def test_unparseable_or_missing_timestamps_skipped():
    """A malformed deep epoch is dropped, not crashed on; if a valid one
    remains it still wins."""
    levels = [{"startGMT": None, "activityLevel": 0.0},
              _level("not-a-timestamp", 0.0),
              _level("2026-06-10T05:43:10.0", 0.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) == 600
    assert deep_sleep_latency_seconds(SLEEP, levels[:2]) is None


def test_deep_epoch_before_sleep_start_is_none():
    """Inconsistent data (deep 'before' falling asleep) must not become a
    negative or zero latency."""
    levels = [_level("2026-06-10T05:00:00.0", 0.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) is None


def test_deep_at_sleep_onset_is_zero():
    levels = [_level("2026-06-10T05:33:10.0", 0.0)]
    assert deep_sleep_latency_seconds(SLEEP, levels) == 0
