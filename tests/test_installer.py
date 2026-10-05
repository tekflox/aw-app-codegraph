"""scripts/install_codegraph.sh — static checks plus one real run of the
integrity-verification path.

The full installer downloads 64 MB from the npm registry, so it isn't run
end-to-end here. What IS exercised is the part that has to be right or the
app silently ships an unverified binary: the sha512 check, and the
verify-don't-detect guard that distinguishes "installed" from "a directory
with the right name".
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install_codegraph.sh"
UNINSTALLER = ROOT / "scripts" / "uninstall_codegraph.sh"
MANIFEST_VERSION = "1.6.2"


def test_both_scripts_are_executable():
    for script in (INSTALLER, UNINSTALLER):
        assert os.stat(script).st_mode & stat.S_IXUSR, f"{script.name} is not executable"


def test_the_installer_is_strict_mode_bash():
    assert INSTALLER.read_text().splitlines()[0].startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in INSTALLER.read_text()


def test_the_pinned_version_matches_the_manifest():
    """A drift here means the manifest's `verify` passes on a bundle nobody
    declared, which is exactly the kind of mismatch the version pin exists
    to prevent."""
    import json

    manifest = json.loads((ROOT / "aw-app.json").read_text())
    declared = manifest["contributes"]["system_clis"][0]["version"]
    pinned = re.search(r'^CODEGRAPH_VERSION="([^"]+)"', INSTALLER.read_text(),
                       re.MULTILINE).group(1)
    assert pinned == declared == MANIFEST_VERSION


def test_both_architectures_have_a_pinned_integrity_string():
    body = INSTALLER.read_text()
    for name in ("INTEGRITY_X64", "INTEGRITY_ARM64"):
        value = re.search(rf'^{name}="([^"]+)"', body, re.MULTILINE).group(1)
        assert value.startswith("sha512-")
        # 64 raw bytes base64-encoded, including padding.
        assert len(base64.b64decode(value[len("sha512-"):])) == 64


def test_the_installer_never_invokes_npm():
    """The container's npm cache under $HOME is root-owned, so `npm` dies
    with EACCES here — a documented workspace gotcha, and the reason this
    installer pins an integrity string by hand instead. The registry URL and
    the comments that explain this naturally mention npm; what must not
    appear is a call to it."""
    code = [line.split("#", 1)[0] for line in INSTALLER.read_text().splitlines()]
    assert not re.search(r"(^|[;&|(\s])npm\s", "\n".join(code))


def test_telemetry_is_off_in_the_generated_shim():
    body = INSTALLER.read_text()
    assert "CODEGRAPH_TELEMETRY" in body
    assert "DO_NOT_TRACK" in body


def _fake_bundle_tarball(dest: Path, *, version: str = MANIFEST_VERSION) -> str:
    """A tarball with the real bundle's `package/{bin,lib,node}` shape, whose
    launcher just prints a version. Returns its sha512 integrity string."""
    stage = dest.parent / "stage" / "package"
    (stage / "bin").mkdir(parents=True)
    (stage / "lib").mkdir()
    (stage / "bin" / "codegraph").write_text(f"#!/bin/sh\necho {version}\n")
    (stage / "node").write_text("#!/bin/sh\nexit 0\n")
    (stage / "lib" / "placeholder").write_text("x")
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(stage, arcname="package")
    shutil.rmtree(stage.parent)
    digest = hashlib.sha512(dest.read_bytes()).digest()
    return "sha512-" + base64.b64encode(digest).decode()


def _patched_installer(tmp_path: Path, tarball: Path, integrity: str) -> Path:
    """The real installer with its download URL swapped for a local file and
    its integrity pin swapped for the fake tarball's — everything else,
    including the verification logic itself, runs verbatim."""
    body = INSTALLER.read_text()
    body = re.sub(r'^INTEGRITY_X64=".*"$', f'INTEGRITY_X64="{integrity}"',
                  body, flags=re.MULTILINE)
    body = re.sub(r'^INTEGRITY_ARM64=".*"$', f'INTEGRITY_ARM64="{integrity}"',
                  body, flags=re.MULTILINE)
    body = body.replace(
        'curl -fsSL --retry 3 --retry-delay 2 -o "${TMP_TGZ}" "${TARBALL_URL}"',
        f'cp "{tarball}" "${{TMP_TGZ}}"')
    path = tmp_path / "scripts" / "install_codegraph.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)
    return path


def _run(script: Path, home: Path):
    return subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, "AW_WORKSPACE_HOME": str(home)})


@pytest.fixture
def fake_install(tmp_path: Path):
    tarball = tmp_path / "bundle.tgz"
    integrity = _fake_bundle_tarball(tarball)
    script = _patched_installer(tmp_path, tarball, integrity)
    aw_home = tmp_path / "aw-home"
    return script, tarball, integrity, aw_home


def test_a_good_tarball_extracts_and_installs_a_working_shim(fake_install):
    script, _, _, aw_home = fake_install

    result = _run(script, aw_home)

    assert result.returncode == 0, result.stdout + result.stderr
    shim = aw_home / "bin" / "codegraph"
    assert shim.exists()
    # The bundle landed WITHOUT its `package/` wrapper — the launcher resolves
    # `node`/`lib` relative to its own parent, so an extra level breaks it.
    bundle = script.parent.parent / ".data" / f"codegraph-{MANIFEST_VERSION}"
    assert (bundle / "bin" / "codegraph").exists()
    assert (bundle / "node").exists()
    assert (bundle / "lib").is_dir()
    assert subprocess.run([str(shim)], capture_output=True, text=True
                          ).stdout.strip() == MANIFEST_VERSION


def test_a_tampered_tarball_is_refused_before_anything_is_extracted(fake_install):
    script, tarball, _, aw_home = fake_install
    tarball.write_bytes(tarball.read_bytes() + b"tampered")

    result = _run(script, aw_home)

    assert result.returncode != 0
    assert "INTEGRITY MISMATCH" in result.stderr
    assert not (script.parent.parent / ".data" /
                f"codegraph-{MANIFEST_VERSION}" / "bin").exists()


def test_a_second_run_is_a_no_op(fake_install):
    script, tarball, _, aw_home = fake_install
    assert _run(script, aw_home).returncode == 0
    tarball.unlink()  # a no-op run must not need the tarball at all

    result = _run(script, aw_home)

    assert result.returncode == 0
    assert "already installed" in result.stdout


def test_a_present_but_wrong_version_bundle_is_reinstalled(fake_install):
    """Verify the capability, don't detect the name: a truncated download
    leaves a launcher that exists and fails on every invocation."""
    script, tarball, integrity, aw_home = fake_install
    bundle = script.parent.parent / ".data" / f"codegraph-{MANIFEST_VERSION}"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "codegraph").write_text("#!/bin/sh\necho 0.0.1\n")
    (bundle / "bin" / "codegraph").chmod(0o755)

    result = _run(script, aw_home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reinstalling" in result.stdout
    assert subprocess.run([str(aw_home / "bin" / "codegraph")], capture_output=True,
                          text=True).stdout.strip() == MANIFEST_VERSION


def test_a_bundle_whose_launcher_does_not_run_is_reinstalled(fake_install):
    script, _, _, aw_home = fake_install
    bundle = script.parent.parent / ".data" / f"codegraph-{MANIFEST_VERSION}"
    (bundle / "bin").mkdir(parents=True)
    (bundle / "bin" / "codegraph").write_text("#!/bin/sh\nexit 1\n")
    (bundle / "bin" / "codegraph").chmod(0o755)

    result = _run(script, aw_home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "reinstalling" in result.stdout


def test_the_uninstaller_removes_the_shim_and_the_bundle_but_not_the_index(
        fake_install, tmp_path: Path):
    """index_root is the user's own tree — making an uninstall/install cycle
    pay for a full re-index of the workspace would be a punishment, and
    `codegraph uninit` is the deliberate way to drop it."""
    script, _, _, aw_home = fake_install
    assert _run(script, aw_home).returncode == 0
    package_dir = script.parent.parent
    index_root = tmp_path / "index-root"
    (index_root / ".codegraph").mkdir(parents=True)
    (index_root / "codegraph.json").write_text("{}")

    uninstaller = package_dir / "scripts" / "uninstall_codegraph.sh"
    shutil.copy(UNINSTALLER, uninstaller)
    result = _run(uninstaller, aw_home)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (aw_home / "bin" / "codegraph").exists()
    assert not (package_dir / ".data").exists()
    assert (index_root / ".codegraph").exists()
    assert (index_root / "codegraph.json").exists()
