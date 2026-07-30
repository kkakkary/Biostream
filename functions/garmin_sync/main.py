"""garmin-sync: daily poll of Garmin baseline stats -> BigQuery garmin_daily.

Runs on a schedule (Cloud Scheduler). For each configured user it loads a
cached Garmin token from Secret Manager (no password/MFA needed — see README),
pulls the last DAYS_BACK days of daily wellness + sleep + HRV, and idempotently
upserts one row per (user, date) into garmin_daily.

It also flattens the overnight HRV series (one value ~every 5 min during sleep)
into per-reading rows in hrv_readings, for datapoint-level HRV tracking.

Idempotent + Preview-friendly: deletes the (user, date) rows it's about to
write, then loads via a BigQuery load job (committed storage, not streaming).

HOW TO READ THIS FILE (bottom-up is easiest):
  1. `garmin_sync()` at the bottom is the entry point — Cloud Functions calls
     it once per scheduled run. Everything else is a helper it uses.
  2. The flow per run: figure out which dates to fetch -> for each user, log
     in to Garmin -> build one row per day (`_row`) -> write to BigQuery
     (`_upsert` / `_upsert_readings`).
  3. "Idempotent" means: running the same sync twice produces the same result
     as running it once — no duplicate rows. That's why every write is
     "delete the rows I'm about to write, then insert fresh copies".
"""

# Lets us write modern type hints like `list[str]` and `dict | None` even on
# older Python versions — purely a typing convenience, no runtime effect.
from __future__ import annotations

import datetime as dt   # `as dt` = shorthand alias, so we write dt.date not datetime.date
import json             # convert Python dicts <-> JSON text (for the HTTP response)
import os               # read environment variables (configuration)
import sys              # lets us print to stderr, which Cloud Logging treats as ERROR severity

import functions_framework                       # Google's library that turns a function into an HTTP endpoint
from garminconnect import Garmin                 # third-party client for Garmin's (unofficial) API
from google.cloud import bigquery, secretmanager # Google Cloud client libraries

