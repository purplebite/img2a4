"""Tests for img2a4. All images are generated synthetically: no real timesheets
(which contain personal data) belong in this repository."""

import base64
import io
import json
import os
import time
import urllib.error

import pytest
from PIL import Image
from pypdf import PdfReader

os.environ["API_TOKEN"] = "test-token"
os.environ.pop("HEALTHCHECKS_CONVERSION_LOG_URL", None)
import main  # noqa: E402  (import after env is set)

A4_W, A4_H = 595.28, 841.89
AUTH = {"Authorization": "Bearer test-token"}


# --- helpers -----------------------------------------------------------------
def img_bytes(w, h, fmt="JPEG", mode="RGB", exif_orientation=None):
    im = Image.new(mode, (w, h), "white" if mode != "RGBA" else (255, 255, 255, 0))
    buf = io.BytesIO()
    kwargs = {}
    if exif_orientation:
        exif = im.getexif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif
    im.save(buf, fmt, **kwargs)
    return buf.getvalue()


def noisy_photo(w=1932, h=2576):
    im = Image.effect_noise((w, h), 40).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def pages(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages]


def is_a4(size, landscape=False):
    w, h = size
    tw, th = (A4_H, A4_W) if landscape else (A4_W, A4_H)
    return abs(w - tw) < 1 and abs(h - th) < 1


@pytest.fixture
def client():
    import flask
    app = flask.Flask(__name__)
    app.add_url_rule("/", "to_pdf", lambda: main.to_pdf(flask.request), methods=["GET", "POST", "PUT"])
    return app.test_client()


def post(client, *files, query="", headers=AUTH):
    data = {"files": [(io.BytesIO(b), name) for name, b in files]} if files else {}
    return client.post("/" + query, headers=headers, data=data)


def body(resp):
    return json.loads(resp.data)


def pdf_of(resp):
    return base64.b64decode(body(resp)["pdf_base64"])


# --- conversion core ---------------------------------------------------------
@pytest.mark.parametrize("w,h", [(736, 1600), (1200, 1600), (1536, 2048), (1932, 2576), (828, 1792)])
def test_portrait_photos_become_a4_portrait(w, h):
    assert is_a4(pages(main.images_to_a4_pdf([img_bytes(w, h)]))[0])


def test_landscape_photo_becomes_a4_landscape():
    assert is_a4(pages(main.images_to_a4_pdf([img_bytes(2048, 1536)]))[0], landscape=True)


def test_exif_rotation_is_applied():
    assert is_a4(pages(main.images_to_a4_pdf([img_bytes(2048, 1536, exif_orientation=6)]))[0])


@pytest.mark.parametrize("profile,max_w,max_h", [("small", 1240, 1754), ("standard", 1240, 1754), ("high", 2480, 3508)])
def test_huge_photo_is_capped_per_profile(profile, max_w, max_h):
    normalized = Image.open(io.BytesIO(main.normalize(img_bytes(6144, 8192), profile)[0]))
    assert normalized.size[0] <= max_w and normalized.size[1] <= max_h


def test_small_image_is_not_upscaled():
    assert Image.open(io.BytesIO(main.normalize(img_bytes(400, 600))[0])).size == (400, 600)


def test_transparent_png_and_tiff_and_webp():
    for data in (img_bytes(800, 1200, "PNG", "RGBA"), img_bytes(800, 1200, "TIFF"), img_bytes(800, 1200, "WEBP")):
        assert is_a4(pages(main.images_to_a4_pdf([data]))[0])


def test_heic():
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buf, "HEIF")
    assert is_a4(pages(main.images_to_a4_pdf([buf.getvalue()]))[0])


def test_multiple_images_one_pdf_mixed_orientation():
    sizes = pages(main.images_to_a4_pdf([img_bytes(1200, 1600), img_bytes(1600, 1200)]))
    assert len(sizes) == 2 and is_a4(sizes[0]) and is_a4(sizes[1], landscape=True)


def test_profiles_order_by_size():
    data = noisy_photo()
    small, standard, high = (len(main.images_to_a4_pdf([data], p)) for p in ("small", "standard", "high"))
    assert small < standard < high


# --- response contract: success ----------------------------------------------
def test_success_json_contract(client):
    r = post(client, ("a.jpg", img_bytes(1200, 1600)), ("b.png", img_bytes(1600, 1200, "PNG")),
             query="?filename=Timesheet week 42")
    assert r.status_code == 200 and r.headers["Content-Type"] == "application/json"
    b = body(r)
    assert b["status"] == "success"
    assert b["files"] == 2 and b["pages"] == 2 and b["profile"] == "standard"
    assert b["filename"] == "Timesheet week 42.pdf"
    assert len(b["ref"]) == 8 and b["bytes_out"] > 0 and b["duration_ms"] >= 0
    sizes = pages(pdf_of(r))
    assert is_a4(sizes[0]) and is_a4(sizes[1], landscape=True)


