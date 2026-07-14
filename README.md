# DigitalTwin — multi-user metabolic digital twin

Predicts each person's post-meal health response (glucose excursion, etc.) from
**meal macros** (Gemini Vision on a photo) + **CGM glucose** + **Garmin** context.
Built for **3 people**, fully attributed and siloed per user.

GCP project: `digitaltwin-499202` · region: `us-central1`

## Architecture

```
 iPhone meal photo ─(Apple Shortcut + user token)─►  Cloud Function: upload_meal
                                                          │ writes
                                                          ▼
                       GCS  gs://digitaltwin-499202-meals/<user_id>/inbox/*.jpg
                                                          │ object-finalize event
                                                          ▼
                                          Cloud Function: process_meal
                                          ├─ Vertex AI Gemini → macros JSON
                                          └─ write row → BigQuery health_twin.meals
 Garmin puller ─► garmin_daily ┐
 CGM poller    ─► glucose      ┼─► BigQuery (per-user, partitioned + clustered)
                               ┘        │ feature builder windows CGM per meal
                                        ▼
                            per-user model (XGBoost) → predictions
```

**Multi-user model:** one project, one bucket, one dataset. Every row carries a
`user_id`; tables are date-partitioned and **clustered by `user_id`** so each
person's queries stay fast and cheap. Per-user credentials live in Secret
Manager (`upload-token-<user>`, `garmin-creds-<user>`, `cgm-creds-<user>`).

## Status / prerequisites (do these first)

Nothing cloud-side can be created until **both** are done:

1. **Enable billing** on `digitaltwin-499202`
   → https://console.cloud.google.com/billing (BigQuery + GCS + Functions all
   require it; this project's data volume stays within the free tiers).
2. **Refresh Application Default Credentials**:
   ```
   gcloud auth application-default login
   gcloud auth application-default set-quota-project digitaltwin-499202
   ```

## Provision the foundation

After the prerequisites:

```bash
cp config/users.example.yaml config/users.yaml   # fill in the 3 real user_ids
# edit the USERS=(...) array in infra/setup.sh to match
./infra/setup.sh
```

This enables APIs and creates the bucket, the `health_twin` dataset + tables
(`meals`, `glucose`, `garmin_daily`), the pipeline service account, and per-user
secret placeholders. It refuses to run if billing isn't enabled.

## Repo layout

```
infra/              Infra-as-code: setup.sh + BigQuery table schemas
config/             users.example.yaml (3-person config)
ingestion/
  garmin/           Garmin Connect puller + grapher (proof of concept)
  omron/            Omron Connect reader
  cgm/              (pending) Libre/Dexcom poller
functions/
  meal_upload/      photo/description -> Gemini -> BigQuery meals
  meal_web/         phone web app (notes, saved meals, time picker)
  garmin_sync/          daily Garmin wellness poll -> garmin_daily
  garmin_intraday_sync/ 15-min Garmin intraday poll -> garmin_intraday
```

## What's built vs. pending

- ✅ Infra-as-code (`infra/setup.sh`), BigQuery schemas, user config
- ✅ Meal logging: `meal-upload` + `meal-web` (photo and/or description, saved
  meals, optional meal time) → `meals`
- ✅ Garmin daily wellness → `garmin-sync` (daily `garmin-daily` job) → `garmin_daily`
- ✅ Garmin **intraday** (heart rate, stress, body battery, respiration) →
  `garmin-intraday-sync` (15-min `garmin-intraday` job) → `garmin_intraday`,
  UTC timestamps to align with glucose + meals
- ⏳ CGM poller (`ingestion/cgm/`) once a sensor is chosen → `glucose`
- ⏳ Feature builder (window CGM per meal) + per-user model training

## Garmin sync — onboarding a user

Garmin login needs a password + MFA (can't run unattended), but the token it
returns auto-refreshes for ~1 year. So each person logs in **once**; only the
token is stored (in Secret Manager as `garmin-token-<user>`), never the password.

**Self-serve (recommended):** each person opens **`/connect/<their-link-token>`**
on the `meal-web` service — the same personal link they use for meals — enters
their Garmin login once (handles MFA), and they're done. The sync functions
**auto-discover** anyone with a `garmin-token-<user>` secret, so no redeploy is
needed. (A new user's empty token secret must exist first; pre-create with
`gcloud secrets create garmin-token-<user>`.)

**CLI alternative (operator):** run `ingestion/garmin/bootstrap_token.py <user>`.

The pipeline SA holds only `secretVersionAdder` + `viewer` + `accessor` on
secrets (no admin), so the public web service can write/discover tokens but
cannot read or delete them.

Backfill history any time: `GET garmin-sync?days=N` (default 2 days).
Connected: **kevin** (christian/vincent: secrets staged, awaiting their login).

## Privacy notes

Health data for 3 people. Bucket is private with public-access-prevention;
`process_meal` uses **Vertex AI Gemini** (inputs are *not* used to train Google's
models, unlike the free AI Studio tier). Secrets never live in git
(`config/users.yaml` and `.env` are gitignored).
