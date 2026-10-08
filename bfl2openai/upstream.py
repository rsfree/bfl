"""Upstream client for flux-tools.bfl.ai /api/flux3-image/* (reverse-engineered).

Contract (captured from the official playground via browser fetch hook):

1. POST /api/flux3-image/generate          multipart/form-data, anonymous
       mode         = "i2i"
       prompt       = <global instruction, may be empty when boxes carry instructions>
       aspectRatio  = "auto" | "<w>:<h>"
       resolution   = "1.5k"
       boxes        = JSON string: [{"id":"b-x","box":{"x":0.3,"y":0.3,"w":0.4,"h":0.4},
                                     "instruction":"...","source":"user"}]   (normalized 0..1)
       image        = PNG file (full current canvas)
       references   = JSON string (usually "[]")
   -> 200 {"status":"submitted","requestId":uuid,"pollToken":...,"prompt":...}

2. GET /api/flux3-image/status?pollToken=...
       -> {"status":"pending"}
       -> {"status":"ready","imageToken":...}
       -> {"status":"error","error":...}

3. GET /api/flux3-image/media?imageToken=...  -> image bytes (PNG)

Endpoints are anonymous; the site is fronted by a Vercel security checkpoint that
a chrome-impersonated TLS client passes (verified with curl_cffi).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

log = logging.getLogger("bfl2openai.upstream")

DEFAULT_BASE = "https://flux-tools.bfl.ai"
POLL_INTERVAL_S = 2.0
POLL_TIMEOUT_S = 300.0
SUBMIT_TIMEOUT_S = 120.0
MEDIA_TIMEOUT_S = 300.0


def _load_project_env() -> None:
    """Reads the deployment .env next to the package (missing file is fine).

    Runs at import so every module-level env read below (FALLBACK_PROXY
    included) sees the file. Real environment variables win: setdefault.
    """
    path = Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_project_env()

#: Optional fallback egress for resilient GETs (media / reference fetches).
#: When the direct attempts exhaust themselves on 429/403/503, the same GET
#: retries through this proxy (a rotating pool URL works: new exit IP + fresh
#: session). Empty = direct retries only. The pool's verified spelling is
#: socks5h (its HTTP entry is flaky; measured 2026-10-09):
#:   socks5h://<user>:<pass>@pool.livetest.cn:2088
FALLBACK_PROXY = os.environ.get("BFL2OPENAI_FALLBACK_PROXY", "")

_DIRECT_ATTEMPTS = 3
_PROXY_ATTEMPTS = 2
#: Transport-ish failures that a retry (or a different exit) can fix. A 400
#: "invalid token" is an answer, not an outage, and returns immediately.
_RETRY_STATUSES = frozenset({429, 403, 503})


class UpstreamError(Exception):
    """Upstream returned a business error."""

    def __init__(self, message: str, kind: str = "upstream_error"):
        super().__init__(message)
        self.message = message
        self.kind = kind


def _session() -> AsyncSession:
    # impersonate chrome so the Vercel checkpoint lets us through;
    # trust_env=False so HTTP_PROXY cannot swallow authenticated calls.
    return AsyncSession(impersonate="chrome", trust_env=False, timeout=SUBMIT_TIMEOUT_S)


def _make_session(proxy: str | None = None, timeout: float | None = None) -> AsyncSession:
    """One attempt's session; a factory (not inline) so tests can fake it."""
    if proxy:
        return AsyncSession(impersonate="chrome", trust_env=False,
                            proxy=proxy, timeout=timeout or MEDIA_TIMEOUT_S)
    return _session()


async def _resilient_get(url: str, params: dict | None = None,
                         timeout: float = MEDIA_TIMEOUT_S,
                         label: str = "GET") -> bytes:
    """GET with retry + optional proxy fallback.

    Sequence: 3 direct attempts (impersonated session, 1s/3s backoff -- these
    429s are rate-limit flavoured and clear on their own), then, when
    FALLBACK_PROXY is configured, 2 attempts through the proxy (fresh session,
    different exit IP). Any other status returns immediately.
    """
    attempts: list[tuple[str, AsyncSession]] = [
        (f"direct#{i + 1}", _make_session()) for i in range(_DIRECT_ATTEMPTS)
    ]
    if FALLBACK_PROXY:
        attempts.extend(
            (f"proxy#{i + 1}", _make_session(proxy=FALLBACK_PROXY, timeout=timeout))
            for i in range(_PROXY_ATTEMPTS)
        )

    last = "no attempts"
    for index, (attempt_label, session) in enumerate(attempts):
        try:
            async with session as s:
                r = await s.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.content
            last = f"HTTP {r.status_code}: {r.text[:120]}"
            if r.status_code not in _RETRY_STATUSES:
                raise UpstreamError(f"{label} failed: {last}")
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport errors retry
            last = f"{type(exc).__name__}: {exc}"
        log.warning("%s %s via %s failed (%s)", label, url[:80], attempt_label, last)
        if index < len(attempts) - 1:
            if index == _DIRECT_ATTEMPTS - 1:
                await asyncio.sleep(3.0)   # before the first proxy attempt
            else:
                await asyncio.sleep(1.0)
    raise UpstreamError(f"{label} failed after {len(attempts)} attempts: {last}")


