"""img2a4: convert timesheet photos to A4 PDF pages.

Every image is placed on an exact A4 page (portrait or landscape, following the
image), scaled to fit inside the page with its proportions kept. Phone EXIF
rotation is applied, photos are downscaled per size profile (default A4 @ 150 DPI),
and HEIC, PNG (incl. transparency), TIFF (incl. multi-page), WebP and JPEG are accepted.

Every request gets a JSON answer with "status": "success" or "status": "error";
errors carry a stable code and a descriptive message (see README, "Response").

Runs as a Google Cloud Run function (functions-framework, HTTP trigger).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hmac
import io
import json
import math
import os
import threading
import time
import traceback
import unicodedata
import urllib.parse
import urllib.request
import uuid

import functions_framework
import img2pdf
import pillow_heif
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

# --- configuration ----------------------------------------------------------------
# Set by the deploy (scripts/deploy.sh):
API_TOKEN = os.environ.get("API_TOKEN", "")
# Optional: healthchecks.io check that receives a one-line summary of every conversion
# (an activity log, not a health status). Max 5 pings per minute: see README.
HEALTHCHECKS_CONVERSION_LOG_URL = os.environ.get("HEALTHCHECKS_CONVERSION_LOG_URL", "").rstrip("/")

# Fixed limits (change here, then deploy):
MAX_FILES = 20
MAX_TOTAL_BYTES = 30 * 1024 * 1024
# Largest image we accept, per type. JPEG is decoded at reduced scale (up to 1/8), so even
# 400+ MP costs ~200 MB; other formats are decoded in full (80 MP PNG ~ 770 MB), so they stay low.
MAX_MEGAPIXELS = 60                         # PNG, HEIC, TIFF, WebP, ...
MAX_MEGAPIXELS_JPEG = 500                   # JPEG
MAX_RESPONSE_BYTES = 30 * 1024 * 1024       # Cloud Run caps a response at 32 MB
DEFAULT_PROFILE = "standard"
HEALTHCHECKS_TIMEOUT = 2                    # max wait for the ping; no answer = "healthchecks: no"

# Size/sharpness profiles: (max DPI on A4, JPEG quality).
# Measured on real timesheet photos (avg PDF size per page):
#   small     150 DPI q60  ~150 KB   handwriting + small print still readable
#   standard  150 DPI q70  ~175 KB   default
#   high      300 DPI q85  ~430 KB   for receipts / very small print
PROFILES = {
    "small": (150, 60),
    "standard": (150, 70),
    "high": (300, 85),
}

OUTPUTS = ("json", "pdf")

# We enforce our own pixel limits before decoding (see above); Pillow's own bomb check
# (which trips at 2x this value) is only a backstop.
Image.MAX_IMAGE_PIXELS = MAX_MEGAPIXELS_JPEG * 1_000_000

A4_PORTRAIT = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
A4_LANDSCAPE = (A4_PORTRAIT[1], A4_PORTRAIT[0])

SUPPORTED = "JPEG, PNG, HEIC, TIFF or WebP"
_SRGB = ImageCms.createProfile("sRGB")


class ConversionError(ValueError):
    """A request or file that cannot be converted. Carries a stable code + HTTP status.

    `detail` is technical context for the server log only (never sent to the client)."""

    def __init__(self, code: str, message: str, http_status: int = 422, file: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status
        self.file, self.detail = file, detail


def _sniff(data: bytes) -> str | None:
    """Best-effort name of a non-image file type, for a helpful error message."""
    if data[:4] == b"%PDF":
        return "a PDF"
    if data[:2] == b"PK":
        return "a ZIP or Office document (e.g. .docx/.xlsx)"
    if data[:4] == b"\xd0\xcf\x11\xe0":
        return "an old Office document (.doc/.xls)"
    head = data[:64].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<?xml", b"{", b"[")):
        return "a text/HTML/JSON file"
    return None


# --- core conversion ---------------------------------------------------------
def _to_display_mode(im: Image.Image) -> Image.Image:
    """Bring any image mode to 8-bit RGB or L, on white, in sRGB. Done BEFORE resizing
    so the resize can use a high-quality filter (bilevel/palette would otherwise use
    nearest-neighbour and lose thin lines)."""
    mode = im.mode
    if mode == "1":
        return im.convert("L")
    if mode.startswith("I") or mode == "F":
        # 16/32-bit grey: stretch the actual value range to 0..255 (otherwise it
        # clips to white and the page looks blank).
        im = im.convert("F")
        lo, hi = im.getextrema()
        scale = 255.0 / (hi - lo) if hi > lo else 1.0
        return im.point(lambda v: v * scale - lo * scale).convert("L")
    if mode in ("P", "PA", "LA", "RGBA", "La", "RGBa") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, "white")
        bg.paste(rgba, mask=rgba.getchannel("A"))
        im = bg
    elif mode not in ("RGB", "L"):
        im = im.convert("RGB")  # CMYK, YCbCr, LAB, HSV, ...
    icc = im.info.get("icc_profile")
    if icc and im.mode == "RGB":
        try:  # e.g. Display P3 iPhone photos -> sRGB, so colours stay right without the profile
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, _SRGB, outputMode="RGB")
        except Exception:  # noqa: BLE001 - a broken profile must not fail the conversion
            pass
    return im


def _decode_pages(data: bytes, long_px: int, short_px: int, which: str, index: int) -> list[Image.Image]:
    """Open one input file and return its page(s) as upright, resized RGB/L images."""
    if not data:
        raise ConversionError("empty_file", f"{which} is empty (0 bytes).", 422, index)
    try:
        im = Image.open(io.BytesIO(data))  # reads the header only, no pixels yet
    except UnidentifiedImageError as e:
        kind = _sniff(data)
        hint = f" It looks like {kind}." if kind else ""
        raise ConversionError("unsupported_file", f"{which} is not a supported image.{hint} Send {SUPPORTED}.",
                              415, index) from e
    except Image.DecompressionBombError as e:
        raise ConversionError("image_too_large",
                              f"{which} is too large to process (limit {MAX_MEGAPIXELS_JPEG} megapixels "
                              f"for JPEG, {MAX_MEGAPIXELS} for other formats). Send a smaller image.",
                              413, index) from e
    except Exception as e:  # noqa: BLE001
        raise ConversionError("corrupt_image", f"{which} is a damaged or incomplete image. "
                              "Ask the sender to send it again.", 422, index, detail=repr(e)) from e

    w, h = im.size
    limit = MAX_MEGAPIXELS_JPEG if im.format == "JPEG" else MAX_MEGAPIXELS
    if w * h > limit * 1_000_000:
        kind = "JPEG" if im.format == "JPEG" else f"{im.format or 'this format'}"
        raise ConversionError("image_too_large",
                              f"{which} is {w}×{h} pixels ({w * h / 1e6:.0f} megapixels); the limit for "
                              f"{kind} is {limit} megapixels. Send a smaller image"
                              f"{'' if im.format == 'JPEG' else ', or save it as JPEG'}.", 413, index)

    multipage = im.format == "TIFF" and getattr(im, "n_frames", 1) > 1  # scanned multi-page TIFF
    frames = range(im.n_frames) if multipage else [0]  # animated GIF/WebP: first frame only
    pages = []
    try:
        for f in frames:
            if multipage:
                im.seek(f)
            fw, fh = im.size
            # Final fit factor is orientation-independent: the page follows the image.
            fit = min(1.0, long_px / max(fw, fh), short_px / min(fw, fh))
            if im.format == "JPEG":
                # Decode directly at (at least) the target size: a 50 MP photo then
                # needs a fraction of the memory and time.
                im.draft(None, (math.ceil(fw * fit), math.ceil(fh * fit)))
            frame = ImageOps.exif_transpose(im)  # loads pixels; copies when rotating
            frame = _to_display_mode(frame)
            if frame.width > frame.height:
                frame.thumbnail((long_px, short_px), Image.Resampling.LANCZOS)
            else:
                frame.thumbnail((short_px, long_px), Image.Resampling.LANCZOS)
            pages.append(frame)
    except ConversionError:
        raise
    except Image.DecompressionBombError as e:
        raise ConversionError("image_too_large", f"{which} is too large to process "
                              f"(limit {MAX_MEGAPIXELS_JPEG} megapixels for JPEG, {MAX_MEGAPIXELS} for "
                              "other formats).", 413, index) from e
    except MemoryError as e:
        raise ConversionError("image_too_large", f"{which} needs too much memory to process. "
                              "Send a smaller photo.", 413, index) from e
    except Exception as e:  # noqa: BLE001 - truncated JPEG, broken HEIC (ValueError/EOFError), ...
        raise ConversionError("corrupt_image", f"{which} is a damaged or incomplete image. "
                              "Ask the sender to send it again.", 422, index, detail=repr(e)) from e
    return pages


def _encode(im: Image.Image, quality: int) -> bytes:
    out = io.BytesIO()
    im.save(out, "JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()


def normalize(data: bytes, profile: str = DEFAULT_PROFILE, index: int = 1, total: int = 1) -> list[bytes]:
    """Return one upright, size-capped JPEG per page of one input file."""
    which = f"File {index} of {total}" if total > 1 else "The file"
    dpi, quality = PROFILES[profile]
    long_px, short_px = round(297 / 25.4 * dpi), round(210 / 25.4 * dpi)
    return [_encode(p, quality) for p in _decode_pages(data, long_px, short_px, which, index)]


def _layout(imgwidthpx, imgheightpx, ndpi):
    """A4, orientation following the image, image fitted inside."""
    page = A4_LANDSCAPE if imgwidthpx > imgheightpx else A4_PORTRAIT
    return img2pdf.get_layout_fun(page, fit=img2pdf.FitMode.into)(imgwidthpx, imgheightpx, ndpi)


def convert_images(blobs: list[bytes], profile: str = DEFAULT_PROFILE) -> tuple[bytes, int]:
    """Convert one or more images into a single multi-page A4 PDF. Returns (pdf, pages)."""
    if not blobs:
        raise ConversionError("no_file", "No image received.", 400)
    if profile not in PROFILES:
        raise ConversionError("unknown_profile", f"Unknown profile. Use one of: {', '.join(PROFILES)}.", 400)
    total = len(blobs)
    jpegs = []
    for i, b in enumerate(blobs, start=1):
        jpegs.extend(normalize(b, profile, i, total))
    return img2pdf.convert(jpegs, layout_fun=_layout, producer="IMG-2-A4"), len(jpegs)


def images_to_a4_pdf(blobs: list[bytes], profile: str = DEFAULT_PROFILE) -> bytes:
    """Convenience wrapper: just the PDF bytes."""
    return convert_images(blobs, profile)[0]


# --- reporting: Cloud Logging + optional healthchecks.io conversion log -------
# Never logged or pinged: anything that can contain a person's name.
_PRIVATE_KEYS = ("pdf_base64", "file_names", "filename")


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"


def _summary(result: dict) -> str:
    """One human-readable line, shown in the server log and the healthchecks.io event list."""
    if result["status"] == "success":
        files, pages = result["files"], result["pages"]
        return (f"✅ success · {files} image{'s' if files != 1 else ''} → {pages} page"
                f"{'s' if pages != 1 else ''} · {_size(result['bytes_in'])} → "
                f"{_size(result['bytes_out'])} · {result['profile']} · {result['duration_ms']} ms"
                f" · ref {result['ref']}")
    err = result["error"]
    return f"❌ error · {err['code']} · {err['message']} · {result['duration_ms']} ms · ref {result['ref']}"


def _ping(url: str, body: bytes) -> bool:
    """True if healthchecks.io accepted the ping. When it drops a ping (more than 5 per
    minute) it answers 200 "OK (rate limited)" (or 429): that counts as not delivered."""
    try:
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "text/plain; charset=utf-8",
                                              "User-Agent": "img2a4"})
        with urllib.request.urlopen(req, timeout=HEALTHCHECKS_TIMEOUT) as r:
            return "rate limited" not in r.read(200).decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001  429 / other HTTP error, timeout, network
        return False


def _report(result: dict, trace: str | None = None, ping: bool = True) -> None:
    """Ping the optional healthchecks.io conversion log, then write ONE server log line.

    If a ping was sent, the line ends in "· healthchecks: yes" or "· healthchecks: no"
    (field "healthchecks": true/false), so the server log can be compared with healthchecks.io.
    Never contains file names, the requested PDF name or image content (personal data).
    Reporting never breaks or noticeably slows a conversion.
    """
    entry = {k: v for k, v in result.items() if k not in _PRIVATE_KEYS}
    message = _summary(result)
    delivered = None
    if HEALTHCHECKS_CONVERSION_LOG_URL and ping:
        # success -> regular ping (check shows the last successful conversion);
        # success            -> ping   (check up)
        # bad input (4xx)    -> /log   (listed, check stays up: not a service problem)
        # service error (5xx)-> /fail  (check down + alert until the next success)
        if result["status"] == "success":
            suffix = ""
        else:
            suffix = "/fail" if result.get("http_status", 500) >= 500 else "/log"
        url = HEALTHCHECKS_CONVERSION_LOG_URL + suffix
        # Same content as the server log line: summary first, then the record (compact JSON).
        record = {k: v for k, v in entry.items() if k != "detail"}
        body = (message + "\n" + json.dumps(record, separators=(",", ":"))).encode()
        box: list = []
        t = threading.Thread(target=lambda: box.append(_ping(url, body)), daemon=True)
        t.start()
        t.join(HEALTHCHECKS_TIMEOUT)  # hard upper bound on the wait; no answer in time = "no"
        delivered = bool(box and box[0])
        message += f" · healthchecks: {'yes' if delivered else 'no'}"
    severity = "INFO" if result["status"] == "success" else (
        "ERROR" if result.get("http_status", 500) >= 500 else "WARNING")
    print(json.dumps({"severity": severity, "service": "img2a4", "message": message, **entry,
                      **({"healthchecks": delivered} if delivered is not None else {}),
                      **({"trace": trace} if trace else {})}), flush=True)


# --- HTTP layer --------------------------------------------------------------
def _json_response(payload: dict, http_status: int):
    return json.dumps(payload), http_status, {"Content-Type": "application/json"}


def _authorized(request) -> bool:
    if not API_TOKEN:
        return False  # fail closed: a missing secret must never mean "open"
    header = request.headers.get("Authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {API_TOKEN}".encode())


def _collect_files(request) -> tuple[list[bytes], list[str]]:
    files = request.files.getlist("files") or request.files.getlist("file")
    if files:
        return [f.read() for f in files], [f.filename or "" for f in files]
    # Fallback: raw binary body (Content-Type image/* or application/octet-stream)
    body = request.get_data()
    return ([body], [""]) if body else ([], [])


def _today() -> str:
    """Today's date as YYYYMMDD in Dutch time (UTC if the time zone data is missing)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%Y%m%d")
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).strftime("%Y%m%d")


