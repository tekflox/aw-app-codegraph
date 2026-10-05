"""codegraph_app/plugin.py — the app's whole aw-workspace integration
surface (activate()/deactivate() plus the index/sync/status helpers).

Every `codegraph` subprocess is stubbed: the real binary is a 286 MB bundle
that is not present in CI, and what these tests are about is the argv and env
the plugin builds, not CodeGraph's own behaviour.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from codegraph_app import plugin as plugin_mod
from codegraph_app.plugin import CodeGraphAppPlugin

FAKE_CHILD = str(Path(__file__).resolve().parent / "fake_mcp_child.py")


@pytest.fixture
def quiet_bridge(monkeypatch):
    """Point the bridge at the fake child instead of the real `codegraph`
    shim, so activate() can start it for real without the bundle."""
    monkeypatch.setattr(plugin_mod, "codegraph_shim_path", lambda: sys.executable)
    return FAKE_CHILD


@pytest.fixture
def spawns(monkeypatch):
    """Record every one-shot subprocess the plugin would run."""
    calls: list[list[str]] = []

    async def fake_spawn(self, argv):
        calls.append(list(argv))
        return 0, "ok"

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", fake_spawn)
    return calls


async def _activate_and_index(plugin, ctx) -> None:
    """activate() then await the one-shot initial-index task to completion."""
    await plugin.activate(ctx)
    await plugin._initial_index_task


async def activate(ctx, *, start_bridge: bool = False) -> CodeGraphAppPlugin:
    plugin = CodeGraphAppPlugin()
    await plugin.activate(ctx)
    if not start_bridge and plugin._initial_index_task is not None:
        plugin._initial_index_task.cancel()
        try:
            await plugin._initial_index_task
        except asyncio.CancelledError:
            pass
    return plugin


# ── activate() wiring ─────────────────────────────────────────────────

def test_activate_installs_the_manifest_cli_through_the_gated_facade(make_ctx, spawns):
    ctx = make_ctx({"index_root": "/tmp/does-not-matter"})

    asyncio.run(activate(ctx))

    manifest = json.loads((Path(ctx.package_dir) / "aw-app.json").read_text())
    clis = manifest["contributes"]["system_clis"]
    assert ctx.commands.install_system_cli.call_count == len(clis)
    args, kwargs = ctx.commands.install_system_cli.call_args
    assert args == (clis[0]["name"], clis[0]["installer"])
    assert kwargs == {"uninstall": "scripts/uninstall_codegraph.sh",
                      "verify": clis[0]["verify"]}


def test_activate_registers_routes_and_writes_mcp_json(make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path)})

    asyncio.run(activate(ctx))

    ctx.routes.register.assert_called_once()
    doc = json.loads((Path(ctx.package_dir) / "mcp.json").read_text())
    assert list(doc["mcpServers"]) == ["codegraph"]
    assert doc["mcpServers"]["codegraph"]["type"] == "http"
    assert doc["mcpServers"]["codegraph"]["url"].endswith("/api/apps/codegraph/mcp")


def test_activate_writes_the_project_config_at_the_index_root(make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path), "include_ignored": ["repos/", "extra/"]})

    asyncio.run(activate(ctx))

    doc = json.loads((tmp_path / "codegraph.json").read_text())
    assert doc["includeIgnored"] == ["repos/", "extra/"]


def test_native_watch_on_registers_no_polling_watchdog(make_ctx, spawns, tmp_path):
    """The whole point of the migration: the serve child's own watcher keeps
    the index current, so there is no reconcile tick to register."""
    ctx = make_ctx({"index_root": str(tmp_path)})

    asyncio.run(activate(ctx))

    ctx.watchdog.register.assert_not_called()


def test_native_watch_off_registers_the_reconcile_watchdog(make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path), "native_watch": False,
                    "reconcile_interval_s": 60})

    asyncio.run(activate(ctx))

    ctx.watchdog.register.assert_called_once()
    args, kwargs = ctx.watchdog.register.call_args
    assert args[0] == plugin_mod.WATCHDOG_ID
    assert kwargs["run_immediately"] is False
    assert kwargs["interval_s"]() == 60.0


def test_serve_args_follow_native_watch(make_ctx, spawns, tmp_path):
    on = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))
    assert on.bridge.args == ["serve", "--mcp", "--path", str(tmp_path)]

    off = asyncio.run(activate(make_ctx({"index_root": str(tmp_path), "native_watch": False})))
    assert off.bridge.args == ["serve", "--mcp", "--path", str(tmp_path), "--no-watch"]


def test_the_bridge_child_runs_with_telemetry_off_and_the_tool_allowlist(
        make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path), "mcp_tools": ["explore", "callers"]})

    plugin = asyncio.run(activate(ctx))

    assert plugin.bridge.env == {
        "CODEGRAPH_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
        "CODEGRAPH_MCP_TOOLS": "explore,callers",
    }


def test_the_default_tool_allowlist_covers_all_eight_tools(make_ctx, spawns, tmp_path):
    """CodeGraph lists ONLY codegraph_explore unless an allowlist says
    otherwise — the other seven handlers exist but are invisible."""
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    assert plugin.bridge.env["CODEGRAPH_MCP_TOOLS"] == (
        "explore,node,search,callers,callees,impact,files,status")


def test_env_never_repoints_home_into_the_workspace_tree(make_ctx, spawns, tmp_path):
    """CodeGraph refuses to index an ancestor of $HOME, and every path under
    this app's .data is inside /opt/aw-workspace — so moving HOME there would
    make `codegraph init /opt/aw-workspace` refuse outright."""
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    assert "HOME" not in plugin.codegraph_env()


# ── config accessors ─────────────────────────────────────────────────

@pytest.mark.parametrize("config,expected", [
    ({}, "/opt/aw-workspace"),
    ({"index_root": ""}, "/opt/aw-workspace"),
    ({"index_root": "/srv/code"}, "/srv/code"),
])
def test_index_root_defaults(make_ctx, spawns, config, expected):
    plugin = asyncio.run(activate(make_ctx(config)))
    assert plugin.index_root() == expected


@pytest.mark.parametrize("config,expected", [
    ({}, ["repos/"]),
    ({"include_ignored": []}, ["repos/"]),
    ({"include_ignored": "repos/"}, ["repos/"]),
    ({"include_ignored": ["a/", " ", "b/"]}, ["a/", "b/"]),
])
def test_include_ignored_falls_back_to_the_default(make_ctx, spawns, tmp_path,
                                                   config, expected):
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path), **config})))
    assert plugin.include_ignored() == expected


@pytest.mark.parametrize("config,expected", [
    ({}, True),
    ({"native_watch": None}, True),
    ({"native_watch": False}, False),
    ({"native_watch": 0}, False),
])
def test_native_watch_defaults_to_on(make_ctx, spawns, tmp_path, config, expected):
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path), **config})))
    assert plugin.native_watch() is expected


@pytest.mark.parametrize("config", [{"mcp_tools": []}, {"mcp_tools": "explore"}])
def test_mcp_tools_falls_back_to_the_default_list(make_ctx, spawns, tmp_path, config):
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path), **config})))
    assert plugin.mcp_tools() == plugin_mod.DEFAULT_MCP_TOOLS


# ── indexing ──────────────────────────────────────────────────────────

def test_initial_index_builds_the_index_when_none_exists(make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path)})
    plugin = CodeGraphAppPlugin()

    async def run():
        await plugin.activate(ctx)
        await plugin._initial_index_task

    asyncio.run(run())

    assert [plugin_mod.codegraph_shim_path(), "init", "-y", str(tmp_path)] in spawns
    assert plugin.last_index["ok"] is True


def test_the_initial_index_does_not_start_the_mcp_child(make_ctx, spawns, tmp_path):
    """Every core worker runs this same activate(). Starting the child here
    meant one `codegraph serve --mcp` per worker — measured on the first real
    install: 12 children, 2.26 GB RSS and 12 file watchers over one 80-repo
    tree, against a ~600 MB estimate. It is spawned lazily instead, by the
    first MCP request that actually needs it."""
    ctx = make_ctx({"index_root": str(tmp_path)})
    plugin = CodeGraphAppPlugin()

    async def run():
        await plugin.activate(ctx)
        await plugin._initial_index_task

    asyncio.run(run())

    assert plugin.bridge.spawn_count == 0
    assert not plugin.bridge.running


def test_initial_index_skips_an_already_indexed_root(make_ctx, spawns, tmp_path):
    """Re-indexing on every boot would re-parse the whole workspace for
    nothing — the serve child catches up on start."""
    (tmp_path / ".codegraph").mkdir()
    ctx = make_ctx({"index_root": str(tmp_path)})
    plugin = CodeGraphAppPlugin()

    async def run():
        await plugin.activate(ctx)
        await plugin._initial_index_task

    asyncio.run(run())

    assert not any("init" in argv for argv in spawns)
    assert plugin.last_index["ok"] is True
    assert "already present" in plugin.last_index["detail"]


def test_a_failing_initial_index_is_recorded_not_raised(make_ctx, tmp_path, monkeypatch):
    async def failing_spawn(self, argv):
        return 1, "could not open the project"

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", failing_spawn)
    plugin = CodeGraphAppPlugin()

    asyncio.run(_activate_and_index(plugin, make_ctx({"index_root": str(tmp_path)})))

    assert plugin.last_index["ok"] is False
    assert "could not open" in plugin.last_index["detail"]


def test_reindex_runs_a_full_index_and_reasserts_the_project_config(
        make_ctx, spawns, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path)})
    plugin = asyncio.run(activate(ctx))
    (tmp_path / "codegraph.json").unlink()

    result = asyncio.run(plugin.reindex())

    assert [plugin_mod.codegraph_shim_path(), "index", str(tmp_path)] in spawns
    assert result["ok"] is True
    assert (tmp_path / "codegraph.json").exists()


def test_sync_tick_is_niced_and_uses_sync_not_index(make_ctx, spawns, tmp_path,
                                                    monkeypatch):
    monkeypatch.setattr(plugin_mod.shutil, "which", lambda name: "/usr/bin/nice")
    ctx = make_ctx({"index_root": str(tmp_path), "native_watch": False})
    plugin = asyncio.run(activate(ctx))

    asyncio.run(plugin._sync_tick())

    assert spawns[-1] == ["nice", "-n", "19", plugin_mod.codegraph_shim_path(),
                          "sync", str(tmp_path), "--quiet"]
    assert plugin.last_sync["ok"] is True


def test_sync_tick_without_nice_still_runs(make_ctx, spawns, tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_mod.shutil, "which", lambda name: None)
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path),
                                            "native_watch": False})))

    asyncio.run(plugin._sync_tick())

    assert spawns[-1] == [plugin_mod.codegraph_shim_path(), "sync",
                          str(tmp_path), "--quiet"]


def test_a_failing_sync_tick_raises_so_the_watchdog_sees_it(make_ctx, tmp_path,
                                                            monkeypatch):
    async def failing_spawn(self, argv):
        return 2, "locked"

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", failing_spawn)
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path),
                                            "native_watch": False})))

    with pytest.raises(RuntimeError, match="exited 2"):
        asyncio.run(plugin._sync_tick())
    assert plugin.last_sync["ok"] is False


# ── status ────────────────────────────────────────────────────────────

STATUS_JSON = {
    "initialized": True, "version": "1.6.2", "fileCount": 12345,
    "nodeCount": 99, "edgeCount": 88, "dbSizeBytes": 2 * 1024 * 1024,
    "walSizeBytes": 0, "backend": "node-sqlite", "journalMode": "wal",
    "languages": ["python"], "lastIndexed": "2026-10-05T18:00:00.000Z",
    "pendingChanges": {"added": 1, "modified": 2, "removed": 0},
}


def test_status_parses_codegraph_status_json(make_ctx, tmp_path, monkeypatch):
    async def fake_spawn(self, argv):
        assert argv[-2:] == [str(tmp_path), "--json"]
        return 0, json.dumps(STATUS_JSON)

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", fake_spawn)
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    assert asyncio.run(plugin.status())["fileCount"] == 12345


def test_status_reports_a_nonzero_exit_rather_than_raising(make_ctx, tmp_path,
                                                           monkeypatch):
    async def fake_spawn(self, argv):
        return 1, "not initialized"

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", fake_spawn)
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    result = asyncio.run(plugin.status())
    assert result == {"ok": False, "error": "not initialized"}


def test_status_reports_unparseable_output(make_ctx, tmp_path, monkeypatch):
    async def fake_spawn(self, argv):
        return 0, "a progress bar leaked onto stdout"

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", fake_spawn)
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    result = asyncio.run(plugin.status())
    assert result["ok"] is False
    assert "could not parse" in result["error"]


# ── subprocess plumbing ──────────────────────────────────────────────

def test_spawn_returns_the_exit_code_and_output_tail(make_ctx, tmp_path):
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    rc, detail = asyncio.run(plugin._spawn(
        [sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"]))

    assert rc == 3
    assert "hello" in detail


def test_spawn_flags_a_signal_death_as_a_likely_oom(make_ctx, tmp_path):
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    rc, detail = asyncio.run(plugin._spawn(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]))

    assert rc == -9
    assert "OOM-killed" in detail


# ── deactivate ───────────────────────────────────────────────────────

def test_deactivate_stops_the_child_and_cancels_the_index_task(make_ctx, spawns,
                                                               quiet_bridge, tmp_path):
    ctx = make_ctx({"index_root": str(tmp_path)})
    plugin = CodeGraphAppPlugin()

    async def run():
        await plugin.activate(ctx)
        plugin.bridge.args = [quiet_bridge]
        await plugin.bridge.start()
        assert plugin.bridge.running
        await plugin.deactivate()

    asyncio.run(run())

    assert not plugin.bridge.running
    assert plugin._initial_index_task.cancelled() or plugin._initial_index_task.done()


def test_an_unexpected_failure_in_the_index_task_is_recorded_not_swallowed(
        make_ctx, tmp_path, monkeypatch):
    """A fire-and-forget task that dies silently is how an app comes to look
    healthy with no index behind it."""
    def exploding_lock(self, name):
        raise RuntimeError("the filesystem went away")

    monkeypatch.setattr(CodeGraphAppPlugin, "_single_worker", exploding_lock)
    plugin = CodeGraphAppPlugin()

    asyncio.run(_activate_and_index(plugin, make_ctx({"index_root": str(tmp_path)})))

    assert plugin.last_index["ok"] is False
    assert "filesystem went away" in plugin.last_index["detail"]


def test_deactivate_cancels_an_initial_index_still_in_flight(make_ctx, tmp_path,
                                                             monkeypatch):
    """A full index takes minutes. An uninstall/reload mid-build must not
    leave the task (and its subprocess) running against a half-removed app."""
    started = asyncio.Event()

    async def hanging_spawn(self, argv):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", hanging_spawn)
    plugin = CodeGraphAppPlugin()

    async def run():
        await plugin.activate(make_ctx({"index_root": str(tmp_path)}))
        await started.wait()
        await plugin.deactivate()

    asyncio.run(run())

    assert plugin._initial_index_task.cancelled()
    assert not plugin.bridge.running


# ── cross-worker guards ──────────────────────────────────────────────

def test_only_one_worker_builds_the_initial_index(make_ctx, tmp_path):
    """The race this replaces was observed live on the first install: with a
    plain isdir() guard, one worker saw .codegraph absent and ran init while
    another saw the directory init had just created and reported "already
    present"."""
    ctx = make_ctx({"index_root": str(tmp_path)})
    first, second = CodeGraphAppPlugin(), CodeGraphAppPlugin()
    second_ctx = MagicMock()
    second_ctx.package_dir = ctx.package_dir   # same app dir == same lock file
    second_ctx.config = {"index_root": str(tmp_path)}

    ran = []

    async def slow_spawn(self, argv):
        ran.append(argv)
        await asyncio.sleep(0.2)   # hold the lock long enough to overlap
        return 0, "ok"

    async def run():
        await first.activate(ctx)
        await second.activate(second_ctx)
        first._spawn = slow_spawn.__get__(first)
        second._spawn = slow_spawn.__get__(second)
        await asyncio.gather(first._initial_index_task, second._initial_index_task)

    asyncio.run(run())

    assert len(ran) == 1, f"expected exactly one init, got {len(ran)}"
    assert len([p for p in (first, second) if p.last_index["ok"] is True]) == 1
    deferred = [p for p in (first, second) if p.last_index["ok"] is None]
    assert len(deferred) == 1
    assert "another worker" in deferred[0].last_index["detail"]


