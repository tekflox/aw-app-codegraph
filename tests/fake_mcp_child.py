"""A stand-in for `codegraph serve --mcp`: speaks the same newline-delimited
JSON-RPC over stdin/stdout, so the bridge can be tested against a real child
process without a 286 MB CodeGraph bundle.

Behaviour is driven by env vars so one script covers every case:

* ``FAKE_JOURNAL``   — append one line per received method to this file, so a
  test can assert the child saw (or never saw) a second ``initialize``.
* ``FAKE_NO_TOOLS=1``   — answer ``tools/list`` with an empty list.
* ``FAKE_TOOLS_ERROR=1``— answer ``tools/list`` with a JSON-RPC error.
* ``FAKE_HANG=1``       — never answer anything (handshake timeout).
* ``FAKE_ECHO_ENV``     — comma-separated env names echoed back in a
  ``tools/call`` result, so a test can assert what the bridge spawned with.
* ``FAKE_NOISE=1``      — emit a blank line and a non-JSON line before every
  real answer, the way a tool that logs to stdout would.
* ``FAKE_IGNORE_SIGTERM=1`` — refuse SIGTERM, so stop() has to escalate.
* ``FAKE_FAT_LINE=1``   — answer ``tools/call`` with a payload far past the
  reader's buffer limit.

``tools/call`` on ``die`` exits the process without answering — the
mid-session crash the bridge has to recover from transparently.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time

JOURNAL = os.environ.get("FAKE_JOURNAL")

TOOLS = [
    {"name": "codegraph_explore", "description": "explore", "inputSchema": {"type": "object"}},
    {"name": "codegraph_status", "description": "status", "inputSchema": {"type": "object"}},
]


def journal(method: str) -> None:
    if JOURNAL:
        with open(JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(method + "\n")


def send(msg: dict) -> None:
    if os.environ.get("FAKE_NOISE"):
        sys.stdout.write("\n[codegraph] a progress line that is not JSON\n")
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    if os.environ.get("FAKE_IGNORE_SIGTERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.environ.get("FAKE_HANG"):
        time.sleep(600)
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        req_id = req.get("id")
        journal(method)

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "codegraph", "version": "1.6.2"}}})
        elif method.startswith("notifications/"):
            continue
        elif method == "tools/list":
            if os.environ.get("FAKE_TOOLS_ERROR"):
                send({"jsonrpc": "2.0", "id": req_id,
                      "error": {"code": -32603, "message": "boom"}})
            elif os.environ.get("FAKE_NO_TOOLS"):
                send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": []}})
            else:
                send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name", "")
            if name == "die":
                sys.exit(7)
            if name == "slow":
                time.sleep(30)
            if name == "fat" or os.environ.get("FAKE_FAT_LINE"):
                send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": "x" * (4 * 1024 * 1024)}]}})
                continue
            payload = {"name": name, "arguments": params.get("arguments", {})}
            echo = os.environ.get("FAKE_ECHO_ENV")
            if echo:
                payload["env"] = {k: os.environ.get(k) for k in echo.split(",")}
            send({"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}]}})
        else:
            send({"jsonrpc": "2.0", "id": req_id,
                  "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    main()
