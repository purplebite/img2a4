#!/usr/bin/env bash
# Send one or more real photos to img2a4 and save the PDF as img2a4-test.pdf.
#   ./scripts/smoke-test.sh https://<url> <token> photo1.jpg [photo2.png ...]
set -euo pipefail
URL="${1:?url}"; TOKEN="${2:?token}"; shift 2
[[ $# -gt 0 ]] || { echo "give at least one image" >&2; exit 1; }
args=(); for f in "$@"; do args+=(-F "files=@${f}"); done
curl -sS -o img2a4-test.pdf -w 'HTTP %{http_code} in %{time_total}s\n' \
  -H "Authorization: Bearer ${TOKEN}" "${args[@]}" "${URL%/}/?output=pdf"
if [[ "$(head -c 4 img2a4-test.pdf)" != "%PDF" ]]; then cat img2a4-test.pdf; echo; exit 1; fi
echo "Saved img2a4-test.pdf"
if command -v pdfinfo >/dev/null; then pdfinfo -f 1 -l 99 img2a4-test.pdf | grep -E 'Pages|size'; fi
