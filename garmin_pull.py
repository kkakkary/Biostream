"""Pull longitudinal health data from Garmin Connect to local files.

Proof of concept: authenticate to Garmin Connect, pull the last N days of
daily-wellness, sleep, and HRV/recovery metrics, and write each metric/day as
raw JSON (lossless, BigQuery-ready) plus a flattened per-metric CSV.

Usage:
    cp .env.example .env   # then fill in GARMIN_EMAIL / GARMIN_PASSWORD
    python garmin_pull.py

Credentials come from environment variables (loaded from .env). On the first
run you may be prompted for a 2FA code; the auth token is cached under
~/.garminconnect so later runs (and cloud runs) need no interaction.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
load_dotenv()

EMAIL = os.getenv("GARMIN_EMAIL")
PASSWORD = os.getenv("GARMIN_PASSWORD")
DAYS_BACK = int(os.getenv("DAYS_BACK", "7"))
TOKENSTORE = os.path.expanduser(os.getenv("GARMIN_TOKENSTORE", "~/.garminconnect"))

OUTPUT_ROOT = Path(__file__).resolve().parent / "data"
JSON_DIR = OUTPUT_ROOT / "json"
CSV_DIR = OUTPUT_ROOT / "csv"


# --------------------------------------------------------------------------- #
# Metric registry: name -> function(client, cdate) -> raw API response.
#
# Data-driven so adding a metric later (e.g. activities) is a one-line change.
# Each callable takes the Garmin client and an ISO date string (YYYY-MM-DD).
# --------------------------------------------------------------------------- #
METRICS = {
    # --- Daily wellness ---
    "user_summary": lambda c, d: c.get_user_summary(d),
    "heart_rates": lambda c, d: c.get_heart_rates(d),
    "stress": lambda c, d: c.get_stress_data(d),
    "body_battery": lambda c, d: c.get_body_battery(d, d),
    "steps": lambda c, d: c.get_steps_data(d),
    "intensity_minutes": lambda c, d: c.get_intensity_minutes_data(d),
    "floors": lambda c, d: c.get_floors(d),
    # --- Sleep ---
    "sleep": lambda c, d: c.get_sleep_data(d),
    # --- HRV / recovery ---
    "hrv": lambda c, d: c.get_hrv_data(d),
    "training_readiness": lambda c, d: c.get_training_readiness(d),
    "spo2": lambda c, d: c.get_spo2_data(d),
    "respiration": lambda c, d: c.get_respiration_data(d),
}


def authenticate() -> Garmin:
    """Log in to Garmin Connect, reusing a cached token when available."""
    if not EMAIL or not PASSWORD:
        sys.exit(
            "Missing credentials. Copy .env.example to .env and set "
            "GARMIN_EMAIL and GARMIN_PASSWORD."
        )

    client = Garmin(EMAIL, PASSWORD, prompt_mfa=lambda: input("MFA code: ").strip())
    try:
        # login() loads the cached token from TOKENSTORE if present; otherwise it
        # performs a full credential login (prompting for MFA if needed) and
        # persists the token back to TOKENSTORE automatically.
        client.login(TOKENSTORE)
    except GarminConnectTooManyRequestsError:
        sys.exit(
            "Garmin rate-limited the login (429). Wait a few minutes and retry."
        )
    except GarminConnectAuthenticationError as exc:
        sys.exit(f"Authentication failed: {exc}")
    except GarminConnectConnectionError as exc:
        sys.exit(f"Could not connect to Garmin: {exc}")

    print(f"Authenticated as {EMAIL} (token cached at {TOKENSTORE})")
    return client


def date_range(days_back: int) -> list[str]:
    """Return ISO date strings for the last `days_back` days, oldest first."""
    today = date.today()
    days = [today - timedelta(days=i) for i in range(days_back)]
    return [d.isoformat() for d in reversed(days)]


def is_empty(data) -> bool:
    """True when an API response carries no useful data."""
    if data is None:
        return True
    if isinstance(data, (list, dict, str)) and len(data) == 0:
        return True
    return False


def write_json(metric: str, cdate: str, data) -> Path:
    """Write one metric/day response to data/json/<metric>/<date>.json."""
    out_dir = JSON_DIR / metric
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{cdate}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return path


def write_csv(metric: str, rows: list[dict]) -> Path | None:
    """Flatten collected rows for a metric into data/csv/<metric>.csv.

    Uses pandas.json_normalize so nested top-level fields become dotted columns.
    Long sub-series (e.g. per-minute heart-rate arrays) stay only in the JSON;
    the CSV captures the day-level summary fields, one row per day.
    """
    if not rows:
        return None
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    for row in rows:
        payload = row["data"]
        # json_normalize needs a dict (or list of dicts); wrap bare lists.
        record = payload if isinstance(payload, dict) else {"value": payload}
        frame = pd.json_normalize(record)
        frame.insert(0, "pull_date", row["date"])
        frames.append(frame)
    df = pd.concat(frames, ignore_index=True, sort=False)
    path = CSV_DIR / f"{metric}.csv"
    df.to_csv(path, index=False)
    return path


def main() -> None:
    client = authenticate()
    dates = date_range(DAYS_BACK)
    print(f"Pulling {len(METRICS)} metrics across {len(dates)} days "
          f"({dates[0]} to {dates[-1]})\n")

    json_files = 0
    csv_files = 0

    for metric, fetch in METRICS.items():
        rows: list[dict] = []
        ok = skipped = failed = 0
        for cdate in dates:
            try:
                data = fetch(client, cdate)
            except Exception as exc:  # noqa: BLE001 - one bad day must not abort
                failed += 1
                print(f"  [{metric}] {cdate}: ERROR {type(exc).__name__}: {exc}")
                continue
            if is_empty(data):
                skipped += 1
                continue
            write_json(metric, cdate, data)
            json_files += 1
            rows.append({"date": cdate, "data": data})
            ok += 1

        csv_path = write_csv(metric, rows)
        if csv_path is not None:
            csv_files += 1
        status = f"{ok} ok"
        if skipped:
            status += f", {skipped} empty"
        if failed:
            status += f", {failed} failed"
        print(f"{metric:20s} {status}")

    print(f"\nDone. Wrote {json_files} JSON files and {csv_files} CSV files "
          f"under {OUTPUT_ROOT}")


if __name__ == "__main__":
    main()
