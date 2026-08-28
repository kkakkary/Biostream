"""Tests for sleep_latency.py — hand-built Garmin sleep payloads in, minutes out.

The payloads mimic get_sleep_data(date)'s real shape: a dailySleepDTO with an
epoch-milliseconds sleep start, and a sleepLevels list of stage intervals with
'.0'-suffixed GMT text timestamps (that mismatch of formats is exactly what
the module under test has to reconcile).
"""

import datetime as dt
import sys
from pathlib import Path

# Make the parent folder importable so `import sleep_latency` works under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sleep_latency  # noqa: E402

# One fixed sleep start used by every test: 2026-08-26 06:00:00 GMT
# (11pm Pacific), expressed the way Garmin sends it — epoch milliseconds.
SLEEP_START = dt.datetime(2026, 8, 26, 6, 0, 0, tzinfo=dt.timezone.utc)
SLEEP_START_MS = int(SLEEP_START.timestamp() * 1000)


def _level(minutes_after_start: int, stage: float) -> dict:
    """One sleepLevels interval starting N minutes into the night.
    endGMT is start+10min — the module only reads starts, but real payloads
    always carry both, so the fake should too."""
    start = SLEEP_START + dt.timedelta(minutes=minutes_after_start)
    end = start + dt.timedelta(minutes=10)
    fmt = "%Y-%m-%dT%H:%M:%S.0"   # Garmin's '.0' fractional-second suffix
    return {"startGMT": start.strftime(fmt), "endGMT": end.strftime(fmt),
            "activityLevel": stage}


def _payload(levels) -> dict:
    return {"dailySleepDTO": {"sleepStartTimestampGMT": SLEEP_START_MS},
            "sleepLevels": levels}


def test_latency_is_minutes_to_first_deep_interval():
    # Light for 25 min, then deep: latency = 25.
    sleep = _payload([_level(0, 1.0), _level(25, 0.0), _level(80, 2.0)])
    assert sleep_latency.deep_sleep_latency_min(sleep) == 25


def test_latency_zero_when_night_opens_in_deep():
    sleep = _payload([_level(0, 0.0), _level(40, 1.0)])
    assert sleep_latency.deep_sleep_latency_min(sleep) == 0


def test_unsorted_timeline_still_finds_the_earliest_deep_interval():
    # Garmin isn't trusted to order the list: the 90-min deep block arrives
    # before the 30-min one, and the answer must still be 30.
    sleep = _payload([_level(90, 0.0), _level(30, 0.0), _level(0, 1.0)])
    assert sleep_latency.deep_sleep_latency_min(sleep) == 30


def test_none_when_no_deep_sleep_at_all():
    # A rough night of only light/REM/awake: no deep interval, no latency —
    # None, not 0 (0 would mean "fell into deep sleep instantly").
    sleep = _payload([_level(0, 1.0), _level(60, 2.0), _level(120, 3.0)])
    assert sleep_latency.deep_sleep_latency_min(sleep) is None


def test_none_when_stage_timeline_missing():
    # Older watches / un-staged nights: DTO present, sleepLevels absent.
    sleep = {"dailySleepDTO": {"sleepStartTimestampGMT": SLEEP_START_MS}}
    assert sleep_latency.deep_sleep_latency_min(sleep) is None


def test_none_when_sleep_record_missing_entirely():
    assert sleep_latency.deep_sleep_latency_min(None) is None
    assert sleep_latency.deep_sleep_latency_min({}) is None
    # Timeline present but no sleep-start timestamp to measure from.
    assert sleep_latency.deep_sleep_latency_min(
        {"dailySleepDTO": {}, "sleepLevels": [_level(10, 0.0)]}) is None


def test_negative_latency_clamps_to_zero():
    # Garmin sometimes stamps the first stage a hair before the official
    # sleep start; that's clock noise, reported as 0 rather than -3.
    early = SLEEP_START - dt.timedelta(minutes=3)
    sleep = _payload([{"startGMT": early.strftime("%Y-%m-%dT%H:%M:%S.0"),
                       "endGMT": SLEEP_START.strftime("%Y-%m-%dT%H:%M:%S.0"),
                       "activityLevel": 0.0}])
    assert sleep_latency.deep_sleep_latency_min(sleep) == 0


def test_unparseable_timestamps_are_skipped_not_fatal():
    # A malformed deep interval is ignored; the good one behind it still counts.
    sleep = _payload([{"startGMT": "not-a-time", "activityLevel": 0.0},
                      _level(45, 0.0)])
    assert sleep_latency.deep_sleep_latency_min(sleep) == 45
