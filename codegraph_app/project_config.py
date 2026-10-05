"""Self-heals ``<index_root>/codegraph.json`` — CodeGraph's own per-project
config file.

Why this file has to exist at all: CodeGraph respects the indexed tree's
``.gitignore``, and this workspace's root ``.gitignore`` excludes ``/repos/``
(see that file's own comment — ``repos/`` is runtime state that happens to
live inside the repo's working tree). Without an opt-in, the ~50 nested git
repos under ``repos/`` are never discovered and the index covers only the
workspace core. ``includeIgnored`` is CodeGraph's answer to exactly that
shape (upstream #622/#699/#1156): gitignore-style patterns naming gitignored
directories whose embedded git repos get indexed anyway. Each nested repo's
OWN ``.gitignore`` is still honored, so this widens discovery without
dragging in anyone's ``node_modules``.

The filename and location are not configurable — ``PROJECT_CONFIG_FILENAME``
is hardcoded to ``codegraph.json`` and resolved relative to the project root
(``lib/dist/project-config.js``). So this app writes into the indexed tree,
which is why ``/codegraph.json`` is gitignored in aw-workspace.

``apps/`` is deliberately NOT in the default patterns: ``apps/<slug>`` is an
installed COPY of an app, fetched from its release tag and overwritten
wholesale on update — indexing it would put a second, stale copy of code
whose source already lives in ``repos/aw-app-<slug>`` into the graph, and
every symbol would come back twice.

Rewritten on every activate so an operator's hand-edit converges back to the
app's config, and so a fresh workspace (where the file does not exist yet)
gets one. The write is skipped when nothing changed: CodeGraph mtime-caches
this file per project root and re-reads it on every index/scan/watch event,
so a pointless rewrite invalidates that cache for no reason.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("aw_apps.codegraph")

#: Hardcoded in CodeGraph (``project-config.js``'s ``PROJECT_CONFIG_FILENAME``).
PROJECT_CONFIG_FILENAME = "codegraph.json"

DEFAULT_INCLUDE_IGNORED = ["repos/"]


def build_project_config(include_ignored: list[str], existing: dict | None = None) -> dict:
    """The config document to write — ``existing`` merged with our key.

    Every other key is preserved: ``codegraph.json`` is a file an operator (or
    a future CodeGraph feature) may legitimately carry ``extensions`` /
    ``exclude`` / ``deprioritize`` in, and this app only owns
    ``includeIgnored``. Mirrors upstream's own
    ``addIncludeIgnoredPatterns``, which also preserves unknown keys.
    """
    doc = dict(existing or {})
    doc["includeIgnored"] = list(include_ignored)
    return doc


def ensure_project_config(index_root: str, include_ignored: list[str] | None = None) -> dict:
    """Write ``<index_root>/codegraph.json``, skipping a no-op write.

    Returns ``{"path", "written", "include_ignored"}``. Never raises for a
    reason the caller can't act on — a malformed existing file is replaced
    (CodeGraph would ignore it with a warning anyway, which means the nested
    repos would silently drop out of the index), and an unwritable index_root
    is logged and reported as ``written: False`` rather than taking down
    activate().
    """
    patterns = list(include_ignored if include_ignored is not None else DEFAULT_INCLUDE_IGNORED)
    path = Path(index_root) / PROJECT_CONFIG_FILENAME

    existing: dict | None = None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            existing = parsed
        else:
            log.warning("codegraph: %s is not a JSON object — replacing it", path)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("codegraph: could not read %s (%s) — replacing it", path, exc)

    doc = build_project_config(patterns, existing)
    body = json.dumps(doc, indent=2) + "\n"
    try:
        if path.read_text(encoding="utf-8") == body:
            return {"path": str(path), "written": False, "include_ignored": patterns}
    except (OSError, UnicodeDecodeError):
        pass

    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.warning("codegraph: could not write %s (%s)", path, exc)
        return {"path": str(path), "written": False, "include_ignored": patterns}
    return {"path": str(path), "written": True, "include_ignored": patterns}
