"""whoop-sync: heart-rate ingestion from WHOOP's unofficial metrics-service.

Split into this pure-ish sync logic and main.py's thin HTTP wrapper because
sync_user() is called two ways: by the deployed Cloud Function (one user at
a time, token read from Secret Manager), and directly in-process by
ingestion/whoop/login_and_sync.py right after a fresh manual token capture —
that script already holds the token in memory, so there's no reason for it
to go back out over HTTP to reach this same logic.

See ingestion/whoop/probe_metrics.py's docstring for how this request shape
was found and how to pull a fresh token by hand. WHOOP has no usable
refresh-token flow (confirmed by exhaustive live testing — the web client
never exercises it), so there is no automatic renewal anywhere in this
file: a 401 means a human needs to log in again, not a bug here.
"""

from __future__ import annotations

import datetime as dt

import httpx
from google.cloud import bigquery

PROJECT = "digitaltwin-499202"
DATASET = "health_twin"
TABLE = f"{PROJECT}.{DATASET}.whoop_hr_readings"

API_HOST = "https://api.prod.whoop.com"
STEP_SECONDS = 6   # validated live: continuous, gap-free, all-day coverage at this step

# This pipeline stores local wall-clock time (fixed PDT, UTC-7 — no DST
# handling, same simplification glucose/meals already use) directly in
# TIMESTAMP columns, not genuine UTC. Only the WHOOP API request itself
# (start/end sent to _fetch_heart_rate) stays in real UTC/ISO-8601 — that's
# what the API expects; this offset only applies to what gets stored.
PDT_OFFSET = dt.timedelta(hours=-7)

_bq_client: bigquery.Client | None = None


def _bq() -> bigquery.Client:
    """Lazy, cached BigQuery client — NOT built at import time (unlike
    omron_sync/garmin_sync's module-level client), because test_sync.py
    imports this module to test parse_rows() in CI, where there are no GCP
    credentials at all. Building eagerly broke exactly that: the test
    runner has no ADC file, so bigquery.Client() raised
    DefaultCredentialsError just from importing the module, before any test
    even ran."""
    global _bq_client
    if _bq_client is None:
        _bq_client = bigquery.Client(project=PROJECT)
    return _bq_client


def _iso_ms(epoch_ms: int) -> str:
    """Epoch milliseconds -> "2026-10-21T00:00:00.000Z". The metrics-service
    400s ("Start or end invalid") on a bare epoch integer for start/end —
    confirmed live — even though its response timestamps come back as
    epoch-ms. ISO-8601 matches how WHOOP's public API takes dates elsewhere."""
    return (dt.datetime.fromtimestamp(epoch_ms / 1000, tz=dt.timezone.utc)
           .strftime("%Y-%m-%dT%H:%M:%S.") + f"{epoch_ms % 1000:03d}Z")


def _headers(access_token: str) -> dict:
    """Browser-mimicking headers. api.prod.whoop.com sits behind Cloudflare's
    Browser Integrity Check, which 403s (error 1010) a bare httpx call on
    header/fingerprint alone — confirmed live, fixed by these headers alone
    (this host's Cloudflare tier doesn't need a solved JS challenge /
    cf_clearance cookie the way app.whoop.com itself does). Capital "Bearer"
    is deliberate: confirmed off a real browser request, overriding
    third-party reverse-engineering notes that claimed lowercase."""
    return {
        "authorization": f"Bearer {access_token}",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/129.0.0.0 Safari/537.36"),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://app.whoop.com",
        "referer": "https://app.whoop.com/",
        "sec-fetch-site": "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    }


def _fetch_heart_rate(whoop_user_id: str, access_token: str,
                      start_ms: int, end_ms: int) -> dict:
    """One call to the metrics-service heart_rate endpoint. Raises
    httpx.HTTPStatusError on a non-2xx response — a 401 here means the
    token's dead; callers decide how to report that, this doesn't swallow it."""
    params = {
        "name": "heart_rate",
        "start": _iso_ms(start_ms),
        "end": _iso_ms(end_ms),
        "step": STEP_SECONDS,
        "order": "t",
        "apiVersion": 7,
    }
    url = f"{API_HOST}/metrics-service/v1/metrics/user/{whoop_user_id}"
    r = httpx.get(url, params=params, headers=_headers(access_token), timeout=30)
    r.raise_for_status()
    return r.json()


def parse_rows(user_id: str, payload: dict, ingested_ts: str) -> list[dict]:
    """Parse one metrics-service response into whoop_hr_readings rows.
    Points missing a usable time/value are skipped rather than crashing the
    whole sync over one bad sample."""
    out = []
    for v in payload.get("values", []):
        ts_ms, value = v.get("time"), v.get("data")
        if not isinstance(ts_ms, (int, float)) or not isinstance(value, (int, float)):
            continue
        ts = (dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc) + PDT_OFFSET).replace(tzinfo=None)
        out.append({
            "user_id": user_id,
            "ts": ts.isoformat(),
            "hr_value": int(value),
            "ingested_ts": ingested_ts,
        })
    return out


def _upsert(user_id: str, rows: list[dict]) -> None:
    """Idempotent delete-then-load, same pattern as omron_sync._upsert —
    scoped to the UTC dates actually covered by these rows, so a generous
    backfill window is cheap to re-run and never creates duplicates."""
    dates = sorted({r["ts"][:10] for r in rows})
    _bq().query(
        f"DELETE FROM `{TABLE}` WHERE user_id=@u AND DATE(ts) IN UNNEST(@d)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("u", "STRING", user_id),
            bigquery.ArrayQueryParameter("d", "DATE", dates),
        ]),
    ).result()
    _bq().load_table_from_json(
        rows, TABLE,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND"),
    ).result()


def sync_user(user_id: str, whoop_user_id: str, access_token: str, days: int) -> int:
    """Fetch the last `days` of heart rate for one user and upsert into
    whoop_hr_readings. Returns the number of rows written (0 if none).
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    payload = _fetch_heart_rate(whoop_user_id, access_token, start_ms, end_ms)
    ingested_ts = (now + PDT_OFFSET).replace(tzinfo=None).isoformat()
    rows = parse_rows(user_id, payload, ingested_ts)
    if rows:
        _upsert(user_id, rows)
    return len(rows)
