"""Pure dataframe transforms — no Streamlit, no BigQuery, unit-testable."""

import pandas as pd

GLUCOSE_RANGE_MG_DL = (70, 180)  # standard CGM time-in-range window


def sleep_stages(daily: pd.DataFrame) -> pd.DataFrame:
    """Split nightly sleep into deep / REM / light hours.

    Light sleep is not stored; it's the remainder of total minus deep and REM.
    Nights with no sleep data are dropped.
    """
    df = daily.dropna(subset=["sleep_seconds"]).copy()
    df = df[df["sleep_seconds"] > 0]
    deep = df["deep_sleep_seconds"].fillna(0)
    rem = df["rem_sleep_seconds"].fillna(0)
    df["deep_h"] = deep / 3600
    df["rem_h"] = rem / 3600
    df["light_h"] = ((df["sleep_seconds"] - deep - rem).clip(lower=0)) / 3600
    return df[["date", "deep_h", "rem_h", "light_h"]]


def time_in_range(glucose: pd.DataFrame,
                  lo: float = GLUCOSE_RANGE_MG_DL[0],
                  hi: float = GLUCOSE_RANGE_MG_DL[1]) -> float | None:
    """Percent of CGM readings inside [lo, hi]. None when there are no readings."""
    values = glucose["glucose_mg_dl"].dropna()
    if values.empty:
        return None
    return float(((values >= lo) & (values <= hi)).mean() * 100)


def break_time_gaps(df: pd.DataFrame, ts_col: str,
                    max_gap: pd.Timedelta) -> pd.DataFrame:
    """Insert an all-NaN row inside every sampling gap wider than max_gap,
    so line charts show a break instead of a false bridge across missing data."""
    if len(df) < 2:
        return df
    df = df.sort_values(ts_col).reset_index(drop=True)
    gap_starts = df[ts_col].diff() > max_gap
    if not gap_starts.any():
        return df
    breaks = pd.DataFrame({
        ts_col: df.loc[gap_starts, ts_col] - max_gap / 2,
    })
    return (pd.concat([df, breaks], ignore_index=True)
            .sort_values(ts_col).reset_index(drop=True))


def fill_date_gaps(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    """Reindex a daily frame onto its full calendar range so missing days
    become NaN rows (line charts then break instead of bridging them)."""
    if df.empty:
        return df
    df = df.sort_values(date_col)
    idx = pd.date_range(df[date_col].min(), df[date_col].max(), freq="D")
    out = (df.set_index(pd.to_datetime(df[date_col])).drop(columns=[date_col])
           .reindex(idx))
    out.index.name = date_col
    return out.reset_index()


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
        latest = float(series[col].iloc[-1])
        prior = series[col].iloc[-8:-1]
        delta = float(latest - prior.mean()) if len(prior) >= 3 else None
        return latest, delta

    resting_hr, resting_hr_delta = latest_and_delta("resting_hr")
    hrv, hrv_delta = latest_and_delta("hrv_avg")

    sleep = df.dropna(subset=["sleep_seconds"])
    sleep_h = float(sleep["sleep_seconds"].iloc[-1]) / 3600 if not sleep.empty else None

    steps = df.dropna(subset=["total_steps"])
    steps_yday = float(steps["total_steps"].iloc[-2]) if len(steps) >= 2 else None

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
