"""A GitHub that records everything it is asked to do, and changes nothing real."""

from __future__ import annotations

from veto_agent.github import GitHubError


class FakeGitHub:
    def __init__(self, issues: list[dict] | None = None) -> None:
        self.open = list(issues or [])
        self.calls: list[tuple] = []
        self.fail_on: set[int] = set()

    def issues(self, repo: str, state: str = "open") -> list[dict]:
        self.calls.append(("GET", repo, state))
        return [dict(issue) for issue in self.open]

    def close(self, repo: str, number: int) -> dict:
        self.calls.append(("PATCH", repo, number))
        if number in self.fail_on:
            raise GitHubError(f"GitHub answered 403 for PATCH /repos/{repo}/issues/{number}")
        self.open = [issue for issue in self.open if issue["number"] != number]
        return {"number": number, "state": "closed"}

    @property
    def writes(self) -> list[tuple]:
        return [call for call in self.calls if call[0] != "GET"]
