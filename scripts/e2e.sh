#!/usr/bin/env bash
# End-to-end check: a real conversion with the real API token.
#   ./scripts/e2e.sh                 against the live service URL
#   ./scripts/e2e.sh <test URL>      against a new revision before switching
# Reports to healthchecks.io only if HEALTHCHECKS_E2E_URL is set.
set -euo pipefail
PROJECT="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west3}"
IMG2A4_URL="${1:-$(gcloud run services describe img2a4 --project="$PROJECT" --region="$REGION" --format='value(status.url)')}"
if ! IMG2A4_TOKEN="$(gcloud secrets versions access latest --project="$PROJECT" --secret=img2a4-api-token)"; then
  echo "Cannot read secret img2a4-api-token (secretAccessor missing?)" >&2
  exit 1
fi
[[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::add-mask::${IMG2A4_TOKEN}"
export IMG2A4_URL IMG2A4_TOKEN
python3 "$(dirname "$0")/e2e_check.py"
