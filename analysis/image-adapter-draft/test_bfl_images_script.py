"""bfl/images@v1: the flux-tools precise-editing script, specified.

The suite is organised by the decisions the script makes:

  * the submit call: a multipart plan pointed at the channel origin's
    /api/flux3-image/generate, carrying the playground-shaped browser
    headers (the submit POST answers an empty 403 without them) and one
    PNG file part; input normalization covers data URIs, bare base64 and
    URLs, and non-PNG bytes are converted;
  * the input guards: edit-only (no image refused), exactly one image,
    and the 256x256 minimum checked here -- with the measured size named
    -- because the upstream only says it after the upload;
  * `boxes`: accepted as a native list (canonical door) or a JSON string
    (edits door), coordinates clamped into 0..1, per-box instructions
    kept, ids minted when absent; junk fails with the field named; a
    request with neither a prompt nor a single box instruction is
    refused before anything is spent;
  * `mask` and `size` are dropped by ruling, explicit aspect_ratio /
    resolution strings are forwarded;
  * the poll pair: the submit reply's pollToken becomes a GET status call
    with a query parameter, and the three documented statuses map to
    done/verdict/error;
  * the reply: the imageToken never travels -- b64_json (default) fetches
    and encodes the media, url re-hosts it, and a deployment without
    object storage degrades that image to b64_json instead of handing
    over a short-lived signed link.

No live upstream is contacted anywhere in this file: every network-shaped
ctx method (checked download, rehost) is a recorder and the upstream
answers are fabrications. That proves the script speaks the contract
captured on 2026-10-09; the live end-to-end run of that day is the
traffic half of the evidence.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

from adapter.context import AdapterContext
from adapter.errors import AdapterError
from adapter.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "script_store" / "bfl"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bfl = _load("bfl_images_v1", "images@v1.py")

CHANNEL_URL = "https://flux-tools.bfl.ai/api/flux3-image/generate"
RAW = b"\x89PNG-not-really-bytes"
BARE = base64.b64encode(RAW).decode()
DATA_URI = "data:image/png;base64," + BARE
OURS = "https://oss.example/temp/x.png"
MEDIA = "https://flux-tools.bfl.ai/api/flux3-image/media?imageToken=tok-1"
READY = {"status": "ready", "imageToken": "tok-1"}


class _ChannelStub:
    upstream_key = ""
    upstream_url = CHANNEL_URL
    stage_urls: dict = {}

    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}


class Recorder:
    """Stands in for the checked download and the rehost."""

    def __init__(self, stored: str | None = OURS) -> None:
        self.stored = stored
        self.rehosts: list[str] = []
        self.b64_fetches: list[str] = []

    async def rehost(self, url: str):
        self.rehosts.append(url)
        return self.stored

    async def fetch_b64(self, ref: str) -> str:
        self.b64_fetches.append(ref)
        return BARE


def _ctx(monkeypatch, *, stored: str | None = OURS, upstream_url: str = CHANNEL_URL):
    channel = _ChannelStub()
    channel.upstream_url = upstream_url
    ctx = AdapterContext(
        request_id="req-bfl-1",
        channel=channel,
        settings=Settings(minio_endpoint=""),
    )
    spy = Recorder(stored=stored)

    async def info(data):
        return {"width": 512, "height": 512, "format": "PNG", "mode": "RGB"}

    async def convert(data, fmt, quality=None):
        return data

    monkeypatch.setattr(ctx, "image_b64", spy.fetch_b64)
    monkeypatch.setattr(ctx, "rehost_image", spy.rehost)
    # One cached stub per ctx: tests reach into ctx.image._info/_convert to
    # vary dimensions or format without rebuilding the facade.
    stub = _ImageStub(info, convert)
    monkeypatch.setattr(type(ctx), "image", property(lambda self: stub))
    return ctx, spy


class _ImageStub:
    def __init__(self, info, convert) -> None:
        self._info, self._convert = info, convert

    async def info(self, data):
        return await self._info(data)

    async def convert(self, data, fmt, quality=None):
        return await self._convert(data, fmt, quality)


@pytest.fixture(autouse=True)
def _clean_state():
    bfl._STATE.clear()
    yield
    bfl._STATE.clear()


async def _request(ctx, payload: dict) -> dict:
    return await bfl.transform(ctx, payload, "request")


async def _respond(ctx, payload: dict) -> dict:
    return await bfl.transform(ctx, payload, "response")


# ---------------------------------------------------------------- submit


async def test_submit_builds_multipart_plan_with_browser_headers(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(ctx, {"prompt": "red", "image": DATA_URI})
    assert ctx.plan.url == "https://flux-tools.bfl.ai/api/flux3-image/generate"
    assert ctx.plan.files["image"][0][0] == "image.png"
    assert ctx.plan.files["image"][0][1] == RAW
    assert ctx.plan.files["image"][0][2] == "image/png"
    assert ctx.plan.headers["Origin"] == "https://flux-tools.bfl.ai"
    assert (
        ctx.plan.headers["Referer"]
        == "https://flux-tools.bfl.ai/precise-editing"
    )
    assert ctx.plan.headers["Sec-Fetch-Site"] == "same-origin"
    assert fields["mode"] == "i2i"
    assert fields["prompt"] == "red"
    assert fields["aspectRatio"] == "auto"
    assert fields["resolution"] == "1.5k"
    assert json.loads(fields["boxes"]) == []
    assert fields["references"] == "[]"


async def test_url_reference_is_fetched_through_checked_path(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "red", "image": "https://cdn.example/cat.png"})
    assert spy.b64_fetches == ["https://cdn.example/cat.png"]
    assert ctx.plan.files["image"][0][1] == RAW


async def test_non_png_input_is_converted(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    seen = {}

    async def info(data):
        return {"width": 300, "height": 300, "format": "JPEG", "mode": "RGB"}

    async def convert(data, fmt, quality=None):
        seen["fmt"] = fmt
        return data

    ctx.image._info, ctx.image._convert = info, convert
    await _request(ctx, {"prompt": "red", "image": DATA_URI})
    assert seen["fmt"] == "PNG"


async def test_edit_only_channel_refuses_missing_image(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "red"})
    assert exc.value.param == "image"


async def test_more_than_one_image_is_refused_with_the_count(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "red", "image": [DATA_URI, DATA_URI]})
    assert "exactly one" in exc.value.message
    assert exc.value.param == "image"


async def test_small_input_is_refused_locally_with_measured_size(monkeypatch):
    ctx, _ = _ctx(monkeypatch)

    async def info(data):
        return {"width": 64, "height": 64, "format": "PNG", "mode": "RGB"}

    ctx.image._info = info
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "red", "image": DATA_URI})
    assert "256x256" in exc.value.message
    assert "64x64" in exc.value.message


# ---------------------------------------------------------------- boxes


async def test_boxes_native_list_is_normalized_and_clamped(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(
        ctx,
        {
            "prompt": "",
            "image": DATA_URI,
            "boxes": [
                {
                    "box": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 1.4},
                    "instruction": "gold wheels",
                }
            ],
        },
    )
    boxes = json.loads(fields["boxes"])
    assert len(boxes) == 1
    assert boxes[0]["box"]["h"] == 1.0  # clamped
    assert boxes[0]["instruction"] == "gold wheels"
    assert boxes[0]["id"].startswith("b-")
    assert boxes[0]["source"] == "user"


async def test_boxes_json_string_from_edits_door_is_accepted(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    raw = json.dumps(
        [{"box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, "instruction": "sky"}]
    )
    fields = await _request(ctx, {"prompt": "", "image": DATA_URI, "boxes": raw})
    boxes = json.loads(fields["boxes"])
    assert boxes[0]["instruction"] == "sky"
    assert boxes[0]["box"] == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}


async def test_box_id_from_caller_is_kept(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(
        ctx,
        {
            "prompt": "red",
            "image": DATA_URI,
            "boxes": [{"id": "b-mine", "box": {"x": 0, "y": 0, "w": 1, "h": 1}}],
        },
    )
    assert json.loads(fields["boxes"])[0]["id"] == "b-mine"


async def test_junk_boxes_fail_with_the_field_named(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "red", "image": DATA_URI, "boxes": "{not json"})
    assert exc.value.param == "boxes"


async def test_box_without_coordinates_fails(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _request(
            ctx,
            {"prompt": "red", "image": DATA_URI, "boxes": [{"instruction": "x"}]},
        )
    assert exc.value.param == "boxes"


async def test_empty_prompt_and_empty_boxes_are_refused(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "", "image": DATA_URI})
    assert exc.value.param == "prompt"


async def test_box_instruction_alone_justifies_an_empty_prompt(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(
        ctx,
        {
            "prompt": "",
            "image": DATA_URI,
            "boxes": [
                {"box": {"x": 0, "y": 0, "w": 1, "h": 1}, "instruction": "night"}
            ],
        },
    )
    assert fields["prompt"] == ""
    assert len(json.loads(fields["boxes"])) == 1


# ---------------------------------------------------------------- drops


async def test_mask_is_dropped_by_ruling(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(
        ctx, {"prompt": "red", "image": DATA_URI, "mask": DATA_URI}
    )
    assert fields["mode"] == "i2i"  # went through, no failure


async def test_size_is_dropped_and_explicit_dialect_is_forwarded(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    fields = await _request(
        ctx,
        {
            "prompt": "red",
            "image": DATA_URI,
            "size": "1024x1024",
            "aspect_ratio": "16:9",
            "resolution": "2k",
        },
    )
    assert fields["aspectRatio"] == "16:9"
    assert fields["resolution"] == "2k"
    assert "size" not in fields


# ---------------------------------------------------------------- poll


async def test_poll_request_emits_status_get_with_token_query(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    handle = await bfl.transform(
        ctx, {"status": "submitted", "pollToken": "tok-9"}, "poll_request"
    )
    assert ctx.plan.method == "GET"
    assert ctx.plan.url == "https://flux-tools.bfl.ai/api/flux3-image/status"
    assert ctx.plan.query == {"pollToken": "tok-9"}
    assert handle == {}


async def test_sidecar_channel_keeps_prefix_but_names_real_origin(monkeypatch):
    """A local egress sidecar fronts the vendor: every emitted URL keeps the
    sidecar prefix, while the browser headers still name the real origin --
    an Origin of the sidecar's own host reads as cross-site upstream."""
    sidecar = "http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image/generate"
    ctx, _ = _ctx(monkeypatch, upstream_url=sidecar)
    await _request(ctx, {"prompt": "red", "image": DATA_URI})
    assert (
        ctx.plan.url
        == "http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image/generate"
    )
    assert ctx.plan.headers["Origin"] == "https://flux-tools.bfl.ai"
    assert (
        ctx.plan.headers["Referer"]
        == "https://flux-tools.bfl.ai/precise-editing"
    )
    await bfl.transform(
        ctx, {"status": "submitted", "pollToken": "t"}, "poll_request"
    )
    assert ctx.plan.url == (
        "http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image/status"
    )


