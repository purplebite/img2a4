# IMG-2-A4

**Photos shown as an A4 PDF page. Always.**

A tiny set-up-and-forget service for Google Cloud Run.

[![tests](https://github.com/purplebite/img2a4/actions/workflows/ci.yml/badge.svg)](https://github.com/purplebite/img2a4/actions/workflows/ci.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-blue)
![Cloud Run](https://img.shields.io/badge/runs%20on-Google%20Cloud%20Run-4285F4)
![License MIT](https://img.shields.io/badge/license-MIT-green)

> **Running in production: 1,000+ conversions per week**, at
> practically €0 per month.

---

## First: do you need this at all?

For most file conversions, **use [CloudConvert](https://cloudconvert.com)**. It handles
hundreds of formats, has a clean API and integrations for Make, Zapier and n8n, and fair
pay-as-you-go pricing. Easy does it. We use it ourselves.

**Use IMG-2-A4 when you have one specific job:** turning photos into PDF pages of a **fixed size
like A4**, for example to merge them with other A4 documents. Set it up once in your own Google
Cloud project and forget about it.

---

## The problem it solves

Someone sends a photo of a timesheet, receipt or form. You convert it to PDF and merge it with
your other A4 documents. And then this happens:

```
 What you expect                What you get

 ┌────────┐ ┌────────┐          ┌────────┐ ┌──────────────────────────┐
 │  A4    │ │ photo  │          │  A4    │ │                          │
 │ letter │ │  on A4 │          │ letter │ │      photo page,         │
 │        │ │        │          │        │ │      90 cm wide          │
 └────────┘ └────────┘          └────────┘ │                          │
                                           └──────────────────────────┘
```

Most converters take the page size from the photo's DPI metadata, which is usually 72 or
missing. Technically correct, but a phone photo then becomes a page of 44 to 90 cm or more.
Acrobat, browsers and Windows show it like that (macOS Preview hides it).

IMG-2-A4 **decides** the page is A4 and fits the photo on it, sharp enough to read small print.

Measured on real received documents, one page per PDF:

| | DPI-based conversion | **IMG-2-A4** |
|---|---|---|
| Pages exactly A4 | 0% | **100%** |
| Longest side (median / max) | 98.6 cm / 2.8 m | **29.7 cm** |
| Average file size | 1.67 MB | **195 KB** |
| Resolution on the page | 72 DPI | **150 DPI** |
| Time per photo | | **0.1 to 0.4 s** |

---

## Why "set up and forget"

- **Scales to zero.** No requests, no cost. 1,000+ conversions a week stays inside Google
  Cloud's free tier.
- **Monitored in two ways.** A health endpoint for uptime monitors, and end-to-end results
  on healthchecks.io.
- **Tested before it goes live.** Every deploy runs a real conversion on a test URL first and
  only switches over when it passes.
- **Patches itself.** Google applies OS and Python security updates automatically.
- **Locked packages.** Exact versions with hashes; nothing changes unless you deploy.
- **Your data stays yours.** Images are processed in memory in your own project, in the region
  you choose. Nothing is stored, no file names or content in the logs, no third party involved.
- **Every answer is JSON** with a clear `status`, so your automation always knows what happened.

---

## Deploy in a few minutes

You need a Google Cloud project with billing enabled. [Cloud Shell](https://shell.cloud.google.com)
has every tool already.

```bash
git clone https://github.com/purplebite/img2a4.git
cd img2a4
pip install --user pillow pypdf        # once, for the end-to-end check
export GCP_PROJECT_ID=my-project
./scripts/deploy.sh
```

The script enables the APIs, creates two service accounts and a random API token in Secret
Manager, and deploys the service `img2a4` to `europe-west3` (Frankfurt; set `REGION` for another
one). At the end it prints your service URL and how to show the token.

Try it:

```bash
./scripts/smoke-test.sh <service URL> <token> photo.jpg
```

This saves `img2a4-test.pdf`. Run `./scripts/deploy.sh` again whenever you want a new version.

---

## End-to-end check: know it works, every day

One command does a real conversion against your live service and tells you if anything in the
chain broke: URL, token, code, packages or Google's runtime.

```bash
./scripts/e2e.sh                  # uses GCP_PROJECT_ID and REGION
```

It generates two fake photos in code (portrait and landscape, no real data), converts them with
your real API token and checks: HTTP 200, a PDF with 2 pages, page 1 exactly A4 portrait, page 2
exactly A4 landscape, answered within 30 seconds (cold start included). Exit code 0 means pass.

**Every deploy is tested before anyone uses it.** `deploy.sh` puts a new version on its own
test URL without traffic, runs this check there, and only then switches your users over. If the
check fails, nothing switches: users keep the current version.

---

## Monitoring: two signals

| Signal | What it proves | How often | Tool |
|---|---|---|---|
| **Health endpoint** | the service is up and answering | every few minutes | any uptime monitor |
| **End-to-end result** | a real conversion works, end to end | e.g. twice a day | [healthchecks.io](https://healthchecks.io) |

**Health endpoint.** A `GET` on your service URL needs no token and does no work:

```json
{"status": "success", "service": "img2a4", "healthy": true}
```

Point an uptime monitor at it (Uptime Kuma, UptimeRobot, Better Stack, ...) and alert when
`healthy` is not `true`. Use a timeout of 60 seconds (a cold start can take a few seconds).

**End-to-end results.** Create a check on healthchecks.io with the schedule you run the
end-to-end check on, and set its ping URL:

```bash
export HEALTHCHECKS_E2E_URL=https://hc-ping.com/<your-uuid>
./scripts/e2e.sh
```

A pass pings the check, a failure reports `/fail`. healthchecks.io alerts you on a failure **and**
when the check stops reporting, so a broken schedule gets noticed too. Run it from any scheduler
you already have.

Want both in one place? healthchecks.io offers a JSON badge URL per check. Add it to the same
uptime monitor and alert when `status` is not `up`.

---

## What goes in, what comes out

**In:** JPEG, PNG (incl. transparency), HEIC/HEIF (iPhone), WebP, TIFF (incl. multi-page);
BMP and GIF also work. The type is detected from the content, not the extension. Up to 20 files
and 30 MB per request.

**Out:** one PDF, one exact A4 page per image, portrait or landscape following the photo, phone
rotation applied, proportions kept.

### API

`POST` the image(s) as multipart (field `file` or `files`) with
`Authorization: Bearer <token>`.

| Parameter | Effect |
|---|---|
| `?profile=standard` | default: 150 DPI, about 200 KB per page |
| `?profile=small` | 150 DPI, stronger compression, about 150 KB per page |
| `?profile=high` | 300 DPI, about 430 KB per page, for receipts and tiny print |
| `?filename=Name` | name of the PDF (default: the uploaded name with `.pdf`) |
| `?output=pdf` | the raw PDF instead of JSON |

```json
{"status": "success", "ref": "1630bf10", "files": 1, "pages": 1, "profile": "standard",
 "bytes_in": 979000, "bytes_out": 280000, "duration_ms": 195, "filename": "photo.pdf", "pdf_base64": "JVBERi0..."}

{"status": "error", "ref": "2e5af557", "duration_ms": 12,
 "error": {"code": "unsupported_file", "message": "The file is not a supported image. It looks like a PDF. ...", "file": 1, "file_name": "x.pdf"}}
```

The `ref` finds that exact conversion in your server log. Without a usable file name, the PDF
becomes `<date>-<ref>.pdf`, so uploads never overwrite each other. `GET` is a health check.

| Error code | HTTP | Meaning |
|---|---|---|
| `unauthorized` | 401 | Wrong or missing token |
| `no_file` | 400 | No image received |
| `unknown_profile` / `unknown_output` | 400 | Invalid parameter |
| `method_not_allowed` | 405 | Not POST or GET |
| `too_many_files` / `payload_too_large` | 413 | More than 20 files / 30 MB |
| `image_too_large` / `output_too_large` | 413 | Above 500 MP (JPEG) or 60 MP (other) / PDF above 30 MB |
| `unsupported_file` | 415 | Not a supported image, e.g. a PDF |
| `corrupt_image` / `empty_file` | 422 | File broken or empty |
| `conversion_failed` | 500 | Unexpected server problem |

### From Make, Zapier or n8n

Use an HTTP module: multipart, field `file`, the `Authorization` header, and turn off "treat HTTP
errors as failures" so error answers reach your flow. Then route on `status`: on `success`
decode `pdf_base64`; on `error` use `error.code`, `error.message` and `ref`.

### Optional: conversion log

Set `HEALTHCHECKS_CONVERSION_LOG_URL` (a [healthchecks.io](https://healthchecks.io) ping URL)
before deploying, and every conversion sends a one-line summary: success as a ping, bad input
(4xx) to `/log`, server errors (5xx) to `/fail`. No file names or content.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
```

Run locally:

```bash
API_TOKEN=test-token functions-framework --target=to_pdf --port=8080
./scripts/smoke-test.sh http://localhost:8080 test-token photo.jpg
```

Change a package: edit `requirements.in` (or `requirements-dev.in`), then

```bash
pip install pip-tools
pip-compile --generate-hashes --strip-extras --output-file=requirements.txt requirements.in
pip-compile --generate-hashes --strip-extras --output-file=requirements-dev.txt requirements-dev.in
```

Tests use generated images only. Please never commit real photos or documents.

### Keep packages up to date (in your own copy)

This repo gets its updates from the source repo, so Dependabot is off here. Running your own
copy? Turn on monthly update PRs by adding `.github/dependabot.yml`:

```yaml
version: 2
updates:
  - package-ecosystem: pip
    directory: /
    schedule:
      interval: monthly
    groups:
      python-packages:
        patterns: ["*"]
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: monthly
```

The tests run on every update PR; merge when green, then run `./scripts/deploy.sh`. A new
version only goes live when the end-to-end check passes.

## License

MIT. Use it, fork it, ship it.
