"""Pure dataframe transforms — no Streamlit, no BigQuery, unit-testable.

"Pure" means: same input -> same output, no side effects, nothing external
touched. That's what makes tests/test_transforms.py able to test these with
tiny hand-built dataframes and no cloud access.
"""

import pandas as pd

GLUCOSE_RANGE_MG_DL = (70, 180)  # standard CGM time-in-range window

# --- Activity vs Deep Sleep: what makes a day "Active" or "Sedentary" -------
# Three independent exertion signals, because no single one covers every day:
#   recorded activities — only exist when a workout was deliberately logged
#   intensity minutes   — Garmin's own "prolonged elevated heart rate" metric:
#                         minutes spent with HR sustained in the moderate/
#                         vigorous zones, workout logged or not. This is the
#                         signal that catches un-recorded exertion.
#   steps               — catches long walking days the HR zones may miss
# A day is Active on ANY strong signal; Sedentary only when EVERY signal is
# quiet. The gap between the cutoffs is deliberate: a middling day (say 7k
# steps, 15 intensity minutes) is neither, and including it in either group
# would blur the very contrast being tested — those days are excluded.
ACTIVE_INTENSITY_MIN = 30     # >= this many intensity minutes -> Active
SEDENTARY_INTENSITY_MIN = 10  # < this (and quiet elsewhere) -> Sedentary
ACTIVE_STEPS = 10_000
SEDENTARY_STEPS = 5_000
EXCLUDED = "In-between"       # group label for days that are neither


def sleep_stages(daily: pd.DataFrame) -> pd.DataFrame:
    """Split nightly sleep into deep / REM / light hours.

    Light sleep is not stored; it's the remainder of total minus deep and REM.
    Nights with no sleep data are dropped.
    """
    # .copy() so we mutate our own frame, not the caller's (pandas warns otherwise).
    df = daily.dropna(subset=["sleep_seconds"]).copy()
    df = df[df["sleep_seconds"] > 0]
    deep = df["deep_sleep_seconds"].fillna(0)
    rem = df["rem_sleep_seconds"].fillna(0)
    df["deep_h"] = deep / 3600
    df["rem_h"] = rem / 3600
    # clip(lower=0): if deep+REM ever exceeds the total (bad vendor data),
    # report 0 light sleep rather than a negative number.
    df["light_h"] = ((df["sleep_seconds"] - deep - rem).clip(lower=0)) / 3600
    return df[["date", "deep_h", "rem_h", "light_h"]]


def time_in_range(glucose: pd.DataFrame,
                  lo: float = GLUCOSE_RANGE_MG_DL[0],
                  hi: float = GLUCOSE_RANGE_MG_DL[1]) -> float | None:
    """Percent of CGM readings inside [lo, hi]. None when there are no readings.

    The trick: `(values >= lo) & (values <= hi)` is a series of True/False,
    and .mean() of booleans = fraction of Trues; *100 makes it a percent.
    """
    values = glucose["glucose_mg_dl"].dropna()
    if values.empty:
        return None
    return float(((values >= lo) & (values <= hi)).mean() * 100)


def break_time_gaps(df: pd.DataFrame, ts_col: str,
                    max_gap: pd.Timedelta) -> pd.DataFrame:
    """Insert an all-NaN row inside every sampling gap wider than max_gap,
    so line charts show a break instead of a false bridge across missing data.

    (Plotly draws a straight line between consecutive points; a NaN point in
    the middle of a gap forces the pen to lift.)
    """
    if len(df) < 2:
        return df
    df = df.sort_values(ts_col).reset_index(drop=True)
    # .diff() = time since the previous reading; True where that exceeds max_gap.
    gap_starts = df[ts_col].diff() > max_gap
    if not gap_starts.any():
        return df
    # Build one row per gap, timestamped just inside it. Only ts_col is set,
    # so every value column is NaN — exactly what breaks the line.
    breaks = pd.DataFrame({
        ts_col: df.loc[gap_starts, ts_col] - max_gap / 2,
    })
    return (pd.concat([df, breaks], ignore_index=True)
            .sort_values(ts_col).reset_index(drop=True))