def test_the_lock_is_released_so_a_later_boot_can_retry(make_ctx, spawns, tmp_path):
    """A flock, not a marker: a worker killed mid-index must not leave the
    workspace permanently un-indexed. Nothing persistent is written, so a
    fresh plugin on the same root indexes again."""
    ctx = make_ctx({"index_root": str(tmp_path)})
    asyncio.run(_activate_and_index(CodeGraphAppPlugin(), ctx))
    assert len([a for a in spawns if "init" in a]) == 1

    # No marker file left behind to block the retry.
    assert not list(tmp_path.glob(".codegraph.aw-init"))

    second = CodeGraphAppPlugin()
    asyncio.run(_activate_and_index(second, make_ctx({"index_root": str(tmp_path)})))
    assert len([a for a in spawns if "init" in a]) == 2


def test_the_sync_tick_is_not_double_gated(make_ctx, spawns, tmp_path):
    """ctx.watchdog tasks are already single-owner via core's watchdog leader
    flock (src/apps/watchdog.py "W1: leader mode"), so every registered tick
    must actually run — a second gate here would hide a regression in core's."""
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path),
                                            "native_watch": False})))

    asyncio.run(plugin._sync_tick())
    asyncio.run(plugin._sync_tick())

    assert len([a for a in spawns if "sync" in a]) == 2
    assert plugin.last_sync["ok"] is True


