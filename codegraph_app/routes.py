"""Backend sub-app mounted at /api/apps/codegraph.

Three surfaces:

* ``POST /mcp`` — the Streamable-HTTP MCP endpoint aw-mcp-gateway dials as a
  ``{"type": "http"}`` upstream. A pure proxy onto the plugin's
  ``codegraph serve --mcp`` child; see ``mcp/bridge.py`` for why the child
  lives on this side of the hop at all. Same route shape as
  aw-app-notion's ``POST /mcp`` (``notion_app/routes.py``), which is the live
  precedent for this pattern — that one implements its tools in-process,
  this one forwards them.
* ``GET /status`` — flat scalars for the declarative window's ``auth_status``
  widget (which interpolates any scalar field of the response), plus the
  bridge/index detail a human debugging a stale answer actually needs.
* ``POST /reindex`` — force a full rebuild. Fire-and-forget: a full index of
  the workspace takes minutes, far longer than any HTTP client will wait.
"""
from __future__ import annotations

import asyncio

from fastapi import Body, FastAPI, Response
from fastapi.responses import JSONResponse

from . import plugin as plugin_mod


def build_routes(ctx, plugin: "plugin_mod.CodeGraphAppPlugin") -> FastAPI:
    app = FastAPI()

    @app.get("/status")
    async def status():
        index_root = plugin.index_root()
        cg = await plugin.status()
        bridge = plugin.bridge.snapshot()
        indexed = bool(cg.get("ok") and cg.get("initialized"))
        db_bytes = int(cg.get("dbSizeBytes") or 0) + int(cg.get("walSizeBytes") or 0)
        pending = cg.get("pendingChanges") or {}
        return {
            # auth_status reads `logged_in` to pick which line to render, and
            # `configured` to tell "saved but not working" from "never set up".
            # Here: configured == the CLI ran at all, logged_in == there is a
            # usable index behind it.
            "configured": bool(cg.get("ok")),
            "logged_in": indexed,
            "index_root": index_root,
            "indexed": indexed,
            "codegraph_version": cg.get("version") or "",
            "files": int(cg.get("fileCount") or 0),
            "nodes": int(cg.get("nodeCount") or 0),
            "edges": int(cg.get("edgeCount") or 0),
            "db_size_mb": round(db_bytes / (1024 * 1024), 1),
            "backend": cg.get("backend") or "",
            "journal_mode": cg.get("journalMode") or "",
            "languages": cg.get("languages") or [],
            "last_indexed": cg.get("lastIndexed") or "",
            "pending_changes": int(pending.get("added", 0)) + int(pending.get("modified", 0))
            + int(pending.get("removed", 0)),
            "native_watch": plugin.native_watch(),
            "reconcile_interval_s": float(ctx.config.get("reconcile_interval_s", 900)),
            "include_ignored": plugin.include_ignored(),
            "mcp_running": bridge["running"],
            "mcp_tool_count": bridge["tool_count"],
            "mcp_tools": bridge["tools"],
            "mcp_spawn_count": bridge["spawn_count"],
            "mcp_last_error": bridge["last_error"],
            "last_index": plugin.last_index,
            "last_sync": plugin.last_sync,
            "status_error": cg.get("error", ""),
        }

    @app.post("/reindex")
    async def reindex():
        asyncio.create_task(plugin.reindex())
        return {"started": True, "index_root": plugin.index_root()}

    @app.post("/mcp")
    async def mcp_post(data: dict | list = Body(...)):
        messages = data if isinstance(data, list) else [data]
        responses = []
        for message in messages:
            result = await plugin.bridge.handle_request(message)
            if result is not None:
                responses.append(result)
        if not responses:
            # Every message was a notification — Streamable HTTP's "accepted,
            # nothing to say" answer. A JSON body here would be a protocol
            # error, not an empty result.
            return Response(status_code=202)
        return JSONResponse(responses if isinstance(data, list) else responses[0])

    @app.get("/mcp")
    async def mcp_get():
        # No SSE channel: this upstream is request/response only, and the
        # gateway's HttpUpstream never opens a GET stream. 405 is the honest
        # answer (same as aw-app-notion's).
        return Response(status_code=405)

    return app
