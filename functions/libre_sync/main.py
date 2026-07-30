"""libre-sync: CGM glucose readings from LibreLinkUp collector account → BigQuery.

One "collector" LibreLinkUp account receives sharing invites from all sensor
wearers (Christian, Kevin, Vincent). This function authenticates as the collector,
fetches the connections list, pulls the ~12 h glucose graph for each patient,
and idempotently upserts rows into health_twin.glucose.

Secret: `cgm-creds-collector` (or override with LIBRE_SECRET env var)
  Format: {"email": "...", "password": "..."}

BQ table: health_twin.glucose   (source = "libre")

Scheduled via Cloud Scheduler — every 15 min keeps CGM coverage gap-free.

HOW TO READ THIS FILE (bottom-up):
  `libre_sync()` at the very bottom is the entry point. Per run:
    1. _load_creds()        — collector email/password from Secret Manager
    2. _authenticate()      — log in to Abbott's LibreLinkUp API, get a token
    3. _fetch_connections() — list everyone sharing their sensor with us
    4. _fetch_graph()       — per person: current reading + ~12h of history
    5. _parse()             — normalize each raw reading into a BigQuery row
    6. _upsert()            — delete-then-insert those rows (idempotent)
"""

from __future__ import annotations  # modern type-hint syntax on older Pythons

import datetime as dt
import hashlib   # SHA-256, needed for Abbott's Account-Id header (see _authenticate)
import json
import os
import sys       # stderr printing -> shows as ERROR in Cloud Logging
from collections import defaultdict   # dict that auto-creates missing entries

import functions_framework   # turns libre_sync() into an HTTP endpoint
import httpx                 # HTTP client (like `requests`, but more modern)
from google.cloud import bigquery, secretmanager

# --- Configuration (env vars, read once at cold start) ----------------------
PROJECT       = os.environ["PROJECT"]                        # required — crash if missing
DATASET       = os.environ.get("BQ_DATASET", "health_twin")  # optional, with default
TABLE         = f"{PROJECT}.{DATASET}.glucose"
REGION        = os.environ.get("LIBRE_REGION", "us")
SECRET_NAME   = os.environ.get("LIBRE_SECRET", "cgm-creds-collector")

# Optional explicit firstName → user_id map, e.g. "Christian:christian,Kevin:kevin"
# This dict comprehension parses that string: split on commas into pairs, keep
# only pairs containing ":", split each into key/value, strip whitespace.
_USER_MAP = {
    k.strip(): v.strip()
    for pair in os.environ.get("LIBRE_USER_MAP", "").split(",")
    if ":" in pair
    for k, v in [pair.split(":", 1)]
}

# Module-level clients are reused across warm invocations (faster than
# reconnecting every request).
_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()

# LibreLinkUp (follower) app headers.
# Abbott's API only answers requests that look like they come from the real
# LibreLinkUp phone app — these headers are the disguise.
# `version` must be >= 4.16.0; bump here if Abbott returns 403.
_HEADERS = {
    "product":         "llu.android",
    "version":         "4.16.0",
    "Accept":          "application/json",
    "Content-Type":    "application/json",
    "accept-encoding": "gzip, deflate, br",
}


# --------------------------------------------------------------------------- #
# Secret Manager
# --------------------------------------------------------------------------- #
def _load_creds() -> dict:
    """Collector account credentials, stored as JSON in Secret Manager."""
    name = f"projects/{PROJECT}/secrets/{SECRET_NAME}/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


