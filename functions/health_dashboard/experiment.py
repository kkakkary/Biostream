"""Meal-anchored post-prandial analysis — pure functions, unit-testable.

Most functions here take a glucose time series plus a meal timestamp and
answer one question about the excursion that follows. The exceptions are the
two activity helpers at the top, which encode the experiment's protocol rule:
which Garmin activity counts as a given meal's post-meal exercise. Times are always
minutes elapsed since the meal (matching how a clinician reads a CGM trace),
never minutes since the peak — so time_to_peak and time_to_baseline are
directly comparable and summable.

HOW TO READ THIS FILE: cgm_meal_stats() at the bottom is the one function
app.py calls; everything above it computes one statistic each. The shared
vocabulary:

  baseline  — your average glucose in the 30 min *before* the meal
  excursion — how far glucose rises above that baseline after eating
  AUC       — "area under the curve": total excursion x time, the single best
              summary of how much a meal moved your glucose overall
  velocity / acceleration — how *fast* the rise happened (first and second
              derivatives of the curve on its way up to the peak)
"""

from typing import NamedTuple

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

DEFAULT_BASELINE_WINDOW_MIN = 30
DEFAULT_POST_MEAL_HOURS = 15  # meal through ~next-morning

# THE EXPERIMENT PROTOCOL: an activity only counts as a meal's "post-meal
# exercise" if it started within this many minutes of eating. A walk three
# hours later is a different event in the day, not this meal's intervention —
# by then the excursion it was supposed to blunt is already over.
POST_MEAL_EXERCISE_MAX_MIN = 120


def activities_around_meal(activities: pd.DataFrame, meal_ts,
                           before_min: int = 0,
                           after_min: int = POST_MEAL_EXERCISE_MAX_MIN) -> pd.DataFrame:
    """Activities whose START falls in [meal - before_min, meal + after_min].

    Judged on start_ts, not overlap: an activity that began in the window is
    this meal's, and one that began outside it isn't, however long it ran.
    before_min defaults to 0 — the protocol window is post-meal only; callers
    that want pre-meal context on a chart pass a positive before_min.
    """
    if activities.empty:
        return activities
    start = meal_ts - pd.Timedelta(minutes=before_min)
    end = meal_ts + pd.Timedelta(minutes=after_min)
    return activities[(activities["start_ts"] >= start) &
                      (activities["start_ts"] <= end)]


def has_post_meal_exercise(activities: pd.DataFrame, meal_ts,
                           max_min: int = POST_MEAL_EXERCISE_MAX_MIN) -> bool:
    """Is this meal an exercise arm? True when at least one activity started
    between the meal and `max_min` minutes after it."""
    return not activities_around_meal(activities, meal_ts, 0, max_min).empty


def baseline_glucose(glucose: pd.DataFrame, meal_ts,
                     window_min: int = DEFAULT_BASELINE_WINDOW_MIN) -> float | None:
    """Mean glucose in the window immediately before the meal.
    Note `< meal_ts`, not `<=`: a reading at the exact meal instant belongs
    to the response, not the baseline."""
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
    # reset_index renumbers rows 0,1,2,... — needed because .loc[:i] slicing
    # in the functions below assumes labels equal positions.
    return df.reset_index(drop=True)


def glucose_auc(post_meal: pd.DataFrame, baseline: float | None) -> float | None:
    """Incremental AUC above baseline (mg/dL·min), trapezoidal rule.

    Area below baseline doesn't count — a dip isn't a glucose response.

    Mechanics: x-axis = minutes since the first reading; y-axis = how far each
    reading sits above baseline (clipped at 0 so dips contribute nothing);
    np.trapezoid sums the trapezoid strips between consecutive readings.
    Using actual minutes as x means unevenly spaced readings are weighted
    correctly — a value that held for 15 min counts 3x one that held for 5.
    """
    if baseline is None or len(post_meal) < 2:
        return None
    minutes = (post_meal["ts"] - post_meal["ts"].iloc[0]).dt.total_seconds() / 60
    excursion = (post_meal["glucose_mg_dl"] - baseline).clip(lower=0)
    return float(np.trapezoid(excursion, minutes))


def glucose_peak(post_meal: pd.DataFrame) -> dict:
    """Peak value and minutes-from-meal-start at which it occurred.
    idxmax() = index label of the maximum value (the peak's row)."""
    if post_meal.empty:
        return {"peak_mg_dl": None, "time_to_peak_min": None}
    i = post_meal["glucose_mg_dl"].idxmax()
    minutes = (post_meal["ts"].loc[i] - post_meal["ts"].iloc[0]).total_seconds() / 60
    return {"peak_mg_dl": float(post_meal["glucose_mg_dl"].loc[i]),
            "time_to_peak_min": round(minutes, 1)}


