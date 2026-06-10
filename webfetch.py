#!/usr/bin/env python3
"""One hardened HTTP layer for every fetcher in the pipeline.

All outbound requests go through ``get()`` so the guards live in exactly one
audited place: a hard response-size cap (memory / repo exhaustion), an
optional HTTPS + host allowlist with refused redirects for
attacker-influenced URLs (SSRF), and a bounded gunzip (zip bombs). A future
source (e.g. HyperKitty) inherits the full guard set by calling ``get()``
with its own policy instead of growing another bespoke fetcher.
"""
from __future__ import annotations

import urllib.request
import zlib
from urllib.parse import urlsplit

UA = "xymon-discussion-public/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: ARG002
        return None                       # refuse every redirect


_OPENERS = {
    True: urllib.request.build_opener(),
    False: urllib.request.build_opener(_NoRedirect),
}


def get(url: str, *, max_bytes: int, allowed_hosts=None,
        follow_redirects: bool = True, timeout: int = 60,
        ua: str = UA) -> tuple[bytes, object]:
    """GET ``url``; returns ``(body_bytes, headers)``.

    ``allowed_hosts`` additionally enforces HTTPS and refuses every other
    host -- set it (with ``follow_redirects=False``) whenever the URL is
    attacker-influenced. The body is capped at ``max_bytes``."""
    if allowed_hosts is not None:
        parts = urlsplit(url)
        if (parts.scheme != "https"
                or (parts.hostname or "").lower() not in allowed_hosts):
            raise ValueError(f"refusing non-allowlisted URL: {url}")
    resp = _OPENERS[bool(follow_redirects)].open(
        urllib.request.Request(url, headers={"User-Agent": ua}),
        timeout=timeout)
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes: {url}")
    return data, resp.headers


def gunzip_bounded(data: bytes, limit: int) -> bytes:
    """Decompress a gzip stream, aborting once output would exceed ``limit``
    -- a crafted (or corrupted) .gz can't expand to gigabytes and OOM the
    run before a size check fires."""
    d = zlib.decompressobj(31)             # 16 + MAX_WBITS -> gzip framing
    out = bytearray(d.decompress(data, limit + 1))
    while d.unconsumed_tail and len(out) <= limit:
        out += d.decompress(d.unconsumed_tail, limit + 1 - len(out))
    if len(out) > limit:
        raise ValueError(f"gzip expands beyond {limit} bytes")
    out += d.flush()
    if len(out) > limit:
        raise ValueError(f"gzip expands beyond {limit} bytes")
    return bytes(out)