# --------------------------------------------------------------------------- #
# LibreLinkUp API
# --------------------------------------------------------------------------- #
def _authenticate(client: httpx.Client, creds: dict) -> tuple[str, str, str]:
    """Return (bearer_token, active_server, account_id).

    account_id = SHA-256(user.id) — required as the Account-Id header on all
    subsequent requests. Derivation confirmed against DevTools captures.

    Region handling: we first try the default regional server (api-us). If the
    account actually lives in another region, Abbott replies with a
    "redirect" flag + the right region, and we log in again over there.
    """
    default_server = f"https://api-{REGION}.libreview.io"

    # Inner helper (a function defined inside a function) — just avoids
    # repeating the login POST for the redirect case below.
    def _login(server: str) -> tuple[httpx.Response, dict]:
        r    = client.post(
            f"{server}/llu/auth/login",
            json={"email": creds["email"], "password": creds["password"]},
        )
        body = r.json()
        return r, body

    r, body = _login(default_server)
    active_server = default_server

    if body.get("data", {}).get("redirect"):
        region        = body["data"].get("region", REGION)
        active_server = f"https://api-{region}.libreview.io"
        r, body       = _login(active_server)

    r.raise_for_status()          # raises if HTTP status is 4xx/5xx
    if body.get("status") != 0:   # Abbott's own in-body error signal
        raise RuntimeError(f"LLU auth failed: {json.dumps(body)}")

    user       = body["data"]["user"]
    token      = body["data"]["authTicket"]["token"]
    account_id = hashlib.sha256(user["id"].encode()).hexdigest()
    return token, active_server, account_id


def _llu_headers(token: str, account_id: str) -> dict:
    """The two per-account headers every authenticated request must carry."""
    return {"Authorization": f"Bearer {token}", "Account-Id": account_id}


def _fetch_connections(
    client: httpx.Client, server: str, token: str, account_id: str
) -> list[dict]:
    """Everyone who has shared their sensor with the collector account.
    One 'connection' per person, including their patientId and sensor info."""
    r = client.get(
        f"{server}/llu/connections",
        headers=_llu_headers(token, account_id),
    )
    r.raise_for_status()
    return r.json().get("data") or []


def _fetch_graph(
    client: httpx.Client, server: str, token: str, account_id: str, patient_id: str
) -> dict:
    """One person's glucose data: the current live reading plus roughly the
    last 12 hours of history (that's all this endpoint ever returns — which is
    why the sync must run frequently to avoid gaps)."""
    r = client.get(
        f"{server}/llu/connections/{patient_id}/graph",
        headers=_llu_headers(token, account_id),
    )
    r.raise_for_status()
    return r.json().get("data") or {}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
# Abbott encodes the trend arrow as a number 1–5; map to readable names.
_TREND_MAP = {
    1: "SingleDown", 2: "FortyFiveDown", 3: "Flat",
    4: "FortyFiveUp", 5: "SingleUp",
}


def _parse(m: dict, user_id: str, sensor_id: str, ingested_ts: str) -> dict | None:
    """Normalize one raw Abbott measurement into a glucose-table row.
    Returns None for anything unusable (missing timestamp, zero value)."""
    if not m:
        return None
    # "Timestamp" is already local to the collector device's timezone (confirmed
    # against "FactoryTimestamp", which is the true-UTC equivalent, 7h ahead in
    # PDT) — use it as-is, no conversion. Only fall back to FactoryTimestamp
    # (true UTC, needs the fixed PDT shift) if Timestamp is missing.
    local = True
    ts_raw = m.get("Timestamp")
    if not ts_raw:
        ts_raw = m.get("FactoryTimestamp")
        local = False
    if not ts_raw:
        return None

    # Abbott's format looks like "7/28/2026 9:15:04 AM" -> strptime parses it.
    # If that fails, try ISO format ("2026-07-28T09:15:04") as a backstop.
    try:
        ts = dt.datetime.strptime(ts_raw, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        ts = dt.datetime.fromisoformat(ts_raw)
    if not local:
        ts = ts - dt.timedelta(hours=7)

    # Prefer the explicit mg/dL field; `or` falls through to "Value", then 0.
    glucose_mg_dl = float(m.get("ValueInMgPerDl") or m.get("Value") or 0)
    if glucose_mg_dl == 0:   # 0 = missing/invalid, never a real blood sugar
        return None

    trend_raw = m.get("TrendArrow")
    return {
        "user_id":       user_id,
        "ts":            ts.isoformat(),
        "glucose_mg_dl": glucose_mg_dl,
        # Known code -> name; unknown code -> its number as a string; absent -> None.
        "trend":         _TREND_MAP.get(trend_raw, str(trend_raw) if trend_raw else None),
        "source":        "libre",
        "sensor_id":     sensor_id or None,   # "" becomes None (NULL in BigQuery)
        "ingested_ts":   ingested_ts,
    }


def _first_name_to_user_id(first_name: str) -> str:
    """Map Abbott's firstName to our canonical user_id: explicit LIBRE_USER_MAP
    entry if present, else just lowercase the name ("Kevin" -> "kevin")."""
    return _USER_MAP.get(first_name, first_name.lower())


# --------------------------------------------------------------------------- #
# BigQuery upsert
# --------------------------------------------------------------------------- #
def _upsert(user_id: str, rows: list[dict]) -> None:
    """Delete existing (user_id, ts) rows then reload — idempotent.

    Because each run re-fetches ~12h of history, most readings were already
    stored by earlier runs; deleting those exact timestamps first means
    re-inserting them creates no duplicates.
    """
    timestamps = list({r["ts"] for r in rows})   # set comprehension -> unique ts
    # @u/@t are query parameters (safe value substitution — never build SQL by
    # string concatenation). UNNEST turns the array into something IN can match.
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND ts IN UNNEST(@t)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user_id),
            bigquery.ArrayQueryParameter("t", "DATETIME", timestamps),
        ]),
    ).result()   # block until the delete finishes before loading
    _bq.load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