def test_success_raw_pdf_output(client):
    r = post(client, ("a.jpg", img_bytes(1200, 1600)), query="?output=pdf&filename=x")
    assert r.status_code == 200 and r.headers["Content-Type"] == "application/pdf"
    assert r.headers["X-Img2a4-Status"] == "success" and r.headers["X-Img2a4-Ref"]
    assert 'filename="x.pdf"' in r.headers["Content-Disposition"]
    assert is_a4(pages(r.data)[0])


def test_raw_body_upload(client):
    r = client.post("/", headers={**AUTH, "Content-Type": "image/jpeg"}, data=img_bytes(1200, 1600))
    assert r.status_code == 200 and body(r)["status"] == "success"


def test_profile_query_param(client):
    data = noisy_photo()
    std = body(post(client, ("a.jpg", data)))
    high = body(post(client, ("a.jpg", data), query="?profile=high"))
    assert std["profile"] == "standard" and high["profile"] == "high"
    assert std["bytes_out"] < high["bytes_out"]


def test_health(client):
    r = client.get("/")
    assert r.status_code == 200 and body(r)["status"] == "success"


# --- response contract: errors -----------------------------------------------
def assert_error(resp, http_status, code, *fragments):
    assert resp.status_code == http_status
    b = body(resp)
    assert b["status"] == "error" and b["error"]["code"] == code
    assert len(b["ref"]) == 8 and "pdf_base64" not in b
    for f in fragments:
        assert f in b["error"]["message"], b["error"]["message"]
    return b


def test_error_unauthorized(client):
    assert_error(post(client, ("a.jpg", img_bytes(800, 1200)), headers={}), 401, "unauthorized", "Bearer")
    assert_error(post(client, ("a.jpg", img_bytes(800, 1200)), headers={"Authorization": "Bearer nope"}),
                 401, "unauthorized")


def test_error_fails_closed_without_configured_token(client, monkeypatch):
    monkeypatch.setattr(main, "API_TOKEN", "")
    assert_error(post(client, ("a.jpg", img_bytes(800, 1200)), headers={"Authorization": "Bearer "}),
                 401, "unauthorized")


def test_error_pdf_sent_instead_of_image_names_file(client):
    b = assert_error(post(client, ("ok.jpg", img_bytes(800, 1200)), ("Timesheet.pdf", b"%PDF-1.7 ...")),
                     415, "unsupported_file", "File 2 of 2", "looks like a PDF", "JPEG")
    assert b["error"]["file"] == 2 and b["error"]["file_name"] == "Timesheet.pdf"


def test_error_office_document(client):
    assert_error(post(client, ("a.docx", b"PK\x03\x04rest")), 415, "unsupported_file", "Office")


def test_error_empty_file(client):
    assert_error(post(client, ("a.jpg", b"")), 422, "empty_file", "empty")


def test_error_corrupt_image(client):
    truncated = img_bytes(1200, 1600)[:600]
    assert_error(post(client, ("a.jpg", truncated)), 422, "corrupt_image", "damaged")


def test_error_no_file(client):
    assert_error(post(client), 400, "no_file", "multipart field 'files'")


def test_error_unknown_profile(client):
    b = assert_error(post(client, ("a.jpg", img_bytes(800, 1200)), query="?profile=Jan_Jansen"),
                     400, "unknown_profile", "small, standard, high")
    assert "Jan_Jansen" not in json.dumps(b)  # user input is not echoed


def test_error_unknown_output(client):
    assert_error(post(client, ("a.jpg", img_bytes(800, 1200)), query="?output=xml"), 400, "unknown_output")


