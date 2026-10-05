"""Entry describing this app's own ``/mcp`` endpoint, for aw-mcp-gateway's
app-scan (``scan_app_mcp_servers()``, which reads ``<app dir>/mcp.json``).

``contributes.mcp.provides`` in aw-app.json registers **nothing** — it is the
marketplace's "what you get" list. The gateway only ever finds an upstream by
scanning for the file ``mcp_config.write_mcp_json`` writes.

Mirrors ``aw-app-notion``'s and ``aw-app-mobile``'s modules of the same name.
Tier-1 (in-process): this *is* the aw-workspace process, so
``socket.gethostname()`` is exactly the value ContainerSupervisor injects into
sibling containers as ``AW_WORKSPACE_HOST`` — ``127.0.0.1`` would resolve
inside the gateway's own netns, not ours. ``AW_WORKSPACE_API_KEY`` is already
in this process's environment; the header is required because Tier-1 routes
sit behind IdentityGuard.

The server name is ``codegraph``, so the gateway prefixes this app's tools as
``aw__codegraph__codegraph_explore`` and friends.
"""
from __future__ import annotations

import os
import socket

MCP_SERVER_NAME = "codegraph"
ROUTE_PATH = "/api/apps/codegraph/mcp"


def build_self_entry(port: int | None = None) -> dict:
    host = socket.gethostname()
    port = port or int(os.environ.get("AW_PORT") or 9030)
    entry: dict = {
        "type": "http",
        "url": f"http://{host}:{port}{ROUTE_PATH}",
        "enabled": True,
    }
    api_key = os.environ.get("AW_WORKSPACE_API_KEY")
    if api_key:
        entry["headers"] = {"X-Api-Key": api_key}
    return entry
