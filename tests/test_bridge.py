"""codegraph_app/mcp/bridge.py against a real child process (tests/
fake_mcp_child.py), not a mock — the failure modes that matter here are all
process-level: a child that dies mid-session, a second handshake, a caller id
getting crossed with another caller's."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from codegraph_app.mcp import bridge as bridge_mod
from codegraph_app.mcp.bridge import CodeGraphBridge

FAKE = str(Path(__file__).resolve().parent / "fake_mcp_child.py")


def make_bridge(**env) -> CodeGraphBridge:
    return CodeGraphBridge(command=sys.executable, args=[FAKE], env=env)


def text_of(response: dict) -> dict:
    return json.loads(response["result"]["content"][0]["text"])


@pytest.mark.asyncio
async def test_start_handshakes_and_lists_the_childs_tools():
    br = make_bridge()
    try:
        await br.start()
        assert br.running
        assert [t["name"] for t in br.tools] == ["codegraph_explore", "codegraph_status"]
        assert br.spawn_count == 1
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_initialize_is_answered_locally_and_never_reaches_the_child(tmp_path: Path):
    """The gateway re-dials (and re-initializes) on every reload. Forwarding
    that would hand an already-serving child a second handshake."""
    journal = tmp_path / "journal.txt"
    br = make_bridge(FAKE_JOURNAL=str(journal))
    try:
        await br.start()
        for _ in range(3):
            resp = await br.handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            assert resp["result"]["serverInfo"]["name"] == "codegraph"
            assert resp["result"]["protocolVersion"] == bridge_mod.PROTOCOL_VERSION
        # Exactly one initialize — the bridge's own, at spawn.
        seen = journal.read_text().split()
        assert seen.count("initialize") == 1
        assert seen.count("notifications/initialized") == 1
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_notifications_get_no_response():
    br = make_bridge()
    try:
        await br.start()
        assert await br.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_tools_list_is_forwarded():
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({"jsonrpc": "2.0", "id": "abc", "method": "tools/list"})
        assert resp["id"] == "abc"
        assert [t["name"] for t in resp["result"]["tools"]] == [
            "codegraph_explore", "codegraph_status"]
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_tools_call_is_forwarded_and_keeps_the_callers_own_id():
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {"query": "scan_app_mcp_servers"}},
        })
        assert resp["id"] == 42
        assert text_of(resp) == {"name": "codegraph_explore",
                                 "arguments": {"query": "scan_app_mcp_servers"}}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_gateway_injected_underscore_arguments_are_stripped():
    """CodeGraph validates its input schemas strictly; an argument it never
    declared broke every aw__notion__* call once for exactly this reason."""
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_explore",
                       "arguments": {"query": "x", "_aw_context": {"NOTION_TASK_ID": "y"},
                                     "_gateway_caller_run_id": "z"}},
        })
        assert text_of(resp)["arguments"] == {"query": "x"}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_the_env_the_bridge_spawns_with_reaches_the_child():
    br = make_bridge(CODEGRAPH_TELEMETRY="0", DO_NOT_TRACK="1",
                     CODEGRAPH_MCP_TOOLS="explore,status",
                     FAKE_ECHO_ENV="CODEGRAPH_TELEMETRY,DO_NOT_TRACK,CODEGRAPH_MCP_TOOLS")
    try:
        await br.start()
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_status", "arguments": {}}})
        assert text_of(resp)["env"] == {
            "CODEGRAPH_TELEMETRY": "0", "DO_NOT_TRACK": "1",
            "CODEGRAPH_MCP_TOOLS": "explore,status"}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_child_that_dies_mid_session_is_respawned_transparently():
    """The gateway keeps POSTing to a live upstream. A dead child must cost
    the caller that hit it an error, and nobody after that."""
    br = make_bridge()
    try:
        await br.start()
        first_pid = br.proc.pid

        killed = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "die", "arguments": {}}})
        assert killed["result"]["isError"] is True

        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {"query": "after"}}})
        assert text_of(resp)["arguments"] == {"query": "after"}
        assert br.spawn_count == 2
        assert br.proc.pid != first_pid
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_an_externally_killed_child_is_respawned_on_the_next_request():
    """`codegraph unlock`, a stray kill, an OOM reaper — the child can go
    away without the bridge being told."""
    br = make_bridge()
    try:
        await br.start()
        br.proc.kill()
        await br.proc.wait()

        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {}}})
        assert "isError" not in resp["result"]
        assert br.spawn_count == 2
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_concurrent_calls_each_get_their_own_answer():
    """One child serves every concurrent caller, so the id a call waits on
    has to be the bridge's own — independent MCP clients number their
    requests from 1 and would otherwise collide."""
    br = make_bridge()
    try:
        await br.start()
        requests = [
            br.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "codegraph_explore",
                                          "arguments": {"query": f"q{i}"}}})
            for i in range(12)
        ]
        results = await asyncio.gather(*requests)
        assert [text_of(r)["arguments"]["query"] for r in results] == [
            f"q{i}" for i in range(12)]
        assert {r["id"] for r in results} == {1}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_unknown_method_is_a_jsonrpc_error():
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
        assert resp["error"]["code"] == -32601
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_ping_is_answered_locally():
    br = make_bridge()
    try:
        await br.start()
        assert await br.handle_request(
            {"jsonrpc": "2.0", "id": 9, "method": "ping"}) == {
                "jsonrpc": "2.0", "id": 9, "result": {}}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_child_listing_zero_tools_fails_the_start():
    """An empty-but-successful start is how a broken upstream comes to look
    identical to a healthy one with nothing to offer."""
    br = make_bridge(FAKE_NO_TOOLS="1")
    with pytest.raises(RuntimeError, match="zero tools"):
        await br.start()
    assert not br.running


@pytest.mark.asyncio
async def test_a_child_whose_tools_list_errors_fails_the_start():
    br = make_bridge(FAKE_TOOLS_ERROR="1")
    with pytest.raises(RuntimeError, match="tools/list returned an error"):
        await br.start()
    assert not br.running


@pytest.mark.asyncio
async def test_a_child_that_never_answers_the_handshake_is_killed(monkeypatch):
    monkeypatch.setattr(bridge_mod, "HANDSHAKE_TIMEOUT_S", 1.0)
    br = make_bridge(FAKE_HANG="1")
    with pytest.raises(RuntimeError, match="handshake"):
        await br.start()
    # Not left running unreferenced — that is how a stuck child holds pipes
    # open forever with nothing pointing at it.
    assert not br.running


@pytest.mark.asyncio
async def test_a_tools_call_that_times_out_is_an_is_error_result(monkeypatch):
    monkeypatch.setattr(bridge_mod, "CALL_TIMEOUT_S", 1.0)
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "slow", "arguments": {}}})
        assert resp["result"]["isError"] is True
        assert "timed out" in resp["result"]["content"][0]["text"]
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_tools_list_against_an_unspawnable_command_is_a_jsonrpc_error():
    br = CodeGraphBridge(command="/nonexistent/codegraph", args=["serve"], env={})
    resp = await br.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp["error"]["code"] == -32603
    assert br.last_error.startswith("tools/list:")


@pytest.mark.asyncio
async def test_tools_call_against_an_unspawnable_command_is_an_is_error_result():
    br = CodeGraphBridge(command="/nonexistent/codegraph", args=["serve"], env={})
    resp = await br.handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "codegraph_explore", "arguments": {}}})
    assert resp["result"]["isError"] is True


@pytest.mark.asyncio
async def test_snapshot_reports_the_child_state():
    br = make_bridge()
    try:
        await br.start()
        snap = br.snapshot()
        assert snap["running"] is True
        assert snap["pid"] == br.proc.pid
        assert snap["tool_count"] == 2
        assert snap["tools"] == ["codegraph_explore", "codegraph_status"]
    finally:
        await br.stop()
    stopped = br.snapshot()
    assert stopped["running"] is False and stopped["pid"] is None


@pytest.mark.asyncio
async def test_stop_is_safe_before_any_start():
    br = make_bridge()
    await br.stop()
    assert not br.running


@pytest.mark.asyncio
async def test_start_is_idempotent_and_does_not_respawn_a_live_child():
    br = make_bridge()
    try:
        await br.start()
        await br.start()
        assert br.spawn_count == 1
    finally:
        await br.stop()


def test_wire_ids_are_unique_per_process():
    ids = {bridge_mod.next_wire_id() for _ in range(100)}
    assert len(ids) == 100


@pytest.mark.asyncio
async def test_non_json_noise_on_stdout_is_skipped_not_fatal():
    """Anything a tool logs to stdout shares the protocol channel. A stray
    progress line must be stepped over, in the handshake AND in the reader
    loop, or the very first noisy release of CodeGraph breaks every call."""
    br = make_bridge(FAKE_NOISE="1")
    try:
        await br.start()
        assert [t["name"] for t in br.tools] == ["codegraph_explore", "codegraph_status"]
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {"query": "noisy"}}})
        assert text_of(resp)["arguments"] == {"query": "noisy"}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_child_that_ignores_sigterm_is_killed():
    br = make_bridge(FAKE_IGNORE_SIGTERM="1")
    try:
        await br.start()
        proc = br.proc
        # Keep the escalation quick — the real budget is 10s and the test
        # only needs to prove the kill path runs.
        await asyncio.wait_for(br.stop(), timeout=30)
        assert proc.returncode is not None
    finally:
        if br.running:
            br.proc.kill()
    assert not br.running


@pytest.mark.asyncio
async def test_a_response_line_past_the_read_limit_drops_the_child(monkeypatch):
    """A single oversized line can't be recovered from mid-stream, so the
    child is dropped and the next request respawns it — rather than the
    reader loop spinning on a buffer it can never clear."""
    monkeypatch.setattr(bridge_mod, "STDOUT_LIMIT_BYTES", 4096)
    br = make_bridge()
    try:
        await br.start()
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "fat", "arguments": {}}})
        assert resp["result"]["isError"] is True

        after = await br.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {}}})
        assert "isError" not in after["result"]
        assert br.spawn_count == 2
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_respawn_while_the_old_reader_is_still_alive_cancels_it():
    """The reader loop clears ``self.proc`` on EOF before it returns, so
    there is a window where the child is gone and its reader task is not
    finished yet. A respawn landing in that window must not leave two reader
    tasks on one bridge."""
    br = make_bridge()
    try:
        await br.start()
        old_reader = br._reader_task
        br.proc = None  # the window: child gone, reader task still running

        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {}}})

        assert "isError" not in resp["result"]
        assert old_reader.done()
        assert br._reader_task is not old_reader
        assert br.spawn_count == 2
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_write_failure_does_not_leave_a_pending_future_behind():
    """A future nobody will ever resolve is a caller hung forever."""
    br = make_bridge()
    try:
        await br.start()

        async def exploding_write(_msg):
            raise BrokenPipeError("stdin closed")

        br._write = exploding_write
        resp = await br.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "codegraph_explore", "arguments": {}}})

        assert resp["result"]["isError"] is True
        assert br._pending == {}
    finally:
        await br.stop()


@pytest.mark.asyncio
async def test_a_child_that_exits_on_startup_fails_the_start_with_a_clear_reason():
    """A broken bundle, a bad flag, an index dir it can't open — the child
    dies before saying hello. Without this the next write hits a closed
    stdin and reports a BrokenPipeError that names nothing."""
    br = CodeGraphBridge(command=sys.executable, args=["-c", "pass"], env={})
    with pytest.raises(RuntimeError, match="exited without answering initialize"):
        await br.start()
    assert not br.running
