"""bfl2openai native (docs.bfl.ai) routes + MPS upscale client, specified.

Everything runs against fabrications: the upstream contract was captured
once (analysis/bfl_openapi.json + playground traffic of 2026-10-09) and
these tests pin the translation layer, never the billable path.

Covered:

  native request validation   prompt required, resolution enum (with the
                              4k/8k composite extensions), images count
                              and spelling (data URI / http / base64)
  boxes translation           official 0..1000 [x1,y1,x2,y2] rows ->
                              normalized {x,y,w,h} + instruction; junk
                              refused
  composite guard             4k/8k with MPS unconfigured -> 503 BEFORE
                              any upstream submission (no wasted
                              generation)
  native shapes               AsyncResponse on submit ({id, polling_url,
                              cost, input_mp}); poll mapping
                              Pending/Ready(sample=proxy URL)/Error
  TC3 signer                  deterministic given fixed inputs: same
                              timestamp+body -> same Authorization, and
                              the credential scope carries the UTC date
  MPS state parsing           SUCCESS+SignUrl -> done; PROCESSING ->
                              pending; FINISH without success -> error
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

BFL_DIR = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "bfl2openai_upscale_test", BFL_DIR / "upscale.py"
)
# the module does `from . import upstream` -- import it as a package module
import sys  # noqa: E402

sys.path.insert(0, str(BFL_DIR.parent))
from bfl2openai import upscale as mps  # noqa: E402
from bfl2openai.native import _boxes_native  # noqa: E402

RAW = b"\x89PNG-small"
DATA_URI = "data:image/png;base64," + base64.b64encode(RAW).decode()


# ---------------------------------------------------------------- boxes


def test_official_bbox_row_maps_to_normalized_box():
    rows = [{"id": "b1", "bbox": [100, 200, 500, 600], "desc": "a red knight"}]
    out = _boxes_native(rows)
    assert out == [{
        "id": "b1",
        "box": {"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.4},
        "instruction": "a red knight",
        "source": "user",
    }]


def test_bbox_clamped_into_unit_square():
    out = _boxes_native([{"bbox": [-50, 0, 1200, 800], "desc": "x"}])
    b = out[0]["box"]
    assert b["x"] == 0.0 and b["w"] == 1.0 and b["h"] == 0.8


def test_bbox_without_four_numbers_refused():
    with pytest.raises(ValueError):
        _boxes_native([{"bbox": [1, 2, 3]}])


# ---------------------------------------------------------------- TC3


def _with_creds(monkeypatch):
    monkeypatch.setattr(mps, "SECRET_ID", "AKIDexample")
    monkeypatch.setattr(mps, "SECRET_KEY", "kSecret")
    monkeypatch.setattr(mps, "REGION", "ap-shanghai")


def test_tc3_signature_is_deterministic(monkeypatch):
    _with_creds(monkeypatch)
    monkeypatch.setattr(mps.time, "time", lambda: 1_700_000_000)
    h1 = mps._tc3_headers("ProcessImage", {"a": 1})
    h2 = mps._tc3_headers("ProcessImage", {"a": 1})
    assert h1["Authorization"] == h2["Authorization"]
    assert "credential=akidexample/2023-11-14/mps/tc3_request" in (
        h1["Authorization"].lower().replace(" ", "")
    )
    assert h1["X-TC-Action"] == "ProcessImage"
    assert h1["X-TC-Version"] == "2019-06-12"


def test_tc3_signature_changes_with_body(monkeypatch):
    _with_creds(monkeypatch)
    monkeypatch.setattr(mps.time, "time", lambda: 1_700_000_000)
    a = mps._tc3_headers("ProcessImage", {"a": 1})
    b = mps._tc3_headers("ProcessImage", {"a": 2})
    assert a["Authorization"] != b["Authorization"]


def test_unconfigured_signer_raises(monkeypatch):
    monkeypatch.setattr(mps, "SECRET_ID", "")
    monkeypatch.setattr(mps, "SECRET_KEY", "")
    with pytest.raises(RuntimeError):
        mps._tc3_headers("ProcessImage", {})


# ---------------------------------------------------------------- MPS state


def test_finished_success_yields_sign_url():
    response = {
        "Status": "FINISH",
        "ImageProcessTaskInfoSet": [
            {"Status": "SUCCESS",
             "Output": {"SignUrl": "https://cos.example/x.png?sign=1"}}
        ],
    }
    assert mps._finished(response) == ("https://cos.example/x.png?sign=1", None)


def test_finished_processing_is_pending():
    response = {
        "Status": "RUNNING",
        "ImageProcessTaskInfoSet": [{"Status": "PROCESSING", "Output": {}}],
    }
    assert mps._finished(response) == (None, None)


def test_finished_without_success_is_an_error():
    response = {
        "Status": "FINISH",
        "ImageProcessTaskInfoSet": [{"Status": "FAIL", "Output": {}}],
    }
    url, err = mps._finished(response)
    assert url is None and err and "FAIL" in err


# ---------------------------------------------------------------- via TestClient


@pytest.fixture()
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from bfl2openai import native

    app = FastAPI()
    native.register(app)
    return TestClient(app)


def test_native_prompt_required(client):
    r = client.post("/v1/flux-3-image", json={"images": [DATA_URI]})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "prompt"]


def test_native_resolution_enum(client):
    r = client.post("/v1/flux-3-image", json={"prompt": "x", "resolution": "3k"})
    assert r.status_code == 422
    assert "8k" in r.json()["detail"][0]["msg"]  # composite tiers advertised


def test_native_too_many_images(client):
    r = client.post(
        "/v1/flux-3-image",
        json={"prompt": "x", "images": [DATA_URI] * 11},
    )
    assert r.status_code == 422


def test_native_bad_image_spelling_refused_locally(client, monkeypatch):
    # file:// never reaches the upstream: refused as a 422 naming the field.
    called = {}
    monkeypatch.setattr(
        "bfl2openai.upstream.submit_generation",
        lambda **kw: called.__setitem__("called", True) or "tok",
    )
    r = client.post(
        "/v1/flux-3-image",
        json={"prompt": "x", "images": ["file:///etc/passwd"]},
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "images"]
    assert not called


def test_native_composite_without_mps_is_503_before_submission(client, monkeypatch):
    """4k is refused anonymously upstream; the composite must not spend a
    generation when MPS is not configured."""
    monkeypatch.setattr(mps, "SECRET_ID", "")
    monkeypatch.setattr(mps, "SECRET_KEY", "")
    called = {}
    monkeypatch.setattr(
        "bfl2openai.upstream.submit_generation",
        lambda **kw: called.__setitem__("called", True) or "tok",
    )
    r = client.post(
        "/v1/flux-3-image",
        json={"prompt": "x", "images": [DATA_URI], "resolution": "4k"},
    )
    assert r.status_code == 503
    assert "MPS" in r.json()["detail"]
    assert not called


def test_native_submit_returns_async_response_shape(client, monkeypatch):
    captured = {}

    async def fake_submit(**kw):
        captured.update(kw)
        return "tok-123"

    async def fake_png(ref):
        return RAW

    monkeypatch.setattr("bfl2openai.native.upstream.submit_generation", fake_submit)
    monkeypatch.setattr("bfl2openai.native._reference_png", fake_png)
    r = client.post(
        "/v1/flux-3-image",
        json={"prompt": "x", "images": [DATA_URI], "resolution": "2k"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "tok-123"
    assert body["polling_url"].endswith("/v1/get_result?id=tok-123")
    assert body["cost"] is None and body["input_mp"] is None
    assert captured["mode"] == "i2i"
    assert captured["resolution"] == "2k"


def test_native_poll_maps_states(client, monkeypatch):
    import asyncio

    async def fake_poll(token):
        return {"status": "ready", "imageToken": "img-9"}

    monkeypatch.setattr("bfl2openai.native.upstream.poll_once", fake_poll)
    r = client.get("/v1/get_result", params={"id": "tok-123"})
    body = r.json()
    assert body["status"] == "Ready"
    assert body["result"]["sample"].endswith("/v1/flux3-image/results/img-9")

    async def fake_pending(token):
        return {"status": "pending"}

    monkeypatch.setattr("bfl2openai.native.upstream.poll_once", fake_pending)
    r = client.get("/v1/get_result", params={"id": "tok-123"})
    assert r.json()["status"] == "Pending"

    async def fake_error(token):
        return {"status": "error", "error": "bad token"}

    monkeypatch.setattr("bfl2openai.native.upstream.poll_once", fake_error)
    r = client.get("/v1/get_result", params={"id": "tok-123"})
    body = r.json()
    assert body["status"] == "Error"
    assert body["details"]["error"] == "bad token"
