"""Tencent Cloud MPS super-resolution client.

Implements the MPS `ProcessImage` + `DescribeTaskDetail` pair with a TC3
signature, so generated images can be upscaled beyond the anonymous
generation ceiling (the upstream refuses 4k without an account: measured
403 "4K resolution is available to BFL accounts only.", 2026-10-09).

Design inherited from image-adapter docs/08 (kept as reference only -- the
standalone project owns this implementation):

  submit    POST https://mps.tencentcloudapi.com  Action=ProcessImage
            InputInfo.Type="URL"   (a public URL -- the playground's own
            media URL qualifies; no COS bucket needed)
            OutputStorage.Type="TOS"  (managed storage; the result is a
            SignUrl, valid about 1h)
            ImageTask.EnhanceConfig.AdvancedSuperResolutionConfig
              Switch="ON", Type="ultra", Mode="percent", Percent=1.0..10.0
  poll      Action=DescribeTaskDetail by TaskId until
            Response.Status=="FINISH"; then
            ImageProcessTaskInfoSet[].Output.SignUrl (Status=="SUCCESS")

The reference doc flagged two unknowns it never resolved (both "待实测"):
the query action name (the public MPS API is `DescribeTaskDetail`; the doc
half-remembered `DescribeImageTaskDetail`) and the exact final-state
vocabulary. Both live in `query()` so a live probe fixes them in one place.

Credentials: MPS_SECRET_ID / MPS_SECRET_KEY env (MPS_REGION defaults to
ap-shanghai). Without them the client is "unconfigured" and callers must
refuse 4k/8k with a clear error instead of failing mid-request.

Timing reference (from the doc's own measurements): 1080p to 4k about 14s,
4k to 8k about 50s -- the synchronous endpoint budgets 180s.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

from . import upstream

log = logging.getLogger("bfl2openai.upscale")

HOST = "mps.tencentcloudapi.com"
SERVICE = "mps"
VERSION = "2019-06-12"

# the deployment .env is loaded by `upstream` at import (this module imports
# it above), so the reads below already see TENCENT_API_KEY and friends.

SECRET_ID = os.environ.get("MPS_SECRET_ID", "")
SECRET_KEY = os.environ.get("MPS_SECRET_KEY", "")
REGION = os.environ.get("MPS_REGION", "ap-singapore")

#: Where upscaled files land. The account's COS (managed TOS output is not
#: enabled for it: measured "OutputStorage.Type(TOS) not supported."); the
#: chaofen* buckets were created for exactly this purpose.
OUTPUT_BUCKET = os.environ.get("MPS_OUTPUT_BUCKET", "agi-1436636638")
OUTPUT_REGION = os.environ.get("MPS_OUTPUT_REGION", "ap-singapore")

#: One-line credential from the environment: `TENCENT_API_KEY=SECRET_ID|SECRET_KEY`
#: (the deployment's own spelling). MPS_* vars win when both are set.
_tencent = os.environ.get("TENCENT_API_KEY", "")
if _tencent and "|" in _tencent and not (SECRET_ID and SECRET_KEY):
    _sid, _, _skey = _tencent.partition("|")
    SECRET_ID, SECRET_KEY = _sid.strip(), _skey.strip()


def _cos_signature(method: str, pathname: str, key_time: str) -> str:
    """COS XML-API request signature (sha1) for one path, no headers/params."""
    sign_key = hmac.new(SECRET_KEY.encode("utf-8"), key_time.encode("utf-8"),
                        hashlib.sha1).hexdigest()
    format_string = f"{method.lower()}\n{pathname}\n\n\n"
    string_to_sign = (
        f"sha1\n{key_time}\n"
        + hashlib.sha1(format_string.encode("utf-8")).hexdigest()
        + "\n"
    )
    return hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"),
                    hashlib.sha1).hexdigest()


def cos_presigned_get(path: str, ttl_s: int = 3600) -> str:
    """A time-limited download URL for one COS object (Path starts with /)."""
    now = int(time.time()) - 60
    key_time = f"{now};{now + ttl_s}"
    signature = _cos_signature("get", path, key_time)
    return (
        f"https://{OUTPUT_BUCKET}.cos.{OUTPUT_REGION}.myqcloud.com{path}"
        f"?q-sign-algorithm=sha1&q-ak={SECRET_ID}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list=&q-url-param-list="
        f"&q-signature={signature}"
    )


async def upload_to_cos(data: bytes, object_key: str) -> None:
    """PUT one object into the output bucket.

    Why this exists: MPS fetches URL inputs from Tencent's own network, and
    the playground's media URL answers that fetch with a 429 (measured:
    "download failed: status 429" on a real task). Uploading the bytes to COS
    ourselves and handing MPS a CosInputInfo keeps the whole pipeline inside
    Tencent's network.
    """
    pathname = "/" + object_key.lstrip("/")
    now = int(time.time()) - 60
    key_time = f"{now};{now + 900}"
    signature = _cos_signature("put", pathname, key_time)
    auth = (
        f"q-sign-algorithm=sha1&q-ak={SECRET_ID}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list=&q-url-param-list="
        f"&q-signature={signature}"
    )
    url = f"https://{OUTPUT_BUCKET}.cos.{OUTPUT_REGION}.myqcloud.com{pathname}"
    async with upstream._session() as s:
        r = await s.put(url, data=data, headers={"Authorization": auth},
                        timeout=120)
    if r.status_code not in (200, 201):
        raise MPSError(f"COS upload failed: HTTP {r.status_code}: {r.text[:150]}")

#: Per-image processing states seen in the reference doc; anything else is
#: treated as final-and-failed (the full vocabulary is 待实测 there too).
_IN_FLIGHT = {"", None, "PROCESSING", "WAITING"}
_OK = "SUCCESS"


class MPSError(Exception):
    """The API answered with an error, or never reached a success state."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


