"""whoop-aggregates-sync: recovery/cycle/sleep/workout from WHOOP's OFFICIAL
public API — a different pipeline from functions/whoop_sync/ (which pulls
raw heart rate from WHOOP's unofficial internal endpoint). Separate secret
prefix (whoop-oauth-<user>, vs. whoop-token-<user>), separate tables,
separate everything — the two never interact.

Unlike the unofficial pipeline, this ONE has a real, working OAuth2 refresh
token (requested via the `offline` scope at connect time — see
functions/meal_web/main.py's /connect-whoop/<token> and /oauth/whoop/callback
routes), so this is meant to run unattended on a Cloud Scheduler job, not a
daily manual script.

Split into this pure-ish logic and main.py's thin HTTP wrapper for the same
reason as whoop_sync: main.py reads credentials from Secret Manager and
calls into here; a test can import this module directly. The BigQuery
client is lazy (_bq(), not a module-level client) from the start — a module-
level client broke whoop_sync's CI once already (DefaultCredentialsError on
import, no ADC on the test runner); not repeating that here.
"""

from __future__ import annotations

import datetime as dt

import requests
from google.cloud import bigquery

PROJECT = "digitaltwin-499202"
DATASET = "health_twin"
DAILY_TABLE = f"{PROJECT}.{DATASET}.whoop_daily"
WORKOUTS_TABLE = f"{PROJECT}.{DATASET}.whoop_workouts"

API_HOST = "https://api.prod.whoop.com"
TOKEN_URL = f"{API_HOST}/oauth/oauth2/token"
PAGE_LIMIT = 25   # WHOOP's documented max per page

# This pipeline stores local wall-clock time (fixed PDT, UTC-7, no DST) in
# TIMESTAMP columns, matching every other table in this repo (glucose,
# meals, whoop_hr_readings) — see whoop_sync/sync.py's PDT_OFFSET comment
# for why. WHOOP's own API request params (start/end filters) stay real
# UTC/ISO-8601, same split as the other pipeline.
PDT_OFFSET = dt.timedelta(hours=-7)

_bq_client: bigquery.Client | None = None


def _bq() -> bigquery.Client:
    global _bq_client
    if _bq_client is None:
        _bq_client = bigquery.Client(project=PROJECT)
    return _bq_client


def _local_iso(utc_iso: str) -> str:
    """WHOOP's own ISO-8601 UTC string -> fixed-PDT local, naive isoformat."""
    ts = dt.datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    return (ts + PDT_OFFSET).replace(tzinfo=None).isoformat()


def _to_int(x) -> int | None:
    """WHOOP's API returns some logically-integer fields (recovery_score,
    heart rates, step_count, sleep_performance_percentage) as floats, e.g.
    79.0 — BigQuery's JSON loader refuses to coerce a float literal into an
    INTEGER column, so every field typed INTEGER in whoop_daily.json/
    whoop_workouts.json must be cast explicitly before loading."""
    return int(x) if isinstance(x, (int, float)) else None


def refresh_if_needed(tokens: dict, client_id: str, client_secret: str) -> dict:
    """Refresh only when actually needed (checked against the stored
    expires_at, 5-minute safety buffer) — unlike Omron, WHOOP's access
    token does NOT rotate on every API call, only the refresh token
    rotates on an actual refresh. Refreshing every run for no reason would
    burn rotations (and, per WHOOP's own docs, race if ever run
    concurrently) with nothing to show for it.
    """
    expires_at = dt.datetime.fromisoformat(tokens["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    if dt.datetime.now(dt.timezone.utc) < expires_at - dt.timedelta(minutes=5):
        return tokens

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "offline",
    }, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    return {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": (dt.datetime.now(dt.timezone.utc)
                       + dt.timedelta(seconds=body["expires_in"])).isoformat(),
    }


