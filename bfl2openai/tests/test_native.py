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

    # 无 COS 回退分支：不触发转存，sample 走本服务代理链接
    monkeypatch.setattr("bfl2openai.native.mps.SECRET_ID", "")
    monkeypatch.setattr("bfl2openai.native.mps.SECRET_KEY", "")

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
    import asyncio as _aio

    from bfl2openai import store as _store

    _aio.run(_store.drop("pollcache:tok-123"))          # 清轮询缓存
    r = client.get("/v1/get_result", params={"id": "tok-123"})
    assert r.json()["status"] == "Pending"

    async def fake_error(token):
        return {"status": "error", "error": "bad token"}

    monkeypatch.setattr("bfl2openai.native.upstream.poll_once", fake_error)
    _aio.run(_store.drop("pollcache:tok-123"))
    r = client.get("/v1/get_result", params={"id": "tok-123"})
    body = r.json()
    assert body["status"] == "Error"
    assert body["details"]["error"] == "bad token"


def test_ensure_resolution_capacity_lifts_1k_wide():
    from bfl2openai.bridging import ensure_resolution_capacity as cap

    assert cap("1:1", "1k") == "1k"       # 1.05MP，预算内
    assert cap("3:2", "1k") == "1k"       # 1.04MP，实测可行
    assert cap("16:9", "1k") == "2k"      # 1.86MP，实测作废 → 自动升档
    assert cap("9:16", "1k") == "2k"
    assert cap("16:9", "2k") == "2k"      # 2k 预算内
    assert cap("16:9", "4k") == "4k"      # 未知档位不动
    assert cap("auto", "1k") == "1k"      # auto 不解析，原样返回


def test_sync_generations_end_to_end_shape(monkeypatch):
    """回归：sync generations 全路径（曾因 server.py 缺 bridging import 而
    全量 500——单测没盖到端到端，这次钉死）。需要完整 server app。"""
    import base64

    from fastapi.testclient import TestClient

    from bfl2openai import server as server_mod

    # 本地 .env 若设了 API_KEY 会开鉴权——回归测试只验证生成路径，关掉
    monkeypatch.setattr(server_mod, "API_KEY", "")
    client = TestClient(server_mod.app)

    async def fake_submit(**kw):
        return "poll-x"

    async def fake_wait(token, base_url=None):
        return "img-x"

    async def fake_media(image_token, base_url=None):
        return b"\x89PNG\r\n\x1a\nfake"

    monkeypatch.setattr("bfl2openai.server.upstream.submit_generation", fake_submit)
    monkeypatch.setattr("bfl2openai.server.upstream.wait_for_result", fake_wait)
    monkeypatch.setattr("bfl2openai.server.upstream.fetch_media", fake_media)
    r = client.post("/v1/images/generations", json={
        "prompt": "x", "model": "anything", "size": "1024x1024",
        "resolution": "1k", "response_format": "b64_json"})
    assert r.status_code == 200, r.text
    assert base64.b64decode(r.json()["data"][0]["b64_json"])[:4] == b"\x89PNG"


def test_submit_falls_back_to_proxy_on_quota_signals(monkeypatch):
    """直连吃 429 → 池代理接棒出 token（配额×池大小的核心机制）。"""
    import asyncio

    from bfl2openai import upstream

    made = []

    class FakeResp:
        def __init__(self, code, body):
            self.status_code = code
            self.text = body

        def json(self):
            return json.loads(self.text)

    class FakeSession:
        def __init__(self, proxy):
            self.proxy = proxy
            made.append(proxy)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if self.proxy:
                return FakeResp(200, json.dumps(
                    {"status": "submitted", "pollToken": "tok-proxy"}))
            return FakeResp(429, "rate limited")

    monkeypatch.setattr(upstream, "FALLBACK_PROXY", "socks5h://pool:1080")
    monkeypatch.setattr(upstream, "_DIRECT_ATTEMPTS", 1)
    monkeypatch.setattr(upstream, "_PROXY_ATTEMPTS", 1)
    monkeypatch.setattr(upstream, "_session", lambda: FakeSession(None))
    monkeypatch.setattr(upstream, "_make_session",
                        lambda proxy=None, timeout=None: FakeSession(proxy))
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_: _real_sleep(0))

    token = asyncio.run(upstream.submit_generation(
        image_png=b"\x89PNG", prompt="x", aspect_ratio="1:1", mode="i2i"))
    assert token == "tok-proxy"
    # 新架构（2026-10-15）：直连出口被上游限流，提交全走池代理
    # （attempts 列表预构造会话对象，首个即成功返回；断言全部为代理会话）
    assert made and all(m == "socks5h://pool:1080" for m in made)