def configured() -> bool:
    return bool(SECRET_ID and SECRET_KEY)


def _tc3_headers(action: str, payload: dict) -> dict:
    """TC3-HMAC-SHA256 signed headers for one MPS API call."""
    if not configured():
        raise RuntimeError(
            "MPS credentials not configured (MPS_SECRET_ID / MPS_SECRET_KEY)"
        )
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

    canonical_headers = (
        f"content-type:application/json\nhost:{HOST}\nx-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    hashed_payload = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_request = (
        f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{hashed_payload}"
    )
    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{ts}\n{scope}\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )
    k_date = hmac.new(("TC3" + SECRET_KEY).encode("utf-8"), date.encode("utf-8"),
                      hashlib.sha256).digest()
    k_service = hmac.new(k_date, SERVICE.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={SECRET_ID}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "X-TC-Action": action,
        "X-TC-Version": VERSION,
        "X-TC-Timestamp": str(ts),
        "X-TC-Region": REGION,
        "Content-Type": "application/json",
    }


async def _call(action: str, payload: dict) -> dict:
    headers = _tc3_headers(action, payload)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    async with upstream._session() as s:
        r = await s.post(f"https://{HOST}", data=body, headers=headers, timeout=60)
    if r.status_code != 200:
        raise MPSError(f"HTTP {r.status_code}: {r.text[:200]}")
    try:
        data = json.loads(r.text)
    except ValueError:
        raise MPSError(f"non-JSON answer: {r.text[:200]}") from None
    response = data.get("Response", {})
    err = response.get("Error")
    if err:
        raise MPSError(str(err.get("Message", ""))[:200], code=str(err.get("Code", "")))
    return response


