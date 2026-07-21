"""BigQuery loaders for the public dashboard.

Read-only, one hard-coded subject, and a deliberate column allowlist:
sensitive fields (medications, weight, meals, raw payloads) are never
selected, so they can't leak onto the public page.
"""

import pandas as pd
import streamlit as st
from google.cloud import bigquery, storage

PROJECT = "digitaltwin-499202"
DATASET = f"{PROJECT}.health_twin"
USER_ID = "kevin"

CACHE_TTL_S = 1800  # refresh from BigQuery at most every 30 min


@st.cache_resource
def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


@st.cache_resource
def _storage_client() -> storage.Client:
    return storage.Client(project=PROJECT)


def _query(sql: str, days: int) -> pd.DataFrame:
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
    return [_user_param(),
            bigquery.ScalarQueryParameter("start_ts", "TIMESTAMP", start_ts),
            bigquery.ScalarQueryParameter("end_ts", "TIMESTAMP", end_ts)]


@st.cache_data(ttl=CACHE_TTL_S)
def load_daily(days: int) -> pd.DataFrame:
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
    return _query(f"""
        SELECT ts, glucose_mg_dl
        FROM `{DATASET}.glucose`
        WHERE user_id = @user_id
          AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days * 24 HOUR)
        ORDER BY ts
    """, days)


@st.cache_data(ttl=CACHE_TTL_S)
def load_intraday(hours: int = 48) -> pd.DataFrame:
    return _query(f"""
        SELECT ts, heart_rate, stress, body_battery
        FROM `{DATASET}.garmin_intraday`
        WHERE user_id = @user_id
          AND ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days HOUR)
        ORDER BY ts
    """, hours)


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
    """Meals for the picker, most recent first. capture_ts is stored UTC."""
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
    URL / public link is ever generated for it)."""
    if not isinstance(gcs_uri, str) or not gcs_uri.startswith("gs://"):
        return None
    bucket_name, blob_name = gcs_uri.removeprefix("gs://").split("/", 1)
    try:
        return (_storage_client().bucket(bucket_name).blob(blob_name)
                .download_as_bytes())
    except Exception:
        return None


@st.cache_data(ttl=CACHE_TTL_S)
def load_glucose_window(start_ts, end_ts) -> pd.DataFrame:
    return _query_params(f"""
        SELECT ts, glucose_mg_dl
        FROM `{DATASET}.glucose`
        WHERE user_id = @user_id AND ts BETWEEN @start_ts AND @end_ts
        ORDER BY ts
    """, _window_params(start_ts, end_ts))


@st.cache_data(ttl=CACHE_TTL_S)
def load_intraday_window(start_ts, end_ts) -> pd.DataFrame:
    return _query_params(f"""
        SELECT ts, heart_rate, stress, body_battery
        FROM `{DATASET}.garmin_intraday`
        WHERE user_id = @user_id AND ts BETWEEN @start_ts AND @end_ts
        ORDER BY ts
    """, _window_params(start_ts, end_ts))


@st.cache_data(ttl=CACHE_TTL_S)
def load_activities_window(start_ts, end_ts) -> pd.DataFrame:
    return _query_params(f"""
        SELECT activity_id, activity_type, activity_name, start_ts, end_ts,
               duration_seconds, calories, avg_hr, max_hr
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