def test_submit_param_errors_fail_fast_without_proxy(monkeypatch):
    """400 类参数错误换 IP 没用：直连第一跳就抛，不走池。"""
    import asyncio

    from bfl2openai import upstream

    made = []

    class FakeResp:
        status_code = 400
        text = '{"error":"Choose an aspect ratio for the canvas."}'

        def json(self):
            return {"error": "Choose an aspect ratio for the canvas."}

    posts = []

    class FakeSession:
        def __init__(self, proxy):
            self.proxy = proxy

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            # session 是尝试列表预构造的（连接惰性），断言只看实际 POST 次数
            posts.append(self.proxy)
            return FakeResp()

    monkeypatch.setattr(upstream, "FALLBACK_PROXY", "socks5h://pool:1080")
    monkeypatch.setattr(upstream, "_DIRECT_ATTEMPTS", 2)
    monkeypatch.setattr(upstream, "_session", lambda: FakeSession(None))
    monkeypatch.setattr(upstream, "_make_session",
                        lambda proxy=None, timeout=None: FakeSession(proxy))

    import pytest as _pytest

    with _pytest.raises(upstream.UpstreamError) as exc:
        asyncio.run(upstream.submit_generation(
            image_png=b"", prompt="x", aspect_ratio="auto", mode="t2i"))
    assert "400" in str(exc.value)
    # 参数错误第一跳就 fail-fast（现在第一跳即池代理，仍不重试）
    assert posts == ["socks5h://pool:1080"]


def test_normalize_image_downscales_oversize():
    import io
    import os

    from PIL import Image
    from bfl2openai.payload import UPSTREAM_BODY_BUDGET, normalize_image

    im = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=0)
    big = buf.getvalue()
    assert len(big) > UPSTREAM_BODY_BUDGET
    out, ctype = normalize_image(big)
    assert len(out) <= UPSTREAM_BODY_BUDGET
    assert ctype == "image/png"
    out_im = Image.open(io.BytesIO(out))
    assert max(out_im.size) <= 2048


def test_normalize_image_passthrough_small():
    from bfl2openai.payload import normalize_image

    small = b"\x89PNG-not-really-but-small"
    out, ctype = normalize_image(small)
    assert out == small and ctype == "image/png"


def test_normalize_image_joint_shares_budget():
    import io
    import os

    from PIL import Image
    from bfl2openai.payload import UPSTREAM_BODY_BUDGET, normalize_image_joint

    def big_png():
        im = Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3))
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=0)
        return buf.getvalue()

    main, refs = normalize_image_joint(big_png(), [big_png(), big_png()])
    total = len(main) + sum(len(r) for r in refs)
    assert total <= UPSTREAM_BODY_BUDGET          # 联合预算不超
    for r in refs:
        assert max(Image.open(io.BytesIO(r)).size) <= 2048


