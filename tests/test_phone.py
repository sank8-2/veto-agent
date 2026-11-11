"""The approval device over a real socket: an inbox, a decision, and one use."""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from veto_agent import approvals, phone
from veto_agent.agent import Agent
from veto_agent.config import Config
from veto_agent.store import APPROVED, EXPIRED, PENDING, Store

from .fakes import FakeGitHub

REPO = "sank8-2/demo"
KEY = "test-key"


def make_config() -> Config:
    return Config(
        domain="example.invalid",
        client_id="client",
        client_secret="secret",
        redirect_uri="http://localhost:3000/callback",
        secret_key=KEY,
        github_repo=REPO,
        approval_base="http://localhost:4100",
        approval_ttl=600,
    )


@pytest.fixture()
def env(tmp_path):
    config = make_config()
    store = Store(str(tmp_path / "actions.db"))
    github = FakeGitHub(
        [
            {"number": 1, "title": "Crash on start", "user": "alice"},
            {"number": 2, "title": "Typo in the docs", "user": "alice"},
        ]
    )
    agent = Agent(store, github, config)
    server = phone.serve(config, store, port=0, agent=agent)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, agent, github, store, config
    finally:
        server.shutdown()
        server.server_close()


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(url: str, decision: str) -> tuple[int, str]:
    body = urllib.parse.urlencode({"decision": decision}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_healthz_reports_the_queue(env):
    base, agent, _, _, _ = env
    agent.run("close all the issues", "auth0|alice")
    status, body = get(f"{base}/healthz")
    assert status == 200
    assert '"pending": 1' in body


def test_an_empty_inbox_says_so(env):
    base, _, _, _, _ = env
    status, body = get(f"{base}/")
    assert status == 200
    assert "Nothing is waiting for you" in body


def test_the_inbox_lists_a_pending_action_with_a_link(env):
    base, agent, _, _, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    status, body = get(f"{base}/")
    assert status == 200
    assert "close 2 issues in sank8-2/demo" in body
    assert "auth0|alice" in body
    assert "/a/" in body
    assert proposed["veto"]["id"][:8] not in body  # the page shows the request, not the row id


def test_the_decision_page_shows_both_buttons(env):
    base, agent, _, _, config = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], KEY, 600)
    status, body = get(f"{base}/a/{token}")
    assert status == 200
    assert "Approve" in body
    assert "Deny" in body
    assert "close 2 issues in sank8-2/demo" in body


def test_approving_executes_the_action_once(env):
    base, agent, github, store, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], KEY, 600)

    status, body = post(f"{base}/a/{token}", "approve")
    assert status == 200
    assert "executed" in body.lower()
    assert github.writes == [("PATCH", REPO, 1), ("PATCH", REPO, 2)]
    assert store.get(proposed["veto"]["id"])["status"] == "executed"

    status, body = post(f"{base}/a/{token}", "approve")
    assert "already used" in body
    assert github.writes == [("PATCH", REPO, 1), ("PATCH", REPO, 2)]


def test_reopening_a_used_link_reports_the_outcome_without_acting(env):
    base, agent, github, _, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], KEY, 600)
    post(f"{base}/a/{token}", "approve")

    status, body = get(f"{base}/a/{token}")
    assert status == 200
    assert "closed #1, #2" in body
    assert github.writes == [("PATCH", REPO, 1), ("PATCH", REPO, 2)]


def test_denying_never_touches_github(env):
    base, agent, github, store, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], KEY, 600)

    status, body = post(f"{base}/a/{token}", "deny")
    assert status == 200
    assert "Denied" in body
    assert github.writes == []
    assert store.get(proposed["veto"]["id"])["status"] == "denied"


def test_a_forged_link_is_refused(env):
    base, _, _, _, _ = env
    status, body = get(f"{base}/a/not-a-real-token")
    assert status == 400
    assert "forged" in body


def test_a_link_signed_with_another_key_is_refused(env):
    base, agent, _, _, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], "someone-elses-key", 600)
    status, body = get(f"{base}/a/{token}")
    assert status == 400
    assert "forged" in body


def test_an_expired_request_is_refused_even_with_a_live_link(env):
    base, _, github, store, _ = env
    stale = store.create("auth0|alice", "close_issues", REPO, "[1]", -1)
    token = approvals.mint(stale["id"], KEY, 600)  # the link is fine; the request is not

    status, body = post(f"{base}/a/{token}", "approve")
    assert status == 200
    assert "expired" in body.lower()
    assert store.get(stale["id"])["status"] == EXPIRED
    assert github.writes == []


def test_a_decision_that_is_not_approve_or_deny_is_refused(env):
    base, agent, github, store, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    token = approvals.mint(proposed["veto"]["id"], KEY, 600)
    status, body = post(f"{base}/a/{token}", "maybe")
    assert status == 400
    assert store.get(proposed["veto"]["id"])["status"] == PENDING
    assert github.writes == []


def test_unknown_address_is_404(env):
    base, _, _, _, _ = env
    status, _ = get(f"{base}/nope")
    assert status == 404


def test_the_inbox_hides_decided_actions(env):
    base, agent, _, store, _ = env
    proposed = agent.run("close all the issues", "auth0|alice")
    store.decide(proposed["veto"]["id"], APPROVED)
    status, body = get(f"{base}/")
    assert status == 200
    assert "Nothing is waiting for you" in body
