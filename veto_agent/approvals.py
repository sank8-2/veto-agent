"""The approval link: signed, expiring, and single-use by construction.

This is the stand-in for CIBA's push notification. The link carries nothing but
the action id, signed with the server's key, so a link the user invents is
rejected. It is single-use not because the token is burned, but because the row
it points at can only leave `pending` once: `Store.decide` is a conditional
update, so a second approval finds nothing to move.
"""

from __future__ import annotations

from .session import sign, unsign

PURPOSE = "approval"


def mint(action_id: str, key: str, ttl_seconds: int) -> str:
    return sign({"action": action_id, "purpose": PURPOSE}, key, ttl_seconds)


def read(token: str | None, key: str) -> str | None:
    """Return the action id, or None if the token is forged, expired, or not an approval."""
    payload = unsign(token, key)
    if not payload or payload.get("purpose") != PURPOSE:
        return None
    action_id = payload.get("action")
    return action_id if isinstance(action_id, str) and action_id else None


def link(base: str, action_id: str, key: str, ttl_seconds: int) -> str:
    return f"{base.rstrip('/')}/a/{mint(action_id, key, ttl_seconds)}"