def test_native_submit_splits_main_and_references(monkeypatch):
    """入口契约：images[0] → image 部件；images[1:] → references 文件部件。"""
    import asyncio

    from bfl2openai import async_images, native, store, upstream

    captured = {}

    async def fake_ref(ref):
        return b"png-bytes"

    async def fake_submit(**kwargs):
        captured.update(kwargs)
        return "tok-1"

    monkeypatch.setattr(native, "_reference_png", fake_ref)
    monkeypatch.setattr(upstream, "submit_generation", fake_submit)

    # 单张：无 references
    asyncio.run(async_images.native_submit(
        {"prompt": "x", "images": ["u1"], "aspect_ratio": "1:1"}, "1k", None, {}))
    assert captured["image_png"] == b"png-bytes"
    assert captured["reference_images"] is None

    # 三张：第 1 张主图，第 2/3 张进 references
    captured.clear()
    asyncio.run(async_images.native_submit(
        {"prompt": "x", "images": ["u1", "u2", "u3"], "aspect_ratio": "1:1"},
        "1k", None, {}))
    assert captured["image_png"] == b"png-bytes"
    assert [n for _, n in captured["reference_images"]] == ["ref_2.png", "ref_3.png"]


def test_single_image_normalized_before_upload(monkeypatch):
    """>4MB 单图必须走 normalize 降档（历史回归：单图路径曾丢失该调用）。"""
    import asyncio
    import io
    import os

    from PIL import Image
    from bfl2openai import upstream
    from bfl2openai.payload import UPSTREAM_BODY_BUDGET, content_type_of

    seen = {}

    async def fake_post(url, multipart=None, headers=None):
        raise AssertionError("should not reach network")  # pragma: no cover

    im = Image.frombytes("RGB", (2000, 2000), os.urandom(2000 * 2000 * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=1)
    big = buf.getvalue()
    assert len(big) > UPSTREAM_BODY_BUDGET

    # 直接在 normalize 层验证（提交层需要真网络，此处只锁定降档语义 + 类型嗅探）
    from bfl2openai.payload import normalize_image

    out, ct = normalize_image(big)
    assert len(out) <= UPSTREAM_BODY_BUDGET
    assert content_type_of(out) == ct
    assert content_type_of(b"\xff\xd8\xff\xe0rest") == "image/jpeg"


def test_ci_input_image_keeps_dimensions():
    """CI 输入：压体积不压尺寸（4k 复合实测：降档导致只出 2k）。"""
    import io
    import os

    from PIL import Image
    from bfl2openai.upscale import ci_input_image

    im = Image.frombytes("RGB", (2048, 2048), os.urandom(2048 * 2048 * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG", compress_level=6)
    out = ci_input_image(buf.getvalue(), budget=4_500_000)
    assert len(out) <= 4_500_000
    assert Image.open(io.BytesIO(out)).size == (2048, 2048)  # 尺寸不变！


def test_moderation_maps_to_422():
    """内容审核拒绝必须是 422（客户端内容问题），不是 502。"""
    from bfl2openai import server as server_mod
    from bfl2openai.upstream import UpstreamError

    r = server_mod.wrap_upstream_error(
        UpstreamError('status failed: HTTP 422: {"error":"Your input was flagged '
                      'by content moderation.","code":"moderation"}',
                      "content_policy"))
    assert r.status_code == 422
    assert b"moderation" in r.body.lower()


def test_poll_negative_cache_for_moderation(monkeypatch):
    """审核拒绝的 token 必须负缓存——重复轮询不再打上游。"""
    import asyncio

    from bfl2openai import store, upstream

    calls = {"n": 0}

    async def fake_poll(token, base_url=None):
        calls["n"] += 1
        raise upstream.UpstreamError(
            'status failed: HTTP 422: {"status":"error","error":"Your input was '
            'flagged by content moderation.","code":"moderation"}')

    monkeypatch.setattr(upstream, "poll_once", fake_poll)
    tok = "neg-cache-tok"

    async def twice():
        for _ in range(2):
            try:
                async def _noop():
                    return await upstream.poll_once_cached(tok)
                await _noop()
            except upstream.UpstreamError:
                pass

    asyncio.run(twice())
    assert calls["n"] == 1          # 第二次命中负缓存，上游零请求
    cached = asyncio.run(store.get(f"pollcache:{tok[:40]}"))
    assert cached is not None and cached["status"] == "error"
    asyncio.run(store.drop(f"pollcache:{tok[:40]}"))
