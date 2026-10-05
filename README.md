# aw-app-codegraph

A decoupled [aw-workspace](https://github.com/fredericowu/aw-workspace) app
that installs [CodeGraph](https://github.com/colbymchenry/codegraph), keeps
the whole workspace indexed, and serves the result to any agent on the tenant
as `aw__codegraph__*` MCP tools.

Replaces `aw-app-codegraphcontext` (`cgc`): different engine, different
storage (SQLite + FTS5 + WAL instead of KuzuDB), a real file watcher instead
of a 15-minute poll, and no graph visualizer — a known, accepted product loss.

For how to *use* it, read `skills/aw-codegraph/SKILL.md`. This file is for
whoever has to change it.

## Layout

| Path | What |
|---|---|
| `aw-app.json` | Manifest. Tier-1 (in-process), `id: codegraph`. |
| `scripts/install_codegraph.sh` | Downloads + sha512-verifies the pinned platform bundle, drops the `codegraph` PATH shim. |
| `codegraph_app/plugin.py` | `activate()`: install the CLI, write `codegraph.json` and `mcp.json`, register routes, build the index if absent, start the MCP child. |
| `codegraph_app/mcp/bridge.py` | The persistent `codegraph serve --mcp` child + JSON-RPC proxy. **Read its docstring before changing anything here.** |
| `codegraph_app/routes.py` | `POST /mcp` (the gateway's upstream), `GET /status`, `POST /reindex`. |
| `codegraph_app/project_config.py` | Self-heals `<index_root>/codegraph.json`. |
| `windows/main.json` | Status-only declarative window. No graph body. |

## The three decisions worth knowing before you edit

**1. One index at the workspace root, not one per repo.** `codegraph init` at
`/opt/aw-workspace` plus `{"includeIgnored": ["repos/"]}` in `codegraph.json`
covers the root repo and every nested git repo under `repos/`, respecting each
nested repo's own `.gitignore`. Verified against a synthetic tree of the same
shape before this app existed. So there is no per-repo init and no aggregation
glue — `codegraph files`/`status` already report the multi-repo structure.

**2. The MCP child runs here, not in the gateway.** The index can only live
next to the source (`CODEGRAPH_DIR` renames the data dir, it cannot relocate
it), the gateway's container can't see the source, and `codegraph serve` is
stdio-only. So this app runs the child and exposes it over HTTP — the same
shape `aw-app-notion` uses for `aw-kanban`. `codegraph_app/mcp/bridge.py`'s
docstring has the full argument.

**3. No write-lock choreography.** `cgc` needed `_visualizer_paused` and a
write lock because KuzuDB is single-writer. CodeGraph is SQLite in WAL mode.
Porting that machinery here would be cargo cult; `codegraph status` runs
safely against a live server.

## Tests

```bash
pip install -r requirements-dev.txt pytest jsonschema fastapi httpx uvicorn
python3 tests/validate_manifest.py aw-app.json --schema ../aw-marketplace/schemas/aw-app.schema.json
pytest tests/ -q --cov=codegraph_app --cov-report=term-missing
```

The gate in `pyproject.toml` is a hard 100%. The bridge's tests drive a **real
stdio child** (`tests/fake_mcp_child.py`) rather than a mock, because every
failure mode that matters there is process-level: a child that dies
mid-session, a second `initialize`, two callers' JSON-RPC ids crossing, a
response line past the reader's buffer limit. The installer's integrity check
is likewise exercised against a real tarball, with the download swapped for a
local copy.

No test imports `docker` — one that does passes locally and turns the
baremetal runner's CI red.

## Releasing

Push to `master`; `.github/workflows/release.yml` bumps the version, tags it,
and opens the catalog-sync PR against `tekflox/aw-marketplace`. Then install
**through the marketplace**, never by sideloading a `package_dir` — the
reconciler converges to the catalog and will quietly revert a sideload, and
the gateway's app-scan only ever looks at `apps/<slug>/mcp.json`.

The raw catalog can lag a merged sync PR by ~5 minutes (GitHub's raw CDN).
Poll `GET /api/apps/-/catalog` rather than debugging the install path.
