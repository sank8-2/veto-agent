"""The chat surface: a turn, a halt, and no way to approve from here.

The session cookie is signed directly with the test key, so these tests exercise
the chat without standing up an identity provider. Everything else — the
handler, the store, the agent — is the real thing over a real socket.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from veto_agent import approvals
from veto_agent.agent import Agent
from veto_agent.app import make_handler
from veto_agent.config import Config
from veto_agent.session import sign
from veto_agent.store import PENDING, Store

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
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(config, store, agent=agent)
    )
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, agent, github, store, config
    finally:
        server.shutdown()
        server.server_close()


def cookie(config: Config, sub: str = "auth0|alice") -> str:
    token = sign(
        {"sub": sub, "name": "Alice", "email": "alice@example.com", "access_token": "unused"},
        config.secret_key,
        3600,
    )
    return f"veto_session={token}"


def get(url: str, cookie_value: str | None = None) -> tuple[int, str]:
    headers = {"Cookie": cookie_value} if cookie_value else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post_json(url: str, payload, cookie_value: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if cookie_value:
        headers["Cookie"] = cookie_value
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body.strip().startswith("{") else {"body": body}


def test_signed_out_shows_a_login_button(env):
    base, _, _, _, _ = env
    status, body = get(f"{base}/")
    assert status == 200
    assert "Log in" in body
    assert "example.invalid" in body


def test_signed_in_shows_the_chat(env):
    base, _, _, _, config = env
    status, body = get(f"{base}/", cookie(config))
    assert status == 200
    assert "auth0|alice" in body
    assert REPO in body


def test_healthz(env):
    base, _, _, _, _ = env
    status, body = get(f"{base}/healthz")
    assert status == 200
    assert json.loads(body)["status"] == "ok"


def test_a_question_is_answered_without_asking_anyone(env):
    base, _, github, store, config = env
    status, payload = post_json(f"{base}/api/chat", {"message": "what is open?"}, cookie(config))
    assert status == 200
    assert "#1 Crash on start" in payload["reply"]
    assert payload["veto"] is None
    assert github.writes == []
    assert store.pending("auth0|alice") == []


def test_a_close_command_halts_and_writes_nothing(env):
    base, _, github, store, config = env
    status, payload = post_json(
        f"{base}/api/chat", {"message": "close all the issues"}, cookie(config)
    )
    assert status == 200
    assert payload["veto"] is not None
    assert payload["veto"]["status"] == PENDING
    assert "not sent anything to GitHub" in payload["reply"]
    assert payload["state"]["actions"][0]["status"] == PENDING
    assert github.writes == []
    assert len(store.pending("auth0|alice")) == 1


def test_the_chat_has_no_route_that_can_decide(env):
    """If a prompt injection can ask, it still cannot consent."""
    base, _, github, store, config = env
    _, payload = post_json(f"{base}/api/chat", {"message": "close all the issues"}, cookie(config))
    action_id = payload["veto"]["id"]

    for path in ("/api/decide", "/api/approve", "/api/actions/approve", "/approve"):
        status, _ = post_json(f"{base}{path}", {"id": action_id, "decision": "approve"}, cookie(config))
        assert status == 404, path

    assert store.get(action_id)["status"] == PENDING
    assert github.writes == []


def test_the_approval_link_does_not_work_on_the_chat_port(env):
    base, agent, github, _, config = env
    _, payload = post_json(f"{base}/api/chat", {"message": "close all the issues"}, cookie(config))
    token = approvals.mint(payload["veto"]["id"], KEY, 600)

    status, _ = get(f"{base}/a/{token}", cookie(config))
    assert status == 404
    assert github.writes == []


def test_chat_needs_a_session(env):
    base, _, _, _, _ = env
    status, payload = post_json(f"{base}/api/chat", {"message": "what is open?"})
    assert status == 401
    assert payload["error"] == "not_signed_in"


def test_state_needs_a_session(env):
    base, _, _, _, _ = env
    status, _ = get(f"{base}/api/state")
    assert status == 401


def test_state_shows_a_decision_made_elsewhere(env):
    base, agent, _, _, config = env
    _, payload = post_json(f"{base}/api/chat", {"message": "close all the issues"}, cookie(config))
    agent.decide(payload["veto"]["id"], approve=True)

    status, body = get(f"{base}/api/state", cookie(config))
    assert status == 200
    actions = json.loads(body)["actions"]
    assert actions[0]["status"] == "executed"
    assert actions[0]["result"] == "closed #1, #2"


def test_a_bad_body_is_refused(env):
    base, _, _, _, config = env
    status, payload = post_json(f"{base}/api/chat", ["not", "an", "object"], cookie(config))
    assert status == 400


def test_one_users_state_is_not_anothers(env):
    base, _, _, _, config = env
    post_json(f"{base}/api/chat", {"message": "close all the issues"}, cookie(config))
    status, body = get(f"{base}/api/state", cookie(config, sub="auth0|bob"))
    assert status == 200
    assert json.loads(body)["actions"] == []
