# Biostream — GCP data ingestion pipeline for N-of-1 studies

A Google Cloud–hosted pipeline that continuously ingests multi-source health
data for **3 subjects** (kevin, christian, vincent) into one BigQuery dataset,
so we can run **N-of-1 studies** — single-subject experiments where each person
serves as their own control (e.g. *how does this meal, medication, or habit
change my glucose, HRV, or blood pressure?*).

Every reading is timestamped, attributed to a `user_id`, and stored in
partitioned/clustered BigQuery tables, giving each subject a dense personal
time series that analysis notebooks can slice per experiment.

GCP project: `digitaltwin-499202` · region: `us-central1`

## Data sources

| Source | How it's ingested | BigQuery table(s) |
|---|---|---|
| **Garmin** daily wellness (sleep, steps, weight, …) | `garmin-sync` Cloud Function, daily scheduled poll | `garmin_daily` |
| **Garmin** intraday (heart rate, stress, body battery, respiration) | `garmin-intraday-sync`, 15-min scheduled poll, UTC timestamps | `garmin_intraday` |
| **Garmin** overnight HRV datapoints | recorded during the daily sync | `hrv_readings` |
| **CGM glucose** (FreeStyle Libre) | `libre-sync` polls a LibreLinkUp collector account | `glucose` |
| **Blood pressure** (Omron) | `omron-sync`, daily poll of Omron Connect | `blood_pressure` |
| **Meals** (photo and/or description → macros) | `meal-web` phone web app + `meal-upload` function; Vertex AI Gemini estimates macros | `meals`, `saved_meals` |
| **Medications** | logged manually alongside daily data | `garmin_daily` (`medications` column) |

## Architecture

```
 Phone (meal photo / description) ─► Cloud Run: meal-web ─► Cloud Function: meal-upload
                                                                 ├─ Vertex AI Gemini → macros JSON
                                                                 └─► BigQuery meals / saved_meals

 Cloud Scheduler ─► garmin-sync (daily)        ─► garmin_daily, hrv_readings
                 ─► garmin-intraday-sync (15m) ─► garmin_intraday
                 ─► libre-sync                 ─► glucose
                 ─► omron-sync                 ─► blood_pressure

 BigQuery dataset health_twin  (all tables date-partitioned, clustered by user_id)
        │
        ▼
 notebooks/  — per-subject analyses (e.g. glucose × overnight HRV)
```

**Multi-subject model:** one project, one bucket, one dataset. Every row
carries a `user_id`; tables are date-partitioned and **clustered by `user_id`**
so each subject's queries stay fast and cheap. Per-user credentials live in
Secret Manager (`upload-token-<user>`, `garmin-token-<user>`, …) — never in git.

## Repo layout

```
infra/               Infra-as-code: setup.sh, setup_cicd.sh, BigQuery table schemas
config/              users.example.yaml (3-subject config; real users.yaml is gitignored)
ingestion/
  garmin/            Garmin Connect puller + token bootstrap (CLI)
  libre/             LibreLinkUp CGM test scripts
  omron/             Omron Connect reader
functions/
  meal_upload/       photo/description -> Gemini -> BigQuery meals
  meal_web/          phone web app (meal logging, saved meals, time picker, Garmin connect)
  garmin_sync/       daily Garmin wellness poll -> garmin_daily + hrv_readings
  garmin_intraday_sync/  15-min Garmin intraday poll -> garmin_intraday
  libre_sync/        LibreLinkUp CGM poll -> glucose
  omron_sync/        daily Omron blood-pressure poll -> blood_pressure
  upload_photo/      helper: push an image to Google Photos
  health_dashboard/  public Streamlit dashboard (Cloud Run) showcasing one subject's data
notebooks/           Jupyter analyses over the BigQuery data (see notebooks/README.md)
.github/workflows/   CI/CD: deploy changed services to GCP on merge to main (WIF, no keys)
```

## Running an N-of-1 study

1. **Ingest continuously** — the scheduled functions above keep each subject's
   glucose, wearable, blood-pressure, and meal streams flowing into BigQuery
   with no manual effort beyond wearing the devices and logging meals.
2. **Intervene** — the subject changes one variable (a meal, medication timing,
   sleep habit, …) and logs it (meals via `meal-web`; medications in
   `garmin_daily.medications`).
3. **Analyze** — query the aligned time series in `notebooks/`
   (e.g. `glucose_hrv_analysis.ipynb` interpolates overnight HRV onto the dense
   CGM timeline for one subject). SQL lives in `notebooks/queries/`.

## Setup

Prerequisites: billing enabled on `digitaltwin-499202`, plus
Application Default Credentials:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project digitaltwin-499202
```

Provision the foundation:

```bash
cp config/users.example.yaml config/users.yaml   # fill in the 3 real user_ids
# edit the USERS=(...) array in infra/setup.sh to match
./infra/setup.sh        # APIs, bucket, health_twin dataset + tables, SA, secrets
./infra/setup_cicd.sh   # GitHub Actions deploy via Workload Identity Federation
```

Deploys after that are automatic: merging to `main` redeploys only the
service(s) whose source changed (`.github/workflows/deploy.yml`).

## Onboarding a subject (Garmin)

Garmin login needs a password + MFA (can't run unattended), but the token it
returns auto-refreshes for ~1 year. So each person logs in **once**; only the
token is stored (in Secret Manager as `garmin-token-<user>`), never the password.

**Self-serve (recommended):** the subject opens **`/connect/<their-link-token>`**
on the `meal-web` service — the same personal link they use for meals — enters
their Garmin login once (handles MFA), and they're done. The sync functions
**auto-discover** anyone with a `garmin-token-<user>` secret, so no redeploy is
needed. (Pre-create the empty secret with `gcloud secrets create garmin-token-<user>`.)

**CLI alternative (operator):** run `ingestion/garmin/bootstrap_token.py <user>`.

The pipeline SA holds only `secretVersionAdder` + `viewer` + `accessor` on
secrets (no admin), so the public web service can write/discover tokens but
cannot read or delete them.

Backfill history any time: `GET garmin-sync?days=N` (default 2 days).

## Privacy

Health data for 3 people. The bucket is private with public-access-prevention;
meal photos go through **Vertex AI Gemini** (inputs are *not* used to train
Google's models, unlike the free AI Studio tier). Secrets live only in Secret
Manager; `config/users.yaml` and `.env` are gitignored. CI/CD authenticates
with Workload Identity Federation — no long-lived service-account keys.