async def submit_cos(object_key: str, percent: float = 2.0) -> str:
    """One ProcessImage task with the input taken from our own COS object.

    URL inputs are rejected in practice: MPS's fetcher gets 429'd by the
    playground's edge (measured on a real task: "download failed after retry
    3 times: download failed: status 429"). COS input keeps the transfer
    inside Tencent's network.
    """
    percent = min(10.0, max(1.0, float(percent)))
    payload = {
        "InputInfo": {
            "Type": "COS",
            "CosInputInfo": {"Bucket": OUTPUT_BUCKET, "Region": OUTPUT_REGION,
                             "Object": object_key.lstrip("/")},
        },
        "OutputStorage": {
            "Type": "COS",
            "CosOutputStorage": {"Bucket": OUTPUT_BUCKET, "Region": OUTPUT_REGION},
        },
        "ImageTask": {
            "EnhanceConfig": {
                "AdvancedSuperResolutionConfig": {
                    "Switch": "ON",
                    "Type": "ultra",
                    "Mode": "percent",
                    "Percent": percent,
                }
            }
        },
    }
    response = await _call("ProcessImage", payload)
    task_id = response.get("TaskId")
    if not task_id:
        raise MPSError(f"ProcessImage returned no TaskId: {json.dumps(response)[:200]}")
    return task_id


def _finished(response: dict) -> tuple[str | None, str | None]:
    """(download_url, None) done; (None, None) still processing; (None, err) fail.

    Two response shapes exist in the wild (both measured 2026-10-09):

    * COS output mode (this deployment): a FLAT body -- Status/ErrMsg/Message
      at the top level, no ImageProcessTaskInfoSet. FINISH + ErrMsg means the
      task failed (e.g. "fetch main input: ... status 429" when the upstream
      edge refused MPS's own fetcher -- the reason inputs go through COS).
    * Managed TOS mode (the reference doc's shape): rows under
      ImageProcessTaskInfoSet with Output.SignUrl.
    """
    infos = (response.get("ImageProcessTaskResultSet")      # verified spelling
             or response.get("ImageProcessTaskInfoSet")     # the doc's spelling
             or [])
    for info in infos:
        status = info.get("Status")
        out = info.get("Output") or {}
        if status == _OK:
            # 24h 预签名：1h 的链接被调用方拿到手就快过期了（401 报障）。
            if out.get("SignUrl"):
                return out["SignUrl"], None
            path = out.get("Path")
            if path:
                return cos_presigned_get(
                    path if path.startswith("/") else "/" + path, ttl_s=86400), None
        if status not in _IN_FLIGHT and status != _OK:
            return None, f"task status {status}"
    if response.get("Status") == "FINISH":
        if response.get("ErrMsg") or response.get("Message"):
            detail = f"{response.get('ErrMsg', '')} {response.get('Message', '')}".strip()
            return None, detail[:300]
        if infos:
            return None, f"finished without a success row: {json.dumps(infos[0])[:200]}"
        out = response.get("Output") or {}
        if out.get("SignUrl"):
            return out["SignUrl"], None
        path = out.get("Path")
        if path:
            return cos_presigned_get(
                path if path.startswith("/") else "/" + path, ttl_s=86400), None
        return None, "FINISH without any recognizable output"
    return None, None


async def query(task_id: str, action: str = "DescribeImageTaskDetail") -> dict:
    """One detail probe -> {"state": "pending"|"done"|"error", ...}.

    `DescribeImageTaskDetail` is the verified action (2026-10-09, real task:
    returns the full flat body); `DescribeTaskDetail` answers only
    "Unknown Error!" for these tasks and is kept as a legacy fallback.
    """
    try:
        response = await _call(action, {"TaskId": task_id})
    except MPSError:
        if action == "DescribeImageTaskDetail":
            return await query(task_id, action="DescribeTaskDetail")
        raise
    sign_url, error = _finished(response)
    if sign_url:
        return {"state": "done", "url": sign_url}
    if error:
        return {"state": "error", "error": error}
    return {"state": "pending"}