def time_to_baseline_min(post_meal: pd.DataFrame, baseline: float | None) -> float | None:
    """Minutes from meal start until glucose first falls back to <= baseline,
    searching only after the peak. None if it never returns in the window.

    Why only after the peak: glucose is usually AT baseline right after the
    meal (it hasn't risen yet) — counting that would always give ~0."""
    if baseline is None or post_meal.empty:
        return None
    i = post_meal["glucose_mg_dl"].idxmax()
    after_peak = post_meal.loc[i:]                       # peak row onward
    returned = after_peak[after_peak["glucose_mg_dl"] <= baseline]
    if returned.empty:
        return None
    minutes = (returned["ts"].iloc[0] - post_meal["ts"].iloc[0]).total_seconds() / 60
    return round(minutes, 1)


def glucose_rise_velocity(post_meal: pd.DataFrame) -> float | None:
    """Peak rate of rise (mg/dL per minute) between meal start and the peak.

    .diff() = change from the previous reading, so value-diff / time-diff is
    the slope between each consecutive pair; .max() takes the steepest one.
    """
    rising = _rising_segment(post_meal)
    if rising is None or len(rising) < 2:
        return None
    dt_min = rising["ts"].diff().dt.total_seconds() / 60
    rate = (rising["glucose_mg_dl"].diff() / dt_min).dropna()
    return float(rate.max()) if not rate.empty else None


def glucose_rise_acceleration(post_meal: pd.DataFrame) -> float | None:
    """Peak acceleration of rise (mg/dL per minute^2) between meal start and
    the peak — needs at least 3 rising readings to define a 2nd derivative.
    Same construction as velocity, applied once more (diff of the diffs)."""
    rising = _rising_segment(post_meal)
    if rising is None or len(rising) < 3:
        return None
    dt_min = rising["ts"].diff().dt.total_seconds() / 60
    velocity = rising["glucose_mg_dl"].diff() / dt_min
    accel = (velocity.diff() / dt_min).dropna()
    return float(accel.max()) if not accel.empty else None


def _rising_segment(post_meal: pd.DataFrame) -> pd.DataFrame | None:
    """Readings from the meal up to and including the peak — the 'way up'."""
    if post_meal.empty:
        return None
    i = post_meal["glucose_mg_dl"].idxmax()
    return post_meal.loc[:i].reset_index(drop=True)


def cgm_meal_stats(glucose: pd.DataFrame, meal_ts,
                   baseline_window_min: int = DEFAULT_BASELINE_WINDOW_MIN,
                   post_meal_hours: int = DEFAULT_POST_MEAL_HOURS) -> dict:
    """All CGM statistics for one meal, bundled for the Single Meal view.
    `**glucose_peak(window)` splices that function's two keys straight into
    this dict (dict-unpacking)."""
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


# Maps internal stat keys -> human-readable table labels (also fixes the
# display order of the Statistics table).
STAT_LABELS = {
    "baseline_mg_dl": "Baseline glucose (mg/dL)",
    "auc_mg_dl_min": "Incremental AUC (mg/dL·min)",
    "peak_mg_dl": "Peak glucose (mg/dL)",
    "time_to_peak_min": "Time to peak (min)",
    "time_to_baseline_min": "Time back to baseline (min)",
    "peak_velocity_mg_dl_per_min": "Peak rise rate (mg/dL/min)",
    "peak_acceleration_mg_dl_per_min2": "Peak rise acceleration (mg/dL/min²)",
}


class WindowStat(NamedTuple):
    """A stat whose table cell shows more than a bare number — a value plus
    the clock-time window it occurred in (see the 5-min rolling HRV windows
    in hrv_sleep_stats). compare_meal_stats shows `display` in the Meal A/B
    columns but computes the Δ column from `value` alone, per the "delta
    should only show the delta of the max/min window values, not the delta
    between clock times" requirement."""
    display: str
    value: float | None


ROLLING_HRV_WINDOW = "5min"