def _pdf_filename(request, names: list[str], ref: str) -> str:
    """PDF name, in this order:
    1. ?filename=            (explicit choice)
    2. the first uploaded file's name, extension replaced by .pdf ("foto.jpeg" -> "foto.pdf")
    3. <date>-<ref>.pdf  (e.g. 20261020-c5e8300e.pdf, date in Dutch time;
       unique per request: never overwrites an earlier upload)
    Keeps letters like ă/ń/Ł (the header uses RFC 5987)."""
    def clean(raw: str) -> str:
        return "".join(c for c in raw if c.isalnum() or c in " ._-")[:120].strip(" .")

    name = clean(request.args.get("filename", ""))
    if not name and names and names[0]:
        base = names[0].replace("\\", "/").rsplit("/", 1)[-1]   # some clients send a path
        name = clean(base.rsplit(".", 1)[0] if "." in base else base)
    if not name:
        return f"{_today()}-{ref}.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def _content_disposition(filename: str) -> str:
    """ASCII fallback + UTF-8 name, so non-Latin names can't break the HTTP header."""
    ascii_name = (unicodedata.normalize("NFKD", filename).encode("ascii", "ignore").decode()
                  .replace('"', "").strip() or "converted.pdf")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(filename)}"


@functions_framework.http
def to_pdf(request):
    started = time.perf_counter()
    ref = uuid.uuid4().hex[:8]  # quote this to find the request in the logs

    def ms():
        return round((time.perf_counter() - started) * 1000)

    def error(exc: ConversionError, ping: bool = True, **extra):
        result = {"status": "error", "ref": ref, "http_status": exc.http_status, "duration_ms": ms(),
                  "error": {"code": exc.code, "message": exc.message,
                            **({"file": exc.file} if exc.file else {})}, **extra}
        _report({**result, **({"detail": exc.detail} if exc.detail else {})}, ping=ping)
        body = {k: v for k, v in result.items() if k not in ("http_status", "file_names")}
        names = extra.get("file_names") or []
        if exc.file and exc.file <= len(names) and names[exc.file - 1]:
            body["error"]["file_name"] = names[exc.file - 1]  # client only, never logged
        return _json_response(body, exc.http_status)

    if request.method == "GET":
        return _json_response({"status": "success", "service": "img2a4", "healthy": True}, 200)
    if request.method != "POST":
        return error(ConversionError("method_not_allowed", "Use POST to convert, GET for a health check.", 405))
    if not _authorized(request):
        # Logged, but not pinged: anyone can hit the public URL and must not flood the conversion log.
        return error(ConversionError("unauthorized",
                                     "Missing or wrong API token. Send header 'Authorization: Bearer <token>'.",
                                     401), ping=False)

    profile = request.args.get("profile", DEFAULT_PROFILE)
    if profile not in PROFILES:
        return error(ConversionError("unknown_profile", f"Unknown profile. Use one of: {', '.join(PROFILES)}.", 400))
    output = request.args.get("output", "json")
    if output not in OUTPUTS:
        return error(ConversionError("unknown_output", "Unknown output. Use 'json' or 'pdf'.", 400))

    too_large = ConversionError("payload_too_large",
                                f"Request is larger than {MAX_TOTAL_BYTES // 1024 // 1024} MB. "
                                "Send fewer or smaller images per request.", 413)
    if request.content_length and request.content_length > MAX_TOTAL_BYTES:
        return error(too_large, bytes_in=request.content_length)
    # Chunked uploads have no Content-Length: check what actually arrived
    # (Cloud Run itself caps every request at 32 MB, so this read is bounded).
    blobs, names = _collect_files(request)
    if sum(len(b) for b in blobs) > MAX_TOTAL_BYTES:
        return error(too_large, bytes_in=sum(len(b) for b in blobs))

    if not blobs:
        return error(ConversionError("no_file",
                                     "No image received. Send multipart field 'files' (one or more), "
                                     "or the raw image as request body.", 400))
    if len(blobs) > MAX_FILES:
        return error(ConversionError("too_many_files",
                                     f"{len(blobs)} files received; the limit is {MAX_FILES} per request.", 413),
                     files=len(blobs))

    bytes_in = sum(len(b) for b in blobs)
    try:
        pdf, pages = convert_images(blobs, profile)
    except ConversionError as e:
        return error(e, files=len(blobs), bytes_in=bytes_in, file_names=names)
    except Exception as e:  # noqa: BLE001
        trace = traceback.format_exc(limit=5)[-1500:]
        result = {"status": "error", "ref": ref, "http_status": 500, "duration_ms": ms(),
                  "files": len(blobs), "bytes_in": bytes_in, "profile": profile,
                  "error": {"code": "conversion_failed",
                            "message": f"Unexpected error while converting ({e.__class__.__name__}). "
                                       f"Retry once; if it persists, check the logs for ref {ref}."}}
        _report({**result, "detail": repr(e)}, trace=trace)
        return _json_response({k: v for k, v in result.items() if k != "http_status"}, 500)

    response_size = len(pdf) if output == "pdf" else math.ceil(len(pdf) / 3) * 4 + 1024
    if response_size > MAX_RESPONSE_BYTES:
        return error(ConversionError("output_too_large",
                                     f"The PDF would be {_size(response_size)}, over the "
                                     f"{MAX_RESPONSE_BYTES // (1024 * 1024)} MB response limit. "
                                     "Use profile 'standard', send fewer images per request, "
                                     "or use ?output=pdf.", 413),
                     files=len(blobs), bytes_in=bytes_in, pages=pages)

    filename = _pdf_filename(request, names, ref)
    result = {"status": "success", "ref": ref, "files": len(blobs), "pages": pages,
              "profile": profile, "bytes_in": bytes_in, "bytes_out": len(pdf),
              "duration_ms": ms(), "filename": filename}
    _report(result)

    if output == "pdf":
        return pdf, 200, {
            "Content-Type": "application/pdf",
            "Content-Disposition": _content_disposition(filename),
            "X-Img2a4-Status": "success",
            "X-Img2a4-Ref": ref,
        }
    return _json_response({**result, "pdf_base64": base64.b64encode(pdf).decode()}, 200)
