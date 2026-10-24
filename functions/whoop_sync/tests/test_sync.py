"""Tests for sync.py's pure parsing logic — a tiny fake metrics-service
payload in, rows out. Doesn't touch httpx or BigQuery.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync  # noqa: E402


def test_parse_rows_converts_epoch_ms_to_fixed_pdt_local_time():
    # 2026-02-14 03:00:00/06 UTC -> 2026-02-13 20:00:00/06 local (UTC-7).
    # Deliberately UTC-early-morning so the -7h shift rolls back a calendar
    # day — this pipeline stores local wall-clock time, not UTC, matching
    # glucose/meals' established "fixed PDT" convention.
    payload = {
        "name": "heart_rate",
        "values": [
            {"time": 1771038000000, "data": 54},
            {"time": 1771038006000, "data": 53},
        ],
    }
    rows = sync.parse_rows("christian", payload, "2026-10-23T00:00:00")

    assert len(rows) == 2
    assert rows[0] == {
        "user_id": "christian",
        "ts": "2026-02-13T20:00:00",
        "hr_value": 54,
        "ingested_ts": "2026-10-23T00:00:00",
    }
    assert rows[1]["ts"] == "2026-02-13T20:00:06"
    assert rows[1]["hr_value"] == 53


def test_parse_rows_skips_malformed_points():
    payload = {"values": [
        {"time": None, "data": 54},          # missing time
        {"time": 1771056000000, "data": None},  # missing value
        {"time": "not-a-number", "data": 50},   # wrong type
    ]}
    assert sync.parse_rows("christian", payload, "2026-10-23T00:00:00") == []


def test_parse_rows_handles_empty_payload():
    assert sync.parse_rows("christian", {}, "2026-10-23T00:00:00") == []
