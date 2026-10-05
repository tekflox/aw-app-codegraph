---
name: aw-codegraph
description: The workspace's code graph — every repo under /opt/aw-workspace indexed into one queryable graph of symbols, call paths and files, exposed to any agent on this tenant as `aw__codegraph__*` MCP tools. Use whenever asked to find where something is defined or called, trace a call chain, judge the blast radius of changing a symbol, check how fresh the index is, or debug why a code-graph answer looks stale.
---

# Code Graph (aw-app-codegraph)

This app installs [CodeGraph](https://github.com/colbymchenry/codegraph)
(`codegraph`) as a self-contained CLI, keeps `/opt/aw-workspace` indexed, and
exposes the result as MCP tools. It replaced `aw-app-codegraphcontext`
(`cgc`) — different engine, different storage, no graph visualizer.

## Reach for `codegraph_explore` first

The default and by far the most useful tool. One call takes a natural-language
query and returns:

* the **verbatim current source** of the symbols that matter, grouped by file
  and line-numbered — re-read from disk on that call, so it is as fresh as a
  `Read`, and you should **not** re-read a file it already showed you;
* the **blast radius** — who calls those symbols, and whether any test covers
  them within a few caller hops.

That is usually the whole answer. The narrower tools below are slices of what
explore already does, which is why CodeGraph itself hides them by default;
this app re-enables them because a shared gateway should not make a caller
reach for the wrong grain of tool.

| Tool | Use it when |
|---|---|
| `codegraph_explore` | Default. "Where does X happen / how does Y work." |
| `codegraph_node` | One symbol's source + its caller/callee trail, or a whole file with line numbers and dependents. |
| `codegraph_search` | You know the symbol name and only want to locate it. |
| `codegraph_callers` / `codegraph_callees` | One hop, in one direction, nothing else. |
| `codegraph_impact` | "What breaks if I change this." |
| `codegraph_files` | The indexed file structure — including which nested repos are in. |
| `codegraph_status` | Index freshness/size, before trusting an answer that looks wrong. |

Through the gateway they are prefixed: `aw__codegraph__codegraph_explore`,
and so on. **A newly installed or reloaded gateway only shows new tools to
NEW sessions** — if yours doesn't list them, that is why.

## What is indexed — one index, every repo

A single index at `/opt/aw-workspace` covers the workspace core **and** every
nested git repo under `repos/`. That needs an opt-in, because the workspace's
own `.gitignore` excludes `/repos/`: the app writes
`/opt/aw-workspace/codegraph.json` with

```json
{ "includeIgnored": ["repos/"] }
```

which is CodeGraph's own mechanism for a super-repo of gitignored child repos.
Each nested repo's **own** `.gitignore` is still honored, so nobody's
`node_modules` comes along.

Two deliberate exclusions:

* **`apps/<slug>` is NOT indexed.** Those are installed *copies* of apps,
  fetched from a release tag and overwritten wholesale on update. Indexing
  them would put a second, stale copy of code whose source already lives in
  `repos/aw-app-<slug>` into the graph, and every symbol would come back
  twice.
* **The index is not a secret store.** It holds source text. It never leaves
  the workspace — telemetry is off on both switches CodeGraph honors
  (`CODEGRAPH_TELEMETRY=0`, `DO_NOT_TRACK=1`) on every spawn, including the
  PATH shim an operator types by hand.

`codegraph.json` and `.codegraph/` are both gitignored in aw-workspace.

## Freshness — a real watcher, not a poll

`codegraph serve --mcp` starts its own debounced file watcher, so the index
folds in your edits as they land. This is the main reason the workspace
migrated off `cgc`, whose reconciler polled every 15 minutes.

`cgc`'s write-lock choreography is deliberately **not** ported. That existed
because KuzuDB is a single-writer store that fails any second opener;
CodeGraph stores the graph in SQLite with WAL, where a reader and a writer at
the same time is the designed-for case. So `codegraph status` is safe to run
against a live server, and nothing has to be paused to reindex.

If the watcher ever misbehaves (CPU, or the host's
`fs.inotify.max_user_watches` ceiling across the 80 nested repos here), set
`native_watch: false` in the app's config: the server is then started
`--no-watch` and a niced `codegraph sync` watchdog tick takes over at
`reconcile_interval_s` (default 900s). Measure before switching — it trades
real-time for calm.

## How the tools actually reach an agent

Worth understanding precisely, because it is not the usual shape.

```
agent ──► aw-gateway (own container)
             │  HTTP, {"type":"http"} upstream from apps/codegraph/mcp.json
             ▼
          POST /api/apps/codegraph/mcp     (this app's route, workspace container)
             │  JSON-RPC over stdin/stdout
             ▼
          codegraph serve --mcp            (one persistent child, this plugin owns it)
             │
             ▼
          /opt/aw-workspace/.codegraph/codegraph.db
```

The child **must** run in the workspace container, not the gateway's:

* The index lives at `<index_root>/.codegraph/` and cannot be moved.
  `CODEGRAPH_DIR` only *renames* that directory — a value containing a path
  separator, `..`, or an absolute path is rejected with a warning. So the trick
  `cgc` used (database under the app's own `.data`, which the gateway sees via
  its `$AW_APPS_ROOT` mount) is unavailable here.
* The gateway's container mounts `$AW_APPS_ROOT` and nothing else — it can see
  neither `repos/` nor the workspace root, so a child it spawned would have no
  index and no sources to read.
* `codegraph serve` is stdio-only; there is no HTTP transport to point the
  gateway at directly.

Hence the bridge (`codegraph_app/mcp/bridge.py`). Two things it guarantees:

* **A dead child is replaced transparently.** The caller that hit it gets an
  error; the next caller gets a fresh child. Killing the child by hand is a
  supported way to test that.
* **`initialize` never reaches the child twice.** The gateway re-dials on
  every reload; the bridge answers that handshake itself and initializes the
  child exactly once, at spawn.

## When an answer looks wrong

Work down this list rather than re-running the query:

1. **`aw-workspace-cli doctor`.** This workspace fails by degrading silently.
   A `codegraph` CLI that is present but broken is reported there and nowhere
   else, and the gateway serving zero tools for this upstream shows up there
   too.
2. **`GET /api/apps/codegraph/status`** (or the Code Graph window). It reports
   file/symbol/edge counts, DB size, when the index was last built, how many
   changes are pending, whether the MCP child is running, and how many tools
   it is serving. `files` far below the workspace's real file count means
   `includeIgnored` isn't taking effect.
3. **`codegraph status /opt/aw-workspace`** from a shell for the same numbers
   plus the per-language breakdown, and `codegraph files` to confirm which
   nested repos are in.
4. **A stale lock.** `codegraph unlock /opt/aw-workspace` clears one. The
   background daemon CodeGraph may run (`writer.pid`) is independent of this
   app's MCP child — stopping the app does not necessarily stop it.
5. **Rebuild**, last resort: the window's "Rebuild the index" button, or
   `POST /api/apps/codegraph/reindex`. It re-parses everything and takes
   minutes on a workspace this size; a full rebuild is only genuinely needed
   after changing *which* paths are indexed.

`CODEGRAPH_NO_DAEMON=1` forces the in-process engine, which is useful when
debugging a daemon that won't start. Don't set it for the long-lived server.

## Config

| Key | Default | Notes |
|---|---|---|
| `index_root` | `/opt/aw-workspace` | One index covers everything below it. |
| `include_ignored` | `["repos/"]` | Written into `codegraph.json`. Don't add `apps/`. |
| `native_watch` | `true` | Off → `--no-watch` + a `codegraph sync` watchdog tick. |
| `reconcile_interval_s` | `900` | Only used when `native_watch` is off. |
| `mcp_tools` | all 8 | CodeGraph lists only `explore` unless this allowlist says otherwise. |
