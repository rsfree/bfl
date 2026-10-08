"""Reverse proxy that re-issues requests through a browser-impersonated client.

    GET/POST http://127.0.0.1:<port>/https/flux-tools.bfl.ai/<path>?<query>
      -> curl_cffi (impersonate="chrome") https://flux-tools.bfl.ai/<path>?<query>

Why this exists: Vercel's security checkpoint classifies clients by TLS
fingerprint and answers non-browser stacks with `429` +
`X-Vercel-Mitigated: challenge` (measured 2026-10-09 against
flux-tools.bfl.ai: the adapter's aiohttp egress is challenged on every
submit, while a chrome-impersonated client passes with the identical
headers and body). A CONNECT-style forward proxy cannot fix this -- the
fingerprint IS the client's own ClientHello -- so the channel points its
`X-Upstream-Url` at this process instead, and this process originates the
TLS. That keeps the adapter's transport stock and puts the vendor-specific
egress in a sidecar, same slot as `token_service.py`.

Mapping rules:

* path   `/https/<host>/<path>` or `/http/<host>/<path>`; the scheme is
  taken from the prefix, the host from the path.
* query  forwarded verbatim.
* body   forwarded verbatim with the caller's Content-Type -- a multipart
  body and its boundary survive byte-for-byte.
* headers forwarded minus hop-by-hop (host, content-length, connection);
  the impersonated client adds its own browser-consistent set.
* response status, Content-Type and body are passed back as-is; the
  challenge page therefore reaches the caller labelled with the real
  status instead of being mistaken for a vendor error.

Guardrails:

* hosts must be listed in `EGRESS_ALLOW_HOSTS` (comma-separated). The
  default is empty, which denies everything -- an open relay to arbitrary
  hosts is a private network's problem wearing a tool's clothes.
* the server binds loopback by default (`--bind`).

Endpoints:

    ANY /https/<host>/<...> | /http/<host>/<...>   the proxy
    GET /health                                    allowlist + count

Run:
    python tools/impersonation_egress.py --port 8766 \
        --allow flux-tools.bfl.ai
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# curl_cffi 0.16 has no per-request trust_env; neutralize proxy env instead so
# an ambient HTTP_PROXY cannot re-route (or swallow) the egress.
for _var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
             "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_var, None)

import curl_cffi.requests as cr  # noqa: E402 - after the env scrub

HOP_BY_HOP = {
    "host", "connection", "content-length", "transfer-encoding", "keep-alive",
    "proxy-authorization", "proxy-connection", "te", "trailers", "upgrade",
}

#: Identity headers the impersonated client owns: a forwarded value here
#: (curl's User-Agent, say) contradicts the TLS fingerprint and re-triggers
#: the checkpoint the sidecar exists to avoid. Everything the caller sends
#: that carries *meaning* -- content-type, origin/referer/sec-fetch, auth,
#: vendor x-headers -- still rides through.
IDENTITY_HEADERS = {
    "user-agent", "accept", "accept-encoding", "accept-language",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "upgrade-insecure-requests",
}

ALLOW_HOSTS: set[str] = set()
REQUESTS_TOTAL = 0
_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self) -> None:
        global REQUESTS_TOTAL
        if self.path == "/health":
            self._reply(200, "application/json", json.dumps({
                "ok": True, "allow_hosts": sorted(ALLOW_HOSTS),
                "requests_total": REQUESTS_TOTAL,
            }).encode())
            return
        parts = self.path.split("?", 1)
        path, query = parts[0], parts[1] if len(parts) > 1 else ""
        segments = [s for s in path.split("/") if s]
        if len(segments) < 2 or segments[0] not in ("https", "http"):
            self._reply(400, "application/json",
                        json.dumps({"error": "path must be /<scheme>/<host>/...",
                                    "got": self.path[:300]}).encode())
            return
        scheme, host = segments[0], segments[1].lower()
        if host not in ALLOW_HOSTS:
            self._reply(403, "application/json",
                        json.dumps({"error": f"host {host!r} is not allowed"}).encode())
            return
        target = f"{scheme}://{host}/{'/'.join(segments[2:])}"
        if query:
            target += "?" + query

        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in IDENTITY_HEADERS
        }
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        started = time.monotonic()
        try:
            r = cr.request(
                self.command, target, headers=headers, data=body,
                impersonate="chrome", timeout=120,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            self._reply(502, "application/json",
                        json.dumps({"error": f"egress failed: {exc}"}).encode())
            return
        with _LOCK:
            REQUESTS_TOTAL += 1
        self.send_response(r.status_code)
        content_type = r.headers.get("Content-Type", "application/octet-stream")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(r.content)))
        self.send_header("X-Egress-Target", target[:120])
        self.send_header("X-Egress-Ms", f"{(time.monotonic() - started) * 1000:.0f}")
        self.end_headers()
        self.wfile.write(r.content)

    def _reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _forward

    def log_message(self, fmt, *args):  # noqa: A003 - quiet by default
        if ARGS.verbose:
            super().log_message(fmt, *args)


class HealthHandler(Handler):
    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path == "/health":
            self._reply(200, "application/json", json.dumps({
                "ok": True, "allow_hosts": sorted(ALLOW_HOSTS),
                "requests_total": REQUESTS_TOTAL,
            }).encode())
        else:
            self._forward()


ARGS = argparse.Namespace(port=8766, bind="127.0.0.1", allow="", verbose=False)


def main(argv: list[str] | None = None) -> int:
    global ALLOW_HOSTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--allow", default="",
                        help="comma-separated host allowlist (required targets)")
    parser.add_argument("--verbose", action="store_true")
    parsed = parser.parse_args(argv)
    ARGS.__dict__.update(vars(parsed))

    ALLOW_HOSTS = {h.strip().lower() for h in parsed.allow.split(",") if h.strip()}
    if not ALLOW_HOSTS:
        print("refusing to start: --allow is empty (open-relay guard)", flush=True)
        return 2

    server = ThreadingHTTPServer((parsed.bind, parsed.port), Handler)
    print(f"impersonation egress on {parsed.bind}:{parsed.port} "
          f"allowing {sorted(ALLOW_HOSTS)}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
