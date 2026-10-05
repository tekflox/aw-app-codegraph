"""codegraph_app/project_config.py — the file that decides whether the 80
nested repos under repos/ are in the index at all."""
from __future__ import annotations

import json
from pathlib import Path

from codegraph_app import project_config


def test_writes_include_ignored_when_no_config_exists(tmp_path: Path):
    result = project_config.ensure_project_config(str(tmp_path), ["repos/"])

    assert result["written"] is True
    doc = json.loads((tmp_path / "codegraph.json").read_text())
    assert doc == {"includeIgnored": ["repos/"]}


def test_defaults_to_repos_when_no_patterns_passed(tmp_path: Path):
    project_config.ensure_project_config(str(tmp_path))

    doc = json.loads((tmp_path / "codegraph.json").read_text())
    assert doc["includeIgnored"] == ["repos/"]


def test_second_call_with_same_patterns_does_not_rewrite(tmp_path: Path):
    """The gateway is not the only mtime-sensitive reader: CodeGraph
    mtime-caches this file per project root and re-reads it on every
    index/scan/watch event."""
    project_config.ensure_project_config(str(tmp_path), ["repos/"])
    mtime_before = (tmp_path / "codegraph.json").stat().st_mtime_ns

    result = project_config.ensure_project_config(str(tmp_path), ["repos/"])

    assert result["written"] is False
    assert (tmp_path / "codegraph.json").stat().st_mtime_ns == mtime_before


def test_preserves_other_keys_an_operator_may_have_added(tmp_path: Path):
    (tmp_path / "codegraph.json").write_text(json.dumps({
        "extensions": {".tpl": "php"},
        "exclude": ["vendor/"],
        "includeIgnored": ["something-stale/"],
    }))

    project_config.ensure_project_config(str(tmp_path), ["repos/"])

    doc = json.loads((tmp_path / "codegraph.json").read_text())
    assert doc["extensions"] == {".tpl": "php"}
    assert doc["exclude"] == ["vendor/"]
    # Ours is the one key this app owns, so it converges back.
    assert doc["includeIgnored"] == ["repos/"]


def test_replaces_a_malformed_config(tmp_path: Path):
    """CodeGraph ignores an unparseable codegraph.json with a warning, which
    would silently drop every nested repo out of the index — so a broken file
    has to be replaced, not preserved."""
    (tmp_path / "codegraph.json").write_text("{not json at all")

    result = project_config.ensure_project_config(str(tmp_path), ["repos/"])

    assert result["written"] is True
    assert json.loads((tmp_path / "codegraph.json").read_text())["includeIgnored"] == ["repos/"]


def test_replaces_a_config_that_is_not_an_object(tmp_path: Path):
    (tmp_path / "codegraph.json").write_text("[1, 2, 3]")

    project_config.ensure_project_config(str(tmp_path), ["repos/"])

    assert json.loads((tmp_path / "codegraph.json").read_text()) == {"includeIgnored": ["repos/"]}


def test_unwritable_root_is_reported_not_raised(tmp_path: Path):
    """activate() must not die because index_root is read-only — the app
    still has a usable (if unextended) index in that case."""
    root = tmp_path / "ro"
    root.mkdir()
    root.chmod(0o500)
    try:
        result = project_config.ensure_project_config(str(root), ["repos/"])
        assert result["written"] is False
    finally:
        root.chmod(0o700)


def test_missing_root_is_reported_not_raised(tmp_path: Path):
    result = project_config.ensure_project_config(str(tmp_path / "nope"), ["repos/"])

    assert result["written"] is False


def test_build_project_config_does_not_mutate_its_input():
    existing = {"extensions": {}}

    project_config.build_project_config(["repos/"], existing)

    assert "includeIgnored" not in existing
