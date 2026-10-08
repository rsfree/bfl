"""bfl/images@v1: Black Forest Labs flux-tools precise editing, job-polled.

Channel setup (New API side):
  X-Upstream-Url:  https://flux-tools.bfl.ai/api/flux3-image/generate
  X-Script-Ref:    bfl/images@v1
  X-Auth-Emit:     none
  X-Async:         poll=2,timeout=300

  The upstream is BFL's own playground backend (flux-tools.bfl.ai). It is
  anonymous -- no credential, no account -- which is why the channel emits
  no auth. What it does require is that the submit call *looks like the
  playground*: POST without Origin/Referer/Sec-Fetch-Site is answered with
  an empty 403 (measured 2026-10-09), so the script emits those headers
  itself. The GET endpoints (status, media) tolerate their absence.

Wire contract (captured from the playground via a fetch hook, verified by
traffic the same day -- see docs/12):

  POST /api/flux3-image/generate   multipart: mode="i2i", prompt,
    aspectRatio ("auto"), resolution ("1.5k"), boxes (JSON string),
    image (png file), references (JSON string, always "[]")
    -> {"status":"submitted","requestId","pollToken","prompt"}
  GET  /api/flux3-image/status?pollToken=...
    -> {"status":"pending"} | {"status":"ready","imageToken"} |
       {"status":"error","error"}
  GET  /api/flux3-image/media?imageToken=...   -> image/png bytes

Decisions this script owns
--------------------------
  * Edit-only, loudly. mode="i2i" is the only mode the playground exposes;
    a request with no image is refused here (param=image) instead of being
    spent on a mode nobody verified. Fabricating a text-to-image call
    against an editing endpoint is how a channel bills for surprises.
  * One input image. The playground edits one canvas; two references is a
    different product and is refused with the count named.
  * Minimum 256x256, checked here. The upstream validates it after upload
    (422 "Images[0] dimensions must be at least 256x256 pixels", measured),
    so the same refusal is given first, locally, with the measured size --
    the request is not spent to learn what a ruler could have said.
  * `boxes` is the channel's reason to exist. Canonical door: a JSON list.
    Edits door: the same list serialized as a text field. Each box is
    {"box":{"x","y","w","h"}} in 0..1 normalized coordinates with an
    optional per-box "instruction"; out-of-range coordinates are clamped,
    not refused -- the playground clamps too, and a box a hair over the
    edge is a working edit, not a bad request. Coordinates that do not
    parse at all fail with the field named. A global prompt may be empty
    when at least one box carries an instruction; empty everywhere is
    refused (it would spend a generation to change nothing).
  * `mask` is dropped. BFL precise editing has no mask primitive -- regions
    are boxes with instructions -- and no in-script pixel analysis exists
    to derive a box honestly. Same ruling as the ark channel: not
    forwarded, not an error, visible in docs. Callers who want regions
    send `boxes`.
  * `size` is decorative and dropped: output follows the input image on an
    i2i channel. Explicit `aspect_ratio` / `resolution` strings are
    forwarded when given (the playground's own defaults, "auto" / "1.5k",
    are the only traffic-verified values; everything else rides on the
    upstream's own validation).
  * `n` is not forwarded; the gate aggregates.

The reply, and the token that must not travel
---------------------------------------------
The ready verdict carries only an `imageToken`. The media URL it names is
publicly fetchable but short-lived (a token minutes old already stalls:
measured 2026-10-09), so a raw media URL is never handed to the caller:

    b64_json (default)  media fetched and encoded here (ctx.image_b64)
    url                 fetched and re-hosted to object storage
                        (ctx.rehost_image); a deployment with no storage
                        degrades that image to b64_json rather than
                        returning a link that will die

`response_format` travels through the module-level table keyed by
request_id, popped on the reply -- the response phase cannot see the
client's body.

Provenance: contract measured 2026-10-09 against the live playground
(submit/poll/media + one end-to-end edit). Unknown: non-"auto" aspect
ratios, resolution tiers other than "1.5k", the `references` field's
non-empty shape, and the media token's exact TTL.
"""

import base64
import binascii
import json
import time
import uuid
from urllib.parse import urlsplit

#: Text fields of the submit form. The image rides separately as a file part.
MODE = "i2i"
DEFAULT_ASPECT = "auto"
DEFAULT_RESOLUTION = "1.5k"

MIN_EDGE = 256

