"""codegraph_app/routes.py through a real TestClient — the /mcp endpoint is
the one the MCP Gateway dials, so its JSON-RPC envelope and its 202/405
behaviour are contract, not implementation detail."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from codegraph_app import routes as routes_mod

STATUS_OK = {
    "ok": True, "initialized": True, "version": "1.6.2", "fileCount": 7,
    "nodeCount": 70, "edgeCount": 700, "dbSizeBytes": 3 * 1024 * 1024,
    "walSizeBytes": 512 * 1024, "backend": "node-sqlite", "journalMode": "wal",
    "languages": ["python", "javascript"], "lastIndexed": "2026-10-05T18:00:00.000Z",
    "pendingChanges": {"added": 1, "modified": 2, "removed": 3},
}


class FakeBridge:
    def __init__(self):
        self.handled: list[dict] = []
        self.responses: list[dict | None] = []

    async def handle_request(self, request):
        self.handled.append(request)
        if self.responses:
            return self.responses.pop(0)
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"echo": request}}

    def snapshot(self):
        return {"running": True, "pid": 123, "spawn_count": 1, "tool_count": 8,
                "tools": ["codegraph_explore"], "last_error": ""}


def make_client(*, status=None, config=None):
    plugin = MagicMock()
    plugin.bridge = FakeBridge()
    plugin.index_root.return_value = "/opt/aw-workspace"
    plugin.native_watch.return_value = True
    plugin.include_ignored.return_value = ["repos/"]
    plugin.last_index = {"ok": True, "at": 1.0, "detail": "done"}
    plugin.last_sync = {"ok": None, "at": None, "detail": ""}

    async def fake_status():
        return status if status is not None else STATUS_OK

    plugin.status = fake_status
    plugin.reindex = MagicMock(side_effect=lambda: _noop())

    ctx = MagicMock()
    ctx.config = config or {}
    return TestClient(routes_mod.build_routes(ctx, plugin)), plugin


async def _noop():
    return {"ok": True}


# ── /status ───────────────────────────────────────────────────────────

def test_status_flattens_codegraph_output_into_scalars_the_widget_can_bind():
    client, _ = make_client()

    body = client.get("/status").json()

    assert body["logged_in"] is True
    assert body["configured"] is True
    assert body["files"] == 7
    assert body["nodes"] == 70
    assert body["edges"] == 700
    # dbSizeBytes + walSizeBytes, the index's real footprint on disk.
    assert body["db_size_mb"] == 3.5
    assert body["pending_changes"] == 6
    assert body["mcp_tool_count"] == 8
    assert body["native_watch"] is True


def test_status_reports_not_logged_in_when_there_is_no_index_yet():
    """`configured` true / `logged_in` false is what makes the window show
    "installed but still building" rather than "the CLI is broken"."""
    client, _ = make_client(status={"ok": True, "initialized": False})

    body = client.get("/status").json()

    assert body["configured"] is True
    assert body["logged_in"] is False
    assert body["files"] == 0


def test_status_reports_a_broken_cli_as_not_configured():
    client, _ = make_client(status={"ok": False, "error": "codegraph: not found"})

    body = client.get("/status").json()

    assert body["configured"] is False
    assert body["logged_in"] is False
    assert body["status_error"] == "codegraph: not found"


# ── /reindex ──────────────────────────────────────────────────────────

def test_reindex_returns_immediately_and_starts_in_the_background():
    client, plugin = make_client()

    body = client.post("/reindex").json()

    assert body == {"started": True, "index_root": "/opt/aw-workspace"}
    plugin.reindex.assert_called_once()


# ── /mcp ──────────────────────────────────────────────────────────────

def test_mcp_post_forwards_one_message_and_returns_one_object():
    client, plugin = make_client()

    body = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).json()

    assert body["id"] == 1
    assert plugin.bridge.handled == [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]


def test_mcp_post_accepts_a_batch_and_answers_with_a_list():
    client, _ = make_client()

    body = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]).json()

    assert isinstance(body, list)
    assert [m["id"] for m in body] == [1, 2]


def test_a_notification_only_post_is_202_with_no_body():
    """Streamable HTTP's "accepted, nothing to say". A JSON body here would be
    a protocol error, not an empty result."""
    client, plugin = make_client()
    plugin.bridge.responses = [None]

    resp = client.post("/mcp", json={"jsonrpc": "2.0",
                                     "method": "notifications/initialized"})

    assert resp.status_code == 202
    assert resp.content == b""


def test_a_batch_of_only_notifications_is_also_202():
    client, plugin = make_client()
    plugin.bridge.responses = [None, None]

    resp = client.post("/mcp", json=[
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled"},
    ])

    assert resp.status_code == 202


def test_mcp_get_is_405_because_there_is_no_sse_channel():
    client, _ = make_client()

    assert client.get("/mcp").status_code == 405


def test_the_mcp_url_in_the_self_entry_matches_the_registered_route():
    """If these drift the gateway dials a 404 and the app serves zero tools
    with nothing reporting it."""
    from codegraph_app.mcp import self_register

    manifest_prefix = "/api/apps/codegraph"
    assert self_register.ROUTE_PATH == f"{manifest_prefix}/mcp"


@pytest.mark.parametrize("payload", ["not-json-object", 12])
def test_mcp_post_rejects_a_non_object_body(payload):
    client, _ = make_client()

    assert client.post("/mcp", json=payload).status_code == 422


def test_status_surfaces_the_configured_reconcile_interval():
    client, _ = make_client(config={"reconcile_interval_s": 300})

    assert client.get("/status").json()["reconcile_interval_s"] == 300.0


def test_status_is_json_serialisable_end_to_end():
    """auth_status interpolates scalar fields straight out of this response —
    a non-serialisable value would 500 the window with no other symptom."""
    client, _ = make_client()

    json.dumps(client.get("/status").json())
