"""omron-sync: daily poll of Omron Connect blood pressure -> BigQuery omron_bp_daily.

Runs on a schedule (Cloud Scheduler). For each connected user it loads their
Omron token JSON from Secret Manager, silently refreshes it (writing the new
tokens back so the next run stays valid), pulls the last DAYS_BACK days of
blood pressure readings, and idempotently upserts into omron_bp_daily.

Idempotent: deletes (user_id, measurement_date) rows it's about to write,
then loads fresh ones via a BigQuery load job (committed storage, not streaming).

?days=N on the request URL overrides DAYS_BACK — useful for backfilling.

HOW TO READ THIS FILE: same skeleton as garmin_sync/main.py (which has the
most detailed beginner annotations — read that one first). The Omron-specific
twists here are:
  * token *refresh*: unlike Garmin's long-lived token, Omron rotates tokens on
    every use, so each run must save the new ones back to Secret Manager.
  * a request "checksum" header Omron requires (see _checksum_hook).
  * pagination: BP readings arrive in pages, fetched in a loop.
"""

from __future__ import annotations

import datetime as dt
import hashlib   # SHA-256 for Omron's required body-checksum header
import json
import os
import sys

import functions_framework
import httpx
from google.cloud import bigquery, secretmanager

PROJECT = os.environ["PROJECT"]
DATASET = os.environ.get("BQ_DATASET", "health_twin")
DAYS_BACK = int(os.environ.get("DAYS_BACK", "2"))
TABLE = f"{PROJECT}.{DATASET}.omron_bp_daily"
# Hard floor on measurement date — filters previous-owner readings when set.
# (A second-hand BP cuff can carry the previous owner's history in Omron's cloud.)
EARLIEST_DATE: str | None = os.environ.get("OMRON_EARLIEST_DATE")

# Omron's private mobile-app API server; the user-agent makes our requests
# look like the official iPhone app, which is what the API expects.
_OMRON_SERVER = "https://vlt-mobile-api.prd.us.ohiomron.com/prd"
_OMRON_USER_AGENT = "OmronConnect/3 CFNetwork/1410.0.3 Darwin/22.6.0"

# Module-level clients, reused across warm invocations.
_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


# --------------------------------------------------------------------------- #
# Secret Manager helpers
# --------------------------------------------------------------------------- #
def _load_tokens(user: str) -> dict:
    """Read this user's Omron token JSON ({email, accessToken, refreshToken})."""
    name = f"projects/{PROJECT}/secrets/omron-token-{user}/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


def _save_tokens(user: str, tokens: dict) -> None:
    """Write refreshed tokens back to Secret Manager.

    Omron rotates both access and refresh tokens on every refresh call — if we
    don't persist the new refresh token, the next scheduled run will fail and
    force the user to re-authenticate via the web form.

    add_secret_version doesn't overwrite: it adds a new version, and readers
    asking for "latest" automatically get it.
    """
    parent = f"projects/{PROJECT}/secrets/omron-token-{user}"
    _sm.add_secret_version(parent=parent, payload={"data": json.dumps(tokens).encode()})


