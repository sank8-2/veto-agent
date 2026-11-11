"""The chat surface: Universal Login, a message box, and a read-only veto panel.

    /            sign in, or the chat if you already are
    /login       redirect to Auth0 with a state value and a PKCE challenge
    /callback    check the state, trade the code, verify the ID token
    /api/chat    one turn: a read, or a proposal that halts
    /api/state   what is pending and what happened, polled by the page
    /logout      clear the cookie and sign out at Auth0 too

There is deliberately no route here that can approve anything. The panel can see
a pending action and watch it change; it cannot move it.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sys
import urllib.request
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from urllib.parse import parse_qs, urlparse

from .agent import Agent, AgentError, describe
from .config import Config
from .github import GitHub
from .oidc import IdTokenVerifier, OidcError, authorize_url, exchange_code, logout_url
from .pkce import code_challenge, code_verifier
from .session import sign, unsign
from .store import Store

PENDING_COOKIE = "pending_login"
SESSION_COOKIE = "veto_session"
PENDING_TTL = 600
SESSION_TTL = 3600

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Veto agent</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; background: #0d1117; color: #e6edf3;
         font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, sans-serif; }
  header { display: flex; align-items: center; justify-content: space-between;
           gap: 16px; padding: 18px 24px; border-bottom: 1px solid #21262d; }
  h1 { font-size: 17px; margin: 0; letter-spacing: -.01em; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
       color: #8b949e; margin: 0 0 10px; }
  .who { color: #8b949e; font-size: 13px; display: flex; align-items: center; gap: 10px; }
  .who code { color: #79c0ff; }
  a { color: #58a6ff; }
  .grid { display: grid; grid-template-columns: minmax(0, 1fr) 400px; gap: 24px;
          padding: 24px; max-width: 1200px; margin: 0 auto; align-items: start; }
  @media (max-width: 940px) { .grid { grid-template-columns: 1fr; } }
  .log { min-height: 340px; max-height: 60vh; overflow-y: auto; display: flex;
         flex-direction: column; gap: 12px; padding: 16px; background: #010409;
         border: 1px solid #21262d; border-radius: 12px; }
  .msg { padding: 10px 14px; border-radius: 10px; max-width: 94%; white-space: pre-wrap;
         overflow-wrap: anywhere; }
  .me { align-self: flex-end; background: #1f6feb; color: #fff; }
  .bot { background: #161b22; border: 1px solid #21262d; }
  .bot a { color: #58a6ff; }
  .veto { border-color: #9e6a03; background: #1c1401; }
  .veto b { color: #e3b341; }
  form { display: flex; gap: 8px; margin-top: 12px; }
  input { flex: 1; padding: 11px 14px; border-radius: 10px; border: 1px solid #30363d;
          background: #010409; color: inherit; font: inherit; }
  input:focus { outline: 2px solid #1f6feb; outline-offset: -1px; }
  button { padding: 11px 18px; border: 0; border-radius: 10px; background: #238636;
           color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .hint { color: #6e7681; font-size: 13px; margin: 10px 0 0; }
  aside { background: #161b22; border: 1px solid #21262d; border-radius: 12px; padding: 18px; }
  ol { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 10px; }
  li { font-size: 12.5px; border-left: 2px solid #30363d; padding-left: 10px; }
  li.pending { border-color: #e3b341; }
  li.executed, li.approved { border-color: #3fb950; }
  li.denied, li.expired, li.failed { border-color: #f85149; }
  .badge { display: inline-block; font-size: 11px; font-weight: 600; letter-spacing: .04em;
           text-transform: uppercase; padding: 1px 7px; border-radius: 999px; margin-right: 6px; }
  .b-pending { background: #3d2c00; color: #e3b341; }
  .b-executed, .b-approved { background: #03301b; color: #56d364; }
  .b-denied, .b-expired, .b-failed { background: #3d0f0f; color: #ff7b72; }
  .meta { color: #6e7681; }
  .empty { color: #6e7681; font-size: 13px; }
  .calls { margin: 22px 0 0; }
  .calls li { border-color: #1f6feb; }
  .panel { width: min(460px, 90vw); margin: 12vh auto; background: #161b22;
           border: 1px solid #263041; border-radius: 14px; padding: 32px; text-align: center; }
  .panel p { color: #8b98a9; }
  .button { display: inline-block; background: #58a6ff; color: #0d1117;
            text-decoration: none; font-weight: 600; padding: 10px 20px; border-radius: 8px; }
  .error { border-color: #6b2b2b; }
</style>
</head>
<body>
%BODY%
</body>
</html>
"""

SIGNED_OUT = """
<div class="panel">
  <h1>Veto agent</h1>
  <p>Sign in with <code>%DOMAIN%</code>. The agent can read your GitHub issues
  straight away. Closing them needs a second device.</p>
  <a class="button" href="/login">Log in</a>
</div>
"""