def test_error_too_many_files(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_FILES", 2)
    files = [(f"{i}.jpg", img_bytes(100, 100)) for i in range(3)]
    assert_error(post(client, *files), 413, "too_many_files", "limit is 2")


def test_error_wrong_method(client):
    assert_error(client.put("/", headers=AUTH), 405, "method_not_allowed")


def test_error_unexpected_is_500_with_ref(client, monkeypatch):
    def boom(*a):
        raise RuntimeError("disk full")
    monkeypatch.setattr(main, "convert_images", boom)
    b = assert_error(post(client, ("a.jpg", img_bytes(800, 1200))), 500, "conversion_failed",
                     "RuntimeError", "Retry once")
    assert "disk full" not in json.dumps(b)  # technical detail only in the server log
    assert b["ref"] in b["error"]["message"]


def test_error_raw_pdf_output_still_returns_json_error(client):
    assert_error(post(client, ("a.jpg", b"nonsense"), query="?output=pdf"), 415, "unsupported_file")


# --- healthchecks.io conversion log ------------------------------------------
class FakeEndpoint(list):
    """Stands in for healthchecks.io. Set .answer to (status, body) or to an exception."""

    def __init__(self):
        super().__init__()
        self.answer = (200, "OK")

    def __call__(self, req, timeout=None):
        self.append((req.full_url, req.data.decode()))
        if isinstance(self.answer, Exception):
            raise self.answer
        status, text = self.answer
        if status >= 400:  # what urllib really does
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(text.encode()))
        return _Resp(text)


class _Resp:
    def __init__(self, text):
        self._text = text.encode()

    def read(self, n=-1):
        return self._text[:n] if n >= 0 else self._text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


@pytest.fixture
def pings(monkeypatch):
    calls = FakeEndpoint()
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "https://hc-ping.example/abc")
    monkeypatch.setattr(main.urllib.request, "urlopen", calls)
    return calls


def server_log(capsys) -> dict:
    """The last img2a4 conversion line written to stdout (= Cloud Logging)."""
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    return [x for x in lines if "status" in x][-1]


def test_success_is_plain_ping_with_summary(client, pings):
    post(client, ("Jan_Jansen.jpg", img_bytes(1200, 1600)), ("Piet.jpg", img_bytes(1600, 1200)))
    url, text = pings[-1]
    assert url == "https://hc-ping.example/abc"
    assert text.startswith("✅ success · 2 images → 2 pages")
    assert "Jan_Jansen" not in text and "Piet" not in text  # no names (personal data)


def test_bad_input_is_log_ping(client, pings):
    post(client, ("Jan_Jansen.pdf", b"%PDF-1.7"))
    url, text = pings[-1]
    assert url.endswith("/log") and not url.endswith("/fail")
    assert text.startswith("❌ error · unsupported_file")
    assert "Jan_Jansen" not in text


def test_unexpected_error_is_also_fail(client, pings, monkeypatch):
    monkeypatch.setattr(main, "convert_images", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    post(client, ("a.jpg", img_bytes(800, 1200)))
    assert pings[-1][0].endswith("/fail")


def test_health_check_does_not_ping(client, pings):
    client.get("/")
    assert pings == []


def test_ping_failure_never_breaks_conversion(client, monkeypatch):
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "https://hc-ping.example/abc")
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert body(post(client, ("a.jpg", img_bytes(1200, 1600))))["status"] == "success"


def test_no_url_no_ping(client, monkeypatch):
    called = []
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "")
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: called.append(1))
    post(client, ("a.jpg", img_bytes(1200, 1600)))
    assert called == []


def test_ping_body_matches_server_log(client, pings, capsys):
    post(client, ("a.jpg", img_bytes(1200, 1600)))
    server = json.loads([l for l in capsys.readouterr().out.splitlines() if '"service"' in l][-1])
    summary_line, record = pings[-1][1].split("\n", 1)
    assert server["message"] == summary_line + " · healthchecks: yes"  # same line + delivery
    assert json.loads(record)["ref"] == server["ref"]