def _users() -> list[str]:
    """Auto-discover users with a connected Omron account via omron-token-* secrets.

    OMRON_USERS env (comma-separated) overrides — handy for targeted testing.
    (Same discovery pattern as garmin_sync._users — see comments there.)
    """
    env = [u.strip() for u in os.environ.get("OMRON_USERS", "").split(",") if u.strip()]
    if env:
        return env
    out = []
    for s in _sm.list_secrets(parent=f"projects/{PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("omron-token-"):
            out.append(short[len("omron-token-"):])
    return out


# --------------------------------------------------------------------------- #
# Omron API
# --------------------------------------------------------------------------- #
def _checksum_hook(req: httpx.Request) -> None:
    """Attach SHA-256 body checksum required by Omron v2 API on POST/DELETE.

    Registered as an httpx "event hook" (see the Client(...) call in
    omron_sync below): httpx calls this automatically on every outgoing
    request, so no individual call site has to remember the checksum.
    """
    if req.method in ("POST", "DELETE") and req.content:
        req.headers["Checksum"] = hashlib.sha256(req.content).hexdigest()


def _refresh(client: httpx.Client, tokens: dict) -> dict:
    """Trade the stored refreshToken for a fresh access/refresh token pair.
    Mutates and returns the same `tokens` dict with the new values."""
    r = client.post(
        f"{_OMRON_SERVER}/login",
        json={
            "app": "OCM",
            "emailAddress": tokens["email"],
            "refreshToken": tokens["refreshToken"],
        },
        headers={"authorization": tokens.get("accessToken", "")},
    )
    r.raise_for_status()
    resp = r.json()
    tokens["accessToken"] = resp["accessToken"]
    tokens["refreshToken"] = resp["refreshToken"]
    return tokens


def _fetch_bp(client: httpx.Client, tokens: dict, since_ms: int) -> list[dict]:
    """Fetch all BP readings since `since_ms` (Unix ms), handling pagination.

    The API returns readings a page at a time plus a `nextpaginationKey`
    cursor; keep requesting until a page comes back empty or the cursor stops
    advancing. `while True` + `break` is the standard "loop until done" shape
    when the number of iterations isn't known up front.
    """
    readings: list[dict] = []
    pagination_key = 0
    while True:
        r = client.get(
            f"{_OMRON_SERVER}/sync/bp",
            params={
                "nextpaginationKey": pagination_key,
                "lastSyncedTime": since_ms if since_ms > 0 else "",
                "phoneIdentifier": "",
            },
            headers={"authorization": tokens["accessToken"]},
        )
        r.raise_for_status()
        resp = r.json()
        page: list[dict] = resp.get("data") or []
        if not page:
            print(f"[fetch-bp] empty data for pagination_key={pagination_key} since_ms={since_ms} resp={resp}")
            break
        readings.extend(page)
        next_key = resp.get("nextpaginationKey")
        if not next_key or next_key == pagination_key:   # no cursor / stuck cursor = done
            break
        pagination_key = next_key
    return readings


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _to_dt(ts_raw: int) -> dt.datetime:
    """Unix timestamp -> UTC datetime. Omron sometimes sends milliseconds and
    sometimes seconds; anything > 1e10 can only be milliseconds (1e10 seconds
    would be the year 2286), so divide those by 1000."""
    ts_sec = ts_raw / 1000 if ts_raw > 1e10 else float(ts_raw)
    return dt.datetime.fromtimestamp(ts_sec, tz=dt.timezone.utc)


def _parse(user: str, m: dict, ingested_ts: str) -> dict:
    """Normalize one raw Omron measurement into an omron_bp_daily row."""
    utc_dt = _to_dt(int(m["measurementDate"]))
    # Omron reports the device's own UTC offset per reading — use that (not a
    # hardcoded shift) so this stays correct if a reading was taken elsewhere.
    tz_offset_minutes = int(m["timeZone"]) // 60   # API gives seconds; // = integer division
    local_dt = (utc_dt + dt.timedelta(minutes=tz_offset_minutes)).replace(tzinfo=None)
    return {
        "user_id": user,
        "measurement_date": local_dt.date().isoformat(),
        "measurement_ts_utc": local_dt.isoformat(),
        "tz_offset_minutes": tz_offset_minutes,
        "systolic": int(m["systolic"]),
        "diastolic": int(m["diastolic"]),
        "pulse": int(m["pulse"]),
        # Omron sends 0/1 flags; `!= 0` converts to proper True/False booleans.
        "irregular_hb": int(m.get("irregularHB", 0)) != 0,
        "movement_detect": int(m.get("movementDetect", 0)) != 0,
        "cuff_wrap_detect": int(m.get("cuffWrapDetect", 0)) != 0,
        "notes": m.get("notes", ""),
        "ingested_ts": ingested_ts,
        "raw": json.dumps(m),   # full original payload, kept for future reference
    }


# --------------------------------------------------------------------------- #
# BigQuery upsert
# --------------------------------------------------------------------------- #
def _upsert(user: str, rows: list[dict]) -> None:
    """Delete (user_id, measurement_date) rows then reload — idempotent.
    (Same delete-then-load pattern as the other sync functions.)"""
    dates = list({r["measurement_date"] for r in rows})
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND measurement_date IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user),
            bigquery.ArrayQueryParameter("d", "DATE", dates),
        ]),
    ).result()
    _bq.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@functions_framework.http
def omron_sync(request):
    days = int(request.args.get("days", DAYS_BACK))
    since_date = dt.date.today() - dt.timedelta(days=days)
    # Omron's API wants "since when" as Unix milliseconds -> build a UTC
    # datetime at midnight of since_date, convert to seconds, then *1000.
    since_ms = int(
        dt.datetime(since_date.year, since_date.month, since_date.day,
                    tzinfo=dt.timezone.utc).timestamp() * 1000
    )
    # Pipeline bookkeeping timestamp (not a device reading) — fixed PDT (UTC-7).
    ingested_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).replace(tzinfo=None).isoformat()
    out: dict[str, object] = {}

    for user in _users():
        try:
            tokens = _load_tokens(user)
            with httpx.Client(
                event_hooks={"request": [_checksum_hook]},   # auto-checksum every request
                headers={"user-agent": _OMRON_USER_AGENT},
            ) as client:
                tokens = _refresh(client, tokens)
                _save_tokens(user, tokens)   # persist rotated tokens IMMEDIATELY —
                                             # before anything below can fail
                raw = _fetch_bp(client, tokens, since_ms)

            rows = [_parse(user, m, ingested_ts) for m in raw]

            if EARLIEST_DATE:
                # String comparison works here because ISO dates ("2026-07-28")
                # sort the same alphabetically as chronologically.
                rows = [r for r in rows if r["measurement_date"] >= EARLIEST_DATE]

            if rows:
                _upsert(user, rows)
            out[user] = len(rows)
        except Exception as exc:  # one user's failure must not abort the rest
            msg = f"error: {type(exc).__name__}: {exc}"
            print(f"[omron-sync] {user}: {msg}", file=sys.stderr)
            out[user] = msg

    status = "ok" if not any(isinstance(v, str) for v in out.values()) else "partial"
    return (
        json.dumps({"status": status, "since": since_date.isoformat(), "rows_per_user": out}),
        200,
        {"Content-Type": "application/json"},
    )
