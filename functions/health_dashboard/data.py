"""BigQuery loaders for the dashboard.

Read-only, one hard-coded subject, and a deliberate column allowlist:
sensitive fields (medications, weight, raw payloads) are never selected.
(The dashboard is private now, but the allowlist stays as defense in depth —
if the page ever leaks, the queries still can't return those columns.)

HOW TO READ THIS FILE: every load_* function is one SQL query returning a
pandas DataFrame. Two Streamlit cache decorators do the heavy lifting:

  @st.cache_resource — for long-lived *objects* (the BigQuery/GCS clients):
      created once per server process and shared by every viewer.
  @st.cache_data — for query *results*: keyed by the function's arguments,
      re-fetched only after `ttl` seconds. Since Streamlit re-runs the whole
      app script on every widget click (see app.py), this caching is what
      keeps each click from re-running every BigQuery query.
"""

import pandas as pd
import streamlit as st
from google.cloud import bigquery, storage

PROJECT = "digitaltwin-499202"
DATASET = f"{PROJECT}.health_twin"
USER_ID = "kevin"   # single-subject dashboard: every query filters on this

CACHE_TTL_S = 1800  # refresh from BigQuery at most every 30 min


@st.cache_resource
def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


@st.cache_resource
def _storage_client() -> storage.Client:
    return storage.Client(project=PROJECT)


def _query(sql: str, days: int) -> pd.DataFrame:
    """Run a query that uses the standard @user_id/@days bind parameters.
    (Parameters are BigQuery's safe way to inject values into SQL — the
    values never get pasted into the query string itself.)"""
    job = _client().query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", USER_ID),
            bigquery.ScalarQueryParameter("days", "INT64", days),
        ]
    ))
    return job.to_dataframe()


def _query_params(sql: str, params: list) -> pd.DataFrame:
    """Like _query, but for callers that need arbitrary bind parameters
    (timestamp-anchored windows) instead of the fixed user_id/days pair."""
    job = _client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return job.to_dataframe()


def _user_param():
    return bigquery.ScalarQueryParameter("user_id", "STRING", USER_ID)


def _window_params(start_ts, end_ts):
    """The parameter triple used by every windowed loader below."""
    return [_user_param(),
            bigquery.ScalarQueryParameter("start_ts", "DATETIME", start_ts),
            bigquery.ScalarQueryParameter("end_ts", "DATETIME", end_ts)]


@st.cache_data(ttl=CACHE_TTL_S)
def load_daily(days: int) -> pd.DataFrame:
    """Last N days of Garmin daily wellness (steps, RHR, sleep, HRV...)."""
    return _query(f"""
        SELECT date, total_steps, resting_hr, avg_stress,
               body_battery_high, body_battery_low,
               sleep_seconds, deep_sleep_seconds, rem_sleep_seconds, hrv_avg
        FROM `{DATASET}.garmin_daily`
        WHERE user_id = @user_id
          AND date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY date
    """, days)


@st.cache_data(ttl=CACHE_TTL_S)
def load_glucose(days: int) -> pd.DataFrame:
    """Last N days of CGM readings. The double DATETIME_SUB first shifts
    "now" from UTC to the pipeline's fixed PDT convention (UTC-7), then goes
    back N days from there."""
    return _query(f"""
        SELECT ts, glucose_mg_dl
        FROM `{DATASET}.glucose`
        WHERE user_id = @user_id
          AND ts >= DATETIME_SUB(DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 7 HOUR),
                                  INTERVAL @days * 24 HOUR)
        ORDER BY ts
    """, days)


@st.cache_data(ttl=CACHE_TTL_S)
def load_blood_pressure(days: int) -> pd.DataFrame:
    return _query(f"""
        SELECT measurement_ts_utc, systolic, diastolic, pulse
        FROM `{DATASET}.omron_bp_daily`
        WHERE user_id = @user_id
          AND measurement_date >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
        ORDER BY measurement_ts_utc
    """, days)


