import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import charts  # noqa: E402


def test_glucose_fig_breaks_line_at_data_gaps():
    # Arrange: two readings 5 min apart, then a 6-hour hole, then one more.
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-07-18 00:00", "2026-07-18 00:05",
                              "2026-07-18 06:00"]),
        "glucose_mg_dl": [80.0, 82.0, 90.0],
    })

    # Act
    fig = charts.glucose_fig(df)

    # Assert: the plotted y-values contain a None/NaN where the hole is,
    # which is what makes Plotly lift the pen instead of bridging the gap.
    y = fig.data[0].y
    assert pd.isna(y).sum() == 1
    assert len(y) == 4  # 3 real points + 1 inserted break


def test_bp_fig_has_one_trace_per_series():
    df = pd.DataFrame({
        "measurement_ts_utc": pd.to_datetime(["2026-07-02", "2026-07-03"]),
        "systolic": [130, 118],
        "diastolic": [79, 75],
        "pulse": [60, 62],
    })

    fig = charts.bp_fig(df)

    assert [t.name for t in fig.data] == ["Systolic", "Diastolic"]
    assert fig.layout.showlegend is True


def test_meal_timeline_fig_marks_exercise_and_bp():
    meal_ts = pd.Timestamp("2026-07-18 19:00", tz="UTC")
    times = pd.date_range(meal_ts - pd.Timedelta(hours=1), meal_ts + pd.Timedelta(hours=3), freq="15min")
    glucose = pd.DataFrame({"ts": times, "glucose_mg_dl": [100.0] * len(times)})
    intraday = pd.DataFrame({"ts": times, "heart_rate": [70.0] * len(times)})
    activities = pd.DataFrame({
        "activity_id": [1], "activity_type": ["walking"], "activity_name": ["walk"],
        "start_ts": [meal_ts + pd.Timedelta(hours=1)],
        "end_ts": [meal_ts + pd.Timedelta(hours=1, minutes=30)],
    })
    bp = pd.DataFrame({"measurement_ts_utc": [meal_ts + pd.Timedelta(hours=14)],
                       "systolic": [120], "diastolic": [78]})

    fig = charts.meal_timeline_fig(glucose, intraday, activities, bp, meal_ts, baseline=95.0)

    # 2 line traces (CGM, HR) + exercise shading is a shape, not a trace.
    assert len(fig.data) == 2
    assert any(s.fillcolor == charts.EXERCISE_BAND for s in fig.layout.shapes)


def test_paired_cgm_overlay_fig_uses_minutes_since_meal_axis():
    meal_ts = pd.Timestamp("2026-07-18 19:00", tz="UTC")
    window = pd.DataFrame({
        "ts": [meal_ts, meal_ts + pd.Timedelta(minutes=30)],
        "glucose_mg_dl": [100.0, 150.0],
    })

    fig = charts.paired_cgm_overlay_fig(window, window, "No exercise", "With exercise")

    assert [t.name for t in fig.data] == ["No exercise", "With exercise"]
    assert list(fig.data[0].x) == [0.0, 30.0]  # minutes since meal, not wall-clock


def test_sleep_fig_stacks_three_stages():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-18"]),
        "deep_h": [2.0],
        "rem_h": [1.0],
        "light_h": [4.0],
    })

    fig = charts.sleep_fig(df)

    assert [t.name for t in fig.data] == ["Deep", "REM", "Light"]
    assert fig.data[0].y[0] == 2.0  # Deep trace carries deep_h, not a swap
    assert fig.layout.barmode == "stack"