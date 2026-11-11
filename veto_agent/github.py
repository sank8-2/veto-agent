"""GitHub's REST API, called with a token only the server holds.

The agent reads with this and, when it is allowed to act, writes with this. The
token never goes near the browser: the chat and the phone both talk to this
process, and this process talks to GitHub.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable

USER_AGENT = "veto-agent/0.1 (+https://github.com/sank8-2/veto-agent)"


class GitHubError(RuntimeError):
    """GitHub refused something, and this is what it said."""


class GitHub:
    def __init__(
        self,
        token: str,
        api: str = "https://api.github.com",
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.token = token
        self.api = api.rstrip("/")
        self.opener = opener

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.api}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GitHubError(f"GitHub answered {exc.code} for {method} {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubError(f"could not reach GitHub: {exc}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise GitHubError(f"GitHub returned something that is not JSON: {raw[:120]}") from exc

    def issues(self, repo: str, state: str = "open") -> list[dict]:
        """Open issues, without the pull requests GitHub mixes into this endpoint."""
        payload = self._call("GET", f"/repos/{repo}/issues?state={state}&per_page=100")
        if not isinstance(payload, list):
            raise GitHubError(f"expected a list of issues for {repo}, got {type(payload).__name__}")
        return [
            {
                "number": issue["number"],
                "title": issue.get("title") or "",
                "user": (issue.get("user") or {}).get("login"),
            }
            for issue in payload
            if "pull_request" not in issue
        ]

    def close(self, repo: str, number: int) -> dict:
        payload = self._call("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})
        if not isinstance(payload, dict):
            raise GitHubError(f"closing #{number} returned no issue")
        return {"number": payload.get("number", number), "state": payload.get("state")}
