"""One persistent ``codegraph serve --mcp`` stdio child, proxied to callers
over JSON-RPC — the whole reason this app can serve MCP tools at all.

Why a bridge rather than letting the gateway spawn the child itself (the
pattern aw-app-codegraphcontext uses for ``cgc``):

* The index lives at ``<index_root>/.codegraph/`` and that location is NOT
  relocatable. ``CODEGRAPH_DIR`` only RENAMES the directory — a value with a
  path separator, ``..`` or an absolute path is rejected outright with a
  warning (``lib/dist/directory.js``'s ``codeGraphDirName``). So the trick
  cgc uses — put the database under the app's own ``.data``, which the
  gateway container sees through its ``$AW_APPS_ROOT`` mount — is simply not
  available here. The index can only ever be next to the source.
* The mcp-gateway container mounts ``$AW_APPS_ROOT`` and nothing else. It can
  see neither ``repos/`` nor the workspace root, so a child it spawned would
  have no index and no sources to read.
* ``codegraph serve`` is stdio-only — there is no HTTP transport to point the
  gateway's ``{"type": "http"}`` upstream at directly (``serve --help``).

The child therefore has to run HERE, in the workspace container, where the
index, the sources and the daemon socket all are; and the gateway has to
reach it over HTTP. That is exactly the shape aw-app-notion already uses for
``aw-kanban`` (``notion_app/mcp/http_handler.py``) — except that one
implements its tools in-process, while this is a pure proxy.

The child-process framing below mirrors aw-mcp-gateway's own stdio
``Upstream`` (``back/gateway/upstream.py``), smaller: newline-delimited JSON
on stdin/stdout, a single reader task, and per-call futures dispatched by
JSON-RPC id. Two properties are load-bearing:

* **Wire ids are ours, never the caller's.** Independent MCP clients number
  their requests from their own counters, so two concurrent callers routinely
  pick the same id; keying ``_pending`` on that handed one caller the other's
  result. The caller's id is restored on the way out.
* **``initialize`` is answered HERE and never forwarded.** The gateway
  re-dials (and re-initializes) on every reload, and a fresh handshake into a
  child that is already serving a session is both pointless and a way to
  reset per-session state. The bridge initializes the child exactly once, at
  spawn, and answers the gateway's own handshake from its own constants.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os

log = logging.getLogger("aw_apps.codegraph")

SERVER_NAME = "codegraph"
PROTOCOL_VERSION = "2024-11-05"

#: A child that never answers its handshake must not hang a request forever.
#: The first spawn on a cold index is the slow case (CodeGraph opens the DB
#: and plans a catch-up sync), so this is generous compared with the
#: gateway's own 30s.
HANDSHAKE_TIMEOUT_S = 120.0

#: ``tools/call`` budget. ``codegraph_explore`` over this workspace's index
#: (measured 2026-10-05: 3,698 files, 75k nodes, 186k edges, a 263 MB DB
#: across 80 nested repos) does real work — FTS5 query, call-path expansion,
#: re-reading the matched files from disk — and the gateway's own HttpUpstream
#: allows read=600s, so a shorter budget here would only move the timeout to
#: the wrong side of the hop. ``tools/list`` is answered from memory and gets
#: the handshake budget instead.
CALL_TIMEOUT_S = 300.0

#: StreamReader buffer for the child's stdout. asyncio's default is 64KB and a
#: ``codegraph_explore`` result carrying the verbatim source of several files
#: is routinely far larger — the line would come back as a LimitOverrunError
#: and be dropped, which looks like a tool that silently returns nothing.
STDOUT_LIMIT_BYTES = 32 * 1024 * 1024

_wire_ids = itertools.count(1)


def next_wire_id() -> str:
    """A process-unique JSON-RPC id for one in-flight call to the child."""
    return f"cgb-{next(_wire_ids)}"


def _result(req_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": payload}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class _ChildGone(Exception):
    """Internal: the child exited mid-handshake. Normalises the three ways
    that surfaces (EOF on read, ConnectionResetError or BrokenPipeError on
    write) into one branch — see ``_handshake``."""


def _tool_error(req_id, text: str) -> dict:
    """A tools/call failure reported as an MCP *result* with isError, not a
    JSON-RPC error: the gateway surfaces the text to the agent either way,
    but an isError result keeps the agent's own retry/readability path
    (same choice aw-app-notion's handler makes)."""
    return _result(req_id, {"content": [{"type": "text", "text": text}], "isError": True})


class CodeGraphBridge:
    """Owns the ``codegraph serve --mcp`` child and speaks JSON-RPC to it."""

    def __init__(self, *, command: str, args: list[str], env: dict[str, str],
                 cwd: str | None = None, server_version: str = "1.6.2"):
        self.command = command
        self.args = list(args)
        self.env = dict(env)
        self.cwd = cwd
        self.server_version = server_version
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[dict] = []
        self.spawn_count = 0
        self.last_error: str = ""
        self._lifecycle_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    # ── child lifecycle ────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _spawn(self) -> None:
        env = {**os.environ, **self.env}
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # CodeGraph logs progress/warnings on stderr and the pipe is
            # never drained, so a long-lived child would eventually block on
            # a full stderr buffer. DEVNULL, same as the gateway does for its
            # own stdio children.
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
            env=env,
            limit=STDOUT_LIMIT_BYTES,
        )
        self.spawn_count += 1
        log.info("codegraph: spawned MCP child (pid %s, spawn #%s)",
                 self.proc.pid, self.spawn_count)

    async def _ensure_alive(self) -> None:
        """Spawn + handshake the child if it isn't running. Called under
        ``_lifecycle_lock`` — a child that died mid-session is replaced here,
        transparently, on the next request."""
        if self.running:
            return
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._reader_task = None
        self._fail_pending("the codegraph MCP child was restarted")
        await self._spawn()
        await self._handshake()
        self._reader_task = asyncio.get_running_loop().create_task(
            self._reader_loop(), name="codegraph-mcp-reader")

    def _fail_pending(self, reason: str) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending.clear()

    async def _handshake(self) -> None:
        """The one and only ``initialize`` the child ever sees, plus the
        ``tools/list`` whose answer the bridge then serves from memory."""
        try:
            await self._write({
                "jsonrpc": "2.0", "id": "init", "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "aw-app-codegraph", "version": "1.0.0"}},
            })
            hello = await asyncio.wait_for(self._read_direct(),
                                           timeout=HANDSHAKE_TIMEOUT_S)
            if hello is None:
                raise _ChildGone()
            await self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            await self._write({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})
            listed = await asyncio.wait_for(self._read_direct(), timeout=HANDSHAKE_TIMEOUT_S)
            if listed is None:
                raise _ChildGone()
        except asyncio.TimeoutError:
            await self.stop()
            raise RuntimeError(
                f"the codegraph MCP child did not answer its handshake within "
                f"{HANDSHAKE_TIMEOUT_S}s") from None
        except (_ChildGone, ConnectionResetError, BrokenPipeError):
            # The child exited during its own handshake — a broken bundle, a
            # bad flag, an index dir it cannot open. Which of the three ways
            # that surfaces is a RACE: if the child is already gone when we
            # write, asyncio's drain() raises ConnectionResetError/
            # BrokenPipeError; if the write lands first, we instead read EOF.
            # Found by the test suite, where adding coverage instrumentation
            # was enough to flip which branch won. Both mean the same thing
            # and must report the same way, because the raw asyncio errors
            # ("Connection lost") name neither the child nor the cause.
            await self.stop()
            raise RuntimeError(
                "the codegraph MCP child exited without completing its MCP "
                "handshake — check `aw-workspace-cli doctor` and "
                "`codegraph status`") from None
        if listed and "error" in listed:
            # A tools/list that ERRORED is not the same thing as a child with
            # no tools — swallowing it would start the bridge "successfully"
            # with an empty tool surface, which is how a broken upstream
            # looks identical to a healthy one that has nothing to offer.
            await self.stop()
            raise RuntimeError(f"codegraph tools/list returned an error: {listed['error']}")
        self.tools = (listed or {}).get("result", {}).get("tools", [])
        if not self.tools:
            await self.stop()
            raise RuntimeError("the codegraph MCP child listed zero tools")
        log.info("codegraph: MCP child ready, %s tools: %s",
                 len(self.tools), [t.get("name") for t in self.tools])

    async def _reader_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        while True:
            try:
                line = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError) as exc:
                # A single oversized line can't be recovered from mid-stream;
                # drop the child so the next request respawns it.
                log.warning("codegraph: MCP child stdout overran the read limit (%s)", exc)
                line = b""
            if not line:
                # EOF — the child exited. Fail everything in flight rather
                # than letting those callers hang on a future nobody will
                # ever resolve.
                self._fail_pending("the codegraph MCP child exited")
                self.proc = None
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def _write(self, msg: dict) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self.proc.stdin.drain()

    async def _read_direct(self) -> dict | None:
        """Read one message straight off stdout — handshake only, before the
        reader task exists."""
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._ensure_alive()

    async def stop(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._reader_task = None
        proc, self.proc = self.proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self._fail_pending("the codegraph MCP child was stopped")

    # ── JSON-RPC ──────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict, timeout: float) -> dict:
        """One request/response round-trip against the child, respawning it
        first if it died."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        wire_id = next_wire_id()
        async with self._lifecycle_lock:
            await self._ensure_alive()
            self._pending[wire_id] = fut
            try:
                await self._write({"jsonrpc": "2.0", "id": wire_id,
                                   "method": method, "params": params})
            except Exception:
                self._pending.pop(wire_id, None)
                raise
        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(wire_id, None)
            raise
        return resp

    async def handle_request(self, request: dict) -> dict | None:
        """Answer one inbound JSON-RPC message from the gateway.

        ``None`` means "no response" (a notification), which the route turns
        into a 202.
        """
        method = request.get("method", "")
        req_id = request.get("id")

        if method == "initialize":
            # Answered here, never forwarded — see the module docstring. Must
            # be idempotent: the gateway re-dials on every reload.
            return _result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": self.server_version},
            })
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return _result(req_id, {})

        if method == "tools/list":
            try:
                resp = await self._request("tools/list", {}, HANDSHAKE_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"tools/list: {exc}"
                log.warning("codegraph: tools/list failed (%s)", exc)
                # The gateway treats an errored tools/list as a failed start,
                # which is the honest answer — better than reporting zero
                # tools, which reads as a healthy upstream with nothing in it.
                return _error(req_id, -32603, f"codegraph tools/list failed: {exc}")
            self.tools = resp.get("result", {}).get("tools", self.tools)
            return _result(req_id, {"tools": self.tools})

        if method == "tools/call":
            params = request.get("params", {}) or {}
            name = params.get("name", "")
            # The gateway injects per-caller context into tool arguments for
            # the upstreams that want it; CodeGraph validates its input
            # schemas strictly, so anything it never declared is stripped
            # rather than forwarded (the same leak that broke every
            # aw__notion__* call once).
            arguments = {k: v for k, v in (params.get("arguments") or {}).items()
                         if not k.startswith("_")}
            try:
                resp = await self._request(
                    "tools/call", {"name": name, "arguments": arguments}, CALL_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.last_error = f"{name}: timed out after {CALL_TIMEOUT_S}s"
                return _tool_error(req_id, f"{name} timed out after {CALL_TIMEOUT_S}s")
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"{name}: {exc}"
                log.warning("codegraph: tools/call %s failed (%s)", name, exc)
                return _tool_error(req_id, f"{name} failed: {exc}")
            self.last_error = ""
            resp["id"] = req_id
            return resp

        return _error(req_id, -32601, f"Unknown method: {method}")

    def snapshot(self) -> dict:
        """Plain-scalar health, for this app's own /status route."""
        return {
            "running": self.running,
            "pid": self.proc.pid if self.running and self.proc else None,
            "spawn_count": self.spawn_count,
            "tool_count": len(self.tools),
            "tools": [t.get("name") for t in self.tools],
            "last_error": self.last_error,
        }
