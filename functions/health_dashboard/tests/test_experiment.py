import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import experiment  # noqa: E402

MEAL_TS = pd.Timestamp("2026-07-18 19:00", tz="UTC")


def _glucose(offsets_min, values):
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
