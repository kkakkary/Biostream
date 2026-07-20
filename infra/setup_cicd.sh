#!/usr/bin/env bash
# Provision CI/CD: a least-privilege GitHub Actions deploy service account and
# Workload Identity Federation so the Deploy workflow can push to Cloud
# Functions / Cloud Run with NO long-lived service-account keys.
#
# Idempotent — safe to re-run. Requires an operator with IAM admin on the
# project (Owner or equivalent).

set -euo pipefail

PROJECT="digitaltwin-499202"
PROJECT_NUMBER="663692868459"
REPO="kkakkary/Biostream"            # GitHub owner/repo allowed to deploy
SA_NAME="github-deploy"
SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
RUNTIME_SAS=(
  "twin-pipeline@${PROJECT}.iam.gserviceaccount.com"
  "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
)
POOL="github-pool"
PROVIDER="github-provider"

echo "==> Enabling required APIs"
gcloud services enable \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com \
  cloudfunctions.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project="$PROJECT"

echo "==> Deploy service account: $SA_EMAIL"
gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA_NAME" \
    --display-name="GitHub Actions deploy" --project="$PROJECT"

echo "==> Granting project roles to the deploy SA"
for role in \
  roles/cloudfunctions.developer \
  roles/run.admin \
  roles/cloudbuild.builds.builder \
  roles/artifactregistry.writer \
  roles/storage.admin ; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" --role="$role" --condition=None --quiet >/dev/null
done

echo "==> Letting the deploy SA act as each runtime service account"
for rsa in "${RUNTIME_SAS[@]}"; do
  gcloud iam service-accounts add-iam-policy-binding "$rsa" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/iam.serviceAccountUser" --project="$PROJECT" --quiet >/dev/null || \
    echo "   (skip $rsa — not found)"
done

echo "==> Workload Identity pool: $POOL"
gcloud iam workload-identity-pools describe "$POOL" \
  --location=global --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$POOL" \
    --location=global --display-name="GitHub Actions" --project="$PROJECT"

echo "==> OIDC provider: $PROVIDER (restricted to repo ${REPO})"
gcloud iam workload-identity-pools providers describe "$PROVIDER" \
  --location=global --workload-identity-pool="$POOL" --project="$PROJECT" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
    --location=global \
    --workload-identity-pool="$POOL" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition="assertion.repository=='${REPO}'" \
    --project="$PROJECT"

echo "==> Allowing the repo to impersonate the deploy SA"
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${REPO}" \
  --project="$PROJECT" --quiet >/dev/null

echo "==> Done. Provider for the workflow:"
echo "    projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
echo "    service_account: ${SA_EMAIL}"