async def wait_done(task_id: str, timeout_s: float = 180.0) -> str:
    """Server-side poll loop for the synchronous upscale endpoint."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = await query(task_id)
        if state["state"] == "done":
            return state["url"]
        if state["state"] == "error":
            raise MPSError(state["error"])
        await asyncio.sleep(3.0)
    raise MPSError(f"upscale task {task_id} did not finish within {timeout_s:.0f}s")


def cos_ci_signed_url(object_key: str, ci_params: dict, ttl_s: int = 900) -> str:
    """COS GET 签名 URL，处理参数（ci-process 等）参与签名。

    CI 同步图片处理直接作用在桶对象上：GET 即返回处理后的字节，无任务无轮询。
    签名按 COS XML-API 规范：Parameters 排序 k=v（k 小写、值 URL 编码），
    q-url-param-list 列参数名（分号分隔）。
    """
    now = int(time.time()) - 60
    key_time = f"{now};{now + ttl_s}"
    sign_key = hmac.new(SECRET_KEY.encode("utf-8"), key_time.encode("utf-8"),
                        hashlib.sha1).hexdigest()
    kv = "&".join(f"{k.lower()}={urllib.parse.quote(str(v), safe='')}"
                  for k, v in sorted(ci_params.items()))
    plist = ";".join(sorted(k.lower() for k in ci_params))
    fmt = f"get\n/{object_key.lstrip('/')}\n{kv}\n\n"
    string_to_sign = (
        f"sha1\n{key_time}\n" + hashlib.sha1(fmt.encode("utf-8")).hexdigest() + "\n")
    signature = hmac.new(sign_key.encode("utf-8"),
                         string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()
    qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}"
                  for k, v in ci_params.items())
    auth = (f"q-sign-algorithm=sha1&q-ak={SECRET_ID}"
            f"&q-sign-time={key_time}&q-key-time={key_time}"
            f"&q-header-list=&q-url-param-list={plist}&q-signature={signature}")
    return (f"https://{OUTPUT_BUCKET}.cos.{OUTPUT_REGION}.myqcloud.com"
            f"/{object_key.lstrip('/')}?{qs}&{auth}")


async def ci_super_resolve_cos(object_key: str, magnify: int) -> bytes:
    """数据万象同步超分：GET ?ci-process=AISuperResolution 直接返回放大图。

    magnify ∈ {2,3,4}；失败抛 MPSError（调用方决定是否回落 MPS 异步任务）。
    实测 2026-10-15：ap-singapore 桶可用，512²→1024² 秒级返回。
    """
    url = cos_ci_signed_url(object_key, {
        "ci-process": "AISuperResolution", "magnify": magnify})
    try:
        async with upstream._session() as s:
            r = await s.get(url)
    except Exception as exc:  # noqa: BLE001 - 网络/超时统一包装
        raise MPSError(f"CI super-resolution request failed: {exc}") from exc
    if r.status_code != 200:
        raise MPSError(f"CI super-resolution failed: HTTP {r.status_code}: "
                       f"{r.text[:150]}")
    if r.content[:8] != b"\x89PNG\r\n\x1a\n":
        raise MPSError(f"CI super-resolution returned non-PNG: {r.content[:120]}")
    return r.content


def ci_input_image(data: bytes, budget: int = 4_500_000) -> bytes:
    """CI 同步超分的输入准备：**保尺寸、压体积**。

    与 payload.normalize_image 的差别：超分要保留原始分辨率（放大倍数作用
    在输入尺寸上），所以优先 JPEG 重编码（照片压缩率高，2048²≈2MB），
    尺寸纹丝不动；确实压不进预算再按比例缩（极端兜底）。
    """
    from io import BytesIO

    from PIL import Image

    if len(data) <= budget:
        return data
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001 - 解不开就原样交由 CI 报错
        return data
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    for quality in (92, 85, 78, 70):
        buf = BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if len(buf.getvalue()) <= budget:
            return buf.getvalue()
    # 极端兜底：降尺寸 + JPEG
    for scale in (0.75, 0.5):
        resized = im.resize((int(im.size[0] * scale), int(im.size[1] * scale)),
                            Image.LANCZOS)
        buf = BytesIO()
        resized.save(buf, "JPEG", quality=85)
        if len(buf.getvalue()) <= budget:
            return buf.getvalue()
    return buf.getvalue()
