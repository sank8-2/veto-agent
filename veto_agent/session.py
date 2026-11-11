"""A signed cookie, small enough to read.

The browser holds two of these: one while a login is in flight (state and PKCE
verifier), and one once it succeeds (who you are, and the access token the
assistant presents to the first-party API). Both are signed with HMAC-SHA256,
so a value the browser invents is rejected rather than trusted.

A real deployment would keep the token server-side and hand the browser an
opaque session id; a local demo keeps it here so the whole flow stays readable
in one file.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign(payload: dict[str, Any], key: str, ttl_seconds: int = 600) -> str:
    body = dict(payload)
    body["exp"] = int(time.time()) + ttl_seconds
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def unsign(token: str | None, key: str) -> dict[str, Any] | None:
    """Return the payload, or None if it was tampered with or has expired."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        raw = _unb64(encoded)
        expected = hmac.new(key.encode("utf-8"), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature)):
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload
