#!/usr/bin/env python3
"""End-to-end check against a running img2a4.

Generates two synthetic "timesheet photos" (portrait + landscape, no personal
data), converts them through the real service with the real token and checks:
HTTP 200, PDF with 2 pages, page 1 A4 portrait, page 2 A4 landscape, response
time below a limit. Reports the result to healthchecks.io when HEALTHCHECKS_E2E_URL is set
(success -> ping, failure -> /fail: this check IS the health status of the service).

Usage:
  IMG2A4_URL=https://... IMG2A4_TOKEN=... [HEALTHCHECKS_E2E_URL=https://hc-ping.com/<uuid>] \
    python scripts/e2e_check.py
Exit code 0 = pass, 1 = fail.
"""

import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

from PIL import Image, ImageDraw
from pypdf import PdfReader

URL = os.environ["IMG2A4_URL"].rstrip("/") + "/"
TOKEN = os.environ["IMG2A4_TOKEN"]
HC = os.environ.get("HEALTHCHECKS_E2E_URL", "").rstrip("/")
MAX_SECONDS = float(os.environ.get("E2E_MAX_SECONDS", "30"))  # includes a possible cold start
A4 = (595.28, 841.89)


def fake_photo(w: int, h: int) -> bytes:
    im = Image.effect_noise((w, h), 25).convert("RGB")
    d = ImageDraw.Draw(im)
    for y in range(h // 10, h - h // 10, h // 25):
        d.line([(w // 10, y), (w - w // 10, y)], fill=(20, 20, 20), width=3)
        d.text((w // 8, y + 5), "E2E TEST 8:00 16:30 8", fill=(20, 20, 160))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def multipart(files: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, data in files:
        out.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
                  f"filename=\"{name}\"\r\nContent-Type: image/jpeg\r\n\r\n".encode())
        out.write(data + b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def close(a, b):
    return abs(a[0] - b[0]) < 1 and abs(a[1] - b[1]) < 1


def run() -> dict:
    body, ctype = multipart([("portrait.jpg", fake_photo(1500, 2000)),
                             ("landscape.jpg", fake_photo(2000, 1500))])
    req = urllib.request.Request(URL + "?filename=e2e-check", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": ctype})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=MAX_SECONDS + 30) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    seconds = round(time.perf_counter() - t0, 2)

    result = {"http_status": status, "seconds": seconds}
    try:
        answer = json.loads(raw)
    except ValueError:
        return {**result, "ok": False, "error": f"response is not JSON: {raw[:200]!r}"}
    result["ref"] = answer.get("ref")
    if answer.get("status") != "success":
        err = answer.get("error", {})
        return {**result, "ok": False, "error": f"service returned error {err.get('code')}: {err.get('message')}"}
    try:
        pdf = base64.b64decode(answer["pdf_base64"])
        pages = [(float(p.mediabox.width), float(p.mediabox.height)) for p in PdfReader(io.BytesIO(pdf)).pages]
    except Exception as e:  # noqa: BLE001
        return {**result, "ok": False, "error": f"pdf_base64 is not a valid PDF: {e}"}
    result.update(bytes=len(pdf), pages=[[round(w), round(h)] for w, h in pages])

    problems = []
    if status != 200:
        problems.append(f"HTTP {status}")
    if len(pages) != 2:
        problems.append(f"expected 2 pages, got {len(pages)}")
    else:
        if not close(pages[0], A4):
            problems.append("page 1 is not A4 portrait")
        if not close(pages[1], (A4[1], A4[0])):
            problems.append("page 2 is not A4 landscape")
    if result.get("bytes", 0) < 20_000:
        problems.append(f"PDF suspiciously small ({result.get('bytes')} bytes): blank pages?")
    if seconds > MAX_SECONDS:
        problems.append(f"too slow: {seconds}s > {MAX_SECONDS}s")
    return {**result, "ok": not problems, "error": "; ".join(problems) or None}


def ping(result: dict) -> None:
    if not HC:
        return
    url = HC if result["ok"] else HC + "/fail"
    if result["ok"]:
        line = (f"✅ end-to-end OK · {len(result['pages'])} pages A4 (portrait + landscape) · "
                f"{result['seconds']} s · ref {result.get('ref')}")
    else:
        line = f"❌ end-to-end FAILED · {result.get('error')} · ref {result.get('ref')}"
    text = line + "\n\n" + json.dumps(result, indent=2)
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=text.encode(), method="POST",
                                                      headers={"Content-Type": "text/plain; charset=utf-8"}),
                               timeout=10).close()
    except Exception as e:  # noqa: BLE001
        print(f"warning: healthchecks ping failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    try:
        res = run()
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    print(json.dumps(res, indent=2))
    ping(res)
    sys.exit(0 if res["ok"] else 1)