#: The playground-shaped headers the submit POST refuses to go without.
#: `=ORIGIN=` is spelled into the real upstream origin at emit time -- which
#: is NOT the channel host when a local egress sidecar fronts the vendor.
BROWSER_HEADERS = {
    "Origin": "=ORIGIN=",
    "Referer": "=ORIGIN=/precise-editing",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

#: response_format parked per request id, consumed once by the reply.
_STATE = {}


def _derive(ctx):
    """(real origin, emitted base) for the three endpoints, sidecar-aware.

    The channel URL ends in `/generate`; the status and media siblings are
    its parent path. When the deployment fronts the upstream with a local
    impersonation egress (channel URL spelled
    `http://127.0.0.1:<port>/https/<host>/...`), that prefix belongs to the
    sidecar and must survive every emitted URL -- while the *browser*
    headers must name the real upstream origin, or the vendor reads them
    as cross-origin and refuses. Both facts fall out of one parse:

      https://flux-tools.bfl.ai/api/flux3-image/generate
        -> ("https://flux-tools.bfl.ai",
            "https://flux-tools.bfl.ai/api/flux3-image")
      http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image/generate
        -> ("https://flux-tools.bfl.ai",
            "http://127.0.0.1:8766/https/flux-tools.bfl.ai/api/flux3-image")

    The second element is what emitted URLs carry (sidecar prefix kept);
    the first is the origin the vendor expects to see in the headers.
    """
    parts = urlsplit(ctx.upstream_url)
    segments = [s for s in parts.path.split("/") if s]
    if not segments or segments[-1] != "generate":
        ctx.fail(
            "X-Upstream-Url must end with /api/flux3-image/generate "
            "(the status and media endpoints are derived from it)",
            code="channel_config_error",
        )
    parent = segments[:-1]
    if len(parent) >= 3 and parent[0] in ("https", "http") and "." in parent[1]:
        real_origin = f"{parent[0]}://{parent[1]}"
    else:
        real_origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    emitted_base = (
        f"{parts.scheme.lower()}://{parts.netloc.lower()}/" + "/".join(parent)
    )
    return real_origin, emitted_base


def _origin(ctx):
    """The origin the *vendor* expects in Origin/Referer headers."""
    return _derive(ctx)[0]


def _emitted_base(ctx):
    """Scheme + host + base path that emitted URLs must carry (sidecar kept)."""
    return _derive(ctx)[1]


def _with_origin(ctx, headers):
    """Browser headers with the channel's own host spelled in."""
    origin = _origin(ctx)
    return {k: v.replace("=ORIGIN=", origin) for k, v in headers.items()}


def _decode_ref(ctx, ref):
    """One image reference as raw bytes: data URI, bare base64 or URL."""
    if ref.startswith("data:"):
        head, sep, payload = ref.partition(",")
        if not sep:
            ctx.fail("'image' is a data URI with no payload", param="image")
        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError):
            ctx.fail("'image' is a data URI that is not base64", param="image")
    if ref.startswith("http"):
        return base64.b64decode(ref)
    try:
        return base64.b64decode(ref, validate=True)
    except (binascii.Error, ValueError):
        ctx.fail("'image' must be a URL, a data URI or base64", param="image")


async def _input_png(ctx, image):
    """The single input image as PNG bytes, guarded and normalized."""
    refs = image if isinstance(image, list) else [image]
    refs = [r for r in refs if isinstance(r, str) and r.strip()]
    if not refs:
        ctx.fail(
            "bfl precise editing is edit-only: mode i2i has no verified "
            "text-to-image path, so an input image is required",
            param="image",
        )
    if len(refs) > 1:
        ctx.fail(
            f"bfl precise editing takes exactly one input image, got {len(refs)}",
            param="image",
        )
    ref = refs[0]
    if ref.startswith("http"):
        # A URL reference is fetched through the checked download path.
        raw = base64.b64decode(await ctx.image_b64(ref))
    else:
        raw = _decode_ref(ctx, ref)
    info = await ctx.image.info(raw)
    if info["width"] < MIN_EDGE or info["height"] < MIN_EDGE:
        ctx.fail(
            "'image' must be at least 256x256 pixels, got "
            f"{info['width']}x{info['height']}",
            param="image",
        )
    if info.get("format") != "PNG":
        raw = await ctx.image.convert(raw, "PNG")
    return raw