ERROR = """
<div class="panel error">
  <h1>That did not finish</h1>
  <p>%MESSAGE%</p>
  <a class="button" href="/">Start again</a>
</div>
"""

APP = """
<header>
  <h1>Veto agent</h1>
  <div class="who">Signed in as <strong>%NAME%</strong> <code>%SUB%</code>
    <a href="/logout">Log out</a></div>
</header>
<main class="grid">
  <section>
    <div id="log" class="log">
      <div class="msg bot">Ask what is open in <code>%REPO%</code>, then ask me to close
        something. I will stop and wait for your phone.</div>
    </div>
    <form id="ask">
      <input id="message" autocomplete="off" placeholder="What is open?">
      <button id="send">Send</button>
    </form>
    <p class="hint">Try “what is open?” then “close them”. The second one will not happen here.</p>
  </section>
  <aside>
    <h2>Veto gate</h2>
    <ol id="actions"><li class="empty">Nothing yet.</li></ol>
    <div class="calls">
      <h2>Calls</h2>
      <ol id="calls"><li class="empty">Nothing yet.</li></ol>
    </div>
  </aside>
</main>
<script>
const log = document.getElementById('log');
const form = document.getElementById('ask');
const input = document.getElementById('message');
const send = document.getElementById('send');
const actions = document.getElementById('actions');
const callsList = document.getElementById('calls');

function bubble(cls, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

function renderActions(items) {
  actions.innerHTML = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'Nothing yet.';
    actions.appendChild(li);
    return;
  }
  for (const action of items) {
    const li = document.createElement('li');
    li.className = action.status;
    const badge = document.createElement('span');
    badge.className = 'badge b-' + action.status;
    badge.textContent = action.status;
    const what = document.createElement('div');
    what.textContent = action.summary;
    li.append(badge, what);
    if (action.result) {
      const result = document.createElement('div');
      result.className = 'meta';
      result.textContent = action.result;
      li.appendChild(result);
    }
    actions.appendChild(li);
  }
}

function renderCalls(calls) {
  if (!calls || !calls.length) return;
  if (callsList.querySelector('.empty')) callsList.innerHTML = '';
  for (const call of calls) {
    const li = document.createElement('li');
    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = call.status + ' · ' + call.ms + 'ms';
    const req = document.createElement('div');
    req.textContent = call.request;
    const detail = document.createElement('div');
    detail.className = 'meta';
    detail.textContent = call.detail || '';
    li.append(meta, req, detail);
    callsList.appendChild(li);
  }
}

async function refresh() {
  try {
    const response = await fetch('/api/state');
    if (!response.ok) return;
    const data = await response.json();
    renderActions(data.actions || []);
  } catch (err) { /* the page is still usable without the panel */ }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  bubble('me', message);
  input.value = '';
  send.disabled = true;
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message })
    });
    const data = await response.json();
    const bubbleEl = bubble(data.veto ? 'bot veto' : 'bot', '');
    if (data.veto) {
      const label = document.createElement('b');
      label.textContent = 'Halted for approval. ';
      bubbleEl.append(label, document.createTextNode(data.reply));
    } else {
      bubbleEl.textContent = data.reply || ('error: ' + (data.error || response.status));
    }
    renderCalls(data.calls);
    refresh();
  } catch (err) {
    bubble('bot', 'error: ' + err);
  } finally {
    send.disabled = false;
    input.focus();
  }
});

refresh();
setInterval(refresh, 1500);
</script>
"""


def _cookie(header: str | None, name: str) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def render_page(body: str) -> str:
    return PAGE.replace("%BODY%", body)


def _fill(template: str, **values: str) -> str:
    text = template
    for key, value in values.items():
        text = text.replace(f"%{key}%", value)
    return text


