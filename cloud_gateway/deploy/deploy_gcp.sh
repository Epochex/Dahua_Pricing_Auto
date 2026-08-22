#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-gen-lang-client-0394580582}"
REGION="${REGION:-europe-west1}"
WORKFLOW_NAME="${WORKFLOW_NAME:-pricing-request-sandbox}"
RUN_SERVICE="${RUN_SERVICE:-pricing-huachat-ingress}"
INGRESS_SA_NAME="${INGRESS_SA_NAME:-pricing-ingress}"
WORKFLOW_SA_NAME="${WORKFLOW_SA_NAME:-pricing-workflow}"
INGRESS_SECRET="${INGRESS_SECRET:-pricing-huachat-ingress-token}"
SPREADSHEET_ID="${SPREADSHEET_ID:-1g96wjgMSGMhXrzLGwCAIQic_mYkS8xsThxJryfFHJ2A}"
SHEET_NAME="${SHEET_NAME:-2026.07}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKFLOW_SOURCE="${GATEWAY_DIR}/workflows/pricing_request.yaml"
INGRESS_SA="${INGRESS_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
WORKFLOW_SA="${WORKFLOW_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
WORKFLOW_EXECUTION_URL="https://workflowexecutions.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/workflows/${WORKFLOW_NAME}/executions"

gcloud config set project "${PROJECT_ID}"

gcloud services enable \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  sheets.googleapis.com \
  workflows.googleapis.com \
  workflowexecutions.googleapis.com \
  --project="${PROJECT_ID}"

gcloud beta services identity create \
  --service="workflows.googleapis.com" \
  --project="${PROJECT_ID}"

if ! gcloud iam service-accounts describe "${INGRESS_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${INGRESS_SA_NAME}" \
    --display-name="Pricing HuaChat ingress" \
    --project="${PROJECT_ID}"
fi

if ! gcloud iam service-accounts describe "${WORKFLOW_SA}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${WORKFLOW_SA_NAME}" \
    --display-name="Pricing task workflow" \
    --project="${PROJECT_ID}"
fi

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${INGRESS_SA}" \
  --role="roles/workflows.invoker" \
  --condition=None \
  --quiet

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${WORKFLOW_SA}" \
  --role="roles/logging.logWriter" \
  --condition=None \
  --quiet

if ! gcloud secrets describe "${INGRESS_SECRET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud secrets create "${INGRESS_SECRET}" \
    --replication-policy="automatic" \
    --project="${PROJECT_ID}"
fi

SECRET_VERSION="$(gcloud secrets versions list "${INGRESS_SECRET}" \
  --filter='state=ENABLED' \
  --limit=1 \
  --format='value(name)' \
  --project="${PROJECT_ID}")"
if [[ -z "${SECRET_VERSION}" ]]; then
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' | gcloud secrets versions add "${INGRESS_SECRET}" \
    --data-file=- \
    --project="${PROJECT_ID}"
fi

gcloud secrets add-iam-policy-binding "${INGRESS_SECRET}" \
  --member="serviceAccount:${INGRESS_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="${PROJECT_ID}" \
  --quiet

gcloud workflows deploy "${WORKFLOW_NAME}" \
  --source="${WORKFLOW_SOURCE}" \
  --location="${REGION}" \
  --service-account="${WORKFLOW_SA}" \
  --execution-history-level="execution-history-basic" \
  --project="${PROJECT_ID}"

gcloud run deploy "${RUN_SERVICE}" \
  --source="${GATEWAY_DIR}" \
  --region="${REGION}" \
  --service-account="${INGRESS_SA}" \
  --allow-unauthenticated \
  --ingress="all" \
  --min-instances=0 \
  --max=1 \
  --max-instances=1 \
  --concurrency=8 \
  --cpu=1 \
  --memory=512Mi \
  --timeout=30 \
  --set-env-vars="GOOGLE_SPREADSHEET_ID=${SPREADSHEET_ID},GOOGLE_SHEET_NAME=${SHEET_NAME},WORKFLOW_EXECUTION_URL=${WORKFLOW_EXECUTION_URL}" \
  --set-secrets="HUACHAT_INGRESS_TOKEN=${INGRESS_SECRET}:latest" \
  --project="${PROJECT_ID}"

RUN_URL="$(gcloud run services describe "${RUN_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
printf 'Workflow service account to share as Sheet editor: %s\n' "${WORKFLOW_SA}"
printf 'Cloud Run URL: %s\n' "${RUN_URL}"
printf 'Workflow console: https://console.cloud.google.com/workflows/workflow/%s/%s?project=%s\n' "${REGION}" "${WORKFLOW_NAME}" "${PROJECT_ID}"
