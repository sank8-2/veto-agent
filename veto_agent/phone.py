"""The approval surface: the second device, on its own port.

    GET  /            what is waiting for a human, with a link per action
    GET  /a/<token>   one action, and two buttons
    POST /a/<token>   the decision itself

Nothing here knows about the chat. There is no session cookie, no login, and no
shared code path with the conversation: the only credential is the signed,
expiring link, and the only thing it can do is move one pending row once.

This is the stand-in for CIBA. A real CIBA approval is pushed by Auth0 to the
user's authenticator app, with Auth0 holding the binding between the request and
the device. Here the user opens a local page on a different port, which is a
weaker channel, and the README says so.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape

from . import approvals
from .agent import Agent, describe
from .config import Config
from .github import GitHub
from .store import PENDING, Store

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veto agent · approvals</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0b0e14; color: #e6edf3; font: 15px/1.55
         ui-sans-serif, system-ui, -apple-system, sans-serif; }
  .phone { max-width: 480px; margin: 0 auto; min-height: 100vh;
           background: #11151d; border-left: 1px solid #1d2430; border-right: 1px solid #1d2430; }
  header { padding: 18px 20px; border-bottom: 1px solid #1d2430; display: flex;
           align-items: baseline; justify-content: space-between; }
  h1 { font-size: 15px; margin: 0; letter-spacing: -.01em; }
  .sub { color: #6e7b8f; font-size: 12px; }
  main { padding: 20px; }
  .card { border: 1px solid #2a3444; border-radius: 14px; padding: 18px; margin-bottom: 14px;
          background: #151b25; }
  .card.waiting { border-color: #9e6a03; }
  .card h2 { font-size: 14px; margin: 0 0 6px; }
  .card p { margin: 0 0 10px; color: #93a1b5; font-size: 13.5px; }
  .repo { color: #79c0ff; font-size: 12.5px; }
  .row { display: flex; gap: 10px; margin-top: 14px; }
  button, .button { flex: 1; padding: 12px 16px; border: 0; border-radius: 10px;
           font: inherit; font-weight: 600; cursor: pointer; text-align: center;
           text-decoration: none; display: inline-block; }
  .yes { background: #238636; color: #fff; }
  .no { background: #21262d; color: #f0f6fc; border: 1px solid #30363d; }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: .04em;
           text-transform: uppercase; padding: 2px 8px; border-radius: 999px;
           background: #3d2c00; color: #e3b341; margin-bottom: 10px; }
  .ok { background: #03301b; color: #56d364; }
  .bad { background: #3d0f0f; color: #ff7b72; }
  .meta { color: #6e7b8f; font-size: 12px; }
  .empty { color: #6e7b8f; text-align: center; padding: 40px 0; font-size: 13.5px; }
  code { color: #79c0ff; font-size: 12px; word-break: break-all; }
</style>
</head>
<body>
<div class="phone">
<header>
  <h1>Veto agent</h1>
  <span class="sub">approval device · :%PORT%</span>
</header>
<main>
%BODY%
</main>
</div>
</body>
</html>
"""

INBOX_EMPTY = """
<p class="empty">Nothing is waiting for you.</p>
"""

INBOX_CARD = """
<div class="card waiting">
  <span class="badge">waiting</span>
  <h2>%SUMMARY%</h2>
  <p>Asked by <code>%SUB%</code> in the chat, %AGE%. Nothing has been sent to GitHub.</p>
  <p class="repo">%REPO%</p>
  <p class="meta">expires %EXPIRES%</p>
  <div class="row"><a class="button no" href="/a/%TOKEN%">Review</a></div>
</div>
"""

DECISION_CARD = """
<div class="card waiting">
  <span class="badge">waiting for you</span>
  <h2>%SUMMARY%</h2>
  <p>The agent asked for this in a chat on another surface. It has not touched GitHub.</p>
  <p class="repo">%REPO%</p>
  <p class="meta">requested by <code>%SUB%</code> · expires %EXPIRES%</p>
  <form method="post">
    <div class="row">
      <button class="yes" name="decision" value="approve">Approve</button>
      <button class="no" name="decision" value="deny">Deny</button>
    </div>
  </form>
</div>
"""

RESULT_CARD = """
<div class="card">
  <span class="badge %TONE%">%STATUS%</span>
  <h2>%SUMMARY%</h2>
  <p>%MESSAGE%</p>
  <p class="meta">%REPO%</p>
</div>
"""

NOT_FOUND = """
<div class="card">
  <span class="badge bad">rejected</span>
  <h2>That link is not usable</h2>
  <p>%MESSAGE%</p>
</div>
"""


def _fill(template: str, **values: str) -> str:
    text = template
    for key, value in values.items():
        text = text.replace(f"%{key}%", value)
    return text


def _ago(created_at: int) -> str:
    seconds = max(0, int(time.time()) - int(created_at))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def _when(timestamp: int) -> str:
    remaining = int(timestamp) - int(time.time())
    if remaining <= 0:
        return "already expired"
    return f"in {max(1, remaining // 60)}m"


