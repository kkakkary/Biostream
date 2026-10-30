"""Tests for sync.py's pure parsing/join logic — tiny fake WHOOP API
payloads in, rows out. Doesn't touch requests or BigQuery.

Named test_aggregates_sync.py, not test_sync.py — pytest's default import
mode collides on identical basenames across directories when CI runs
`pytest functions/ -v` (both this dir and functions/whoop_sync/tests/ had a
module literally named `sync`-under-test), so this needs a unique name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aggregates_sync as sync  # noqa: E402


def test_parse_daily_rows_joins_cycle_recovery_and_sleep_by_cycle_id():
    cycles = [{
        "id": 93845, "start": "2026-10-28T12:00:00.000Z", "end": "2026-10-29T12:00:00.000Z",
        "step_count": 8234,
        "score": {"strain": 5.3, "kilojoule": 8288.3, "average_heart_rate": 68, "max_heart_rate": 141},
    }]
    recovery = [{
        "cycle_id": 93845,
        "score": {"recovery_score": 72, "resting_heart_rate": 52, "hrv_rmssd_milli": 65.4,
                  "spo2_percentage": 97.1, "skin_temp_celsius": 33.2},
    }]
    sleep = [{
        "cycle_id": 93845, "nap": False,
        "score": {"respiratory_rate": 14.5, "sleep_performance_percentage": 98,
                  "sleep_efficiency_percentage": 91.7,
                  "stage_summary": {"total_slow_wave_sleep_time_milli": 3600000,
                                    "total_rem_sleep_time_milli": 1800000,
                                    "total_light_sleep_time_milli": 14400000,
                                    "total_awake_time_milli": 600000}},
    }]

    rows = sync.parse_daily_rows("christian", cycles, recovery, sleep, "2026-10-29T00:00:00")
    assert len(rows) == 1
    r = rows[0]
    assert r["cycle_id"] == 93845
    assert r["day_strain"] == 5.3
    assert r["recovery_score"] == 72
    assert r["hrv_rmssd_milli"] == 65.4
    assert r["resting_hr"] == 52
    assert r["sleep_performance_percentage"] == 98
    # ms -> seconds conversion
    assert r["deep_sleep_seconds"] == 3600
    assert r["rem_sleep_seconds"] == 1800
    assert r["light_sleep_seconds"] == 14400
    assert r["awake_seconds"] == 600
    # fixed-PDT local time (UTC-7): 2026-10-28T12:00:00Z -> 2026-10-28T05:00:00
    assert r["cycle_start"] == "2026-10-28T05:00:00"
    assert r["date"] == "2026-10-28"


def test_parse_daily_rows_keeps_cycle_with_no_recovery_or_sleep_yet():
    # An in-progress cycle (e.g. today) with no scored recovery/sleep yet
    # should still get a row — just with those columns NULL, not dropped.
    cycles = [{"id": 1, "start": "2026-10-29T12:00:00.000Z", "score": {"strain": 2.1}}]
    rows = sync.parse_daily_rows("christian", cycles, recovery=[], sleep=[], ingested_ts="x")
    assert len(rows) == 1
    assert rows[0]["day_strain"] == 2.1
    assert rows[0]["recovery_score"] is None
    assert rows[0]["deep_sleep_seconds"] is None


def test_parse_daily_rows_ignores_naps_when_matching_sleep_to_a_cycle():
    cycles = [{"id": 1, "start": "2026-10-29T12:00:00.000Z", "score": {}}]
    sleep = [{"cycle_id": 1, "nap": True, "score": {"sleep_performance_percentage": 50}}]
    rows = sync.parse_daily_rows("christian", cycles, recovery=[], sleep=sleep, ingested_ts="x")
    assert rows[0]["sleep_performance_percentage"] is None


def test_parse_daily_rows_skips_cycles_with_no_id_or_start():
    cycles = [{"start": "2026-10-29T12:00:00.000Z"}, {"id": 2}]
    assert sync.parse_daily_rows("christian", cycles, [], [], "x") == []


def test_parse_workout_rows_converts_fields_and_local_time():
    workouts = [{
        "id": "abc-123", "start": "2026-10-28T20:00:00.000Z", "end": "2026-10-28T21:00:00.000Z",
        "sport_name": "running",
        "score": {"strain": 9.1, "average_heart_rate": 150, "max_heart_rate": 178,
                  "kilojoule": 2100.5, "distance_meter": 8000.0},
    }]
    rows = sync.parse_workout_rows("christian", workouts, "2026-10-29T00:00:00")
    assert len(rows) == 1
    r = rows[0]
    assert r["workout_id"] == "abc-123"
    assert r["sport_name"] == "running"
    assert r["strain"] == 9.1
    assert r["distance_m"] == 8000.0
    # 2026-10-28T20:00:00Z -> 2026-10-28T13:00:00 local (UTC-7)
    assert r["start_ts"] == "2026-10-28T13:00:00"


def test_parse_workout_rows_skips_workouts_missing_id_or_start():
    assert sync.parse_workout_rows("christian", [{"start": "2026-10-28T20:00:00.000Z"}], "x") == []
    assert sync.parse_workout_rows("christian", [{"id": "x"}], "x") == []


def test_refresh_if_needed_skips_refresh_when_token_still_fresh():
    import datetime as dt
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
    tokens = {"access_token": "a", "refresh_token": "r", "expires_at": future}
    result = sync.refresh_if_needed(tokens, "client-id", "client-secret")
    assert result is tokens   # same object -- no HTTP call, no refresh attempted