def _fmt_clock_range(end_ts, window: pd.Timedelta) -> str:
    """'1:03 - 1:08 am' / '11:55 pm - 12:00 am' — a clock-time range ending
    at end_ts. AM/PM is shown once, on the end, when both sides share the
    same period; shown on both sides only when the window crosses noon/
    midnight. (No %-I here: Windows' C runtime doesn't support strftime's
    '-' no-pad flag — see app.py's _fmt_time_12h for the same workaround.)
    """
    start_ts = end_ts - window

    def parts(ts):
        return f"{int(ts.strftime('%I'))}:{ts.strftime('%M')}", ts.strftime("%p").lower()

    start_clock, start_period = parts(start_ts)
    end_clock, end_period = parts(end_ts)
    if start_period == end_period:
        return f"{start_clock} - {end_clock} {end_period}"
    return f"{start_clock} {start_period} - {end_clock} {end_period}"


def _rolling_hrv_window_extremes(hrv: pd.DataFrame) -> tuple[WindowStat | None, WindowStat | None]:
    """The highest- and lowest-average trailing ROLLING_HRV_WINDOW (5 min) of
    HRV in the night, as (highest, lowest) WindowStats — or (None, None) when
    there's no data. Time-based rolling (not a fixed reading count), so it's
    correct regardless of gaps or Garmin's sampling rate not being exactly
    uniform; each window ends AT a reading's timestamp, e.g. the window
    labelled "1:03 - 1:08" is the 5 minutes up to and including the 1:08
    reading, matching how .rolling() on a time index works.
    """
    s = (hrv.dropna(subset=["ts", "hrv_value"])
            .sort_values("ts")
            .set_index("ts")["hrv_value"])
    if s.empty:
        return None, None
    rolled = s.rolling(ROLLING_HRV_WINDOW, min_periods=1).mean()
    window = pd.Timedelta(ROLLING_HRV_WINDOW)
    hi_end, lo_end = rolled.idxmax(), rolled.idxmin()
    hi_val = round(float(rolled.loc[hi_end]), 1)
    lo_val = round(float(rolled.loc[lo_end]), 1)
    hi = WindowStat(f"{hi_val:.1f} ms {_fmt_clock_range(hi_end, window)}", hi_val)
    lo = WindowStat(f"{lo_val:.1f} ms {_fmt_clock_range(lo_end, window)}", lo_val)
    return hi, lo


def hrv_sleep_stats(hrv: pd.DataFrame) -> dict:
    """Duration/average/min/max/time-to-peak/rolling-extreme HRV across one
    night's readings, for the Paired Meal Experiment's Sleep HRV Statistics
    table. dropna() is the "barring gaps/na values" guard — a missing
    reading can't be anyone's min/max/duration/peak.

    Duration is the span between the first and last HRV reading — the same
    two points charts.py marks as "Sleep"/"Wake" on the overlay chart — not
    Garmin's own sleep_seconds total, which this pipeline's HRV coverage
    doesn't reliably span end-to-end (see module docstring in app.py).
    """
    values = hrv["hrv_value"].dropna() if not hrv.empty else pd.Series(dtype=float)
    ts = hrv["ts"].dropna() if not hrv.empty else pd.Series(dtype="datetime64[ns]")
    duration_hrs = (round((ts.max() - ts.min()).total_seconds() / 3600, 1)
                    if len(ts) >= 2 else None)
    if values.empty:
        return {"duration_hrs": duration_hrs, "avg_hrv_ms": None,
                "min_hrv_ms": None, "max_hrv_ms": None, "time_to_peak_hrv_min": None,
                "max5_hrv": None, "min5_hrv": None}
    # idxmax() on `values` (not `hrv`) so a dropped NaN row can't be picked;
    # its label still indexes correctly into hrv["ts"] since dropna() keeps
    # original row labels, only removing rows, not renumbering them.
    peak_idx = values.idxmax()
    time_to_peak_hrv_min = round((hrv["ts"].loc[peak_idx] - ts.iloc[0]).total_seconds() / 60, 1)
    max5, min5 = _rolling_hrv_window_extremes(hrv)
    return {
        "duration_hrs": duration_hrs,
        "avg_hrv_ms": round(float(values.mean()), 1),
        "min_hrv_ms": round(float(values.min()), 1),
        "max_hrv_ms": round(float(values.max()), 1),
        "time_to_peak_hrv_min": time_to_peak_hrv_min,
        "max5_hrv": max5,
        "min5_hrv": min5,
    }