@functions_framework.http
def libre_sync(request):
    # Pipeline bookkeeping timestamp (not a vendor reading) — fixed PDT (UTC-7).
    ingested_ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).replace(tzinfo=None).isoformat()
    out: dict[str, object] = {}   # per-user summary for the JSON response

    try:
        creds = _load_creds()
        # `with` = context manager: the HTTP client is closed automatically
        # when the block ends, even if an exception occurs.
        with httpx.Client(headers=_HEADERS, follow_redirects=True) as client:
            token, server, account_id = _authenticate(client, creds)
            connections               = _fetch_connections(client, server, token, account_id)

            # defaultdict(list): first access to a new key auto-creates an
            # empty list, so we can .append() without checking existence.
            rows_by_user: dict[str, list[dict]] = defaultdict(list)

            for conn in connections:
                patient_id = conn.get("patientId") or conn.get("id", "")
                first_name = conn.get("firstName", "")
                user_id    = _first_name_to_user_id(first_name)
                sensor_id  = (conn.get("sensor") or {}).get("sn", "")   # sensor serial number

                try:
                    graph   = _fetch_graph(client, server, token, account_id, patient_id)
                    current = (graph.get("connection") or {}).get("glucoseMeasurement")
                    history = graph.get("graphData") or []

                    # Combine the live reading (if any) with the history list,
                    # then parse each; _parse returns None for junk, which the
                    # `if row` filters out.
                    for m in ([current] if current else []) + history:
                        row = _parse(m, user_id, sensor_id, ingested_ts)
                        if row:
                            rows_by_user[user_id].append(row)

                except Exception as exc:   # one person's fetch failing must not stop the others
                    msg = f"error fetching graph: {type(exc).__name__}: {exc}"
                    print(f"[libre-sync] {user_id}: {msg}", file=sys.stderr)
                    out[user_id] = msg

            for user_id, rows in rows_by_user.items():
                try:
                    if rows:
                        _upsert(user_id, rows)
                    out[user_id] = len(rows)
                except Exception as exc:
                    msg = f"error upserting: {type(exc).__name__}: {exc}"
                    print(f"[libre-sync] {user_id}: {msg}", file=sys.stderr)
                    out[user_id] = msg

    except Exception as exc:   # catch-all for auth/creds failures (affects everyone)
        msg = f"error: {type(exc).__name__}: {exc}"
        print(f"[libre-sync] auth: {msg}", file=sys.stderr)
        out["_auth"] = msg

    # Any string value in `out` starting with "error" -> partial failure.
    # (Success values are row *counts*, i.e. ints.)
    status = "ok" if not any(
        isinstance(v, str) and v.startswith("error") for v in out.values()
    ) else "partial"

    return (
        json.dumps({"status": status, "rows_per_user": out}),
        200,
        {"Content-Type": "application/json"},
    )