def _browser_headers(base_url: str, referer_path: str = "/precise-editing") -> dict[str, str]:
    # POST /api/flux3-image/generate returns an empty 403 without these
    # same-origin markers; GET endpoints tolerate their absence.
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "origin": base_url,
        "referer": f"{base_url}{referer_path}",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


async def submit_generation(
    image_png: bytes,
    prompt: str,
    boxes: list[dict[str, Any]] | None = None,
    references: list[dict[str, Any]] | None = None,
    aspect_ratio: str = "auto",
    resolution: str = "1.5k",
    mode: str = "i2i",
    base_url: str = DEFAULT_BASE,
) -> str:
    """Submit an edit job, return the pollToken."""
    fields: list[tuple[str, Any]] = [
        ("mode", mode),
        ("prompt", prompt),
        ("aspectRatio", aspect_ratio),
        ("resolution", resolution),
        ("boxes", json.dumps(boxes or [])),
        ("references", json.dumps(references or [])),
    ]
    mp = CurlMime()
    for name, value in fields:
        mp.addpart(name=name, data=str(value).encode())
    mp.addpart(name="image", filename="image.png", content_type="image/png", data=image_png)
    async with _session() as s:
        r = await s.post(
            f"{base_url}/api/flux3-image/generate",
            multipart=mp,
            headers=_browser_headers(base_url),
        )
    text = r.text
    if r.status_code != 200 or "Security Checkpoint" in text:
        raise UpstreamError(f"generate failed: HTTP {r.status_code}: {text[:300]}")
    data = r.json()
    if data.get("status") != "submitted" or not data.get("pollToken"):
        raise UpstreamError(f"generate rejected: {text[:300]}")
    return data["pollToken"]


async def poll_once(poll_token: str, base_url: str = DEFAULT_BASE) -> dict[str, Any]:
    async with _session() as s:
        r = await s.get(
            f"{base_url}/api/flux3-image/status",
            params={"pollToken": poll_token},
            timeout=60,
        )
    if r.status_code != 200:
        raise UpstreamError(f"status failed: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


async def wait_for_result(
    poll_token: str,
    base_url: str = DEFAULT_BASE,
    on_poll: Any = None,
) -> str:
    """Poll until ready; return imageToken. Raises UpstreamError on error/timeout."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = await poll_once(poll_token, base_url)
        status = last.get("status")
        if status == "ready":
            token = last.get("imageToken")
            if not token:
                raise UpstreamError(f"ready without imageToken: {last}")
            return token
        if status == "error":
            msg = last.get("error") or "unknown upstream error"
            kind = "invalid_request_error" if "token" in str(msg).lower() else "upstream_error"
            raise UpstreamError(str(msg), kind)
        if callable(on_poll):
            on_poll(last)
        await asyncio.sleep(POLL_INTERVAL_S)
    raise UpstreamError(f"timed out after {POLL_TIMEOUT_S:.0f}s polling {poll_token[:8]}… (last={last})")


async def fetch_media(image_token: str, base_url: str = DEFAULT_BASE) -> bytes:
    """Fetch the generated image, with retry + proxy fallback on 429/403/503."""
    return await _resilient_get(
        f"{base_url}/api/flux3-image/media",
        params={"imageToken": image_token},
        label="media",
    )


async def fetch_url(url: str, timeout: float = MEDIA_TIMEOUT_S) -> bytes:
    """GET an absolute URL through the impersonated session (reference images)."""
    return await _resilient_get(url, label="fetch")


def new_box_id() -> str:
    return f"b-{uuid.uuid4().hex[:12]}"
