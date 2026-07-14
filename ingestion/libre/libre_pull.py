"""Pull CGM glucose readings via a LibreLinkUp collector account.

A single "collector" LibreLinkUp account receives sharing invites from all
sensor wearers (Christian, Kevin, Vincent). This script authenticates as that
collector and pulls the ~12 h glucose graph for every connected patient.

Setup (one-time per sensor wearer):
  LibreLink app → Profile → Connected Apps → LibreLinkUp → Invite → <collector email>

Usage:
    cp .env.example .env   # fill in collector LIBRE_EMAIL / LIBRE_PASSWORD
    pip install -r requirements.txt
    python libre_pull.py

    # Discovery / debug mode:
    LIBRE_DISCOVER=1 python libre_pull.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

EMAIL    = os.getenv("LIBRE_EMAIL")
PASSWORD = os.getenv("LIBRE_PASSWORD")
REGION   = os.getenv("LIBRE_REGION", "us")
DISCOVER = os.getenv("LIBRE_DISCOVER", "").lower() in ("1", "true", "yes")
# Optional explicit firstName → user_id map, e.g. "Christian:christian,Kevin:kevin"
_USER_MAP = {
    k.strip(): v.strip()
    for pair in os.getenv("LIBRE_USER_MAP", "").split(",")
    if ":" in pair
    for k, v in [pair.split(":", 1)]
}

DEFAULT_SERVER = f"https://api-{REGION}.libreview.io"

# LibreLinkUp (follower app) headers.
# `version` must be >= 4.16.0; bump here if Abbott rejects with 403.
_HEADERS = {
    "product":         "llu.android",
    "version":         "4.16.0",
    "Accept":          "application/json",
    "Content-Type":    "application/json",
    "accept-encoding": "gzip, deflate, br",
}

OUTPUT_ROOT = Path(__file__).resolve().parent / "data"
JSON_DIR    = OUTPUT_ROOT / "json"
CSV_DIR     = OUTPUT_ROOT / "csv"


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _authenticate(client: httpx.Client) -> tuple[str, str, str]:
    """Return (bearer_token, active_server, account_id).

    account_id = SHA-256(user.id) — required as the `Account-Id` header on
    all subsequent requests (confirmed by DevTools capture 2026-07-09).
    """
    if not EMAIL or not PASSWORD:
        sys.exit(
            "Missing credentials. Copy .env.example to .env and set "
            "LIBRE_EMAIL / LIBRE_PASSWORD (use the collector account)."
        )

    def _login(server: str) -> tuple[httpx.Response, dict]:
        r    = client.post(
            f"{server}/llu/auth/login",
            json={"email": EMAIL, "password": PASSWORD},
        )
        body = r.json()
        if DISCOVER:
            print(f"\n[auth] POST {server}/llu/auth/login  status={r.status_code}")
            print(json.dumps(body, indent=2, default=str))
        return r, body

    r, body = _login(DEFAULT_SERVER)
    active_server = DEFAULT_SERVER

    if body.get("data", {}).get("redirect"):
        region        = body["data"].get("region", REGION)
        active_server = f"https://api-{region}.libreview.io"
        print(f"[auth] Redirected to {active_server}")
        r, body = _login(active_server)

    if r.status_code != 200 or body.get("status") != 0:
        print(f"\n[auth] Failed (status {r.status_code}):\n"
              + json.dumps(body, indent=2, default=str))
        sys.exit(
            "Authentication failed.\n"
            "Common causes:\n"
            "  • Wrong email/password for the collector account\n"
            "  • Accept Terms of Service in the LibreLinkUp app first\n"
            "  • Try LIBRE_REGION=eu if your account is European\n"
            "  • Bump `version` in _HEADERS if Abbott rejects with version error"
        )

    user       = body["data"].get("user", {})
    ticket     = body["data"]["authTicket"]
    token      = ticket["token"]
    account_id = hashlib.sha256(user.get("id", "").encode()).hexdigest()
    print(f"Authenticated as {user.get('email', EMAIL)}  (account_id={account_id[:16]}...)")
    return token, active_server, account_id


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #
def _llu_headers(token: str, account_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Account-Id": account_id}


def _get_connections(
    client: httpx.Client, server: str, token: str, account_id: str
) -> list[dict]:
    """GET /llu/connections — patients sharing with the collector account."""
    r    = client.get(
        f"{server}/llu/connections",
        headers=_llu_headers(token, account_id),
    )
    body = r.json()
    if DISCOVER:
        print(f"\n[connections] status={r.status_code}")
        print(json.dumps(body, indent=2, default=str))
    r.raise_for_status()
    return body.get("data") or []


def _get_graph(
    client: httpx.Client, server: str, token: str, account_id: str, patient_id: str
) -> dict:
    """GET /llu/connections/{patientId}/graph — ~12 h of glucose readings."""
    url  = f"{server}/llu/connections/{patient_id}/graph"
    r    = client.get(url, headers=_llu_headers(token, account_id))
    body = r.json()
    if DISCOVER:
        print(f"\n[graph/{patient_id[:8]}] status={r.status_code}")
        print(json.dumps(body, indent=2, default=str))
    r.raise_for_status()
    return body.get("data") or {}


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
_TREND_MAP = {
    1: "SingleDown", 2: "FortyFiveDown", 3: "Flat",
    4: "FortyFiveUp", 5: "SingleUp",
}


def _parse_measurement(m: dict, user_id: str, sensor_id: str, ingested_ts: str) -> dict | None:
    if not m:
        return None
    ts_raw = m.get("Timestamp") or m.get("FactoryTimestamp")
    if not ts_raw:
        return None

    try:
        ts = datetime.strptime(ts_raw, "%m/%d/%Y %I:%M:%S %p")
    except ValueError:
        ts = datetime.fromisoformat(ts_raw)
    ts = ts.replace(tzinfo=timezone.utc)

    glucose = float(m.get("ValueInMgPerDl") or m.get("Value") or 0)
    if glucose == 0:
        return None

    trend_raw = m.get("TrendArrow")
    return {
        "user_id":       user_id,
        "ts":            ts.isoformat(),
        "glucose_mg_dl": glucose,
        "trend":         _TREND_MAP.get(trend_raw, str(trend_raw) if trend_raw else None),
        "source":        "libre",
        "sensor_id":     sensor_id or None,
        "ingested_ts":   ingested_ts,
        "raw":           json.dumps(m, default=str),
    }


def _first_name_to_user_id(first_name: str) -> str:
    """Map a patient's first name to a user_id slug.

    Checks LIBRE_USER_MAP env override first, then lowercases the first name.
    """
    return _USER_MAP.get(first_name, first_name.lower())


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def _write_outputs(user_id: str, rows: list[dict]) -> None:
    out_dir = JSON_DIR / "cgm"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{user_id}.json").write_text(json.dumps(rows, indent=2, default=str))
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    path = CSV_DIR / f"{user_id}_cgm.csv"
    pd.DataFrame(rows).sort_values("ts").to_csv(path, index=False)
    print(f"  {len(rows)} rows → {out_dir}/{user_id}.json + {path.name}")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    with httpx.Client(headers=_HEADERS, follow_redirects=True) as client:
        token, server, account_id = _authenticate(client)

        connections = _get_connections(client, server, token, account_id)
        print(f"\nConnections: {len(connections)}")

        if not connections:
            print(
                "\nNo connections found. Make sure each sensor wearer has:\n"
                "  LibreLink app → Profile → Connected Apps → LibreLinkUp → Invite → <collector email>\n"
                "and the collector account has accepted each invite in the LibreLinkUp app."
            )
            return

        ingested_ts = datetime.now(timezone.utc).isoformat()
        all_rows: dict[str, list[dict]] = {}

        for conn in connections:
            patient_id  = conn.get("patientId") or conn.get("id", "")
            first_name  = conn.get("firstName", "")
            last_name   = conn.get("lastName", "")
            user_id     = _first_name_to_user_id(first_name)
            sensor_id   = (conn.get("sensor") or {}).get("sn", "")
            print(f"\n  {first_name} {last_name} → user_id={user_id}  sensor={sensor_id or 'unknown'}")

            graph   = _get_graph(client, server, token, account_id, patient_id)
            current = (graph.get("connection") or {}).get("glucoseMeasurement")
            history = graph.get("graphData") or []

            rows = []
            for m in ([current] if current else []) + history:
                row = _parse_measurement(m, user_id, sensor_id, ingested_ts)
                if row:
                    rows.append(row)

            print(f"    {len(rows)} readings ({len(history)} history + current)")
            all_rows[user_id] = rows

        print()
        for user_id, rows in all_rows.items():
            if rows:
                _write_outputs(user_id, rows)

    print("\nDone.")


if __name__ == "__main__":
    main()