def make_handler(
    config: Config,
    store: Store,
    verifier: IdTokenVerifier | None = None,
    agent: Agent | None = None,
    opener=urllib.request.urlopen,
):
    verifier = verifier or IdTokenVerifier(config)
    if agent is None:
        agent = Agent(store, GitHub(config.github_token, config.github_api, opener=opener), config, opener=opener)

    def snapshot(sub: str) -> dict:
        store.expire_due()
        rows = store.recent(sub, limit=10)
        return {
            "actions": [
                {
                    "id": row["id"],
                    "status": row["status"],
                    "repo": row["repo"],
                    "summary": describe(row),
                    "result": row["result"],
                }
                for row in rows
            ]
        }

    class Handler(BaseHTTPRequestHandler):
        server_version = "VetoAgentChat/1.0"

        def _send(self, status: int, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        @staticmethod
        def _cookie_header(name: str, value: str, max_age: int) -> str:
            return f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"

        def log_message(self, fmt: str, *args) -> None:
            print(f"[chat] {fmt % args}", file=sys.stderr)

        def _session(self) -> dict | None:
            return unsign(_cookie(self.headers.get("Cookie"), SESSION_COOKIE), config.secret_key)

        def do_GET(self) -> None:  # noqa: N802 - fixed by the stdlib
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/":
                    self._home()
                elif parsed.path == "/login":
                    self._login()
                elif parsed.path == "/callback":
                    self._callback(parse_qs(parsed.query))
                elif parsed.path == "/logout":
                    self._logout()
                elif parsed.path == "/api/state":
                    session = self._session()
                    if not session:
                        self._json(401, {"error": "not_signed_in"})
                        return
                    self._json(200, snapshot(session["sub"]))
                elif parsed.path == "/healthz":
                    self._json(200, {"status": "ok"})
                else:
                    self._send(404, render_page(_fill(ERROR, MESSAGE="There is nothing here.")))
            except OidcError as exc:
                self._send(502, render_page(_fill(ERROR, MESSAGE=escape(str(exc)))))
            except Exception as exc:  # a demo should explain, not crash
                print(f"unexpected: {exc}", file=sys.stderr)
                self._send(500, render_page(_fill(ERROR, MESSAGE="Something went wrong.")))

        def do_POST(self) -> None:  # noqa: N802 - fixed by the stdlib
            path = urlparse(self.path).path
            if path != "/api/chat":
                self._json(404, {"error": "not_found"})
                return
            session = self._session()
            if not session:
                self._json(401, {"error": "not_signed_in"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4096:
                self._json(400, {"error": "bad_body"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    self._json(400, {"error": "bad_body", "detail": "send a JSON object"})
                    return
                message = str(payload.get("message", ""))
            except (ValueError, UnicodeDecodeError):
                self._json(400, {"error": "bad_json"})
                return
            try:
                result = agent.run(message, session["sub"])
            except AgentError as exc:
                self._json(200, {"reply": f"That did not work: {exc}", "calls": [], "veto": None})
                return
            except Exception as exc:
                print(f"unexpected: {exc}", file=sys.stderr)
                self._json(500, {"error": "unexpected", "detail": str(exc)[:200]})
                return
            result["state"] = snapshot(session["sub"])
            self._json(200, result)

        def _home(self) -> None:
            session = self._session()
            if session:
                self._send(
                    200,
                    render_page(
                        _fill(
                            APP,
                            NAME=escape(session.get("name") or session["sub"]),
                            SUB=escape(session["sub"]),
                            REPO=escape(config.github_repo or "owner/name"),
                        )
                    ),
                )
            else:
                self._send(200, render_page(_fill(SIGNED_OUT, DOMAIN=escape(config.domain))))

        def _login(self) -> None:
            state = secrets.token_urlsafe(24)
            verifier_value = code_verifier()
            pending = sign({"state": state, "verifier": verifier_value}, config.secret_key, PENDING_TTL)
            cookie = self._cookie_header(PENDING_COOKIE, pending, PENDING_TTL)
            self._redirect(authorize_url(config, state, code_challenge(verifier_value)), [cookie])

        def _callback(self, params: dict[str, list[str]]) -> None:
            pending = unsign(_cookie(self.headers.get("Cookie"), PENDING_COOKIE), config.secret_key)
            if not pending:
                self._send(400, render_page(_fill(ERROR, MESSAGE="This login expired or was tampered with.")))
                return
            if "error" in params:
                detail = params.get("error_description", params["error"])[0]
                self._send(400, render_page(_fill(ERROR, MESSAGE=escape(f"Auth0 refused the login: {detail}"))))
                return
            state = (params.get("state") or [""])[0]
            if not hmac.compare_digest(pending.get("state", ""), state):
                self._send(400, render_page(_fill(ERROR, MESSAGE="The state value did not match.")))
                return
            code = (params.get("code") or [""])[0]
            if not code:
                self._send(400, render_page(_fill(ERROR, MESSAGE="Auth0 sent no authorization code.")))
                return

            tokens = exchange_code(config, code, pending["verifier"], opener=opener)
            claims = verifier.verify(tokens["id_token"])

            session = sign(
                {
                    "sub": claims["sub"],
                    "name": claims.get("name") or claims.get("email"),
                    "email": claims.get("email"),
                    "access_token": tokens["access_token"],
                },
                config.secret_key,
                SESSION_TTL,
            )
            cookies = [
                self._cookie_header(SESSION_COOKIE, session, SESSION_TTL),
                self._cookie_header(PENDING_COOKIE, "", 0),
            ]
            self._redirect("/", cookies)

        def _logout(self) -> None:
            self._redirect(logout_url(config), [self._cookie_header(SESSION_COOKIE, "", 0)])

    return Handler


def serve(config: Config, store: Store, port: int = 3000, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(config, store))
    server.daemon_threads = True
    return server
