#!/usr/bin/env bash
# Set up (first run) and deploy IMG-2-A4 to Google Cloud Run in your own project.
# Safe to re-run: it creates what is missing and deploys the current code.
#
#   export GCP_PROJECT_ID=my-project        # an existing project with billing enabled
#   export REGION=europe-west3              # optional, default europe-west3 (Frankfurt)
#   ./scripts/deploy.sh
#
# Every deploy goes to a test URL first (tag "candidate", no traffic). Only when the
# end-to-end check passes there, traffic switches to the new version. A failing check
# switches nothing: your users keep the current version.
set -euo pipefail
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID}"
export GCP_PROJECT_ID REGION="${REGION:-europe-west3}"
P=(--project="$GCP_PROJECT_ID")
R=(--project="$GCP_PROJECT_ID" --region="$REGION")
sa() { echo "$1@${GCP_PROJECT_ID}.iam.gserviceaccount.com"; }
exists() { "$@" >/dev/null 2>&1; }
quiet() { "$@" >/dev/null; }
cd "$(dirname "$0")/.."

if ! python3 -c "import PIL, pypdf" 2>/dev/null; then
  echo "The end-to-end check needs Pillow and pypdf. Install once, then run again:" >&2
  echo "  pip install --user pillow pypdf" >&2
  exit 1
fi

echo "==> Enable APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  storage.googleapis.com secretmanager.googleapis.com logging.googleapis.com "${P[@]}"

echo "==> Service accounts"
for name in img2a4-runtime img2a4-builder; do
  exists gcloud iam service-accounts describe "$(sa "$name")" "${P[@]}" ||
    gcloud iam service-accounts create "$name" --display-name="$name" "${P[@]}"
done
bind() { quiet gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" --member="serviceAccount:$1" --role="$2" --condition=None; }
bind "$(sa img2a4-builder)" roles/run.builder
bind "$(sa img2a4-builder)" roles/logging.logWriter
bind "$(sa img2a4-runtime)" roles/logging.logWriter

echo "==> API token"
if ! exists gcloud secrets describe img2a4-api-token "${P[@]}"; then
  printf '%s' "$(openssl rand -hex 32)" |
    quiet gcloud secrets create img2a4-api-token --replication-policy=automatic --data-file=- "${P[@]}"
  echo "  created (not shown)"
fi
quiet gcloud secrets add-iam-policy-binding img2a4-api-token "${P[@]}" \
  --member="serviceAccount:$(sa img2a4-runtime)" --role=roles/secretmanager.secretAccessor

# First deploy: nothing to protect, the service serves straight away.
first=0
traffic=(--no-traffic)
if ! out="$(gcloud run services describe img2a4 "${R[@]}" --format='value(status.url)' 2>&1)"; then
  if [[ "$out" == *"Cannot find service"* || "$out" == *"could not be found"* ]]; then
    first=1; traffic=()
  else
    echo "$out" >&2; exit 1
  fi
fi

echo "==> Deploy to the test URL"
gcloud run deploy img2a4 "${R[@]}" \
  --source=. \
  --function=to_pdf \
  --base-image=python313 \
  --automatic-updates \
  --service-account="$(sa img2a4-runtime)" \
  --build-service-account="projects/${GCP_PROJECT_ID}/serviceAccounts/$(sa img2a4-builder)" \
  --no-invoker-iam-check \
  --set-secrets="API_TOKEN=img2a4-api-token:latest" \
  --set-env-vars="HEALTHCHECKS_CONVERSION_LOG_URL=${HEALTHCHECKS_CONVERSION_LOG_URL:-}" \
  --memory=2Gi --cpu=1 --concurrency=2 --timeout=60s \
  --min-instances=0 --max-instances=10 \
  --tag=candidate "${traffic[@]}" \
  --quiet

URL="$(gcloud run services describe img2a4 "${R[@]}" --format='value(status.url)')"
CANDIDATE="https://candidate---${URL#https://}"

echo "==> End-to-end check on the test URL"
if ! HEALTHCHECKS_E2E_URL='' ./scripts/e2e.sh "$CANDIDATE"; then
  echo "Check FAILED: nothing switched, users keep the current version." >&2
  echo "Test URL of the failed version: $CANDIDATE" >&2
  exit 1
fi

if [[ $first == 0 ]]; then
  echo "==> Switch traffic to the new version"
  quiet gcloud run services update-traffic img2a4 "${R[@]}" --to-tags=candidate=100 --quiet
fi

cat <<EOF

Done. Service URL: $URL

Show the API token on a clean screen (then run: clear):
  gcloud secrets versions access latest --secret=img2a4-api-token --project=$GCP_PROJECT_ID; echo

Test with your own photo:
  ./scripts/smoke-test.sh $URL <token> photo.jpg

Check the live service any time:
  GCP_PROJECT_ID=$GCP_PROJECT_ID REGION=$REGION ./scripts/e2e.sh
EOF
