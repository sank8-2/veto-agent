"""The row is the veto: it starts pending and can only be decided once."""

from __future__ import annotations

import time

import pytest

from veto_agent.store import (
    APPROVED,
    DENIED,
    EXECUTED,
    EXPIRED,
    PENDING,
    Store,
)


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "actions.db"))


def make(store, ttl: int = 600) -> dict:
    return store.create("auth0|alice", "close_issues", "sank8-2/demo", "[1, 2]", ttl)


def test_a_new_action_is_pending(store):
    action = make(store)
    assert action["status"] == PENDING
    assert store.get(action["id"])["status"] == PENDING
    assert action["expires_at"] > action["created_at"]


def test_pending_lists_only_undecided_actions(store):
    first = make(store)
    second = make(store)
    store.decide(first["id"], APPROVED)
    pending = store.pending()
    assert [row["id"] for row in pending] == [second["id"]]


def test_pending_can_be_scoped_to_one_user(store):
    make(store)
    other = store.create("auth0|bob", "close_issues", "sank8-2/demo", "[]", 600)
    assert [row["id"] for row in store.pending("auth0|bob")] == [other["id"]]


def test_a_decision_moves_the_row_exactly_once(store):
    action = make(store)
    assert store.decide(action["id"], APPROVED) is True
    assert store.decide(action["id"], DENIED) is False
    assert store.get(action["id"])["status"] == APPROVED
    assert store.get(action["id"])["decided_at"] is not None


def test_a_denied_action_cannot_then_be_approved(store):
    action = make(store)
    assert store.decide(action["id"], DENIED) is True
    assert store.decide(action["id"], APPROVED) is False
    assert store.get(action["id"])["status"] == DENIED


def test_finish_records_the_result(store):
    action = make(store)
    store.decide(action["id"], APPROVED)
    store.finish(action["id"], EXECUTED, "closed #1, #2")
    row = store.get(action["id"])
    assert row["status"] == EXECUTED
    assert row["result"] == "closed #1, #2"


def test_expiry_only_touches_pending_rows_past_their_deadline(store):
    stale = make(store, ttl=-1)
    fresh = make(store, ttl=600)
    decided = make(store, ttl=-1)
    store.decide(decided["id"], APPROVED)

    assert store.expire_due() == 1
    assert store.get(stale["id"])["status"] == EXPIRED
    assert store.get(fresh["id"])["status"] == PENDING
    assert store.get(decided["id"])["status"] == APPROVED


def test_an_expired_action_can_no_longer_be_decided(store):
    action = make(store, ttl=-1)
    store.expire_due()
    assert store.decide(action["id"], APPROVED) is False


def test_recent_is_newest_first_and_per_user(store):
    first = make(store)
    time.sleep(0.01)
    second = make(store)
    store.create("auth0|bob", "close_issues", "sank8-2/demo", "[]", 600)
    ids = [row["id"] for row in store.recent("auth0|alice")]
    assert ids == [second["id"], first["id"]]
