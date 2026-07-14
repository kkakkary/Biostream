"""libre-sync: CGM glucose readings from LibreLinkUp collector account → BigQuery.

One "collector" LibreLinkUp account receives sharing invites from all sensor
wearers (Christian, Kevin, Vincent). This function authenticates as the collector,
fetches the connections list, pulls the ~12 h glucose graph for each patient,
and idempotently upserts rows into health_twin.glucose.

Secret: `cgm-creds-collector` (or override with LIBRE_SECRET env var)
  Format: {"email": "...", "password": "..."}

BQ table: health_twin.glucose   (source = "libre")

Scheduled via Cloud Scheduler — every 15 min keeps CGM coverage gap-free.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from collections import defaultdict

import functions_framework
import httpx
from google.cloud import bigquery, secretmanager

PROJECT       = os.environ["PROJECT"]
DATASET       = os.environ.get("BQ_DATASET", "health_twin")
TABLE         = f"{PROJECT}.{DATASET}.glucose"
REGION        = os.environ.get("LIBRE_REGION", "us")
SECRET_NAME   = os.environ.get("LIBRE_SECRET", "cgm-creds-collector")

# Optional explicit firstName → user_id map, e.g. "Christian:christian,Kevin:kevin"
_USER_MAP = {
    k.strip(): v.strip()
    for pair in os.environ.get("LIBRE_USER_MAP", "").split(",")
    if ":" in pair
    for k, v in [pair.split(":", 1)]
}

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()

# LibreLinkUp (follower) app headers.
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
    name = f"projects/{PROJECT}/secrets/{SECRET_NAME}/versions/latest"
    return json.loads(_sm.access_secret_version(name=name).payload.data.decode())


# --------------------------------------------------------------------------- #
# LibreLinkUp API
# --------------------------------------------------------------------------- #
def _authenticate(client: httpx.Client, creds: dict) -> tuple[str, str, str]:
    """Return (bearer_token, active_server, account_id).

    account_id = SHA-256(user.id) — required as the Account-Id header on all
    subsequent requests. Derivation confirmed against DevTools captures.
    """
    default_server = f"https://api-{REGION}.libreview.io"

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

    r.raise_for_status()
    if body.get("status") != 0:
        raise RuntimeError(f"LLU auth failed: {json.dumps(body)}")

    user       = body["data"]["user"]
    token      = body["data"]["authTicket"]["token"]
    account_id = hashlib.sha256(user["id"].encode()).hexdigest()
    return token, active_server, account_id


def _llu_headers(token: str, account_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Account-Id": account_id}


def _fetch_connections(
    client: httpx.Client, server: str, token: str, account_id: str
) -> list[dict]:
    r = client.get(
        f"{server}/llu/connections",
        headers=_llu_headers(token, account_id),
    )
    r.raise_for_status()
    return r.json().get("data") or []


def _fetch_graph(
    client: httpx.Client, server: str, token: str, account_id: str, patient_id: str
) -> dict:
    r = client.get(
        f"{server}/llu/connections/{patient_id}/graph",
        headers=_llu_headers(token, account_id),
    )
    r.raise_for_status()
    return r.json().get("data") or {}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_TREND_MAP = {
    1: "SingleDown", 2: "FortyFiveDown", 3: "Flat",
    4: "FortyFiveUp", 5: "SingleUp",
}


def _parse(m: dict, user_id: str, sensor_id: str, ingested_ts: str) -> dict | None:
    if not m:
        return None
    ts_raw = m.get("Timestamp") or m.get("FactoryTimestamp")
    if not ts_raw:
        return None

    try:
        ts = dt.datetime.strptime(ts_raw, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        ts = dt.datetime.fromisoformat(ts_raw)
    ts = ts.replace(tzinfo=dt.timezone.utc)

    glucose_mg_dl = float(m.get("ValueInMgPerDl") or m.get("Value") or 0)
    if glucose_mg_dl == 0:
        return None

    trend_raw = m.get("TrendArrow")
    return {
        "user_id":       user_id,
        "ts":            ts.isoformat(),
        "glucose_mg_dl": glucose_mg_dl,
        "trend":         _TREND_MAP.get(trend_raw, str(trend_raw) if trend_raw else None),
        "source":        "libre",
        "sensor_id":     sensor_id or None,
        "ingested_ts":   ingested_ts,
    }


def _first_name_to_user_id(first_name: str) -> str:
    return _USER_MAP.get(first_name, first_name.lower())


# --------------------------------------------------------------------------- #
# BigQuery upsert
# --------------------------------------------------------------------------- #
def _upsert(user_id: str, rows: list[dict]) -> None:
    """Delete existing (user_id, ts) rows then reload — idempotent."""
    timestamps = list({r["ts"] for r in rows})
    _bq.query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND ts IN UNNEST(@t)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user_id),
            bigquery.ArrayQueryParameter("t", "TIMESTAMP", timestamps),
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
def libre_sync(request):
    ingested_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    out: dict[str, object] = {}

    try:
        creds = _load_creds()
        with httpx.Client(headers=_HEADERS, follow_redirects=True) as client:
            token, server, account_id = _authenticate(client, creds)
            connections               = _fetch_connections(client, server, token, account_id)

            rows_by_user: dict[str, list[dict]] = defaultdict(list)

            for conn in connections:
                patient_id = conn.get("patientId") or conn.get("id", "")
                first_name = conn.get("firstName", "")
                user_id    = _first_name_to_user_id(first_name)
                sensor_id  = (conn.get("sensor") or {}).get("sn", "")

                try:
                    graph   = _fetch_graph(client, server, token, account_id, patient_id)
                    current = (graph.get("connection") or {}).get("glucoseMeasurement")
                    history = graph.get("graphData") or []

                    for m in ([current] if current else []) + history:
                        row = _parse(m, user_id, sensor_id, ingested_ts)
                        if row:
                            rows_by_user[user_id].append(row)

                except Exception as exc:
                    out[user_id] = f"error fetching graph: {type(exc).__name__}: {exc}"

            for user_id, rows in rows_by_user.items():
                try:
                    if rows:
                        _upsert(user_id, rows)
                    out[user_id] = len(rows)
                except Exception as exc:
                    out[user_id] = f"error upserting: {type(exc).__name__}: {exc}"

    except Exception as exc:
        out["_auth"] = f"error: {type(exc).__name__}: {exc}"

    status = "ok" if not any(
        isinstance(v, str) and v.startswith("error") for v in out.values()
    ) else "partial"

    return (
        json.dumps({"status": status, "rows_per_user": out}),
        200,
        {"Content-Type": "application/json"},
    )
