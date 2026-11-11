"""PKCE: the proof that the app redeeming the code is the app that asked for it."""

from __future__ import annotations

import base64
import hashlib
import secrets


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_verifier(length: int = 64) -> str:
    """A random 43-128 character string, as RFC 7636 requires."""
    if not 43 <= length <= 128:
        raise ValueError("a code verifier must be between 43 and 128 characters")
    return _b64(secrets.token_bytes(length))[:length]


def code_challenge(verifier: str) -> str:
    """S256: base64url(sha256(verifier)) without padding."""
    if not verifier:
        raise ValueError("a code verifier cannot be empty")
    return _b64(hashlib.sha256(verifier.encode("ascii")).digest())
