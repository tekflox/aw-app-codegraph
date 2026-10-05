"""codegraph_app/mcp_config.py + mcp/self_register.py — the two files that
decide whether the MCP Gateway can find this app's tools at all."""
from __future__ import annotations

import json
import socket
from pathlib import Path

from codegraph_app import mcp_config
from codegraph_app.mcp import self_register


def test_the_entry_points_at_this_hosts_own_name_not_loopback(monkeypatch):
    """127.0.0.1 would resolve inside the GATEWAY's netns, not ours — the
    gateway runs in its own container."""
    monkeypatch.delenv("AW_WORKSPACE_API_KEY", raising=False)
    monkeypatch.setenv("AW_PORT", "9030")

    entry = self_register.build_self_entry()

    assert entry["type"] == "http"
    assert entry["url"] == (f"http://{socket.gethostname()}:9030"
                            f"/api/apps/codegraph/mcp")
    assert entry["enabled"] is True
    assert "headers" not in entry


def test_the_api_key_is_attached_when_the_process_has_one(monkeypatch):
    """Tier-1 routes sit behind IdentityGuard, so the header is not optional
    in practice — without it the gateway gets a 401 and serves zero tools."""
    monkeypatch.setenv("AW_WORKSPACE_API_KEY", "k-123")

    entry = self_register.build_self_entry(port=1234)

    assert entry["headers"] == {"X-Api-Key": "k-123"}
    assert ":1234/" in entry["url"]


def test_the_port_defaults_to_9030_when_aw_port_is_unset(monkeypatch):
    monkeypatch.delenv("AW_PORT", raising=False)

    assert ":9030/" in self_register.build_self_entry()["url"]


def test_the_server_name_is_codegraph_so_tools_land_on_aw__codegraph__():
    assert self_register.MCP_SERVER_NAME == "codegraph"
    assert list(mcp_config.build_mcp_servers()) == ["codegraph"]


def test_write_mcp_json_creates_the_file(tmp_path: Path):
    doc = mcp_config.write_mcp_json(str(tmp_path))

    assert json.loads((tmp_path / "mcp.json").read_text()) == doc


def test_a_second_write_with_unchanged_content_does_not_touch_the_file(tmp_path: Path):
    """The gateway reloads on this file's MTIME and each reload briefly drops
    every tool it proxies, workspace-wide. Every boot re-activates every
    installed app, so an unconditional rewrite is a reload loop."""
    mcp_config.write_mcp_json(str(tmp_path))
    mtime_before = (tmp_path / "mcp.json").stat().st_mtime_ns

    mcp_config.write_mcp_json(str(tmp_path))

    assert (tmp_path / "mcp.json").stat().st_mtime_ns == mtime_before


def test_a_changed_entry_does_rewrite_the_file(tmp_path: Path):
    mcp_config.write_mcp_json(str(tmp_path), port=9030)

    mcp_config.write_mcp_json(str(tmp_path), port=9999)

    doc = json.loads((tmp_path / "mcp.json").read_text())
    assert ":9999/" in doc["mcpServers"]["codegraph"]["url"]


def test_an_unreadable_existing_file_is_overwritten(tmp_path: Path):
    (tmp_path / "mcp.json").write_bytes(b"\xff\xfe not utf-8")

    doc = mcp_config.write_mcp_json(str(tmp_path))

    assert json.loads((tmp_path / "mcp.json").read_text()) == doc
