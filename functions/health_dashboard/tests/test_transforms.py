import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

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