def fill_date_gaps(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    """Reindex a daily frame onto its full calendar range so missing days
    become NaN rows (line charts then break instead of bridging them).
    Same goal as break_time_gaps, but for daily data: build the complete
    day-by-day calendar with date_range, then reindex onto it."""
    if df.empty:
        return df
    df = df.sort_values(date_col)
    idx = pd.date_range(df[date_col].min(), df[date_col].max(), freq="D")
    out = (df.set_index(pd.to_datetime(df[date_col])).drop(columns=[date_col])
           .reindex(idx))
    out.index.name = date_col
    return out.reset_index()


def exertion_minutes(daily: pd.DataFrame) -> pd.Series:
    """Garmin-convention exertion score per day: moderate + 2x vigorous
    intensity minutes (vigorous counts double — same weighting Garmin and the
    WHO 150-min/week guideline use). Missing values count as 0 exertion."""
    return (daily["moderate_intensity_min"].fillna(0)
            + 2 * daily["vigorous_intensity_min"].fillna(0))


def activity_sleep_pairs(daily: pd.DataFrame,
                         activity_days: pd.DataFrame) -> pd.DataFrame:
    """Pair each day's exertion with the FOLLOWING night's deep-sleep latency.

    daily          — garmin_daily rows (data.load_daily): needs date,
                     total_steps, moderate/vigorous_intensity_min, and
                     deep_sleep_latency_min.
    activity_days  — per-day recorded-workout rollup (data.load_activity_daily):
                     date, n_activities, activity_seconds. Days with no
                     recorded workout have no row = zero workouts.

    The pairing is off by one on purpose: garmin_daily attributes a night's
    sleep to the morning it ENDED on (same convention app.py's HRV sections
    use), so the sleep that follows day D lives on row D+1. Tuesday's workout
    is judged by the latency stored on Wednesday's row.

    Returns one row per day that could be classified AND has a next-night
    latency: [date, group, total_steps, intensity_min, n_activities,
    deep_sleep_latency_min]. Days that are neither Active nor Sedentary
    (see the cutoffs above) and nights with no stage data are dropped.
    """
    df = daily.copy()
    df["date"] = pd.to_datetime(df["date"])

    # Left-join the workout rollup; a day absent from it had no workouts.
    acts = activity_days.copy()
    if acts.empty:
        acts = pd.DataFrame(columns=["date", "n_activities", "activity_seconds"])
    acts["date"] = pd.to_datetime(acts["date"])
    df = df.merge(acts[["date", "n_activities"]], on="date", how="left")
    df["n_activities"] = df["n_activities"].fillna(0).astype(int)

    df["intensity_min"] = exertion_minutes(df)
    steps = df["total_steps"].fillna(0)

    active = ((df["n_activities"] > 0)
              | (df["intensity_min"] >= ACTIVE_INTENSITY_MIN)
              | (steps >= ACTIVE_STEPS))
    # Sedentary additionally requires steps to be PRESENT: a day with no step
    # count is a day the watch wasn't worn, not a day spent still.
    sedentary = ((df["n_activities"] == 0)
                 & (df["intensity_min"] < SEDENTARY_INTENSITY_MIN)
                 & df["total_steps"].notna() & (steps < SEDENTARY_STEPS))

    df["group"] = EXCLUDED
    df.loc[active, "group"] = "Active"
    df.loc[sedentary, "group"] = "Sedentary"

    # Next-night lookup: index latency by date, then read each day's date + 1.
    latency_by_date = df.set_index("date")["deep_sleep_latency_min"]
    df["deep_sleep_latency_min"] = [
        latency_by_date.get(d + pd.Timedelta(days=1)) for d in df["date"]]

    out = df[df["group"] != EXCLUDED].dropna(subset=["deep_sleep_latency_min"])
    return out[["date", "group", "total_steps", "intensity_min",
                "n_activities", "deep_sleep_latency_min"]].reset_index(drop=True)


def kpi_row(daily: pd.DataFrame, glucose: pd.DataFrame) -> dict:
    """Headline metrics: latest value plus delta vs the mean of the prior 7 days.

    Deltas are None when there isn't enough history. Steps use the most recent
    *complete* day (the newest row is usually today, still accumulating).
    """
    df = daily.sort_values("date").reset_index(drop=True)

    def latest_and_delta(col: str) -> tuple[float | None, float | None]:
        series = df[["date", col]].dropna()
        if series.empty:
            return None, None
        latest = float(series[col].iloc[-1])       # iloc[-1] = last (newest) row
        prior = series[col].iloc[-8:-1]            # the 7 rows before that
        # Require at least 3 prior days, else a delta would be mostly noise.
        delta = float(latest - prior.mean()) if len(prior) >= 3 else None
        return latest, delta

    resting_hr, resting_hr_delta = latest_and_delta("resting_hr")
    hrv, hrv_delta = latest_and_delta("hrv_avg")

    sleep = df.dropna(subset=["sleep_seconds"])
    sleep_h = float(sleep["sleep_seconds"].iloc[-1]) / 3600 if not sleep.empty else None

    steps = df.dropna(subset=["total_steps"])
    steps_yday = float(steps["total_steps"].iloc[-2]) if len(steps) >= 2 else None   # [-2] = second-newest

    glucose_avg = None
    if not glucose.empty:
        glucose_avg = float(glucose["glucose_mg_dl"].mean())

    return {
        "resting_hr": resting_hr, "resting_hr_delta": resting_hr_delta,
        "hrv": hrv, "hrv_delta": hrv_delta,
        "sleep_h": sleep_h,
        "steps_yday": steps_yday,
        "glucose_avg": glucose_avg,
        "time_in_range": time_in_range(glucose),
    }
