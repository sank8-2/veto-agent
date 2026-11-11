"""The asymmetry: reads go straight through, writes wait, and a write happens once."""

from __future__ import annotations

import pytest

from veto_agent.agent import Agent, AgentError
from veto_agent.config import Config
from veto_agent.store import APPROVED, DENIED, EXECUTED, FAILED, PENDING, Store

from .fakes import FakeGitHub

REPO = "sank8-2/demo"


@pytest.fixture()
def env(tmp_path):
    config = Config(
        domain="example.invalid",
        client_id="client",
        client_secret="secret",
        redirect_uri="http://localhost:3000/callback",
        secret_key="test-key",
        github_repo=REPO,
        approval_base="http://localhost:4100",
        approval_ttl=600,
    )
    github = FakeGitHub(
        [
            {"number": 1, "title": "Crash on start", "user": "alice"},
            {"number": 2, "title": "Typo in the docs", "user": "alice"},
        ]
    )
    store = Store(str(tmp_path / "actions.db"))
    return Agent(store, github, config), github, store, config


# -- reads ----------------------------------------------------------------


def test_listing_issues_reads_and_writes_nothing(env):
    agent, github, store, _ = env
    result = agent.run("what is open?", "auth0|alice")

    assert "#1 Crash on start" in result["reply"]
    assert "#2 Typo in the docs" in result["reply"]
    assert result["veto"] is None
    assert github.writes == []
    assert store.recent("auth0|alice") == []


def test_listing_records_the_call_it_made(env):
    agent, _, _, _ = env
    result = agent.run("show me the issues", "auth0|alice")
    assert [call["request"] for call in result["calls"]] == [f"GET /repos/{REPO}/issues"]
    assert result["calls"][0]["status"] == 200


def test_a_repository_can_be_named_in_the_message(env):
    agent, github, _, _ = env
    agent.run("what is open in other/repo?", "auth0|alice")
    assert github.calls == [("GET", "other/repo", "open")]


def test_no_repository_anywhere_is_an_error(tmp_path):
    config = Config(
        domain="example.invalid",
        client_id="client",
        client_secret="secret",
        redirect_uri="http://localhost:3000/callback",
        secret_key="test-key",
    )
    agent = Agent(Store(str(tmp_path / "a.db")), FakeGitHub(), config)
    with pytest.raises(AgentError, match="which repository"):
        agent.run("what is open?", "auth0|alice")


# -- the veto -------------------------------------------------------------


def test_closing_halts_without_touching_github(env):
    """The whole challenge in one assertion."""
    agent, github, store, _ = env
    result = agent.run("close all the open issues", "auth0|alice")

    assert github.writes == []
    assert result["veto"] is not None
    assert result["veto"]["status"] == PENDING
    assert "not sent anything to GitHub" in result["reply"]
    assert "http://localhost:4100/a/" in result["reply"]

    pending = store.pending("auth0|alice")
    assert len(pending) == 1
    assert pending[0]["status"] == PENDING
    assert pending[0]["detail"] == "[1, 2]"


def test_the_proposal_still_reads_first_so_it_can_be_specific(env):
    agent, github, _, _ = env
    result = agent.run("close them", "auth0|alice")
    assert github.writes == []
    assert "close 2 issues in sank8-2/demo (#1, #2)" in result["reply"]
    assert github.calls == [("GET", REPO, "open")]


def test_approval_executes_exactly_once(env):
    agent, github, store, _ = env
    proposed = agent.run("close all the open issues", "auth0|alice")
    action_id = proposed["veto"]["id"]

    first = agent.decide(action_id, approve=True)
    assert first["ok"] is True
    assert first["reason"] == "executed"
    assert first["status"] == EXECUTED
    assert github.writes == [("PATCH", REPO, 1), ("PATCH", REPO, 2)]
    assert store.get(action_id)["status"] == EXECUTED
    assert store.get(action_id)["result"] == "closed #1, #2"

    second = agent.decide(action_id, approve=True)
    assert second["ok"] is False
    assert second["reason"] == "not_pending"
    assert github.writes == [("PATCH", REPO, 1), ("PATCH", REPO, 2)]


