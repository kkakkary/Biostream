"""garmin-intraday-sync: high-frequency poll of Garmin intraday signals.

Runs every ~15 min. For each user it re-pulls today's (and yesterday's, for
the midnight boundary) per-reading series — heart rate, stress, body battery,
respiration — merges them by timestamp into one row per reading (UTC, so it
lines up directly with glucose and meal timestamps), and idempotently upserts
into garmin_intraday (delete the day's rows, reload via a load job).

These intraday signals are what pair with post-meal glucose response; the daily
wellness summary lives separately in garmin_daily.
"""

from __future__ import annotations

import datetime as dt
import json
import os

import functions_framework
from garminconnect import Garmin
from google.cloud import bigquery, secretmanager

PROJECT = os.environ["PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "health_twin")
USERS = [u.strip() for u in os.environ.get("GARMIN_USERS", "").split(",") if u.strip()]
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))
TABLE = f"{PROJECT}.{DATASET}.garmin_intraday"

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


def _token(user: str) -> str:
    name = f"projects/{PROJECT}/secrets/garmin-token-{user}/versions/latest"
    return _sm.access_secret_version(name=name).payload.data.decode()


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _series(arr, val_idx, skip=()):
    """Garmin series are [[epoch_ms, value, ...], ...]; return {epoch_ms: value}."""
    out = {}
    for r in arr or []:
        v = r[val_idx] if len(r) > val_idx else None
        if v is None or v in skip:
            continue
        out[r[0]] = v
    return out


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()


def _rows_for_day(user: str, date: str, g: Garmin) -> list[dict]:
    hr = _safe(lambda: g.get_heart_rates(date)) or {}
    st = _safe(lambda: g.get_stress_data(date)) or {}
    rs = _safe(lambda: g.get_respiration_data(date)) or {}
    series = {
        "heart_rate": _series(hr.get("heartRateValues"), 1),
        "stress": _series(st.get("stressValuesArray"), 1, skip=(-1, -2)),
        "body_battery": _series(st.get("bodyBatteryValuesArray"), 2),  # [ts,status,val,ver]
        "respiration": _series(rs.get("respirationValuesArray"), 1, skip=(-1, -2)),
    }
    merged: dict[int, dict] = {}
    for metric, pts in series.items():
        for ms, val in pts.items():
            merged.setdefault(ms, {})[metric] = val
    rows = []
    for ms, vals in merged.items():
        rows.append({
            "user_id": user,
            "ts": _iso(ms),
            "heart_rate": int(vals["heart_rate"]) if "heart_rate" in vals else None,
            "stress": int(vals["stress"]) if "stress" in vals else None,
            "body_battery": int(vals["body_battery"]) if "body_battery" in vals else None,
            "respiration": float(vals["respiration"]) if "respiration" in vals else None,
        })
    return rows


def _upsert(user: str, dates: list[str], rows: list[dict]) -> None:
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND DATE(ts) IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates)]),
    ).result()
    if rows:
        _bq.load_table_from_json(
            rows, TABLE,
            job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
        ).result()


@functions_framework.http
def garmin_intraday_sync(request):
    days = int(request.args.get("days", DAYS_BACK))
    dates = [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(days)]
    out: dict[str, object] = {}
    for user in USERS:
        try:
            g = Garmin()
            g.login(_token(user))
            rows = []
            for d in dates:
                rows.extend(_rows_for_day(user, d, g))
            _upsert(user, dates, rows)
            out[user] = len(rows)
        except Exception as exc:
            out[user] = f"error: {type(exc).__name__}: {exc}"
    return (json.dumps({"status": "ok", "dates": dates, "readings_per_user": out}),
            200, {"Content-Type": "application/json"})
