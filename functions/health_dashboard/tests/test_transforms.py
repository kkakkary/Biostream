"""Tests for transforms.py — tiny hand-built dataframes in, checked numbers out.
(_daily and _glucose below are helpers that fabricate realistic-shaped test
data so each test stays one readable block.)"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

# Make the parent folder importable so `import transforms` works under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import transforms  # noqa: E402


def _daily(rows):
    cols = ["date", "total_steps", "resting_hr", "avg_stress",
            "body_battery_high", "body_battery_low", "sleep_seconds",
            "deep_sleep_seconds", "rem_sleep_seconds", "hrv_avg"]
    return pd.DataFrame(rows, columns=cols)


def _glucose(values):
    return pd.DataFrame({
        "ts": pd.date_range("2026-07-13", periods=len(values), freq="5min"),
        "glucose_mg_dl": values,
    })


def test_sleep_stages_splits_light_as_remainder():
    daily = _daily([[date(2026, 7, 18), 3000, 51, 31, 96, 20,
                     7 * 3600, 2 * 3600, 1 * 3600, 63]])
    out = transforms.sleep_stages(daily)
    assert out.iloc[0]["deep_h"] == 2.0
    assert out.iloc[0]["rem_h"] == 1.0
    assert out.iloc[0]["light_h"] == 4.0


def test_sleep_stages_drops_missing_and_clamps_negative_light():
    daily = _daily([
        [date(2026, 7, 17), 100, 50, 20, 90, 15, None, None, None, 70],
        # deep+rem exceed total (bad upstream data) -> light clamps to 0
        [date(2026, 7, 18), 100, 50, 20, 90, 15, 3600, 3600, 3600, 70],
    ])
    out = transforms.sleep_stages(daily)
    assert len(out) == 1
    assert out.iloc[0]["light_h"] == 0.0


def test_time_in_range():
    assert transforms.time_in_range(_glucose([65, 100, 150, 200])) == 50.0
    assert transforms.time_in_range(_glucose([])) is None
    assert transforms.time_in_range(_glucose([70,180])) == 100.0


def test_break_time_gaps_inserts_nan_row_inside_gap():
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-07-18 00:00", "2026-07-18 00:05",
                              "2026-07-18 06:00"]),
        "glucose_mg_dl": [80.0, 82.0, 90.0],
    })
    out = transforms.break_time_gaps(df, "ts", pd.Timedelta(minutes=30))
    assert len(out) == 4
    assert out["glucose_mg_dl"].isna().sum() == 1
    # break row sits inside the gap
    nan_ts = out.loc[out["glucose_mg_dl"].isna(), "ts"].iloc[0]
    assert pd.Timestamp("2026-07-18 00:05") < nan_ts < pd.Timestamp("2026-07-18 06:00")


def test_break_time_gaps_no_gap_is_noop():
    df = pd.DataFrame({
        "ts": pd.date_range("2026-07-18", periods=5, freq="5min"),
        "v": range(5),
    })
    assert len(transforms.break_time_gaps(df, "ts", pd.Timedelta("30min"))) == 5


def test_fill_date_gaps_creates_nan_days():
    df = pd.DataFrame({"date": [date(2026, 7, 10), date(2026, 7, 14)],
                       "resting_hr": [50.0, 52.0]})
    out = transforms.fill_date_gaps(df)
    assert len(out) == 5
    assert out["resting_hr"].isna().sum() == 3


def test_kpi_row_latest_and_delta():
    rows = [[date(2026, 7, d), 5000 + d, 50, 30, 95, 20,
             7 * 3600, 3600, 3600, 70] for d in range(10, 18)]
    rows.append([date(2026, 7, 18), 500, 54, 30, 95, 20,
                 6 * 3600, 3600, 3600, 63])
    kpi = transforms.kpi_row(_daily(rows), _glucose([80, 90, 250]))
    assert kpi["resting_hr"] == 54
    assert kpi["resting_hr_delta"] == pytest.approx(4.0)
    assert kpi["sleep_h"] == 6.0
    assert kpi["steps_yday"] == 5017  # newest complete day, not today's partial
    assert kpi["glucose_avg"] == pytest.approx(140.0)
    assert kpi["time_in_range"] == pytest.approx(66.667, abs=0.01)


def test_kpi_row_handles_sparse_history():
    daily = _daily([[date(2026, 7, 18), 1000, 50, 30, 95, 20,
                     None, None, None, None]])
    kpi = transforms.kpi_row(daily, _glucose([]))
    assert kpi["resting_hr"] == 50
    assert kpi["resting_hr_delta"] is None
    assert kpi["sleep_h"] is None
    assert kpi["steps_yday"] is None
    assert kpi["glucose_avg"] is None
    assert kpi["time_in_range"] is None


# --- Activity vs Deep Sleep: day classification + next-night pairing --------

def _asp_daily(rows):
    """Rows for activity_sleep_pairs: only the columns it reads."""
    cols = ["date", "total_steps", "moderate_intensity_min",
            "vigorous_intensity_min", "deep_sleep_latency_min"]
    return pd.DataFrame(rows, columns=cols)


def _asp_acts(rows):
    return pd.DataFrame(rows, columns=["date", "n_activities", "activity_seconds"])


def test_activity_sleep_pairs_uses_next_nights_latency():
    # An active Monday (workout) must be judged by TUESDAY's latency row —
    # garmin_daily stores a night on the morning it ended.
    daily = _asp_daily([
        [date(2026, 8, 24), 12000, 0, 0, 40],   # Mon: active; Mon-morning latency 40 (belongs to Sunday)
        [date(2026, 8, 25), 1000, 0, 0, 15],    # Tue row: latency 15 = Mon night's
    ])
    out = transforms.activity_sleep_pairs(daily, _asp_acts([]))
    mon = out[out["date"] == pd.Timestamp("2026-08-24")]
    assert mon.iloc[0]["group"] == "Active"
    assert mon.iloc[0]["deep_sleep_latency_min"] == 15   # Tue's row, not Mon's


def test_intensity_minutes_alone_make_a_day_active():
    # The un-recorded-exertion case: barely any steps, no logged workout, but
    # 20 moderate + 10 vigorous minutes = 40 weighted (vigorous counts double)
    # -> Active on sustained-elevated-HR evidence alone.
    daily = _asp_daily([
        [date(2026, 8, 24), 2000, 20, 10, None],
        [date(2026, 8, 25), 2000, 0, 0, 22],
    ])
    out = transforms.activity_sleep_pairs(daily, _asp_acts([]))
    assert out.iloc[0]["group"] == "Active"
    assert out.iloc[0]["intensity_min"] == 40


def test_quiet_day_is_sedentary_and_middling_day_is_excluded():
    daily = _asp_daily([
        [date(2026, 8, 24), 3000, 0, 0, None],  # quiet everywhere -> Sedentary
        [date(2026, 8, 25), 7000, 15, 0, 30],   # middling steps + HR -> excluded
        [date(2026, 8, 26), 1000, 0, 0, 25],    # latency rows for both nights
    ])
    out = transforms.activity_sleep_pairs(daily, _asp_acts([]))
    assert list(out["group"]) == ["Sedentary"]
    assert out.iloc[0]["date"] == pd.Timestamp("2026-08-24")


def test_recorded_workout_makes_a_low_step_day_active():
    daily = _asp_daily([
        [date(2026, 8, 24), 2000, 0, 0, None],  # a swim: few steps, logged workout
        [date(2026, 8, 25), 2000, 0, 0, 18],
    ])
    acts = _asp_acts([[date(2026, 8, 24), 1, 3600]])
    out = transforms.activity_sleep_pairs(daily, acts)
    assert out.iloc[0]["group"] == "Active"
    assert out.iloc[0]["n_activities"] == 1


def test_days_without_next_night_latency_are_dropped():
    # Active day, but the following night has no stage timeline (NULL) and
    # the day after doesn't even have a row -> nothing to compare, no pair.
    daily = _asp_daily([
        [date(2026, 8, 24), 15000, 60, 20, None],
        [date(2026, 8, 25), 15000, 60, 20, None],
    ])
    out = transforms.activity_sleep_pairs(daily, _asp_acts([]))
    assert out.empty


def test_no_steps_data_never_counts_as_sedentary():
    # Watch not worn: steps NULL. That day is unknown, not sedentary — only
    # the genuinely-quiet day should classify.
    daily = _asp_daily([
        [date(2026, 8, 24), None, 0, 0, None],
        [date(2026, 8, 25), 500, 0, 0, 12],
        [date(2026, 8, 26), 1000, 0, 0, 33],
    ])
    out = transforms.activity_sleep_pairs(daily, _asp_acts([]))
    assert list(out["date"]) == [pd.Timestamp("2026-08-25")]
    assert list(out["group"]) == ["Sedentary"]