# --------------------------------------------------------------------------- #
# Post-prandial experiment view: meal-anchored loaders.
# --------------------------------------------------------------------------- #

@st.cache_data(ttl=CACHE_TTL_S)
def load_meals(limit: int = 200) -> pd.DataFrame:
    """Meals for the picker, most recent first. capture_ts is stored PDT (fixed UTC-7).
    int(limit) is a guard: it forces the value interpolated into the SQL to be
    a real integer, so nothing string-like can ride in through `limit`."""
    return _query_params(f"""
        SELECT meal_id, capture_ts, items, calories, carbs_g, protein_g,
               fat_g, fiber_g, gcs_uri, source
        FROM `{DATASET}.meals`
        WHERE user_id = @user_id
        ORDER BY capture_ts DESC
        LIMIT {int(limit)}
    """, [_user_param()])


@st.cache_data(ttl=None, show_spinner=False)  # photos never change once uploaded
def load_meal_image_bytes(gcs_uri: str | None) -> bytes | None:
    """Fetch a meal photo server-side (the bucket is private, so no signed
    URL / public link is ever generated for it). ttl=None = cache forever."""
    if not isinstance(gcs_uri, str) or not gcs_uri.startswith("gs://"):
        return None
    # "gs://bucket/path/to/img.jpg" -> ("bucket", "path/to/img.jpg");
    # split("/", 1) splits on the FIRST slash only.
    bucket_name, blob_name = gcs_uri.removeprefix("gs://").split("/", 1)
    try:
        return (_storage_client().bucket(bucket_name).blob(blob_name)
                .download_as_bytes())
    except Exception:
        return None   # missing/inaccessible photo -> the card just says "No photo"


@st.cache_data(ttl=CACHE_TTL_S)
def load_glucose_window(start_ts, end_ts) -> pd.DataFrame:
    """CGM readings between two timestamps (used for meal/HRV windows)."""
    return _query_params(f"""
        SELECT ts, glucose_mg_dl
        FROM `{DATASET}.glucose`
        WHERE user_id = @user_id AND ts BETWEEN @start_ts AND @end_ts
        ORDER BY ts
    """, _window_params(start_ts, end_ts))


@st.cache_data(ttl=CACHE_TTL_S)
def load_activities_window(start_ts, end_ts) -> pd.DataFrame:
    """Garmin activities that *started* inside the window."""
    return _query_params(f"""
        SELECT activity_id, activity_type, activity_name, start_ts, end_ts,
               duration_seconds, distance_m, calories, avg_hr, max_hr
        FROM `{DATASET}.garmin_activities`
        WHERE user_id = @user_id AND start_ts BETWEEN @start_ts AND @end_ts
        ORDER BY start_ts
    """, _window_params(start_ts, end_ts))


@st.cache_data(ttl=CACHE_TTL_S)
def load_bp_window(start_ts, end_ts) -> pd.DataFrame:
    return _query_params(f"""
        SELECT measurement_ts_utc, systolic, diastolic, pulse
        FROM `{DATASET}.omron_bp_daily`
        WHERE user_id = @user_id AND measurement_ts_utc BETWEEN @start_ts AND @end_ts
        ORDER BY measurement_ts_utc
    """, _window_params(start_ts, end_ts))


@st.cache_data(ttl=CACHE_TTL_S)
def load_hrv_for_sleep_date(sleep_date: str) -> pd.DataFrame:
    """Overnight HRV datapoints for one Garmin sleep_date (the table's
    partition column, so this is a partition-pruned lookup, not a ts scan —
    i.e. BigQuery only reads that one day's slice of the table)."""
    return _query_params(f"""
        SELECT ts, hrv_value
        FROM `{DATASET}.hrv_readings`
        WHERE user_id = @user_id AND sleep_date = @sleep_date
        ORDER BY ts
    """, [_user_param(), bigquery.ScalarQueryParameter("sleep_date", "DATE", sleep_date)])
