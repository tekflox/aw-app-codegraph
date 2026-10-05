"""Entrypoint referenced by aw-app.json's runtime.entrypoint
("codegraph_app.plugin:CodeGraphAppPlugin").

What activate() wires up, and why each piece is shaped the way it is:

* **One index at the workspace root.** ``codegraph init`` at ``index_root``
  covers the root repo AND every nested git repo named by ``include_ignored``
  (default ``repos/``) — verified against a synthetic tree of the same shape
  before this app was written: nested repos indexed, each nested
  ``.gitignore`` still respected, ``callers``/``query`` answering across repo
  boundaries from the single index. So there is no per-repo init and no
  aggregation glue; ``codegraph files``/``status`` already report the
  multi-repo structure. See ``project_config.py`` for the config file that
  makes it work.

* **An MCP child owned by this plugin, not by the gateway.** The index can
  only live next to the source and the gateway container can't see the
  source, so the child has to run here. ``mcp/bridge.py``'s docstring has the
  full argument; ``routes.py`` exposes it as a Streamable-HTTP endpoint the
  gateway dials like any other HTTP upstream.

* **The native file watcher, on by default.** This is the point of the
  migration away from CodeGraphContext: ``serve --mcp`` starts a debounced
  watcher itself, so the index tracks edits in near-real-time instead of
  being up to 15 minutes stale. None of cgc's write-lock choreography is
  ported — that existed because KuzuDB is a single-writer store that fails
  any second opener; CodeGraph's storage is SQLite in WAL mode, where
  concurrent readers and a writer are the designed-for case. A
  ``_visualizer_paused``-style lock here would be cargo cult.

  ``native_watch: false`` is the escape hatch if the watcher ever costs too
  much (CPU, or the host's ``fs.inotify.max_user_watches`` ceiling across the
  80 nested repos here): the child is then started with ``--no-watch`` and a
  low-priority ``codegraph sync`` watchdog tick takes over — cgc's old model,
  available but not the default.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import time

from . import mcp_config, project_config, routes as routes_mod
from .mcp.bridge import CodeGraphBridge

log = logging.getLogger("aw_apps.codegraph")

WATCHDOG_ID = "reconciler"

DEFAULT_INDEX_ROOT = "/opt/aw-workspace"
DEFAULT_MCP_TOOLS = ["explore", "node", "search", "callers", "callees",
                     "impact", "files", "status"]


def codegraph_shim_path() -> str:
    """The ``codegraph`` command install_codegraph.sh drops on the persistent
    bin dir.

    Deliberately a bare name, resolved via PATH at spawn time — NOT an
    absolute path computed by importing the host runtime's own
    ``src.apps.paths``. A Tier-1 app must only touch the host through the
    ``ctx`` facades, and importing ``src.apps`` would break this app's own
    standalone test/CI checkout, which has no ``src/`` tree. The installer
    guarantees ``$AW_WORKSPACE_HOME/bin`` is on PATH for the host process and
    everything it spawns.
    """
    return "codegraph"


class CodeGraphAppPlugin:
    async def activate(self, ctx) -> None:
        self.ctx = ctx
        self._initial_index_task: asyncio.Task | None = None
        # Read by routes.py's /status. last_index covers the one-shot initial
        # index; last_sync only ever moves when native_watch is off.
        self.last_index: dict = {"ok": None, "at": None, "detail": ""}
        self.last_sync: dict = {"ok": None, "at": None, "detail": ""}
        # Serializes this plugin's own `codegraph` write subprocesses against
        # each other (initial index vs a manual /reindex vs a sync tick).
        # NOT a port of cgc's `_cgc_write_lock`, which existed to keep two
        # KuzuDB writers apart: here it only avoids two full indexes of the
        # same tree racing each other for CPU and doing the same work twice.
        self._index_lock = asyncio.Lock()

        manifest = self._manifest()
        for cli in manifest.get("contributes", {}).get("system_clis", []):
            ctx.commands.install_system_cli(
                cli["name"], cli["installer"],
                uninstall="scripts/uninstall_codegraph.sh",
                verify=cli.get("verify"),
            )

        index_root = self.index_root()
        config_result = project_config.ensure_project_config(index_root, self.include_ignored())

        self.bridge = CodeGraphBridge(
            command=codegraph_shim_path(),
            args=self._serve_args(index_root),
            env=self.codegraph_env(),
            cwd=index_root,
        )

        mcp_doc = mcp_config.write_mcp_json(ctx.package_dir)
        ctx.routes.register(routes_mod.build_routes(ctx, self))

        self._initial_index_task = asyncio.create_task(self._run_initial_index(index_root))

        if not self.native_watch():
            ctx.watchdog.register(
                WATCHDOG_ID,
                self._sync_tick,
                interval_s=lambda: float(self.ctx.config.get("reconcile_interval_s", 900)),
                run_immediately=False,
            )

        log.info(
            "aw-app-codegraph activated (index_root=%s, codegraph.json written=%s, "
            "native_watch=%s, mcp servers=%s)",
            index_root, config_result["written"], self.native_watch(),
            list(mcp_doc["mcpServers"]),
        )

    # ── config ────────────────────────────────────────────────────────

    def _manifest(self) -> dict:
        with open(os.path.join(self.ctx.package_dir, "aw-app.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def index_root(self) -> str:
        return str(self.ctx.config.get("index_root") or DEFAULT_INDEX_ROOT)

    def include_ignored(self) -> list[str]:
        raw = self.ctx.config.get("include_ignored")
        if not isinstance(raw, list) or not raw:
            return list(project_config.DEFAULT_INCLUDE_IGNORED)
        return [str(p) for p in raw if str(p).strip()]

    def native_watch(self) -> bool:
        value = self.ctx.config.get("native_watch")
        return True if value is None else bool(value)

    def mcp_tools(self) -> list[str]:
        raw = self.ctx.config.get("mcp_tools")
        if not isinstance(raw, list) or not raw:
            return list(DEFAULT_MCP_TOOLS)
        return [str(t).strip() for t in raw if str(t).strip()]

    def codegraph_env(self) -> dict[str, str]:
        """Env for every ``codegraph`` spawn this app makes — installer,
        init, serve, sync, status.

        Telemetry off on both switches CodeGraph honors: nothing about this
        workspace's code should leave it, and an indexer is exactly the
        component with the most to leak.

        ``CODEGRAPH_MCP_TOOLS`` is a comma-separated allowlist of SHORT tool
        names, matched after stripping a ``codegraph_`` prefix (so either form
        works). Without it CodeGraph lists ONLY ``codegraph_explore`` — the
        other seven handlers stay fully functional but invisible, which for a
        shared gateway means seven tools nobody can call.

        HOME is deliberately NOT repointed into this app's ``.data`` (the
        trick aw-app-codegraphcontext uses to sandbox ``cgc``'s config):
        CodeGraph refuses to index a directory that is an ANCESTOR of
        ``$HOME`` (``directory.js``'s ``unsafeIndexRootReason``), and every
        path under this app's data dir is inside ``/opt/aw-workspace``. Moving
        HOME there would make ``codegraph init /opt/aw-workspace`` refuse with
        "a parent of your home directory". What CodeGraph keeps in ``$HOME``
        is only its daemon registry and update-check cache — regenerable
        runtime state, correct to lose on a container recreation.
        """
        return {
            "CODEGRAPH_TELEMETRY": "0",
            "DO_NOT_TRACK": "1",
            "CODEGRAPH_MCP_TOOLS": ",".join(self.mcp_tools()),
        }

    def _serve_args(self, index_root: str) -> list[str]:
        args = ["serve", "--mcp", "--path", index_root]
        if not self.native_watch():
            args.append("--no-watch")
        return args

    # ── indexing ──────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _single_worker(self, name: str):
        """Yield True to exactly one worker, False to every rival.

        A ``flock``, not a marker file, and that is core's own documented
        trade rather than a preference of this app's: see ``Lock D`` in
        ``src/api/app.py`` — "a flock cannot be unreachable — EAGAIN is itself
        the fact that another worker holds it — so the state is binary and the
        whole degrade-open branch is gone." An ``O_EXCL`` marker (tried here
        first) has the same defect in a different coat: it cannot tell "a
        worker is indexing right now" from "a worker died mid-index", so it
        needs an age heuristic, and getting that heuristic wrong leaves the
        workspace permanently un-indexed with nothing reporting it.

        The kernel releases an ``flock`` when the holder exits, so a worker
        killed mid-index simply loses it and the next boot retries. Nothing
        persistent is needed to stop a re-index on later boots either — once
        ``init`` finishes, ``.codegraph/`` exists and the caller's own
        directory check short-circuits before this is ever reached.

        ``fcntl`` is stdlib on purpose: importing the host's
        ``src.apps.fs_lock`` would break this app's standalone CI checkout,
        which has no ``src/`` tree.
        """
        path = os.path.join(self.ctx.package_dir, ".data", f".{name}.lock")
        # Decide the verdict BEFORE yielding, then yield exactly once. An
        # earlier version wrapped the yield in the same `try` as the lock
        # acquisition, which meant an exception from the `with` BODY — thrown
        # back in at the yield by contextlib — landed in that handler and
        # yielded a second time, so the caller got `RuntimeError: generator
        # didn't stop after throw()` INSTEAD of the real error. ENOSPC part
        # way through a 260 MB index write is the live version of that, and
        # it would have reported the wrong cause entirely.
        fd = None
        #: No lock mechanism available at all -> proceed rather than silently
        #: never indexing; CodeGraph's own writer lock still serializes the
        #: actual writes underneath us.
        held = True
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            log.warning("codegraph: no cross-worker lock for %s (%s)", name, exc)
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                held = False
        try:
            yield held
        finally:
            if fd is not None:
                os.close(fd)

    async def _run_initial_index(self, index_root: str) -> None:
        """One-shot, fire-and-forget: build the index if it doesn't exist yet.

        ``.codegraph/`` present means some index is already there, and the
        serve child catches up on start (and keeps up, with the watcher), so
        there is nothing to do — re-indexing on every boot would be a full
        re-parse of the workspace for no gain. ``init -y`` is non-interactive
        by design.

        The MCP child is deliberately NOT started here. See
        ``routes.py``'s ``/mcp`` handler: it is spawned lazily on the first
        request that needs it, because every core worker runs this same
        activate() and an eager start meant one ``codegraph serve --mcp``
        child PER WORKER — measured on the first install: 12 children,
        2.26 GB RSS and 12 independent file watchers over the same 80-repo
        tree, against a manifest estimate of ~600 MB. CodeGraph's own daemon
        election keeps the writes correct (its log says "Another daemon
        already holds the lock; exiting"), so this was pure waste rather than
        corruption — but it is waste proportional to the worker count.
        """
        try:
            if os.path.isdir(os.path.join(index_root, ".codegraph")):
                self.last_index = {
                    "ok": True, "at": time.time(),
                    "detail": f"{index_root}/.codegraph already present — "
                              f"serve syncs it on start",
                }
                return
            with self._single_worker("initial-index") as mine:
                if not mine:
                    self.last_index = {
                        "ok": None, "at": time.time(),
                        "detail": "another worker is building the initial index",
                    }
                    return
                async with self._index_lock:
                    rc, detail = await self._run_codegraph("init", "-y", index_root)
            self.last_index = {"ok": rc == 0, "at": time.time(), "detail": detail}
            if rc != 0:
                log.warning("codegraph: initial index exited %s: %s", rc, detail)
            else:
                log.info("codegraph: initial index of %s complete", index_root)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a background task must not vanish silently
            log.exception("codegraph: initial index failed")
            self.last_index = {"ok": False, "at": time.time(), "detail": str(exc)}

    async def _sync_tick(self) -> None:
        """The watchdog cadence body, registered ONLY when native_watch is
        off. ``sync`` (changes since the last index), never a full ``index``,
        and niced so it never competes with interactive work.

        Deliberately NOT guarded across workers, unlike the initial index.
        ``ctx.watchdog`` tasks are already single-owner: core gates its
        WatchdogSupervisor on a leader flock and genuinely ``pause()``es the
        losers, so exactly one worker ever runs a registered task (see
        ``src/apps/watchdog.py``'s "W1: leader mode" and ``Lock D`` in
        ``src/api/app.py``). Adding a second gate here would be the cargo
        cult this app avoided in dropping cgc's KuzuDB write-lock dance —
        and worse, it would hide a regression in core's gate behind a
        duplicate that silently covers for it.
        """
        argv = [codegraph_shim_path(), "sync", self.index_root(), "--quiet"]
        if shutil.which("nice"):
            argv = ["nice", "-n", "19", *argv]
        async with self._index_lock:
            rc, detail = await self._spawn(argv)
        self.last_sync = {"ok": rc == 0, "at": time.time(), "detail": detail}
        if rc != 0:
            raise RuntimeError(f"codegraph sync exited {rc}: {detail}")

    async def reindex(self) -> dict:
        """Force a full rebuild of the index (the window's button).

        ``index`` rather than ``init``: same result on an already-initialised
        project, and it doesn't care whether ``.codegraph/`` exists.
        """
        index_root = self.index_root()
        project_config.ensure_project_config(index_root, self.include_ignored())
        async with self._index_lock:
            rc, detail = await self._run_codegraph("index", index_root)
        self.last_index = {"ok": rc == 0, "at": time.time(), "detail": detail}
        return self.last_index

    async def _run_codegraph(self, *args: str) -> tuple[int, str]:
        return await self._spawn([codegraph_shim_path(), *args])

    async def _spawn(self, argv: list[str]) -> tuple[int, str]:
        """Run a one-shot ``codegraph`` subprocess, returning (rc, tail)."""
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env={**os.environ, **self.codegraph_env()},
            cwd=self.index_root(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        detail = out.decode(errors="replace")[-4000:]
        rc = proc.returncode if proc.returncode is not None else -1
        if rc < 0:
            detail += f"\n[process terminated by signal {-rc} — likely OOM-killed]"
        return rc, detail

    async def status(self) -> dict:
        """``codegraph status --json`` for the configured index root.

        Safe to run against a live serve child: the storage is SQLite in WAL
        mode, so a reader never blocks the writer or vice versa — the whole
        reason cgc's pause-the-visualizer dance isn't here.
        """
        rc, detail = await self._spawn([codegraph_shim_path(), "status",
                                        self.index_root(), "--json"])
        if rc != 0:
            return {"ok": False, "error": detail.strip()[-1000:]}
        try:
            return {"ok": True, **json.loads(detail)}
        except json.JSONDecodeError:
            return {"ok": False, "error": f"could not parse `codegraph status --json`: "
                                          f"{detail.strip()[-500:]}"}

    async def deactivate(self) -> None:
        if self._initial_index_task is not None and not self._initial_index_task.done():
            self._initial_index_task.cancel()
        await self.bridge.stop()
        log.info("aw-app-codegraph deactivated")
