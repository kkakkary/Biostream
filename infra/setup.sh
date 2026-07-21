#!/usr/bin/env bash
#
# Provision the DigitalTwin GCP foundation: APIs, GCS bucket, BigQuery dataset
# + tables, a service account, and per-user secret placeholders.
#
# Idempotent — safe to re-run. Requires billing to be ENABLED on the project
# first (the script checks and refuses otherwise).
#
# Usage:
#   ./infra/setup.sh
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Config — edit the USERS list to match config/users.yaml
# --------------------------------------------------------------------------- #
PROJECT="digitaltwin-499202"
REGION="us-central1"
BUCKET="gs://${PROJECT}-meals"
DATASET="health_twin"
SA_NAME="twin-pipeline"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

USERS=("person1" "person2" "person3")   # <-- replace with real short user_ids

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #
echo "==> Project: ${PROJECT}  Region: ${REGION}"
gcloud config set project "${PROJECT}" >/dev/null

if [[ "$(gcloud billing projects describe "${PROJECT}" --format='value(billingEnabled)' 2>/dev/null)" != "True" ]]; then
  echo "ERROR: billing is not enabled on ${PROJECT}."
  echo "Link a billing account at https://console.cloud.google.com/billing then re-run."
  exit 1
fi

# --------------------------------------------------------------------------- #
# 1. Enable APIs
# --------------------------------------------------------------------------- #
echo "==> Enabling APIs (first run takes a few minutes)..."
gcloud services enable \
  bigquery.googleapis.com \
  storage.googleapis.com \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  eventarc.googleapis.com \
  pubsub.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com

# --------------------------------------------------------------------------- #
# 2. GCS bucket (private, uniform access, lifecycle)
# --------------------------------------------------------------------------- #
echo "==> Creating bucket ${BUCKET}..."
if ! gcloud storage buckets describe "${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "${BUCKET}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi
# Keep raw photos 365 days, then auto-delete (they're tiny; adjust as desired).
gcloud storage buckets update "${BUCKET}" \
  --lifecycle-file=/dev/stdin <<'JSON'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":365}}]}
JSON

# --------------------------------------------------------------------------- #
# 3. BigQuery dataset + tables (partitioned by date, clustered by user_id)
# --------------------------------------------------------------------------- #
echo "==> Creating BigQuery dataset ${DATASET}..."
bq --location="${REGION}" mk --dataset --force "${PROJECT}:${DATASET}" >/dev/null 2>&1 || true

mk_table () {  # name  schema_file  partition_field  partition_type
  local name="$1" schema="$2" pfield="$3" ptype="$4"
  if bq show "${PROJECT}:${DATASET}.${name}" >/dev/null 2>&1; then
    echo "    table ${name} exists, skipping"
  else
    bq mk --table \
      --time_partitioning_field="${pfield}" \
      --time_partitioning_type="${ptype}" \
      --clustering_fields="user_id" \
      "${PROJECT}:${DATASET}.${name}" "${schema}"
  fi
}
mk_table meals        "${HERE}/bigquery/meals.json"        capture_ts DAY
mk_table glucose      "${HERE}/bigquery/glucose.json"      ts         DAY
mk_table garmin_daily "${HERE}/bigquery/garmin_daily.json" date       DAY
mk_table hrv_readings "${HERE}/bigquery/hrv_readings.json" sleep_date DAY
mk_table garmin_activities "${HERE}/bigquery/garmin_activities.json" start_ts DAY

# --------------------------------------------------------------------------- #
# 4. Service account for the pipeline (functions run as this)
# --------------------------------------------------------------------------- #
echo "==> Service account ${SA_EMAIL}..."
gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SA_NAME}" --display-name="DigitalTwin pipeline"

for role in roles/bigquery.dataEditor roles/bigquery.jobUser roles/storage.objectAdmin \
            roles/aiplatform.user roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${role}" \
    --condition=None >/dev/null
done

# --------------------------------------------------------------------------- #
# 5. Per-user secret placeholders (fill values later, never commit them)
# --------------------------------------------------------------------------- #
echo "==> Per-user secrets..."
for u in "${USERS[@]}"; do
  for s in "upload-token-${u}" "garmin-creds-${u}" "cgm-creds-${u}"; do
    gcloud secrets describe "${s}" >/dev/null 2>&1 || \
      gcloud secrets create "${s}" --replication-policy=automatic
  done
done

cat <<EOF

==> Foundation ready.
    Bucket:   ${BUCKET}   (private, per-user prefixes <user_id>/inbox/)
    Dataset:  ${PROJECT}:${DATASET}  (meals, glucose, garmin_daily)
    SA:       ${SA_EMAIL}
    Users:    ${USERS[*]}

Next:
  * Add secret values, e.g.:
      printf 'YOUR_TOKEN' | gcloud secrets versions add upload-token-person1 --data-file=-
  * Deploy the Cloud Functions (process_meal, upload_meal) — built in the next phase.
EOF
