"""Run the two surfaces:  python -m veto_agent

The chat and the approval device are separate servers on separate ports, so the
thing that asks is never the thing that consents. They share one SQLite file,
which is what lets a decision made on one show up on the other.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import urllib.parse

from . import app, phone
from .config import Config
from .store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description="The veto agent: a chat and an approval device")
    parser.add_argument("--chat-port", type=int, default=int(os.environ.get("PORT", "3000")))
    parser.add_argument("--phone-port", type=int, default=int(os.environ.get("PHONE_PORT", "4100")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--db", default=os.environ.get("ACTIONS_DB", "actions.db"))
    parser.add_argument(
        "--only",
        choices=("both", "chat", "phone"),
        default="both",
        help="run one surface on its own (they still share the database)",
    )
    args = parser.parse_args()

    config = Config.from_env()
    store = Store(args.db)
    servers = []

    if args.only in ("both", "chat"):
        servers.append((app.serve(config, store, port=args.chat_port, host=args.host), "chat"))
    if args.only in ("both", "phone"):
        servers.append((phone.serve(config, store, port=args.phone_port, host=args.host), "phone"))

    for server, name in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        print(f"[{name}] http://{args.host}:{port}", file=sys.stderr)

    approval_port = urllib.parse.urlsplit(config.approval_base).port
    if args.only in ("both", "phone") and approval_port and approval_port != args.phone_port:
        print(
            f"warning: APPROVAL_BASE is {config.approval_base} but the approval device is on "
            f"{args.phone_port}, so the links the chat shows will not resolve",
            file=sys.stderr,
        )

    print(f"database: {args.db}", file=sys.stderr)
    try:
        if len(servers) == 1:
            servers[0][0].serve_forever()  # a single surface blocks the main thread
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        for server, _ in servers:
            server.server_close()


if __name__ == "__main__":
    main()