def test_denial_never_reaches_github(env):
    agent, github, store, _ = env
    proposed = agent.run("close all the open issues", "auth0|alice")
    outcome = agent.decide(proposed["veto"]["id"], approve=False)

    assert outcome["ok"] is True
    assert outcome["reason"] == "denied"
    assert github.writes == []
    assert store.get(proposed["veto"]["id"])["status"] == DENIED


def test_a_denied_action_cannot_be_revived_by_a_second_approval(env):
    agent, github, _, _ = env
    proposed = agent.run("close all the open issues", "auth0|alice")
    agent.decide(proposed["veto"]["id"], approve=False)
    again = agent.decide(proposed["veto"]["id"], approve=True)
    assert again["ok"] is False
    assert github.writes == []


def test_a_specific_issue_closes_only_that_one(env):
    agent, github, _, _ = env
    proposed = agent.run("close issue #2", "auth0|alice")
    assert proposed["veto"]["summary"] == "close 1 issue in sank8-2/demo (#2)"
    agent.decide(proposed["veto"]["id"], approve=True)
    assert github.writes == [("PATCH", REPO, 2)]


def test_asking_to_close_something_that_is_not_open_is_an_error(env):
    agent, github, _, _ = env
    with pytest.raises(AgentError, match="not open"):
        agent.run("close #99", "auth0|alice")
    assert github.writes == []


def test_closing_when_nothing_is_open_creates_no_action(env):
    agent, github, store, _ = env
    github.open = []
    result = agent.run("close all the issues", "auth0|alice")
    assert result["veto"] is None
    assert "nothing to close" in result["reply"]
    assert store.pending("auth0|alice") == []


def test_a_failed_write_is_recorded_as_failed(env):
    agent, github, store, _ = env
    github.fail_on = {2}
    proposed = agent.run("close all the open issues", "auth0|alice")
    outcome = agent.decide(proposed["veto"]["id"], approve=True)

    assert outcome["status"] == FAILED
    row = store.get(proposed["veto"]["id"])
    assert row["status"] == FAILED
    assert "#2" in row["result"]
    assert "#1" in row["result"]


def test_an_unknown_action_cannot_be_decided(env):
    agent, github, _, _ = env
    outcome = agent.decide("nope", approve=True)
    assert outcome["ok"] is False
    assert outcome["reason"] == "unknown_action"
    assert github.writes == []


def test_two_users_get_their_own_pending_rows(env):
    agent, _, store, _ = env
    agent.run("close all the open issues", "auth0|alice")
    agent.run("close all the open issues", "auth0|bob")
    assert len(store.pending("auth0|alice")) == 1
    assert len(store.pending("auth0|bob")) == 1
    assert store.pending("auth0|alice")[0]["sub"] == "auth0|alice"


def test_a_pronoun_after_close_still_halts(env):
    """"close all of them" names no issue and no number, and must still stop."""
    agent, github, store, _ = env
    result = agent.run("close all of them", "auth0|alice")
    assert result["veto"] is not None
    assert github.writes == []
    assert len(store.pending("auth0|alice")) == 1


def test_a_question_mentioning_close_halts_rather_than_acting(env):
    """When the wording is ambiguous the agent halts: over-asking is safe, over-acting is not."""
    agent, github, store, _ = env
    result = agent.run("how many issues did you close yesterday?", "auth0|alice")
    assert result["veto"] is not None
    assert github.writes == []
    assert len(store.pending("auth0|alice")) == 1


def test_an_empty_message_is_refused(env):
    agent, _, _, _ = env
    with pytest.raises(AgentError, match="say something"):
        agent.run("   ", "auth0|alice")
