"""The agent: reads immediately, acts only after someone outside the chat agrees.

The whole point of this file is the asymmetry. A question like "what is open?"
is answered straight away, because reading changes nothing. A command like
"close those issues" is *not* executed: it becomes a row in the store and a
message that says, in as many words, that GitHub has not been touched. The only
way that row moves is through the approval surface, and only once.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import approvals
from .config import Config
from .github import GitHub, GitHubError
from .store import APPROVED, DENIED, EXECUTED, FAILED, PENDING, Store

REPO = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
NUMBER = re.compile(r"#(\d+)")
CLOSE = re.compile(r"\b(close|closing|shut|resolve|delete|archive)\b", re.I)
ISSUE = re.compile(r"\b(issue|issues|ticket|tickets|bug|bugs)\b", re.I)
# "close all of them" is the same request as "close all the issues".
PRONOUN = re.compile(r"\b(them|those|these)\b", re.I)


class AgentError(RuntimeError):
    """A turn could not be completed, and this is why."""


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _repo_for(text: str, config: Config) -> str:
    match = REPO.search(text)
    if match:
        return match.group(1)
    if config.github_repo:
        return config.github_repo
    raise AgentError(
        "tell me which repository, as owner/name (or set GITHUB_REPO in the environment)"
    )


def _plural(count: int) -> str:
    return "issue" if count == 1 else "issues"


def describe(action: dict) -> str:
    try:
        numbers = json.loads(action.get("detail") or "[]")
    except ValueError:
        numbers = []
    listed = ", ".join(f"#{number}" for number in numbers) or "nothing"
    return f"close {len(numbers)} {_plural(len(numbers))} in {action['repo']} ({listed})"


class Agent:
    def __init__(
        self,
        store: Store,
        github: GitHub,
        config: Config,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.store = store
        self.github = github
        self.config = config
        self.opener = opener

    # -- the veto -----------------------------------------------------------

    def propose(self, sub: str, repo: str, numbers: list[int], preview: dict) -> dict:
        """Record what the user asked for. Nothing is sent to GitHub here."""
        action = self.store.create(
            sub=sub,
            kind="close_issues",
            repo=repo,
            detail=json.dumps(numbers),
            ttl=self.config.approval_ttl,
        )
        link = approvals.link(
            self.config.approval_base, action["id"], self.config.secret_key, self.config.approval_ttl
        )
        minutes = max(1, self.config.approval_ttl // 60)
        reply = (
            f"Veto. That is a destructive command, so I have not sent anything to GitHub — "
            f"not one request. I wrote it down as pending action {action['id'][:8]} instead.\n\n"
            f"  {describe(action)}\n\n"
            f"Approve or deny it on the other surface, within {minutes} minutes:\n"
            f"  {link}\n\n"
            "There is no approve button here and no endpoint that can decide it: a prompt "
            "injection in this chat can ask, but it cannot consent."
        )
        return {
            "reply": reply,
            "calls": preview["calls"],
            "veto": {
                "id": action["id"],
                "status": action["status"],
                "repo": action["repo"],
                "summary": describe(action),
                "expires_at": action["expires_at"],
                "link": link,
            },
        }

    # -- the decision -------------------------------------------------------

    def decide(self, action_id: str, approve: bool) -> dict:
        """Called only by the approval surface, never by the chat."""
        action = self.store.get(action_id)
        if action is None:
            return {"ok": False, "reason": "unknown_action", "action": None}
        if action["status"] != PENDING:
            # A second approval of the same link lands here: the row already moved.
            return {"ok": False, "reason": "not_pending", "action": action}
        if action["expires_at"] < time.time():
            self.store.expire_due()
            return {"ok": False, "reason": "expired", "action": self.store.get(action_id)}
        if not self.store.decide(action_id, APPROVED if approve else DENIED):
            return {"ok": False, "reason": "not_pending", "action": self.store.get(action_id)}
        if not approve:
            return {"ok": True, "reason": "denied", "action": self.store.get(action_id)}
        return {"ok": True, "reason": "executed", **self.execute(action)}

    def execute(self, action: dict) -> dict:
        """Close exactly the issues frozen into the action, and record what happened."""
        try:
            numbers = json.loads(action.get("detail") or "[]")
        except ValueError:
            numbers = []
        closed: list[int] = []
        failed: list[str] = []
        started = time.perf_counter()
        for number in numbers:
            try:
                self.github.close(action["repo"], int(number))
                closed.append(int(number))
            except GitHubError as exc:
                failed.append(f"#{number}: {exc}")
        elapsed = _ms(started)

        if failed:
            done = ", ".join(f"#{number}" for number in closed) or "none"
            result = f"closed {done}; failed: {'; '.join(failed)}"
            self.store.finish(action["id"], FAILED, result)
            status = FAILED
        else:
            result = "closed " + (", ".join(f"#{number}" for number in closed) or "nothing")
            self.store.finish(action["id"], EXECUTED, result)
            status = EXECUTED

        return {
            "action": self.store.get(action["id"]),
            "status": status,
            "result": result,
            "calls": [
                {
                    "service": "github",
                    "request": f"PATCH /repos/{action['repo']}/issues (x{len(closed)})",
                    "status": 200 if not failed else 502,
                    "ms": elapsed,
                    "detail": result,
                }
            ],
        }

    # -- a turn -------------------------------------------------------------

    def run(self, message: str, sub: str) -> dict:
        text = message.strip()
        if not text:
            raise AgentError("say something first")
        repo = _repo_for(text, self.config)

        started = time.perf_counter()
        try:
            issues = self.github.issues(repo)
        except GitHubError as exc:
            raise AgentError(str(exc)) from exc
        read_call = {
            "service": "github",
            "request": f"GET /repos/{repo}/issues",
            "status": 200,
            "ms": _ms(started),
            "detail": f"{len(issues)} open {_plural(len(issues))}",
        }

        destructive = CLOSE.search(text) and (
            ISSUE.search(text) or NUMBER.search(text) or PRONOUN.search(text)
        )
        if not destructive:
            if not issues:
                reply = f"There are no open issues in {repo}."
            else:
                listing = "\n".join(
                    f"  #{issue['number']} {issue['title']}" for issue in issues
                )
                reply = (
                    f"{len(issues)} open {_plural(len(issues))} in {repo}:\n{listing}\n\n"
                    "Reading costs nothing, so I did that without asking. Say the word and "
                    "I will propose closing them — which is where I stop and wait."
                )
            return {"reply": reply, "calls": [read_call], "veto": None}

        wanted = {int(number) for number in NUMBER.findall(text)}
        targets = sorted(
            (issue for issue in issues if not wanted or issue["number"] in wanted),
            key=lambda issue: issue["number"],
        )
        missing = sorted(wanted - {issue["number"] for issue in targets})
        if missing:
            raise AgentError(
                "not open in this repository: " + ", ".join(f"#{number}" for number in missing)
            )
        if not targets:
            return {
                "reply": f"There is nothing to close in {repo} — no open issues.",
                "calls": [read_call],
                "veto": None,
            }

        return self.propose(
            sub,
            repo,
            [issue["number"] for issue in targets],
            {"calls": [read_call], "issues": targets},
        )