def _boxes(ctx, raw):
    """The caller's boxes as the playground's JSON-string form."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            ctx.fail("'boxes' is not valid JSON", param="boxes")
    if not isinstance(raw, list):
        ctx.fail("'boxes' must be a JSON array", param="boxes")
    out = []
    for item in raw:
        box = item.get("box") if isinstance(item, dict) else None
        if not isinstance(box, dict):
            ctx.fail("each entry in 'boxes' needs a 'box' object", param="boxes")
        try:
            x, y = float(box["x"]), float(box["y"])
            w, h = float(box["w"]), float(box["h"])
        except (KeyError, TypeError, ValueError):
            ctx.fail(
                "each 'box' needs numeric x, y, w, h (normalized 0..1)",
                param="boxes",
            )
        out.append(
            {
                "id": item.get("id") or f"b-{uuid.uuid4().hex[:12]}",
                "box": {
                    "x": min(1.0, max(0.0, x)),
                    "y": min(1.0, max(0.0, y)),
                    "w": min(1.0, max(0.0, w)),
                    "h": min(1.0, max(0.0, h)),
                },
                "instruction": str(item.get("instruction", "")),
                "source": item.get("source", "user"),
            }
        )
    return out


async def _submit(ctx, payload):
    """The request phase: client body -> multipart submit call."""
    requested = payload.get("response_format")
    if requested == "url" or requested == "b64_json":
        _STATE[ctx.request_id] = requested

    png = await _input_png(ctx, payload.get("image"))
    boxes = _boxes(ctx, payload.get("boxes"))

    prompt = payload.get("prompt")
    prompt = prompt if isinstance(prompt, str) else ""
    if not prompt.strip() and not any(b["instruction"].strip() for b in boxes):
        ctx.fail(
            "a prompt is required (or boxes that each carry an instruction)",
            param="prompt",
        )

    fields = {
        "mode": MODE,
        "prompt": prompt,
        "aspectRatio": DEFAULT_ASPECT,
        "resolution": DEFAULT_RESOLUTION,
        "boxes": json.dumps(boxes),
        "references": "[]",
    }
    aspect = payload.get("aspect_ratio")
    if isinstance(aspect, str) and aspect.strip():
        fields["aspectRatio"] = aspect.strip()
    resolution = payload.get("resolution")
    if isinstance(resolution, str) and resolution.strip():
        fields["resolution"] = resolution.strip()

    ctx.emit(
        url=_emitted_base(ctx) + "/generate",
        headers=_with_origin(ctx, BROWSER_HEADERS),
        files={"image": ("image.png", png, "image/png")},
    )
    return fields


def _poll_request(ctx, handle):
    """Job handle -> the status call."""
    token = handle.get("pollToken") if isinstance(handle, dict) else None
    if not token:
        ctx.fail(
            "upstream returned no pollToken", code="upstream_invalid_response",
            status=502,
        )
    ctx.emit(
        url=_emitted_base(ctx) + "/status",
        method="GET",
        query={"pollToken": token},
    )
    return {}


def _poll_response(ctx, payload):
    """One status reply -> the poll verdict."""
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "ready":
        return {"done": True, "payload": payload}
    if status == "error":
        return {"error": payload.get("error") or "unknown upstream error"}
    return {"done": False}


async def _reply(ctx, payload):
    """The ready verdict -> the canonical envelope, token never exposed."""
    token = payload.get("imageToken")
    if not token:
        ctx.fail(
            "upstream reported ready without an imageToken",
            code="upstream_invalid_response",
            status=502,
        )
    media = _emitted_base(ctx) + "/media"
    item = {}
    if _STATE.pop(ctx.request_id, None) == "url":
        stored = await ctx.rehost_image(f"{media}?imageToken={token}")
        if stored is not None:
            item["url"] = stored
        else:
            # No object storage: a media URL is a short-lived signed token,
            # not a link we may hand over -- degrade to bytes instead.
            item["b64_json"] = await ctx.image_b64(f"{media}?imageToken={token}")
    else:
        item["b64_json"] = await ctx.image_b64(f"{media}?imageToken={token}")
    return {"created": int(time.time()), "data": [item]}


PHASES = ["request", "response", "poll_request", "poll_response"]


async def transform(ctx, payload, phase):
    if phase == "request":
        return await _submit(ctx, payload)
    if phase == "poll_request":
        return _poll_request(ctx, payload)
    if phase == "poll_response":
        return _poll_response(ctx, payload)
    return await _reply(ctx, payload)