def _paginate(path: str, access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    """GET one of the four collection endpoints in full, following
    next_token until it's absent."""
    records: list[dict] = []
    next_token = None
    while True:
        params = {"start": start_iso, "end": end_iso, "limit": PAGE_LIMIT}
        if next_token:
            params["nextToken"] = next_token
        r = requests.get(f"{API_HOST}{path}",
                         headers={"Authorization": f"Bearer {access_token}"},
                         params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        records.extend(body.get("records", []))
        next_token = body.get("next_token")
        if not next_token:
            break
    return records


def fetch_cycles(access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    return _paginate("/developer/v2/cycle", access_token, start_iso, end_iso)


def fetch_recovery(access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    return _paginate("/developer/v2/recovery", access_token, start_iso, end_iso)


def fetch_sleep(access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    return _paginate("/developer/v2/activity/sleep", access_token, start_iso, end_iso)


def fetch_workouts(access_token: str, start_iso: str, end_iso: str) -> list[dict]:
    return _paginate("/developer/v2/activity/workout", access_token, start_iso, end_iso)


def parse_daily_rows(user_id: str, cycles: list[dict], recovery: list[dict],
                     sleep: list[dict], ingested_ts: str) -> list[dict]:
    """Join Cycle + Recovery + that cycle's main (non-nap) Sleep by
    cycle_id into one whoop_daily row per cycle. A cycle with no scored
    recovery yet (score_state != SCORED, e.g. still in progress) still gets
    a row — just with the recovery/sleep columns NULL."""
    recovery_by_cycle = {r["cycle_id"]: r for r in recovery if r.get("cycle_id") is not None}
    sleep_by_cycle = {s["cycle_id"]: s for s in sleep
                      if s.get("cycle_id") is not None and not s.get("nap")}

    rows = []
    for c in cycles:
        cycle_id = c.get("id")
        if cycle_id is None or not c.get("start"):
            continue   # can't build a usable row without these

        row = {
            "user_id": user_id,
            "date": _local_iso(c["start"])[:10],
            "cycle_id": cycle_id,
            "cycle_start": _local_iso(c["start"]),
            "cycle_end": _local_iso(c["end"]) if c.get("end") else None,
            "day_strain": None, "day_kilojoule": None,
            "day_avg_hr": None, "day_max_hr": None,
            "step_count": _to_int(c.get("step_count")),
            "recovery_score": None, "hrv_rmssd_milli": None, "resting_hr": None,
            "spo2_percentage": None, "skin_temp_celsius": None,
            "respiratory_rate": None,
            "sleep_performance_percentage": None, "sleep_efficiency_percentage": None,
            "deep_sleep_seconds": None, "rem_sleep_seconds": None,
            "light_sleep_seconds": None, "awake_seconds": None,
            "ingested_ts": ingested_ts,
            "raw": {"cycle": c, "recovery": recovery_by_cycle.get(cycle_id),
                    "sleep": sleep_by_cycle.get(cycle_id)},
        }

        cycle_score = c.get("score") or {}
        row["day_strain"] = cycle_score.get("strain")
        row["day_kilojoule"] = cycle_score.get("kilojoule")
        row["day_avg_hr"] = _to_int(cycle_score.get("average_heart_rate"))
        row["day_max_hr"] = _to_int(cycle_score.get("max_heart_rate"))

        rec = recovery_by_cycle.get(cycle_id)
        if rec:
            rec_score = rec.get("score") or {}
            row["recovery_score"] = _to_int(rec_score.get("recovery_score"))
            row["hrv_rmssd_milli"] = rec_score.get("hrv_rmssd_milli")
            row["resting_hr"] = _to_int(rec_score.get("resting_heart_rate"))
            row["spo2_percentage"] = rec_score.get("spo2_percentage")
            row["skin_temp_celsius"] = rec_score.get("skin_temp_celsius")

        slp = sleep_by_cycle.get(cycle_id)
        if slp:
            slp_score = slp.get("score") or {}
            stages = slp_score.get("stage_summary") or {}
            row["respiratory_rate"] = slp_score.get("respiratory_rate")
            row["sleep_performance_percentage"] = _to_int(slp_score.get("sleep_performance_percentage"))
            row["sleep_efficiency_percentage"] = slp_score.get("sleep_efficiency_percentage")
            row["deep_sleep_seconds"] = _ms_to_s(stages.get("total_slow_wave_sleep_time_milli"))
            row["rem_sleep_seconds"] = _ms_to_s(stages.get("total_rem_sleep_time_milli"))
            row["light_sleep_seconds"] = _ms_to_s(stages.get("total_light_sleep_time_milli"))
            row["awake_seconds"] = _ms_to_s(stages.get("total_awake_time_milli"))

        rows.append(row)
    return rows


def _ms_to_s(ms) -> int | None:
    return int(ms / 1000) if isinstance(ms, (int, float)) else None


def parse_workout_rows(user_id: str, workouts: list[dict], ingested_ts: str) -> list[dict]:
    """Parse Workout objects into whoop_workouts rows. Workouts with no
    scored strain data still get a row — just with those columns NULL,
    same "keep the row, null the missing parts" approach as parse_daily_rows."""
    rows = []
    for w in workouts:
        workout_id, start = w.get("id"), w.get("start")
        if not workout_id or not start:
            continue
        score = w.get("score") or {}
        rows.append({
            "user_id": user_id,
            "workout_id": workout_id,
            "sport_name": w.get("sport_name"),
            "start_ts": _local_iso(start),
            "end_ts": _local_iso(w["end"]) if w.get("end") else None,
            "strain": score.get("strain"),
            "avg_hr": _to_int(score.get("average_heart_rate")),
            "max_hr": _to_int(score.get("max_heart_rate")),
            "kilojoule": score.get("kilojoule"),
            "distance_m": score.get("distance_meter"),
            "ingested_ts": ingested_ts,
            "raw": w,
        })
    return rows


def _upsert(table: str, date_col: str, rows: list[dict]) -> None:
    """Idempotent delete-then-load, same pattern as omron_sync._upsert —
    scoped to the dates actually covered by these rows."""
    dates = sorted({r[date_col][:10] for r in rows})
    _bq().query(
        f"DELETE FROM `{table}` WHERE user_id=@u AND DATE({date_col}) IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", rows[0]["user_id"]),
            bigquery.ArrayQueryParameter("d", "DATE", dates),
        ]),
    ).result()
    _bq().load_table_from_json(
        rows, table,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


def sync_user_data(user_id: str, access_token: str, days: int) -> dict:
    """Pull the last `days` of cycle/recovery/sleep/workout data and upsert
    into whoop_daily/whoop_workouts. Returns {"daily_rows": N, "workout_rows": N}.

    Deliberately takes a plain access_token, not a tokens dict — refreshing
    is main.py's job, done and PERSISTED (via its own _save_tokens call)
    before this is ever called, not bundled in here. Omron's sync learned
    this the hard way: bundling "refresh" and "fetch data" into one function
    means a fetch failure after a successful refresh loses the newly-
    rotated refresh token, since the caller never gets it back to save.
    """
    now = dt.datetime.now(dt.timezone.utc)
    start_iso = (now - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    cycles = fetch_cycles(access_token, start_iso, end_iso)
    recovery = fetch_recovery(access_token, start_iso, end_iso)
    sleep = fetch_sleep(access_token, start_iso, end_iso)
    workouts = fetch_workouts(access_token, start_iso, end_iso)

    ingested_ts = (now + PDT_OFFSET).replace(tzinfo=None).isoformat()
    daily_rows = parse_daily_rows(user_id, cycles, recovery, sleep, ingested_ts)
    workout_rows = parse_workout_rows(user_id, workouts, ingested_ts)

    if daily_rows:
        _upsert(DAILY_TABLE, "cycle_start", daily_rows)
    if workout_rows:
        _upsert(WORKOUTS_TABLE, "start_ts", workout_rows)

    return {"daily_rows": len(daily_rows), "workout_rows": len(workout_rows)}
