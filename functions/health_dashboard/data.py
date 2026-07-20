"""BigQuery loaders for the public dashboard.

Read-only, one hard-coded subject, and a deliberate column allowlist:
sensitive fields (medications, weight, meals, raw payloads) are never
selected, so they can't leak onto the public page.
"""

import pandas as pd
import streamlit as st
from google.cloud import bigquery

PROJECT = "digitaltwin-499202"
DATASET = f"{PROJECT}.health_twin"
USER_ID = "kevin"

CACHE_TTL_S = 1800  # refresh from BigQuery at most every 30 min


@st.cache_resource
def _client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT)


def _query(sql: str, days: int) -> pd.DataFrame:
    job = _client().query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", USER_ID),
            bigquery.ScalarQueryParameter("days", "INT64", days),
        ]
    ))
    return job.to_dataframe()


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
