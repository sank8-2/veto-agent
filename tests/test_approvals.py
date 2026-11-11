"""The approval link: signed, expiring, and useless if it was not minted here."""

from __future__ import annotations

from veto_agent import approvals

KEY = "test-key"


def test_a_minted_token_reads_back_the_action():
    token = approvals.mint("abc123", KEY, 600)
    assert approvals.read(token, KEY) == "abc123"


def test_a_token_signed_with_another_key_is_rejected():
    token = approvals.mint("abc123", "not-the-key", 600)
    assert approvals.read(token, KEY) is None


def test_a_tampered_token_is_rejected():
    token = approvals.mint("abc123", KEY, 600)
    encoded, signature = token.rsplit(".", 1)
    forged = encoded[:-2] + ("AA" if encoded[-2:] != "AA" else "BB") + "." + signature
    assert approvals.read(forged, KEY) is None


def test_a_token_with_a_swapped_signature_is_rejected():
    first = approvals.mint("abc123", KEY, 600)
    second = approvals.mint("def456", KEY, 600)
    encoded = first.rsplit(".", 1)[0]
    signature = second.rsplit(".", 1)[1]
    assert approvals.read(f"{encoded}.{signature}", KEY) is None


def test_an_expired_token_is_rejected():
    assert approvals.read(approvals.mint("abc123", KEY, -1), KEY) is None


def test_a_token_with_another_purpose_is_rejected():
    from veto_agent.session import sign

    token = sign({"action": "abc123", "purpose": "session"}, KEY, 600)
    assert approvals.read(token, KEY) is None


def test_a_token_without_an_action_is_rejected():
    from veto_agent.session import sign

    token = sign({"purpose": "approval"}, KEY, 600)
    assert approvals.read(token, KEY) is None


def test_nothing_at_all_is_rejected():
    assert approvals.read(None, KEY) is None
    assert approvals.read("", KEY) is None
    assert approvals.read("not-a-token", KEY) is None


def test_the_link_points_at_the_approval_device():
    link = approvals.link("http://localhost:4100/", "abc123", KEY, 600)
    assert link.startswith("http://localhost:4100/a/")
    assert approvals.read(link.rsplit("/", 1)[1], KEY) == "abc123"