# --- regression tests for the independent review findings --------------------
def gray16_gradient(w=600, h=800):
    im = Image.new("I;16", (w, h))
    im.putdata([(x * 65535) // w for y in range(h) for x in range(w)])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def page_image(pdf_bytes):
    """First embedded image of page 1, as a Pillow image."""
    page = PdfReader(io.BytesIO(pdf_bytes)).pages[0]
    return page.images[0].image


def test_h1_non_latin_filename_with_raw_pdf_output(client):
    r = post(client, ("a.jpg", img_bytes(1200, 1600)), query="?output=pdf&filename=Timesheet_Bucureşti_Łódź")
    assert r.status_code == 200 and r.data[:4] == b"%PDF"
    cd = r.headers["Content-Disposition"]
    cd.encode("latin-1")  # header must be encodable, else the server drops the connection
    assert 'filename="Timesheet_Bucuresti_odz.pdf"' in cd  # ASCII fallback (Ł has no ASCII form)
    assert "filename*=UTF-8''Timesheet_Bucure%C5%9Fti_%C5%81%C3%B3d%C5%BA.pdf" in cd


def test_h1_non_latin_filename_kept_in_json(client):
    assert body(post(client, ("a.jpg", img_bytes(1200, 1600)), query="?filename=Bucureşti"))["filename"] == "Bucureşti.pdf"


def test_h2_requested_filename_never_logged_or_pinged(client, pings, capsys):
    post(client, ("a.jpg", img_bytes(1200, 1600)), query="?filename=Jan_Jansen_timesheet")
    out = capsys.readouterr().out
    assert "Jan_Jansen" not in out and "Jan_Jansen" not in pings[-1][1]


def test_h3_too_many_megapixels_rejected_before_decoding(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_MEGAPIXELS", 10)
    b = assert_error(post(client, ("big.png", img_bytes(4000, 3000, "PNG"))), 413, "image_too_large",
                     "12 megapixels", "limit for PNG is 10", "save it as JPEG")
    assert b["error"]["file_name"] == "big.png"


def test_h3_big_jpeg_is_decoded_reduced():
    # 50 MP JPEG: with draft() the decoder already delivers <= ~2x the target size
    data = img_bytes(6144, 8192)
    out = Image.open(io.BytesIO(main.normalize(data, "standard")[0]))
    assert out.size[0] <= 1240 and out.size[1] <= 1754


def test_m1_output_too_large(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_RESPONSE_BYTES", 2000)
    assert_error(post(client, ("a.jpg", noisy_photo(800, 1000))), 413, "output_too_large", "output=pdf")


def test_m2_corrupt_heic_is_422_not_500(client):
    buf = io.BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buf, "HEIF")
    assert_error(post(client, ("a.heic", buf.getvalue()[:400])), 422, "corrupt_image", "damaged")


def test_m3_16bit_grey_is_not_blank():
    im = page_image(main.images_to_a4_pdf([gray16_gradient()])).convert("L")
    lo, hi = im.getextrema()
    assert lo < 40 and hi > 215  # full gradient, not a white page


def test_m4_bilevel_thin_lines_survive_downscale():
    im = Image.new("1", (3000, 4000), 1)
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    for y in range(200, 3800, 100):
        d.line([(100, y), (2900, y)], fill=0, width=2)  # 2-px lines, downscaled ~3x
    buf = io.BytesIO()
    im.save(buf, "PNG")
    out = page_image(main.images_to_a4_pdf([buf.getvalue()])).convert("L")
    column = [out.getpixel((out.width // 2, y)) for y in range(out.height)]
    assert min(column) < 160  # lines still visible (nearest-neighbour would drop most)
    assert sum(1 for v in column if v < 200) >= 20


def test_m5_multipage_tiff_converts_every_page(client):
    frames = [Image.new("RGB", (800, 1100), c) for c in ("white", "gray", "black")]
    buf = io.BytesIO()
    frames[0].save(buf, "TIFF", save_all=True, append_images=frames[1:])
    b = body(post(client, ("scan.tif", buf.getvalue())))
    assert b["status"] == "success" and b["files"] == 1 and b["pages"] == 3
    assert len(pages(base64.b64decode(b["pdf_base64"]))) == 3


def test_l3_unauthorized_is_logged_but_not_pinged(client, pings, capsys):
    post(client, ("a.jpg", img_bytes(800, 1200)), headers={})
    assert pings == [] and "unauthorized" in capsys.readouterr().out


def test_l5_png_trns_transparency_is_white_not_black():
    im = Image.new("RGB", (400, 600), (0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "PNG", transparency=(0, 0, 0))  # tRNS chunk: black = transparent
    out = page_image(main.images_to_a4_pdf([buf.getvalue()])).convert("L")
    assert out.getpixel((out.width // 2, out.height // 2)) > 240


def test_m8_slow_healthchecks_cannot_stall_a_conversion(client, monkeypatch):
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "https://hc-ping.example/abc")
    monkeypatch.setattr(main, "HEALTHCHECKS_TIMEOUT", 0.3)
    monkeypatch.setattr(main, "_ping", lambda *a: time.sleep(5))
    t0 = time.perf_counter()
    assert body(post(client, ("a.jpg", img_bytes(800, 1200))))["status"] == "success"
    assert time.perf_counter() - t0 < 2


# --- "healthchecks: yes/no" in the server log ---------------------------------
def test_delivered_is_yes(client, pings, capsys):
    post(client, ("a.jpg", img_bytes(800, 1200)))
    line = server_log(capsys)
    assert line["message"].endswith(" · healthchecks: yes") and line["healthchecks"] is True
    assert "healthchecks: yes" not in pings[-1][1]  # healthchecks.io gets the plain summary


@pytest.mark.parametrize("answer", [(200, "OK (rate limited)"), (429, "rate limited"),
                                    (404, "not found"), (500, ""), OSError("down")])
def test_not_delivered_is_no(client, pings, capsys, answer):
    pings.answer = answer
    assert body(post(client, ("a.jpg", img_bytes(800, 1200))))["status"] == "success"
    line = server_log(capsys)
    assert line["message"].endswith(" · healthchecks: no") and line["healthchecks"] is False


def test_no_answer_in_time_is_no(client, monkeypatch, capsys):
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "https://hc-ping.example/abc")
    monkeypatch.setattr(main, "HEALTHCHECKS_TIMEOUT", 0.2)
    monkeypatch.setattr(main, "_ping", lambda *a: time.sleep(2) or True)
    post(client, ("a.jpg", img_bytes(800, 1200)))
    assert server_log(capsys)["message"].endswith(" · healthchecks: no")


def test_no_url_means_no_healthchecks_field(client, monkeypatch, capsys):
    monkeypatch.setattr(main, "HEALTHCHECKS_CONVERSION_LOG_URL", "")
    post(client, ("a.jpg", img_bytes(800, 1200)))
    line = server_log(capsys)
    assert "healthchecks" not in line and "healthchecks" not in line["message"]


def test_cmyk_and_panorama_and_1x1(client):
    for data in (img_bytes(800, 1100, "JPEG", "CMYK"), img_bytes(8000, 500), img_bytes(1, 1)):
        assert body(post(client, ("x.jpg", data)))["status"] == "success"


def test_l1_size_limit_applies_without_content_length(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_TOTAL_BYTES", 50_000)
    # A body without Content-Length, like a chunked upload behind a proxy.
    r = client.post("/", headers={**AUTH, "Content-Type": "image/jpeg"}, data=noisy_photo(600, 800),
                    environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True})
    assert_error(r, 413, "payload_too_large")


# --- PDF name: ?filename= > uploaded file name > unique img2a4-<ref>.pdf -----------
def test_pdf_name_from_uploaded_file(client):
    b = body(post(client, ("31610473023 W005844321445 2026-09-21 17.37.jpeg", img_bytes(800, 1200))))
    assert b["filename"] == "31610473023 W005844321445 2026-09-21 17.37.pdf"


def test_pdf_name_query_wins_over_upload_name(client):
    assert body(post(client, ("foto.jpeg", img_bytes(800, 1200)), query="?filename=Week 42"))["filename"] == "Week 42.pdf"


def test_pdf_name_strips_path_and_odd_characters(client):
    assert body(post(client, ("scans/2026/jan@mail.com scan.PNG", img_bytes(800, 1200))))["filename"] == "janmail.com scan.pdf"


def test_pdf_name_unique_when_no_name(client):
    r = client.post("/", headers={**AUTH, "Content-Type": "image/jpeg"}, data=img_bytes(800, 1200))
    b = body(r)
    assert b["filename"] == f"{main._today()}-{b['ref']}.pdf"
    b2 = body(post(client, ("@@@.jpg", img_bytes(800, 1200))))  # nothing usable left after cleaning
    assert b2["filename"] == f"{main._today()}-{b2['ref']}.pdf" and b2["ref"] != b["ref"]
    assert len(main._today()) == 8 and main._today().startswith("20")


def test_uploaded_name_never_logged(client, capsys):
    post(client, ("Jan_Jansen_timesheet.jpg", img_bytes(800, 1200)))
    assert "Jan_Jansen" not in capsys.readouterr().out


def test_pdf_producer_is_img2a4(client):
    pdf = base64.b64decode(body(post(client, ("a.jpg", img_bytes(800, 1200))))["pdf_base64"])
    assert PdfReader(io.BytesIO(pdf)).metadata["/Producer"] == "IMG-2-A4"


def test_jpeg_has_its_own_higher_pixel_limit(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_MEGAPIXELS", 10)        # other formats
    monkeypatch.setattr(main, "MAX_MEGAPIXELS_JPEG", 15)   # JPEG
    assert body(post(client, ("ok.jpg", img_bytes(4000, 3000))))["status"] == "success"   # 12 MP JPEG
    assert_error(post(client, ("big.jpg", img_bytes(5000, 4000))), 413, "image_too_large",  # 20 MP JPEG
                 "20 megapixels", "limit for JPEG is 15")


def test_real_70mp_jpeg_converts():
    data = img_bytes(10000, 7000)   # above the old 60 MP limit
    pdf, pages = main.convert_images([data], "standard")
    assert pages == 1 and len(pdf) > 1000
