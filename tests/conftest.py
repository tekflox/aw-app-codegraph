"""Shared fixtures/doubles for this app's tests.

``ctx`` is a lightweight double, not the real F4 runtime: activate() only
touches ``ctx.commands``, ``ctx.routes``, ``ctx.watchdog``, ``ctx.package_dir``
and ``ctx.config``, so that's all the double needs.

Nothing here imports ``docker`` — a test that does passes locally and turns
the baremetal runner's CI red.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    """A writable stand-in for the installed app dir: activate() writes
    mcp.json into it, so the real repo must not be used."""
    import shutil

    shutil.copy(ROOT / "aw-app.json", tmp_path / "aw-app.json")
    return tmp_path


@pytest.fixture
def make_ctx(package_dir: Path):
    def _make(config: dict | None = None):
        ctx = MagicMock()
        ctx.package_dir = str(package_dir)
        ctx.config = config if config is not None else {}
        return ctx

    return _make
