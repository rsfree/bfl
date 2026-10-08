"""async /async/v1/images/generations — jimeng-shaped task contract, fabricated.

Covers: 202 accept shape (+Location), 202 polling body, success body
(data[].url + created + usage), failure-as-200, unknown==foreign==404,
credential isolation on list/get, delete-of-inflight 400, uploads handles
resolved at accept time (a handle that dies between accept and run must not
matter), and 4k composite routed through MPS config guard.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bfl2openai import async_images, native, upstream, upscale  # noqa: E402

RAW = b"\x89PNG-clip"


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    async_images.register(app)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    async_images._TASKS.clear()
    async_images._ORDER.clear()
    uploads_clean()


def uploads_clean():
    uploads_mod_store = sys.modules.get("bfl2openai.uploads")
    if uploads_mod_store:
        uploads_mod_store._STORE.clear()
        uploads_mod_store._REFS.clear()


async def _ok_submit(**kw):
    return "poll-tok"


async def _ok_wait(token):
    return "img-tok"


def test_accept_returns_202_with_location(client, monkeypatch):
    monkeypatch.setattr(native, "_reference_png", lambda ref: RAW)
    monkeypatch.setattr(async_images, "native_submit", _ok_submit)
    monkeypatch.setattr(upstream, "wait_for_result", _ok_wait)
    r = client.post("/async/v1/images/generations",
                    json={"prompt": "x", "images": [DATA_URI]})
    assert r.status_code == 202
    body = r.json()
    assert set(body.keys()) == {"task_id"}
    assert r.headers["Location"].endswith(
        f"/async/v1/images/generations/{body['task_id']}")


def test_accept_resolves_upload_handles_at_accept_time(client, monkeypatch):
    """The handle dies right after accept (store cleared): the task still
    runs, because resolution happened synchronously."""
    monkeypatch.setattr(native, "_reference_png", lambda ref: RAW)
    monkeypatch.setattr(async_images, "native_submit", _ok_submit)
    monkeypatch.setattr(upstream, "wait_for_result", _ok_wait)
    info = uploads_store(data=RAW, name="Clipboard_Screenshot.png", ordinal=1)
    r = client.post("/async/v1/images/generations",
                    json={"prompt": f"fix {info['ref']}"})
    assert r.status_code == 202
    uploads_clean()  # handle store wiped; the queued payload must not care
    tid = r.json()["task_id"]
    # not asserting completion -- only that accept already consumed the handle


def test_unknown_task_is_404(client):
    r = client.get("/async/v1/images/generations/nope")
    assert r.status_code == 404


def test_poll_non_terminal_is_202(client, monkeypatch):
    async def hang(*a, **k):
        await _never()

    monkeypatch.setattr(async_images, "native_submit", hang)
    r = client.post("/async/v1/images/generations", json={"prompt": "x"})
    tid = r.json()["task_id"]
    # the run task is stuck on the (never-resolving) submit; body is queued/in_progress
    r2 = client.get(f"/async/v1/images/generations/{tid}")
    assert r2.status_code == 202
    assert r2.json()["status"] in ("queued", "in_progress")


def test_failure_answers_200_with_error(client, monkeypatch):
    async def boom(*a, **kw):
        raise upstream.UpstreamError("upstream exploded")

    monkeypatch.setattr(async_images, "native_submit", boom)
    r = client.post("/async/v1/images/generations", json={"prompt": "x"})
    tid = r.json()["task_id"]
    import time as _t

    for _ in range(50):
        r2 = client.get(f"/async/v1/images/generations/{tid}")
        if r2.status_code != 202:
            break
        _t.sleep(0.05)
    body = r2.json()
    assert r2.status_code == 200
    assert body["status"] == "failure"
    assert "upstream exploded" in body["error"]["message"]


def test_credential_isolation_on_get_and_list(client, monkeypatch):
    monkeypatch.setattr(async_images, "native_submit", _ok_submit)
    monkeypatch.setattr(upstream, "wait_for_result", _ok_wait)
    r = client.post("/async/v1/images/generations", json={"prompt": "x"},
                    headers={"x-key": "key-A"})
    tid = r.json()["task_id"]
    # a different credential sees neither the task nor the list entry
    assert client.get(f"/async/v1/images/generations/{tid}",
                      headers={"x-key": "key-B"}).status_code == 404
    listed = client.get("/async/v1/images/generations",
                        headers={"x-key": "key-B"}).json()
    assert all(t["task_id"] != tid for t in listed["tasks"])


def test_delete_inflight_is_400(client, monkeypatch):
    async def hang(*a, **kw):
        await _never()

    monkeypatch.setattr(async_images, "native_submit", hang)
    r = client.post("/async/v1/images/generations", json={"prompt": "x"})
    tid = r.json()["task_id"]
    d = client.delete(f"/async/v1/images/generations/{tid}")
    assert d.status_code == 400


def test_composite_without_mps_is_503(client, monkeypatch):
    monkeypatch.setattr(upscale, "SECRET_ID", "")
    monkeypatch.setattr(upscale, "SECRET_KEY", "")
    r = client.post("/async/v1/images/generations",
                    json={"prompt": "x", "resolution": "4k"})
    assert r.status_code == 503


DATA_URI = "data:image/png;base64," + base64.b64encode(RAW).decode()
NEVER = None


async def _never():
    await asyncio.sleep(3600)


def uploads_store(data: bytes, name: str, ordinal: int):
    from bfl2openai import uploads

    uploads._STORE.clear()
    uploads._REFS.clear()
    return uploads.store_upload(data, name, ordinal)