def test_an_unlockable_data_dir_still_indexes_rather_than_going_quiet(
        make_ctx, spawns, tmp_path, monkeypatch):
    """Losing the lock mechanism must not silently mean "never index" —
    CodeGraph's own writer lock is still underneath."""
    monkeypatch.setattr(plugin_mod.os, "makedirs",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    plugin = CodeGraphAppPlugin()

    asyncio.run(_activate_and_index(plugin, make_ctx({"index_root": str(tmp_path)})))

    assert any("init" in a for a in spawns)
    assert plugin.last_index["ok"] is True


def test_an_unwritable_index_root_records_the_failure_rather_than_raising(
        make_ctx, tmp_path):
    """The lock lives in this app's own .data, so an unwritable index_root no
    longer costs the claim — `init` just fails, and that has to be reported
    rather than escaping a background task."""
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        plugin = CodeGraphAppPlugin()
        asyncio.run(_activate_and_index(plugin, make_ctx({"index_root": str(root)})))
        assert plugin.last_index["ok"] is False
    finally:
        root.chmod(0o700)


def test_an_error_from_the_guarded_body_reaches_the_caller_unchanged(
        make_ctx, tmp_path):
    """The verdict and the yield used to share one `try`, so an exception from
    the `with` BODY — thrown back in at the yield by contextlib — landed in
    the lock's own except handler and yielded a second time. The caller then
    got `RuntimeError: generator didn't stop after throw()` instead of the
    real failure. ENOSPC part way through a 260 MB index write is the live
    version of that, and it would have named the wrong cause entirely."""
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    with pytest.raises(OSError, match="simulated ENOSPC"):
        with plugin._single_worker("initial-index") as mine:
            assert mine is True
            raise OSError(28, "simulated ENOSPC")


def test_a_failing_index_reports_the_real_error_not_a_generator_error(
        make_ctx, tmp_path, monkeypatch):
    """The same bug seen through the path that actually matters: whatever
    `codegraph init` failed with has to be what lands in last_index."""
    async def exploding_spawn(self, argv):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(CodeGraphAppPlugin, "_spawn", exploding_spawn)
    plugin = CodeGraphAppPlugin()

    asyncio.run(_activate_and_index(plugin, make_ctx({"index_root": str(tmp_path)})))

    assert plugin.last_index["ok"] is False
    assert "No space left on device" in plugin.last_index["detail"]
    assert "generator didn't stop" not in plugin.last_index["detail"]


def test_the_lock_fd_is_closed_even_when_the_body_raises(make_ctx, tmp_path):
    """A leaked fd would hold the flock for the life of the process, so the
    next boot's index would defer forever to a worker that already failed."""
    plugin = asyncio.run(activate(make_ctx({"index_root": str(tmp_path)})))

    with contextlib.suppress(OSError):
        with plugin._single_worker("initial-index"):
            raise OSError("boom")

    # The lock is free again: a fresh acquisition in the same process wins.
    with plugin._single_worker("initial-index") as mine:
        assert mine is True
