"""Meal-anchored post-prandial analysis — pure functions, unit-testable.

Every function here takes a glucose time series plus a meal timestamp and
answers one question about the excursion that follows. Times are always
minutes elapsed since the meal (matching how a clinician reads a CGM trace),
never minutes since the peak — so time_to_peak and time_to_baseline are
directly comparable and summable.
"""

import numpy as np
import pandas as pd

DEFAULT_BASELINE_WINDOW_MIN = 30
DEFAULT_POST_MEAL_HOURS = 15  # meal through ~next-morning


def baseline_glucose(glucose: pd.DataFrame, meal_ts,
                     window_min: int = DEFAULT_BASELINE_WINDOW_MIN) -> float | None:
    """Mean glucose in the window immediately before the meal."""
    if glucose.empty:
        return None
    window = glucose[(glucose["ts"] >= meal_ts - pd.Timedelta(minutes=window_min)) &
                     (glucose["ts"] < meal_ts)]
    values = window["glucose_mg_dl"].dropna()
    return float(values.mean()) if not values.empty else None


def post_meal_window(glucose: pd.DataFrame, meal_ts,
                     hours: int = DEFAULT_POST_MEAL_HOURS) -> pd.DataFrame:
    """Glucose readings from meal_ts through `hours` after, time-sorted with a
    clean index (downstream functions rely on positional slicing)."""
    if glucose.empty:
        return glucose
    end = meal_ts + pd.Timedelta(hours=hours)
    df = glucose[(glucose["ts"] >= meal_ts) & (glucose["ts"] <= end)]
    df = df.dropna(subset=["glucose_mg_dl"]).sort_values("ts")
    return df.reset_index(drop=True)


def glucose_auc(post_meal: pd.DataFrame, baseline: float | None) -> float | None:
    """Incremental AUC above baseline (mg/dL·min), trapezoidal rule.

    Area below baseline doesn't count — a dip isn't a glucose response.
    """
    if baseline is None or len(post_meal) < 2:
        return None
    minutes = (post_meal["ts"] - post_meal["ts"].iloc[0]).dt.total_seconds() / 60
    excursion = (post_meal["glucose_mg_dl"] - baseline).clip(lower=0)
    return float(np.trapezoid(excursion, minutes))


def glucose_peak(post_meal: pd.DataFrame) -> dict:
    """Peak value and minutes-from-meal-start at which it occurred."""
    if post_meal.empty:
        return {"peak_mg_dl": None, "time_to_peak_min": None}
    i = post_meal["glucose_mg_dl"].idxmax()
    minutes = (post_meal["ts"].loc[i] - post_meal["ts"].iloc[0]).total_seconds() / 60
    return {"peak_mg_dl": float(post_meal["glucose_mg_dl"].loc[i]),
            "time_to_peak_min": round(minutes, 1)}


def time_to_baseline_min(post_meal: pd.DataFrame, baseline: float | None) -> float | None:
    """Minutes from meal start until glucose first falls back to <= baseline,
    searching only after the peak. None if it never returns in the window."""
    if baseline is None or post_meal.empty:
        return None
    i = post_meal["glucose_mg_dl"].idxmax()
    after_peak = post_meal.loc[i:]
    returned = after_peak[after_peak["glucose_mg_dl"] <= baseline]
    if returned.empty:
        return None
    minutes = (returned["ts"].iloc[0] - post_meal["ts"].iloc[0]).total_seconds() / 60
    return round(minutes, 1)


def glucose_rise_velocity(post_meal: pd.DataFrame) -> float | None:
    """Peak rate of rise (mg/dL per minute) between meal start and the peak."""
    rising = _rising_segment(post_meal)
    if rising is None or len(rising) < 2:
        return None
    dt_min = rising["ts"].diff().dt.total_seconds() / 60
    rate = (rising["glucose_mg_dl"].diff() / dt_min).dropna()
    return float(rate.max()) if not rate.empty else None


def glucose_rise_acceleration(post_meal: pd.DataFrame) -> float | None:
    """Peak acceleration of rise (mg/dL per minute^2) between meal start and
    the peak — needs at least 3 rising readings to define a 2nd derivative."""
    rising = _rising_segment(post_meal)
    if rising is None or len(rising) < 3:
        return None
    dt_min = rising["ts"].diff().dt.total_seconds() / 60
    velocity = rising["glucose_mg_dl"].diff() / dt_min
    accel = (velocity.diff() / dt_min).dropna()
    return float(accel.max()) if not accel.empty else None


def _rising_segment(post_meal: pd.DataFrame) -> pd.DataFrame | None:
    if post_meal.empty:
        return None
    i = post_meal["glucose_mg_dl"].idxmax()
    return post_meal.loc[:i].reset_index(drop=True)


def cgm_meal_stats(glucose: pd.DataFrame, meal_ts,
                   baseline_window_min: int = DEFAULT_BASELINE_WINDOW_MIN,
                   post_meal_hours: int = DEFAULT_POST_MEAL_HOURS) -> dict:
    """All CGM statistics for one meal, bundled for the Single Meal view."""
    baseline = baseline_glucose(glucose, meal_ts, baseline_window_min)
    window = post_meal_window(glucose, meal_ts, post_meal_hours)
    auc = glucose_auc(window, baseline)
    velocity = glucose_rise_velocity(window)
    accel = glucose_rise_acceleration(window)
    return {
        "baseline_mg_dl": round(baseline, 1) if baseline is not None else None,
        "auc_mg_dl_min": round(auc) if auc is not None else None,
        **glucose_peak(window),
        "time_to_baseline_min": time_to_baseline_min(window, baseline),
        "peak_velocity_mg_dl_per_min": round(velocity, 2) if velocity is not None else None,
        "peak_acceleration_mg_dl_per_min2": round(accel, 3) if accel is not None else None,
    }


STAT_LABELS = {
    "baseline_mg_dl": "Baseline glucose (mg/dL)",
    "auc_mg_dl_min": "Incremental AUC (mg/dL·min)",
    "peak_mg_dl": "Peak glucose (mg/dL)",
    "time_to_peak_min": "Time to peak (min)",
    "time_to_baseline_min": "Time back to baseline (min)",
    "peak_velocity_mg_dl_per_min": "Peak rise rate (mg/dL/min)",
    "peak_acceleration_mg_dl_per_min2": "Peak rise acceleration (mg/dL/min²)",
}


def compare_meal_stats(stats_a: dict, stats_b: dict,
                       label_a: str = "Meal A", label_b: str = "Meal B") -> pd.DataFrame:
    """Tidy side-by-side comparison table for the Paired Meal Experiment view.

    Delta is B - A wherever both sides have a value, so a reader can see at a
    glance which direction (e.g. exercise) moved each statistic.
    """
    rows = []
    for key, label in STAT_LABELS.items():
        a, b = stats_a.get(key), stats_b.get(key)
        delta = round(b - a, 2) if a is not None and b is not None else None
        rows.append({"Statistic": label, label_a: a, label_b: b, "Δ (B − A)": delta})
    return pd.DataFrame(rows)