def make_handler(config: Config, store: Store, agent: Agent | None = None, port: int = 4100):
    if agent is None:
        agent = Agent(
            store,
            GitHub(config.github_token, config.github_api),
            config,
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "VetoAgentPhone/1.0"

        def _send(self, status: int, body: str) -> None:
            html = _fill(PAGE, BODY=body, PORT=str(port)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            print(f"[phone] {fmt % args}", file=sys.stderr)

        def _inbox(self) -> None:
            store.expire_due()
            pending = store.pending()
            if not pending:
                self._send(200, INBOX_EMPTY)
                return
            cards = []
            for action in pending:
                token = approvals.mint(action["id"], config.secret_key, config.approval_ttl)
                cards.append(
                    _fill(
                        INBOX_CARD,
                        SUMMARY=escape(describe(action)),
                        SUB=escape(action["sub"]),
                        AGE=escape(_ago(action["created_at"])),
                        REPO=escape(action["repo"]),
                        EXPIRES=escape(_when(action["expires_at"])),
                        TOKEN=escape(token),
                    )
                )
            self._send(200, "\n".join(cards))

        def _action_for(self, token: str) -> tuple[dict | None, str | None]:
            action_id = approvals.read(token, config.secret_key)
            if not action_id:
                return None, "This link is forged, expired, or was never an approval link."
            action = store.get(action_id)
            if action is None:
                return None, "No action matches this link."
            return action, None

        def _review(self, token: str) -> None:
            action, problem = self._action_for(token)
            if action is None:
                self._send(400, _fill(NOT_FOUND, MESSAGE=escape(problem or "")))
                return
            if action["status"] != PENDING:
                self._send(
                    200,
                    _fill(
                        RESULT_CARD,
                        TONE="ok" if action["status"] in ("approved", "executed") else "bad",
                        STATUS=escape(action["status"]),
                        SUMMARY=escape(describe(action)),
                        MESSAGE=escape(
                            action["result"]
                            or "This action has already been decided, so this link can no longer do anything."
                        ),
                        REPO=escape(action["repo"]),
                    ),
                )
                return
            if action["expires_at"] < time.time():
                store.expire_due()
                self._send(
                    200,
                    _fill(
                        RESULT_CARD,
                        TONE="bad",
                        STATUS="expired",
                        SUMMARY=escape(describe(action)),
                        MESSAGE="Too late — the request expired before anyone answered it.",
                        REPO=escape(action["repo"]),
                    ),
                )
                return
            self._send(
                200,
                _fill(
                    DECISION_CARD,
                    SUMMARY=escape(describe(action)),
                    REPO=escape(action["repo"]),
                    SUB=escape(action["sub"]),
                    EXPIRES=escape(_when(action["expires_at"])),
                ),
            )

        def _decide(self, token: str, decision: str) -> None:
            action, problem = self._action_for(token)
            if action is None:
                self._send(400, _fill(NOT_FOUND, MESSAGE=escape(problem or "")))
                return
            if decision not in ("approve", "deny"):
                self._send(400, _fill(NOT_FOUND, MESSAGE="Send approve or deny."))
                return
            if action["status"] == PENDING and action["expires_at"] < time.time():
                store.expire_due()
                self._send(
                    200,
                    _fill(
                        RESULT_CARD,
                        TONE="bad",
                        STATUS="expired",
                        SUMMARY=escape(describe(action)),
                        MESSAGE="Too late — the request expired before anyone answered it, "
                        "so nothing was sent to GitHub.",
                        REPO=escape(action["repo"]),
                    ),
                )
                return
            outcome = agent.decide(action["id"], approve=decision == "approve")
            current = outcome.get("action") or store.get(action["id"])
            if not outcome.get("ok"):
                expired = outcome.get("reason") == "expired"
                self._send(
                    200,
                    _fill(
                        RESULT_CARD,
                        TONE="bad",
                        STATUS=escape((current or {}).get("status", "unknown")),
                        SUMMARY=escape(describe(current) if current else "unknown action"),
                        MESSAGE=escape(
                            "Too late — the request expired before anyone answered it, so nothing "
                            "was sent to GitHub."
                            if expired
                            else "This link was already used. A pending action can only be decided "
                            "once, so nothing else happened."
                        ),
                        REPO=escape((current or {}).get("repo", "")),
                    ),
                )
                return
            if outcome["reason"] == "denied":
                message = "Denied. The agent dropped the request and never contacted GitHub."
                tone = "bad"
            else:
                message = f"Approved and executed once: {outcome.get('result', '')}"
                tone = "ok"
            self._send(
                200,
                _fill(
                    RESULT_CARD,
                    TONE=tone,
                    STATUS=escape((current or {}).get("status", "decided")),
                    SUMMARY=escape(describe(current) if current else "unknown action"),
                    MESSAGE=escape(message),
                    REPO=escape((current or {}).get("repo", "")),
                ),
            )

        def do_GET(self) -> None:  # noqa: N802 - fixed by the stdlib
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                self._json(200, {"status": "ok", "pending": len(store.pending())})
                return
            if path == "/":
                self._inbox()
                return
            if path.startswith("/a/"):
                self._review(path[len("/a/") :])
                return
            self._send(404, _fill(NOT_FOUND, MESSAGE="There is nothing at this address."))

        def do_POST(self) -> None:  # noqa: N802 - fixed by the stdlib
            path = urllib.parse.urlsplit(self.path).path
            if not path.startswith("/a/"):
                self._json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2048:
                self._send(400, _fill(NOT_FOUND, MESSAGE="Send approve or deny."))
                return
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            decision = (form.get("decision") or [""])[0]
            self._decide(path[len("/a/") :], decision)

    return Handler


def serve(
    config: Config,
    store: Store,
    port: int = 4100,
    host: str = "127.0.0.1",
    agent: Agent | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(config, store, agent=agent, port=port))
    server.daemon_threads = True
    return server
