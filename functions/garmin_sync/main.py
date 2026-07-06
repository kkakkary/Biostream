"""garmin-sync: daily poll of Garmin baseline stats -> BigQuery garmin_daily.

Runs on a schedule (Cloud Scheduler). For each configured user it loads a
cached Garmin token from Secret Manager (no password/MFA needed — see README),
pulls the last DAYS_BACK days of daily wellness + sleep + HRV, and idempotently
upserts one row per (user, date) into garmin_daily.

Idempotent + Preview-friendly: deletes the (user, date) rows it's about to
write, then loads via a BigQuery load job (committed storage, not streaming).
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
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))
TABLE = f"{PROJECT}.{DATASET}.garmin_daily"

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


def _token(user: str) -> str:
    name = f"projects/{PROJECT}/secrets/garmin-token-{user}/versions/latest"
    return _sm.access_secret_version(name=name).payload.data.decode()


def _users() -> list[str]:
    """Users with a connected Garmin account = secrets named garmin-token-<user>.
    Auto-discovered, so the Connect Garmin page onboards people with no redeploy.
    GARMIN_USERS env (comma-separated) overrides if set."""
    env = [u.strip() for u in os.environ.get("GARMIN_USERS", "").split(",") if u.strip()]
    if env:
        return env
    out = []
    for s in _sm.list_secrets(parent=f"projects/{PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("garmin-token-"):
            out.append(short[len("garmin-token-"):])
    return out


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _int(x):
    """Garmin returns some integer-ish metrics as floats (e.g. calories 1326.0);
    the garmin_daily schema is INTEGER, so coerce."""
    return int(round(x)) if isinstance(x, (int, float)) else None


_GRAMS_PER_LB = 453.59237


def _weight_lbs(bc: dict) -> float | None:
    """Extract body weight (lbs) from Garmin's body-composition payload.

    Garmin returns weight in grams under totalAverage; fall back to the most
    recent per-day entry. Returns None on days with no weigh-in.
    """
    grams = (bc.get("totalAverage") or {}).get("weight")
    if not isinstance(grams, (int, float)):
        entries = bc.get("dateWeightList") or []
        grams = entries[-1].get("weight") if entries else None
    return round(grams / _GRAMS_PER_LB, 1) if isinstance(grams, (int, float)) else None


def _row(user: str, date: str, g: Garmin) -> dict | None:
    us = _safe(lambda: g.get_user_summary(date)) or {}
    sleep = (_safe(lambda: g.get_sleep_data(date)) or {}).get("dailySleepDTO") or {}
    hrv = _safe(lambda: g.get_hrv_data(date)) or {}
    hrv_sum = (hrv or {}).get("hrvSummary") or {}
    bc = _safe(lambda: g.get_body_composition(date, date)) or {}
    row = {
        "user_id": user,
        "date": date,
        "total_steps": _int(us.get("totalSteps")),
        "resting_hr": _int(us.get("restingHeartRate")),
        "avg_stress": _int(us.get("averageStressLevel")),
        "body_battery_high": _int(us.get("bodyBatteryHighestValue")),
        "body_battery_low": _int(us.get("bodyBatteryLowestValue")),
        "sleep_seconds": _int(us.get("sleepingSeconds")),
        "deep_sleep_seconds": _int(sleep.get("deepSleepSeconds")),
        "rem_sleep_seconds": _int(sleep.get("remSleepSeconds")),
        "hrv_avg": _int(hrv_sum.get("lastNightAvg")),
        "total_kcal": _int(us.get("totalKilocalories")),
        "active_kcal": _int(us.get("activeKilocalories")),
        "weight_lbs": _weight_lbs(bc),
        # Manually logged; populated by _upsert from any existing row (never from Garmin).
        "medications": [],
        "raw": {"user_summary": us, "sleep": sleep, "hrv": hrv, "body_composition": bc},
    }
    # Skip days with no meaningful data (watch not worn, not yet synced).
    # weight_lbs counts: a standalone weigh-in is worth a row on its own.
    if all(row[k] is None for k in
           ("total_steps", "resting_hr", "sleep_seconds", "hrv_avg", "weight_lbs")):
        return None
    return row


def _existing_meds(user: str, dates: list[str]) -> dict[str, list[dict]]:
    """Medications already stored for these (user, date) rows, keyed by date.

    Medications are logged manually, not pulled from Garmin, so they must
    survive the delete-and-reload below — otherwise each daily sync would wipe
    them. Read them first, then carry them into the freshly built rows.
    """
    job = _bq.query(
        f"SELECT date, medications FROM `{TABLE}` "
        "WHERE user_id=@u AND date IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates)]),
    )
    out: dict[str, list[dict]] = {}
    for r in job.result():
        meds = [{"name": m.get("name"), "dose": m.get("dose")}
                for m in (r["medications"] or [])]
        if meds:
            out[r["date"].isoformat()] = meds
    return out


def _upsert(user: str, rows: list[dict]) -> None:
    """Delete the (user, date) rows, then load the fresh ones (idempotent)."""
    dates = [r["date"] for r in rows]
    # Preserve any manually-logged medications before the delete clobbers them.
    meds = _existing_meds(user, dates)
    for r in rows:
        r["medications"] = meds.get(r["date"], [])
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND date IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates)]),
    ).result()
    _bq.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


@functions_framework.http
def garmin_sync(request):
    # ?days=N overrides the default window (handy for backfilling history).
    days = int(request.args.get("days", DAYS_BACK))
    dates = [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(days)]
    out: dict[str, object] = {}
    for user in _users():
        try:
            g = Garmin()
            g.login(_token(user))
            rows = [r for d in dates if (r := _row(user, d, g)) is not None]
            if rows:
                _upsert(user, rows)
            out[user] = len(rows)
        except Exception as exc:  # one user's failure must not abort the rest
            out[user] = f"error: {type(exc).__name__}: {exc}"
    return (json.dumps({"status": "ok", "dates": dates, "rows_per_user": out}),
            200, {"Content-Type": "application/json"})