# --- Configuration, read once when the function "cold starts" ---------------
# os.environ["PROJECT"] crashes if PROJECT isn't set (deliberate: fail fast on
# a misconfigured deploy). os.environ.get(name, default) is the soft version:
# use the default if the variable isn't set.
PROJECT = os.environ["PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "health_twin")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))   # env vars are always strings -> int()
# f-strings build the fully-qualified BigQuery table names, e.g.
# "digitaltwin-499202.health_twin.garmin_daily"
TABLE = f"{PROJECT}.{DATASET}.garmin_daily"
HRV_READINGS = f"{PROJECT}.{DATASET}.hrv_readings"

# Clients created at module level (not inside a function) are reused across
# invocations while the Cloud Function instance stays warm — much faster than
# reconnecting on every request. Leading underscore = "private to this file"
# by Python convention.
_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


def _token(user: str) -> str:
    """Fetch this user's cached Garmin login token from Secret Manager.

    Secrets are versioned; "latest" always points at the newest version, so
    rotating a token doesn't require touching this code.
    """
    name = f"projects/{PROJECT}/secrets/garmin-token-{user}/versions/latest"
    # .payload.data is raw bytes; .decode() turns it into a str.
    return _sm.access_secret_version(name=name).payload.data.decode()


def _users() -> list[str]:
    """Users with a connected Garmin account = secrets named garmin-token-<user>.
    Auto-discovered, so the Connect Garmin page onboards people with no redeploy.
    GARMIN_USERS env (comma-separated) overrides if set."""
    # List comprehension with a filter: split "a, b," on commas, strip spaces,
    # and `if u.strip()` drops the empty strings a trailing comma would leave.
    env = [u.strip() for u in os.environ.get("GARMIN_USERS", "").split(",") if u.strip()]
    if env:
        return env
    # No override: scan all secrets in the project and keep the garmin-token-*
    # ones. s.name is the full path ("projects/123/secrets/garmin-token-kevin"),
    # so split on "/" and take the last piece, then chop off the prefix.
    out = []
    for s in _sm.list_secrets(parent=f"projects/{PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("garmin-token-"):
            out.append(short[len("garmin-token-"):])   # slice off "garmin-token-" -> "kevin"
    return out


def _safe(fn):
    """Call fn() and return None instead of crashing if it raises.

    Used to wrap every individual Garmin API call: any one endpoint can fail
    (e.g. no sleep data that night) without sinking the whole day's row.
    Callers pass `lambda: g.get_x(...)` — a lambda is a tiny unnamed function,
    used here to *delay* the call so it happens inside this try/except.
    """
    try:
        return fn()
    except Exception:
        return None


def _int(x):
    """Garmin returns some integer-ish metrics as floats (e.g. calories 1326.0);
    the garmin_daily schema is INTEGER, so coerce.

    Also acts as a validity filter: anything that isn't a number (None, a
    string, a missing field) comes back as None, which BigQuery stores as NULL.
    """
    return int(round(x)) if isinstance(x, (int, float)) else None


_GRAMS_PER_LB = 453.59237


def _weight_lbs(bc: dict) -> float | None:
    """Extract body weight (lbs) from Garmin's body-composition payload.

    Garmin returns weight in grams under totalAverage; fall back to the most
    recent per-day entry. Returns None on days with no weigh-in.

    The `(x or {})` / `(x or [])` pattern appears all over this file: if the
    key is missing or its value is None, substitute an empty dict/list so the
    next .get() / indexing doesn't crash.
    """
    grams = (bc.get("totalAverage") or {}).get("weight")
    if not isinstance(grams, (int, float)):
        entries = bc.get("dateWeightList") or []
        grams = entries[-1].get("weight") if entries else None   # [-1] = last item
    return round(grams / _GRAMS_PER_LB, 1) if isinstance(grams, (int, float)) else None


def _reading_ts(local: str | None) -> str | None:
    """Parse Garmin's overnight-HRV reading time ('2026-07-11T10:41:35.0',
    already local to the device's timezone) into a naive timestamp string.

    local[:19] keeps just 'YYYY-MM-DDTHH:MM:SS' (19 chars), dropping the
    trailing '.0' fractional seconds that strptime's format doesn't expect.
    """
    if not local:
        return None
    try:
        return dt.datetime.strptime(local[:19], "%Y-%m-%dT%H:%M:%S").isoformat()
    except Exception:
        return None


def _hrv_readings(user: str, date: str, hrv: dict) -> list[dict]:
    """Flatten Garmin's overnight HRV series (~one value per 5 min during sleep)
    into per-reading rows for the hrv_readings table.

    Each returned dict = one BigQuery row. Rows missing a timestamp or a
    numeric value are silently skipped rather than stored as junk.
    """
    out = []
    for r in (hrv.get("hrvReadings") or []):
        ts = _reading_ts(r.get("readingTimeLocal"))
        val = r.get("hrvValue")
        if ts and isinstance(val, (int, float)):
            out.append({"user_id": user, "sleep_date": date, "ts": ts, "hrv_value": int(val)})
    return out


def _row(user: str, date: str, g: Garmin, hrv: dict) -> dict | None:
    """Build the single garmin_daily row for one (user, date).

    Makes three Garmin API calls (summary, sleep, body composition — HRV was
    already fetched by the caller), each wrapped in _safe so a missing dataset
    just yields NULLs instead of aborting the day.
    """
    us = _safe(lambda: g.get_user_summary(date)) or {}
    sleep = (_safe(lambda: g.get_sleep_data(date)) or {}).get("dailySleepDTO") or {}
    hrv_sum = (hrv or {}).get("hrvSummary") or {}
    bc = _safe(lambda: g.get_body_composition(date, date)) or {}
    # The keys here must match the garmin_daily table schema exactly —
    # BigQuery's load job matches JSON keys to column names.
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
        # Full raw API payloads kept alongside the parsed columns, so if we
        # ever want a field we didn't extract, it's already in the warehouse.
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

    Returns e.g. {"2026-07-27": [{"name": "metformin", "dose": "500mg"}]}.
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
        # Rebuild each med as a plain dict with only the fields the schema
        # expects; `or []` guards rows where medications is NULL.
        meds = [{"name": m.get("name"), "dose": m.get("dose")}
                for m in (r["medications"] or [])]
        if meds:
            out[r["date"].isoformat()] = meds
    return out


def _upsert(user: str, rows: list[dict]) -> None:
    """Delete the (user, date) rows, then load the fresh ones (idempotent).

    Why delete-then-insert instead of "update"? BigQuery is a warehouse, not a
    transactional database — replacing whole rows is the simple, reliable way
    to re-run a sync without creating duplicates.
    """
    dates = [r["date"] for r in rows]
    # Preserve any manually-logged medications before the delete clobbers them.
    # (Meds are entered by hand in the meal web app; Garmin knows nothing about
    # them, so a naive replace would erase them every night.)
    meds = _existing_meds(user, dates)
    for r in rows:
        r["medications"] = meds.get(r["date"], [])
    # Parameterized query: @u and @d are placeholders filled in via
    # query_parameters — never string-concatenate values into SQL (injection
    # risk + date/quoting bugs). UNNEST(@d) turns the date array into a set
    # the IN clause can match against.
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND date IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates)]),
    ).result()   # .result() blocks until the job finishes (and raises if it failed)
    # Load job = batch insert into committed storage. WRITE_APPEND adds rows
    # (the delete above already cleared the old copies).
    _bq.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


