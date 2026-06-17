"""Pull blood pressure data from Omron Connect to local files.

Proof of concept: authenticate to Omron Connect (v2 API), pull the last N days
of blood pressure readings, and write each day as raw JSON (lossless,
BigQuery-ready) plus a flattened CSV.

Usage:
    cp .env.example .env   # then fill in OMRON_EMAIL / OMRON_PASSWORD
    python omron_pull.py

Credentials come from environment variables (loaded from .env). On the first
run the auth token is fetched via email/password and cached to
~/.omron_tokens.json; later runs silently refresh it — no interactive input
needed.

Auth/API approach reverse-engineered from https://github.com/bugficks/omramin.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd
from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

EMAIL = os.getenv("OMRON_EMAIL")
PASSWORD = os.getenv("OMRON_PASSWORD")
COUNTRY = os.getenv("OMRON_COUNTRY", "US")
DAYS_BACK = int(os.getenv("DAYS_BACK", "7"))
# Hard floor on measurement date — filters out previous-owner readings that
# Omron's API returns regardless of app-side deletion or lastSyncedTime.
_earliest_raw = os.getenv("OMRON_EARLIEST_DATE")
EARLIEST_DATE: date | None = date.fromisoformat(_earliest_raw) if _earliest_raw else None
TOKENSTORE = Path(
    os.path.expanduser(os.getenv("OMRON_TOKENSTORE", "~/.omron_tokens.json"))
)
# NA/US server; override via OMRON_SERVER for other regions.
SERVER = os.getenv("OMRON_SERVER", "https://vlt-mobile-api.prd.us.ohiomron.com/prd")
_APP = "OCM"
_USER_AGENT = "OmronConnect/3 CFNetwork/1410.0.3 Darwin/22.6.0"

OUTPUT_ROOT = Path(__file__).resolve().parent / "data"
JSON_DIR = OUTPUT_ROOT / "json"
CSV_DIR = OUTPUT_ROOT / "csv"


# --------------------------------------------------------------------------- #
# Omron v2 API requires a SHA-256 checksum header on POST/DELETE requests.
# Attached as an httpx request event hook so it applies transparently.
# --------------------------------------------------------------------------- #
def _add_checksum(request: httpx.Request) -> None:
    if request.method in ("POST", "DELETE") and request.content:
        request.headers["Checksum"] = hashlib.sha256(request.content).hexdigest()


# --------------------------------------------------------------------------- #
# Token persistence
# --------------------------------------------------------------------------- #
def _load_tokens() -> dict:
    if TOKENSTORE.exists():
        try:
            return json.loads(TOKENSTORE.read_text())
        except Exception:
            return {}
    return {}


def _save_tokens(tokens: dict) -> None:
    TOKENSTORE.write_text(json.dumps(tokens))


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def authenticate() -> tuple[httpx.Client, dict]:
    """Return an authenticated httpx client and the current tokens dict.

    Tries a silent token refresh first; falls back to full email/password login.
    Token is cached to TOKENSTORE so subsequent runs need no interaction.
    """
    client = httpx.Client(
        event_hooks={"request": [_add_checksum]},
        headers={"user-agent": _USER_AGENT},
    )
    tokens = _load_tokens()

    if tokens.get("refreshToken") and tokens.get("email"):
        try:
            r = client.post(
                f"{SERVER}/login",
                json={
                    "app": _APP,
                    "emailAddress": tokens["email"],
                    "refreshToken": tokens["refreshToken"],
                },
                headers={"authorization": tokens.get("accessToken", "")},
            )
            r.raise_for_status()
            resp = r.json()
            tokens["accessToken"] = resp["accessToken"]
            tokens["refreshToken"] = resp["refreshToken"]
            _save_tokens(tokens)
            print(f"Authenticated as {tokens['email']} (token refreshed)")
            return client, tokens
        except Exception:
            pass  # Fall through to full login

    if not EMAIL or not PASSWORD:
        sys.exit(
            "Missing credentials. Copy .env.example to .env and set "
            "OMRON_EMAIL and OMRON_PASSWORD."
        )
    try:
        r = client.post(
            f"{SERVER}/login",
            json={
                "emailAddress": EMAIL,
                "password": PASSWORD,
                "country": COUNTRY,
                "app": _APP,
            },
        )
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        sys.exit(
            f"Authentication failed ({exc.response.status_code}): {exc.response.text}"
        )
    except httpx.RequestError as exc:
        sys.exit(f"Could not connect to Omron: {exc}")

    resp = r.json()
    tokens = {
        "email": EMAIL,
        "accessToken": resp["accessToken"],
        "refreshToken": resp["refreshToken"],
    }
    _save_tokens(tokens)
    print(f"Authenticated as {EMAIL} (token cached at {TOKENSTORE})")
    return client, tokens


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #
def fetch_bp_readings(
    client: httpx.Client, tokens: dict, since_ms: int
) -> list[dict]:
    """Fetch all BP readings since `since_ms` (Unix ms), handling pagination."""
    readings: list[dict] = []
    pagination_key: int = 0

    while True:
        try:
            r = client.get(
                f"{SERVER}/sync/bp",
                params={
                    "nextpaginationKey": pagination_key,
                    "lastSyncedTime": since_ms if since_ms > 0 else "",
                    "phoneIdentifier": "",
                },
                headers={"authorization": tokens["accessToken"]},
            )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            sys.exit(
                f"Failed to fetch BP readings ({exc.response.status_code}): "
                f"{exc.response.text}"
            )

        resp = r.json()
        page_data: list[dict] = resp.get("data") or []
        if not page_data:
            break

        readings.extend(page_data)

        next_key = resp.get("nextpaginationKey")
        if not next_key or next_key == pagination_key:
            break
        pagination_key = next_key

    return readings


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _to_datetime(ts_raw: int) -> datetime:
    """Convert Omron timestamp (milliseconds) to UTC datetime."""
    ts_sec = ts_raw / 1000 if ts_raw > 1e10 else float(ts_raw)
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc)


def parse_reading(m: dict) -> dict:
    """Flatten one raw API measurement dict into a BigQuery-friendly record."""
    dt = _to_datetime(int(m["measurementDate"]))
    return {
        "measurement_date": dt.date().isoformat(),
        "measurement_ts_utc": dt.isoformat(),
        "tz_offset_minutes": int(m["timeZone"]) // 60,
        "systolic": int(m["systolic"]),
        "diastolic": int(m["diastolic"]),
        "pulse": int(m["pulse"]),
        "irregular_hb": int(m.get("irregularHB", 0)) != 0,
        "movement_detect": int(m.get("movementDetect", 0)) != 0,
        "cuff_wrap_detect": int(m.get("cuffWrapDetect", 0)) != 0,
        "notes": m.get("notes", ""),
    }


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_json(readings_by_date: dict[str, list[dict]]) -> int:
    """Write one JSON file per day to data/json/blood_pressure/<date>.json."""
    out_base = JSON_DIR / "blood_pressure"
    out_base.mkdir(parents=True, exist_ok=True)
    for cdate, readings in readings_by_date.items():
        (out_base / f"{cdate}.json").write_text(
            json.dumps(readings, indent=2, default=str)
        )
    return len(readings_by_date)


def write_csv(all_readings: list[dict]) -> Path | None:
    """Write all readings to data/csv/blood_pressure.csv, sorted by timestamp."""
    if not all_readings:
        return None
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(all_readings).sort_values("measurement_ts_utc")
    path = CSV_DIR / "blood_pressure.csv"
    df.to_csv(path, index=False)
    return path


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    client, tokens = authenticate()

    since_date = date.today() - timedelta(days=DAYS_BACK)
    since_ms = int(
        datetime(
            since_date.year, since_date.month, since_date.day, tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
    print(
        f"Pulling blood pressure readings since {since_date.isoformat()} "
        f"({DAYS_BACK} days)\n"
    )

    raw = fetch_bp_readings(client, tokens, since_ms)

    if not raw:
        print("blood_pressure        0 readings (none in date range)")
        print("\nDone. No data written.")
        return

    parsed = [parse_reading(m) for m in raw]

    if EARLIEST_DATE:
        before = len(parsed)
        parsed = [r for r in parsed if date.fromisoformat(r["measurement_date"]) >= EARLIEST_DATE]
        dropped = before - len(parsed)
        if dropped:
            print(f"Filtered {dropped} readings before {EARLIEST_DATE} (OMRON_EARLIEST_DATE)\n")

    by_date: dict[str, list[dict]] = {}
    for reading in parsed:
        by_date.setdefault(reading["measurement_date"], []).append(reading)

    json_count = write_json(by_date)
    csv_path = write_csv(parsed)

    print(f"blood_pressure        {len(parsed)} readings across {json_count} days")
    print(
        f"\nDone. Wrote {json_count} JSON files and "
        f"{'1 CSV file' if csv_path else '0 CSV files'} under {OUTPUT_ROOT}"
    )


if __name__ == "__main__":
    main()
