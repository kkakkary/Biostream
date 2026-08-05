"""Tests for experiment.py (the CGM statistics math).

How to read these: each test builds a tiny hand-made glucose series where the
right answer can be computed on paper (the comments show the arithmetic),
calls one function, and asserts the result. pytest runs every function whose
name starts with `test_`; pytest.approx() allows tiny floating-point error.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

# Make the parent folder (health_dashboard/) importable so `import experiment`
# works when pytest runs from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import experiment  # noqa: E402

MEAL_TS = pd.Timestamp("2026-07-18 19:00", tz="UTC")


def _glucose(offsets_min, values):
    """Build a glucose frame from minutes-relative-to-meal + values,
    e.g. _glucose([-10, 30], [80, 150]) = a reading 10 min before the meal
    and one 30 min after."""
    return pd.DataFrame({
        "ts": [MEAL_TS + pd.Timedelta(minutes=m) for m in offsets_min],
        "glucose_mg_dl": values,
    })


def test_baseline_glucose_averages_the_pre_meal_window():
    df = _glucose([-60, -25, -10, 5], [70, 90, 95, 140])  # -60 and +5 excluded
    assert experiment.baseline_glucose(df, MEAL_TS) == pytest.approx(92.5)


def test_baseline_glucose_none_with_no_prior_data():
    df = _glucose([5, 30], [140, 150])  # nothing before the meal
    assert experiment.baseline_glucose(df, MEAL_TS) is None


def test_post_meal_window_filters_and_sorts():
    df = _glucose([-10, 60, 30, 20 * 60], [80, 130, 150, 100])  # last is +20h, outside default 15h
    window = experiment.post_meal_window(df, MEAL_TS)
    assert list(window["glucose_mg_dl"]) == [150, 130]  # +30min then +60min, pre-meal and +20h dropped


def test_glucose_auc_trapezoidal():
    window = _glucose([0, 30, 60], [100, 150, 100])
    # excursion above baseline 100: [0, 50, 0] over 30-min steps
    # = 30*(0+50)/2 + 30*(50+0)/2 = 1500 mg/dL*min
    assert experiment.glucose_auc(window, baseline=100.0) == pytest.approx(1500)


def test_glucose_auc_none_without_baseline():
    window = _glucose([0, 30], [100, 150])
    assert experiment.glucose_auc(window, baseline=None) is None


def test_glucose_peak():
    window = _glucose([0, 30, 60], [100, 150, 100])
    peak = experiment.glucose_peak(window)
    assert peak == {"peak_mg_dl": 150.0, "time_to_peak_min": 30.0}


def test_time_to_baseline_min_returns_minutes_from_meal_start():
    window = _glucose([0, 30, 60], [100, 150, 100])
    assert experiment.time_to_baseline_min(window, baseline=100.0) == 60.0


def test_time_to_baseline_min_none_when_never_returns():
    window = _glucose([0, 30], [100, 150])  # only rises, never comes back down
    assert experiment.time_to_baseline_min(window, baseline=100.0) is None


def test_glucose_rise_velocity():
    window = _glucose([0, 30, 60], [100, 150, 100])
    assert experiment.glucose_rise_velocity(window) == pytest.approx(50 / 30, abs=0.001)


def test_glucose_rise_acceleration():
    window = _glucose([0, 15, 30, 45], [100, 120, 170, 110])  # peak at +30
    # velocities on the rising segment: (120-100)/15=1.333, (170-120)/15=3.333
    # acceleration: (3.333-1.333)/15 = 0.1333 mg/dL/min^2
    assert experiment.glucose_rise_acceleration(window) == pytest.approx(2 / 15, abs=0.001)


def test_glucose_rise_acceleration_none_with_too_few_points():
    window = _glucose([0, 30, 60], [100, 150, 100])  # only 2 rising points
    assert experiment.glucose_rise_acceleration(window) is None


def test_cgm_meal_stats_bundles_everything():
    df = pd.concat([
        _glucose([-30, -15], [95, 105]),       # baseline window, mean=100
        _glucose([0, 30, 60], [100, 150, 100]),  # post-meal excursion
    ], ignore_index=True)

    stats = experiment.cgm_meal_stats(df, MEAL_TS)

    assert stats["baseline_mg_dl"] == 100.0
    assert stats["peak_mg_dl"] == 150.0
    assert stats["time_to_peak_min"] == 30.0
    assert stats["auc_mg_dl_min"] == 1500
    assert stats["time_to_baseline_min"] == 60.0
    assert stats["peak_velocity_mg_dl_per_min"] == pytest.approx(50 / 30, abs=0.01)


def test_cgm_meal_stats_all_none_with_no_data():
    stats = experiment.cgm_meal_stats(pd.DataFrame({"ts": [], "glucose_mg_dl": []}), MEAL_TS)
    assert all(v is None for v in stats.values())


def test_compare_meal_stats_computes_b_minus_a_delta():
    stats_a = {"peak_mg_dl": 150.0, "auc_mg_dl_min": 1000}
    stats_b = {"peak_mg_dl": 120.0, "auc_mg_dl_min": 600}

    table = experiment.compare_meal_stats(stats_a, stats_b, "No exercise", "With exercise")

    peak_row = table[table["Statistic"] == "Peak glucose (mg/dL)"].iloc[0]
    assert peak_row["No exercise"] == 150.0
    assert peak_row["With exercise"] == 120.0
    assert peak_row["Δ (B − A)"] == -30.0


def test_compare_meal_stats_delta_none_when_either_side_missing():
    table = experiment.compare_meal_stats({"peak_mg_dl": 150.0}, {"peak_mg_dl": None})
    peak_row = table[table["Statistic"] == "Peak glucose (mg/dL)"].iloc[0]
    assert peak_row["Δ (B − A)"] is None


# --- The protocol rule: which activity counts as a meal's post-meal exercise ---

def _activities(*offsets_min):
    """Activities starting at the given offsets (minutes) from MEAL_TS.
    Negative = before the meal."""
    return pd.DataFrame({
        "activity_id": range(len(offsets_min)),
        "activity_type": ["walking"] * len(offsets_min),
        "start_ts": [MEAL_TS + pd.Timedelta(minutes=m) for m in offsets_min],
        "end_ts": [MEAL_TS + pd.Timedelta(minutes=m + 30) for m in offsets_min],
    })


def test_has_post_meal_exercise_accepts_activity_inside_the_window():
    # A walk 45 min after eating is the intervention this experiment is about.
    assert experiment.has_post_meal_exercise(_activities(45), MEAL_TS) is True


def test_has_post_meal_exercise_rejects_activity_past_the_window():
    # 121 min out is a different event in the day, not this meal's exercise —
    # the excursion it was meant to blunt is already over.
    assert experiment.has_post_meal_exercise(_activities(121), MEAL_TS) is False
    assert experiment.has_post_meal_exercise(_activities(300), MEAL_TS) is False
    # ...and the boundary itself counts (<=, not <).
    assert experiment.has_post_meal_exercise(_activities(120), MEAL_TS) is True


def test_has_post_meal_exercise_rejects_pre_meal_activity():
    # A walk BEFORE the meal doesn't make it an exercise arm, however close.
    assert experiment.has_post_meal_exercise(_activities(-30), MEAL_TS) is False


def test_has_post_meal_exercise_false_with_no_activities():
    assert experiment.has_post_meal_exercise(_activities(), MEAL_TS) is False


def test_activities_around_meal_keeps_pre_meal_context_when_asked():
    # The charts pass before_min so a pre-meal walk still draws (at negative
    # minutes); the arm test above deliberately does not.
    acts = _activities(-30, 45, 300)

    protocol_only = experiment.activities_around_meal(acts, MEAL_TS)
    with_context = experiment.activities_around_meal(acts, MEAL_TS,
                                                     before_min=120, after_min=120)

    assert len(protocol_only) == 1          # just the +45 walk
    assert len(with_context) == 2           # -30 and +45; +300 is out either way


def test_activities_around_meal_judges_on_start_not_overlap():
    # An activity that began before the window but ran into it is not this
    # meal's: start_ts is what decides.
    late_start = _activities(200)
    late_start.loc[0, "end_ts"] = MEAL_TS + pd.Timedelta(minutes=400)
    assert experiment.activities_around_meal(late_start, MEAL_TS).empty
