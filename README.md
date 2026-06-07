# Garmin Vivoactive 6 — Longitudinal Data Pull

Pulls your Garmin Connect health data to local files. **Proof of concept**: confirm
data can be pulled. Raw JSON output is kept lossless so a later load into a BigQuery
table on GCP is straightforward.

## What it pulls

Last **7 days** (configurable) of:

- **Daily wellness** — user summary (steps, resting HR, calories, intensity minutes,
  floors), heart rates, stress, body battery, steps, intensity minutes, floors
- **Sleep** — stages, duration, sleep score
- **HRV / recovery** — HRV status, training readiness, SpO2, respiration

Output lands in `data/`:

- `data/json/<metric>/<YYYY-MM-DD>.json` — raw, lossless API responses
- `data/csv/<metric>.csv` — flattened, one row per day (summary fields)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and set GARMIN_EMAIL and GARMIN_PASSWORD
```

## Run

```bash
python garmin_pull.py
```

- On the **first run** you may be prompted for a 2FA code (if your account has it on).
  The auth token is then cached at `~/.garminconnect`, so later runs need no input.
- If Garmin returns **429 (too many requests)**, wait a few minutes and retry — the
  login endpoint is rate-limited.

## Configuration (optional, via `.env`)

| Variable            | Default          | Meaning                          |
| ------------------- | ---------------- | -------------------------------- |
| `GARMIN_EMAIL`      | —                | Garmin Connect account email     |
| `GARMIN_PASSWORD`   | —                | Garmin Connect account password  |
| `DAYS_BACK`         | `7`              | How many days back to pull       |
| `GARMIN_TOKENSTORE` | `~/.garminconnect` | Where the auth token is cached |

## Notes

- `.env` and `data/` are gitignored — credentials and personal data never get committed.
- Uses the community [`garminconnect`](https://github.com/cyberjunky/python-garminconnect)
  library (same mobile SSO login as the official app). Garmin has no official public API.
- Each metric call is isolated: one unsupported/empty endpoint won't abort the whole run.

## Next steps (future phases)

- Load the JSON into a BigQuery table (schema per metric).
- Run on a schedule in GCP (Cloud Run / Cloud Functions) with credentials in
  Secret Manager.
- Backfill full history and add activities/workouts to the metric registry.