# Same idea as STAT_LABELS, for hrv_sleep_stats' keys.
HRV_STAT_LABELS = {
    "duration_hrs": "Sleep Duration (hrs)",
    "avg_hrv_ms": "Average HRV (ms)",
    "min_hrv_ms": "Min HRV (ms)",
    "max_hrv_ms": "Max HRV (ms)",
    "time_to_peak_hrv_min": "Time to Peak HRV (min)",
    "max5_hrv": "Highest 5-min. HRV",
    "min5_hrv": "Lowest 5-min. HRV",
}


# --------------------------------------------------------------------------- #
# Intensity × Sleep view: does an active day speed up reaching deep sleep?
# --------------------------------------------------------------------------- #

# The grouping threshold: a day earning at least this many Garmin intensity
# minutes counts as an "active day". 20/day tracks the common public-health
# target of 150 moderate-intensity minutes a week.
INTENSITY_THRESHOLD_MIN = 20


def garmin_intensity_minutes(df: pd.DataFrame) -> pd.Series:
    """Garmin-convention intensity minutes for each day: moderate + 2×vigorous
    (Garmin credits each vigorous minute as two toward its weekly goal).
    fillna(0): a day with a summary but e.g. no vigorous minutes means zero
    earned, not unknown."""
    return (df["moderate_intensity_minutes"].fillna(0)
            + 2 * df["vigorous_intensity_minutes"].fillna(0))


def split_latency_by_intensity(df: pd.DataFrame,
                               threshold: int = INTENSITY_THRESHOLD_MIN
                               ) -> tuple[pd.Series, pd.Series]:
    """Split the paired day/night rows (see data.load_intensity_sleep) into
    (active_days, rest_days) — each a Series of that night's deep-sleep
    latency in MINUTES, grouped by whether the preceding day reached
    `threshold` intensity minutes."""
    latency_min = df["deep_sleep_latency_seconds"].astype(float) / 60
    intensity = garmin_intensity_minutes(df)
    return latency_min[intensity >= threshold], latency_min[intensity < threshold]


def welch_t_test(a: pd.Series, b: pd.Series) -> dict:
    """Welch's two-sample t-test (unequal variances) comparing group means,
    plus the per-group descriptives the view shows alongside it. Welch over
    Student's because nothing guarantees the two groups spread equally —
    it's the safe default and costs nothing when variances happen to match.

    Needs at least 2 values per side to estimate variances; below that the
    test fields come back None and the ns still report what data exists.
    """
    a, b = pd.Series(a).dropna(), pd.Series(b).dropna()
    out = {
        "n_a": len(a), "n_b": len(b),
        "mean_a": float(a.mean()) if len(a) else None,
        "mean_b": float(b.mean()) if len(b) else None,
        # ddof=1 = sample (not population) standard deviation; needs n >= 2.
        "sd_a": float(a.std(ddof=1)) if len(a) >= 2 else None,
        "sd_b": float(b.std(ddof=1)) if len(b) >= 2 else None,
        "t_stat": None, "p_value": None,
    }
    if len(a) >= 2 and len(b) >= 2:
        result = scipy_stats.ttest_ind(a, b, equal_var=False)
        out["t_stat"] = float(result.statistic)
        out["p_value"] = float(result.pvalue)
    return out


def compare_meal_stats(stats_a: dict, stats_b: dict,
                       label_a: str = "Meal A", label_b: str = "Meal B",
                       stat_labels: dict = STAT_LABELS) -> pd.DataFrame:
    """Tidy side-by-side comparison table for the Paired Meal Experiment view.
    stat_labels selects which stat dict this table is for (STAT_LABELS for
    cgm_meal_stats, HRV_STAT_LABELS for hrv_sleep_stats) — same shape either
    way, so one function builds both tables.

    Delta is B - A wherever both sides have a value, so a reader can see at a
    glance which direction (e.g. exercise) moved each statistic. A WindowStat
    value (see hrv_sleep_stats) shows its `display` string in the Meal A/B
    columns but still computes Δ from its bare `value`, not the display text.
    """
    def cell(stat):
        return stat.display if isinstance(stat, WindowStat) else stat

    def numeric(stat):
        return stat.value if isinstance(stat, WindowStat) else stat

    rows = []
    for key, label in stat_labels.items():
        a, b = numeric(stats_a.get(key)), numeric(stats_b.get(key))
        delta = round(b - a, 2) if a is not None and b is not None else None
        rows.append({"Statistic": label, label_a: cell(stats_a.get(key)),
                     label_b: cell(stats_b.get(key)), "Δ (B − A)": delta})
    return pd.DataFrame(rows)
