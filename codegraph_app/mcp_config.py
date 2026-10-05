"""Builds this app's own root ``mcp.json`` — the file aw-mcp-gateway's
app-scan reads directly.

The no-op-on-unchanged behaviour below is **not** an optimisation: the gateway
reloads on this file's **mtime**, and each reload briefly drops every tool it
proxies — workspace-wide, including for the session that triggered it. Since
every boot re-activates every installed app, an unconditional rewrite on
activate is a reload loop. (Copied from ``aw-app-mobile``/``aw-app-notion``,
which carry the same comment for the same reason.)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .mcp import self_register

SERVER_NAME = self_register.MCP_SERVER_NAME


def build_mcp_servers(port: int | None = None) -> dict:
    return {SERVER_NAME: self_register.build_self_entry(port)}


def write_mcp_json(package_dir: str, port: int | None = None) -> dict:
    """Regenerate ``<package_dir>/mcp.json``, skipping the write when nothing
    changed. Returns the document either way."""
    doc = {"mcpServers": build_mcp_servers(port or int(os.environ.get("AW_PORT") or 9030))}
    body = json.dumps(doc, indent=2) + "\n"
    path = Path(package_dir) / "mcp.json"
    try:
        if path.read_text(encoding="utf-8") == body:
            return doc
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    path.write_text(body, encoding="utf-8")
    return doc
