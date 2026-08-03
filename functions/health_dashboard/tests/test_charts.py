"""Tests for charts.py — build a figure from tiny data, then inspect the
figure OBJECT (its traces, shapes, layout) rather than rendering pixels.
That's the standard way to test plotting code: assert on structure, not looks.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

# Make the parent folder importable so `import charts` works under pytest.
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
    activities = pd.DataFrame({
        "activity_id": [1], "activity_type": ["walking"], "activity_name": ["walk"],
        "start_ts": [meal_ts + pd.Timedelta(hours=1)],
        "end_ts": [meal_ts + pd.Timedelta(hours=1, minutes=30)],
    })
    bp = pd.DataFrame({"measurement_ts_utc": [meal_ts + pd.Timedelta(hours=14)],
                       "systolic": [120], "diastolic": [78]})

    fig = charts.meal_timeline_fig(glucose, activities, bp, meal_ts, baseline=95.0)

    # 1 line trace (CGM) + exercise shading is a shape, not a trace.
    assert len(fig.data) == 1
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


def test_paired_cgm_overlay_fig_marks_meal_and_activity_bands():
    # Two meals on different days — the overlay must line them up anyway,
    # because everything is converted to minutes-since-EACH-meal.
    meal_a = pd.Timestamp("2026-07-18 19:00", tz="UTC")
    meal_b = pd.Timestamp("2026-07-20 12:00", tz="UTC")

    def _window(meal_ts):
        return pd.DataFrame({
            "ts": [meal_ts, meal_ts + pd.Timedelta(minutes=30)],
            "glucose_mg_dl": [100.0, 150.0],
        })

    def _walk(start, end):
        return pd.DataFrame({
            "activity_id": [1], "activity_type": ["walking"], "activity_name": ["walk"],
            "start_ts": [start], "end_ts": [end],
        })

    # A walked 60–90 min AFTER the meal; B walked 30–10 min BEFORE it.
    acts_a = _walk(meal_a + pd.Timedelta(minutes=60), meal_a + pd.Timedelta(minutes=90))
    acts_b = _walk(meal_b - pd.Timedelta(minutes=30), meal_b - pd.Timedelta(minutes=10))

    fig = charts.paired_cgm_overlay_fig(_window(meal_a), _window(meal_b), "Meal A", "Meal B",
                                        activities_a=acts_a, activities_b=acts_b,
                                        meal_ts_a=meal_a, meal_ts_b=meal_b)

    # Meal marker: a dashed vertical LINE shape at x=0 (shared by both meals).
    lines = [s for s in fig.layout.shapes if s.type == "line"]
    assert any(s.x0 == 0 and s.line.dash == "dash" for s in lines)

    # Activity bands: RECT shapes at minutes-since-their-own-meal, each tinted
    # with its meal's wash. A's walk spans +60..+90; B's spans -30..-10
    # (negative = pre-meal, deliberately not clamped).
    rects = {s.fillcolor: (s.x0, s.x1)
             for s in fig.layout.shapes if s.type == "rect" and s.fillcolor != charts.BAND}
    assert rects[charts.EXERCISE_BAND_BLUE] == (60.0, 90.0)
    assert rects[charts.EXERCISE_BAND_ORANGE] == (-30.0, -10.0)

    # Band labels say what happened and whose meal it was, colored to match.
    texts = {a.text: a.font.color for a in fig.layout.annotations}
    assert texts.get("walking (A)") == charts.BLUE
    assert texts.get("walking (B)") == charts.ORANGE
    assert "Meal" in texts  # the minute-0 marker is annotated too


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