def _upsert_readings(user: str, dates: list[str], rows: list[dict]) -> None:
    """Idempotently replace the overnight HRV datapoints for these (user, night)
    dates: delete the affected sleep_dates, then load the fresh readings.
    Same delete-then-load pattern as _upsert, just for the hrv_readings table.
    """
    _bq.query(
        f"DELETE FROM `{HRV_READINGS}` WHERE user_id=@u AND sleep_date IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates)]),
    ).result()
    _bq.load_table_from_json(
        rows, HRV_READINGS,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


# The decorator (@...) registers this function as the HTTP handler — when the
# deployed Cloud Function receives a request, Google calls garmin_sync(request).
@functions_framework.http
def garmin_sync(request):
    # ?days=N overrides the default window (handy for backfilling history).
    days = int(request.args.get("days", DAYS_BACK))
    # Build the list of date strings to sync, today first:
    # e.g. days=2 -> ["2026-07-28", "2026-07-27"]
    dates = [(dt.date.today() - dt.timedelta(days=i)).isoformat() for i in range(days)]
    out: dict[str, object] = {}   # per-user result summary, returned as JSON
    for user in _users():
        try:
            g = Garmin()
            g.login(_token(user))   # token login — no password/MFA round-trip
            rows, readings = [], []
            for d in dates:
                hrv = _safe(lambda: g.get_hrv_data(d)) or {}
                r = _row(user, d, g, hrv)
                if r is not None:
                    rows.append(r)
                readings.extend(_hrv_readings(user, d, hrv))
            if rows:
                _upsert(user, rows)
            if readings:  # only touch nights we actually got readings for
                # {r["sleep_date"] ...} is a set comprehension -> unique dates.
                _upsert_readings(user, sorted({r["sleep_date"] for r in readings}), readings)
            out[user] = {"days": len(rows), "hrv_readings": len(readings)}
        except Exception as exc:  # one user's failure must not abort the rest
            msg = f"error: {type(exc).__name__}: {exc}"
            # stderr -> Cloud Logging records it at ERROR severity, so log-based
            # alerts can catch per-user failures even though we still return 200.
            print(f"[garmin-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg
    # If any user's value is a string, it's an error message -> "partial".
    status = "ok" if not any(isinstance(v, str) for v in out.values()) else "partial"
    return (json.dumps({"status": status, "dates": dates, "per_user": out}),
            200, {"Content-Type": "application/json"})
