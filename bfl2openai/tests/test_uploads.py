"""uploads: @image#N:name handles — store/resolve roundtrip + endpoint shapes.

All fabricated; no upstream is contacted.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import uploads  # noqa: E402

RAW = b"\x89PNG-clipboard-bytes"


@pytest.fixture(autouse=True)
def _clean():
    uploads._STORE.clear()
    uploads._REFS.clear()
    yield
    uploads._STORE.clear()
    uploads._REFS.clear()


def _store_one(name="Clipboard_Screenshot.png", data=RAW, ordinal=1):
    return uploads.store_upload(data, name, ordinal)


def test_store_returns_chat_style_ref():
    info = _store_one()
    assert info["ref"] == "@image#1:Clipboard_Screenshot.png"
    assert info["expires_in"] == 3600


def test_resolve_rewrites_handle_to_ref_image_placeholder():
    _store_one()
    prompt = "edit @image#1:Clipboard_Screenshot.png: make it pop"
    out_prompt, images = uploads.resolve_handles(prompt, None)
    assert "<ref_image_0>" in out_prompt
    assert "@image#1" not in out_prompt
    assert len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert base64.b64decode(images[0].split(",", 1)[1]) == RAW


def test_explicit_images_keep_lower_indices():
    _store_one()
    _, images = uploads.resolve_handles("x @image#1:Clipboard_Screenshot.png",
                                        ["https://cdn.example/first.png"])
    assert images[0] == "https://cdn.example/first.png"
    assert images[1].startswith("data:image/png;base64,")


def test_unknown_handle_fails_naming_the_reference():
    with pytest.raises(HTTPException) as exc:
        uploads.resolve_handles("x @image#1:never-uploaded.png", None)
    assert exc.value.status_code == 400
    assert "never-uploaded.png" in exc.value.detail


def test_two_uploads_get_distinct_ordinals():
    a = _store_one(name="Clipboard_Screenshot.png", ordinal=1)
    b = uploads.store_upload(b"\x89PNG-second", "second.png", ordinal=2)
    assert a["ref"] == "@image#1:Clipboard_Screenshot.png"
    assert b["ref"] == "@image#2:second.png"


def test_oversized_file_refused():
    with pytest.raises(HTTPException) as exc:
        uploads.store_upload(b"x" * (33 * 1024 * 1024), "big.png", 1)
    assert exc.value.status_code == 413


def test_uploads_endpoint_roundtrip():
    app = FastAPI()
    uploads.register(app)
    client = TestClient(app)
    r = client.post(
        "/v1/uploads",
        files=[("file", ("Clipboard_Screenshot.png", RAW, "image/png"))],
    )
    assert r.status_code == 200
    body = r.json()["files"][0]
    assert body["ref"] == "@image#1:Clipboard_Screenshot.png"
    # the handle resolves through the endpoint-registered ref
    out_prompt, images = uploads.resolve_handles(
        f"fix {body['ref']}", None)
    assert "<ref_image_0>" in out_prompt
    assert len(images) == 1


def test_uploads_endpoint_empty_part_is_422():
    app = FastAPI()
    uploads.register(app)
    client = TestClient(app)
    r = client.post("/v1/uploads",
                    files=[("file", ("empty.png", b"", "image/png"))])
    assert r.status_code == 422
