"""One-time backfill of the three columns added for the Intensity × Sleep view.

Two independent passes:

  1. Intensity minutes — already sitting in every row's `raw.user_summary`
     JSON, so one UPDATE per run copies them into the new
     moderate/vigorous_intensity_minutes columns. No Garmin API involved.

  2. Deep-sleep latency — needs the sleepLevels stage timeline, which the
     sync did NOT store historically, so this re-fetches get_sleep_data(date)
     from Garmin for every (user, night) still missing a latency and computes
     it with the same sleep_latency module the sync function now uses.

Idempotent: pass 2 only queries dates where deep_sleep_latency_seconds is
still NULL, so re-running after a partial failure resumes where it stopped.
(Nights with stage data but no deep sleep stay NULL and are re-checked on a
re-run — the API calls are cheap and the row count only shrinks.)

Usage:
    python ingestion/garmin/backfill_deep_sleep_latency.py [user ...]
    # no args = every user with a garmin-token-* secret

Requires gcloud ADC (bigquery + secretmanager access) and the deps in
ingestion/garmin/requirements.txt plus google-cloud-bigquery.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from garminconnect import Garmin
from google.cloud import bigquery, secretmanager

# Reuse the sync function's latency math rather than copying it.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "functions" / "garmin_sync"))
from sleep_latency import deep_sleep_latency_seconds  # noqa: E402

PROJECT = "digitaltwin-499202"
TABLE = f"{PROJECT}.health_twin.garmin_daily"
API_PAUSE_S = 0.5   # be gentle with Garmin's unofficial API

_bq = bigquery.Client(project=PROJECT)
_sm = secretmanager.SecretManagerServiceClient()


def _users() -> list[str]:
    """Same auto-discovery as the sync function: garmin-token-* secrets."""
    out = []
    for s in _sm.list_secrets(parent=f"projects/{PROJECT}"):
        short = s.name.split("/")[-1]
        if short.startswith("garmin-token-"):
            out.append(short[len("garmin-token-"):])
    return out


def _token(user: str) -> str:
    name = f"projects/{PROJECT}/secrets/garmin-token-{user}/versions/latest"
    return _sm.access_secret_version(name=name).payload.data.decode()


def backfill_intensity_from_raw() -> int:
    """Copy intensity minutes out of the stored raw JSON into the columns.
    LAX_INT64 tolerates the values arriving as JSON numbers or strings and
    returns NULL (not an error) for anything unparseable."""
    job = _bq.query(f"""
        UPDATE `{TABLE}`
        SET moderate_intensity_minutes = LAX_INT64(raw.user_summary.moderateIntensityMinutes),
            vigorous_intensity_minutes = LAX_INT64(raw.user_summary.vigorousIntensityMinutes)
        WHERE raw IS NOT NULL AND moderate_intensity_minutes IS NULL
    """)
    job.result()
    return job.num_dml_affected_rows or 0


def _dates_missing_latency(user: str) -> list[str]:
    """Nights that recorded sleep but have no latency yet. sleep_seconds
    filters out rows that exist only for e.g. a standalone weigh-in."""
    job = _bq.query(
        f"SELECT date FROM `{TABLE}` WHERE user_id=@u "
        "AND deep_sleep_latency_seconds IS NULL AND sleep_seconds IS NOT NULL "
        "ORDER BY date",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user)]),
    )
    return [r["date"].isoformat() for r in job.result()]


def _write_latencies(user: str, dates: list[str], latencies: list[int]) -> None:
    """One UPDATE for all of a user's computed latencies: the two parallel
    arrays are zipped back together via matching UNNEST offsets."""
    job = _bq.query(f"""
        UPDATE `{TABLE}` t
        SET deep_sleep_latency_seconds = u.latency
        FROM (SELECT d, latency
              FROM UNNEST(@dates) d WITH OFFSET i
              JOIN UNNEST(@latencies) latency WITH OFFSET j ON i = j) u
        WHERE t.user_id = @u AND t.date = u.d
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("u", "STRING", user),
        bigquery.ArrayQueryParameter("dates", "DATE", dates),
        bigquery.ArrayQueryParameter("latencies", "INT64", latencies)]))
    job.result()


def backfill_latency(user: str) -> tuple[int, int]:
    """Fetch stage timelines from Garmin for every night missing a latency;
    returns (nights checked, latencies written)."""
    dates = _dates_missing_latency(user)
    if not dates:
        return 0, 0
    g = Garmin()
    g.login(_token(user))
    got_dates, got_latencies = [], []
    for d in dates:
        try:
            sleep_data = g.get_sleep_data(d) or {}
        except Exception as exc:
            print(f"  {d}: fetch failed ({type(exc).__name__}), skipping", file=sys.stderr)
            continue
        latency = deep_sleep_latency_seconds(
            sleep_data.get("dailySleepDTO") or {},
            sleep_data.get("sleepLevels") or [])
        if latency is not None:
            got_dates.append(d)
            got_latencies.append(latency)
        time.sleep(API_PAUSE_S)
    if got_dates:
        _write_latencies(user, got_dates, got_latencies)
    return len(dates), len(got_dates)


def main() -> None:
    users = [u.strip().lower() for u in sys.argv[1:]] or _users()
    n = backfill_intensity_from_raw()
    print(f"intensity minutes: filled from raw on {n} rows")
    for user in users:
        checked, written = backfill_latency(user)
        print(f"{user}: {checked} nights missing latency, {written} filled")


if __name__ == "__main__":
    main()