async def test_channel_url_without_generate_suffix_is_config_error(monkeypatch):
    ctx, _ = _ctx(
        monkeypatch,
        upstream_url="https://flux-tools.bfl.ai/some/other/path",
    )
    with pytest.raises(AdapterError) as exc:
        await _request(ctx, {"prompt": "red", "image": DATA_URI})
    assert exc.value.code == "channel_config_error"


async def test_poll_request_without_token_fails(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await bfl.transform(ctx, {"status": "submitted"}, "poll_request")
    assert exc.value.code == "upstream_invalid_response"


async def test_poll_response_maps_the_three_statuses(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    pending = await bfl.transform(ctx, {"status": "pending"}, "poll_response")
    assert pending == {"done": False}
    ready = await bfl.transform(ctx, READY, "poll_response")
    assert ready["done"] is True
    assert ready["payload"] == READY
    error = await bfl.transform(
        ctx, {"status": "error", "error": "bad token"}, "poll_response"
    )
    assert error["error"] == "bad token"


# ---------------------------------------------------------------- reply


async def test_reply_defaults_to_b64_and_never_exposes_the_token(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    out = await _respond(ctx, READY)
    assert out["data"][0]["b64_json"] == BARE
    assert "url" not in out["data"][0]
    assert spy.b64_fetches == [MEDIA]
    assert out["created"] > 0


async def test_reply_url_format_rehosts_the_media(monkeypatch):
    ctx, spy = _ctx(monkeypatch)
    bfl._STATE[ctx.request_id] = "url"
    out = await _respond(ctx, READY)
    assert spy.rehosts == [MEDIA]
    assert out["data"][0]["url"] == OURS


async def test_reply_url_format_without_storage_degrades_to_b64(monkeypatch):
    ctx, spy = _ctx(monkeypatch, stored=None)
    bfl._STATE[ctx.request_id] = "url"
    out = await _respond(ctx, READY)
    assert spy.rehosts == [MEDIA]
    assert out["data"][0]["b64_json"] == BARE
    assert "url" not in out["data"][0]


async def test_reply_without_image_token_fails(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    with pytest.raises(AdapterError) as exc:
        await _respond(ctx, {"status": "ready"})
    assert exc.value.code == "upstream_invalid_response"


async def test_response_format_parking_is_consumed_once(monkeypatch):
    ctx, _ = _ctx(monkeypatch)
    await _request(ctx, {"prompt": "red", "image": DATA_URI, "response_format": "url"})
    assert bfl._STATE[ctx.request_id] == "url"
    await _respond(ctx, READY)
    assert ctx.request_id not in bfl._STATE
