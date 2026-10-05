#!/usr/bin/env bash
# Installs the self-contained CodeGraph bundle into this app's own .data dir
# and drops a `codegraph` shim on the persistent bin dir. Idempotent — safe to
# re-run on install and on every heal pass.
#
# Deliberately NOT `npm install`: the container's npm cache under $HOME is
# root-owned, so `npm` dies with EACCES here (documented workspace gotcha).
# The platform packages on the npm registry are plain tarballs, so curl + tar
# gets the same bytes with none of npm's state. The cost of skipping npm is
# losing its provenance/attestation check, so the sha512 integrity string the
# registry publishes for each tarball is pinned below and verified before
# anything is extracted — that is the provenance guarantee we can still
# enforce.
#
# The bundle is one archive carrying its own node runtime (package/node),
# the compiled JS (package/lib) and a launcher (package/bin/codegraph) that
# execs the two together. Nothing else is needed and nothing is installed
# system-wide.
set -euo pipefail

CODEGRAPH_VERSION="1.6.2"

# sha512 integrity strings as published by the npm registry on 2026-10-05
# (`dist.integrity` on each platform package's version metadata). Same base64
# encoding npm uses, so these can be re-checked against the registry verbatim.
INTEGRITY_X64="sha512-DJWizz1qHNr1MsbK6fz1mjVUQjeTH3T+L0D2BGaLkygWzBnNMG30MvLq1ae0m6cUV414U8aEow5Eb/kIORA8+w=="
INTEGRITY_ARM64="sha512-4sUICiYzHgC3brCKw4FDiY10jVQ79oIm2wwE978IOAXj36sDxji6d9l5SEvGyHyolgSXsLImLtagTsIEN2Pvnw=="

# Resolve the app's own package dir from this script's location, not $PWD —
# robust whether the runtime invokes us with cwd=package_dir (the normal case,
# see src/apps/commands.py's `_run`) or a test runs us directly.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

DATA_DIR="${PACKAGE_DIR}/.data"
BUNDLE_DIR="${DATA_DIR}/codegraph-${CODEGRAPH_VERSION}"
AW_BIN_DIR="${AW_WORKSPACE_HOME:-$HOME/.aw-workspace}/bin"
SHIM="${AW_BIN_DIR}/codegraph"

mkdir -p "${DATA_DIR}" "${AW_BIN_DIR}"

case "$(uname -m)" in
  x86_64|amd64)  PLATFORM="linux-x64";   INTEGRITY="${INTEGRITY_X64}" ;;
  aarch64|arm64) PLATFORM="linux-arm64"; INTEGRITY="${INTEGRITY_ARM64}" ;;
  *) echo "install_codegraph.sh: unsupported architecture $(uname -m) — CodeGraph ships linux-x64 and linux-arm64 bundles only" >&2; exit 1 ;;
esac

PKG="@colbymchenry/codegraph-${PLATFORM}"
TARBALL_URL="https://registry.npmjs.org/${PKG}/-/codegraph-${PLATFORM}-${CODEGRAPH_VERSION}.tgz"

write_shim() {
  # A wrapper script, not a symlink: the bundle's own launcher resolves
  # symlinks to find its sibling `node` and `lib`, so a symlink would work
  # too — but a wrapper is also where the telemetry opt-out belongs, so every
  # caller gets it (this app's own spawns set it explicitly as well, but an
  # operator typing `codegraph` by hand should not phone home either).
  cat > "${SHIM}" <<SHIM_EOF
#!/usr/bin/env bash
# aw-app-codegraph shim — auto-generated; do not edit.
export CODEGRAPH_TELEMETRY="\${CODEGRAPH_TELEMETRY:-0}"
export DO_NOT_TRACK="\${DO_NOT_TRACK:-1}"
exec "${BUNDLE_DIR}/bin/codegraph" "\$@"
SHIM_EOF
  chmod +x "${SHIM}"
}

# Verify the CAPABILITY, not the presence of a name: the bundle is a node
# runtime plus 169MB of JS, and a truncated download leaves a launcher that
# exists and fails on every invocation. Running the real thing and matching
# the version is the only check that distinguishes "installed" from "a
# directory with the right name in it". A mismatch (or any failure) falls
# through to a full reinstall rather than reporting success — the installer
# contract's "verify, don't detect" rule.
if [ -x "${BUNDLE_DIR}/bin/codegraph" ]; then
  write_shim
  if [ "$("${SHIM}" --version 2>/dev/null | tr -d '[:space:]')" = "${CODEGRAPH_VERSION}" ]; then
    echo "install_codegraph.sh: codegraph ${CODEGRAPH_VERSION} already installed at ${BUNDLE_DIR}"
    exit 0
  fi
  echo "install_codegraph.sh: ${BUNDLE_DIR} is present but does not run ${CODEGRAPH_VERSION} — reinstalling"
  rm -rf "${BUNDLE_DIR}"
fi

TMP_TGZ="${DATA_DIR}/.download-codegraph-${PLATFORM}-${CODEGRAPH_VERSION}.tgz"
trap 'rm -f "${TMP_TGZ}"' EXIT

echo "install_codegraph.sh: downloading ${PKG}@${CODEGRAPH_VERSION} (~64 MB)"
curl -fsSL --retry 3 --retry-delay 2 -o "${TMP_TGZ}" "${TARBALL_URL}"

echo "install_codegraph.sh: verifying sha512 integrity"
# python3 rather than `openssl dgst | base64`: python3 is guaranteed by the
# base image, openssl's CLI is not, and this avoids a shell pipeline whose
# line wrapping differs between coreutils base64 and busybox.
ACTUAL="$(python3 - "${TMP_TGZ}" <<'PY'
import base64, hashlib, sys
with open(sys.argv[1], "rb") as fh:
    digest = hashlib.sha512()
    for chunk in iter(lambda: fh.read(1 << 20), b""):
        digest.update(chunk)
print("sha512-" + base64.b64encode(digest.digest()).decode())
PY
)"
if [ "${ACTUAL}" != "${INTEGRITY}" ]; then
  echo "install_codegraph.sh: INTEGRITY MISMATCH for ${TARBALL_URL}" >&2
  echo "  expected ${INTEGRITY}" >&2
  echo "  actual   ${ACTUAL}" >&2
  exit 1
fi

echo "install_codegraph.sh: extracting to ${BUNDLE_DIR}"
mkdir -p "${BUNDLE_DIR}"
# --strip-components=1 drops the archive's own `package/` wrapper so the
# bundle lands as ${BUNDLE_DIR}/{bin,lib,node}, which is the layout the
# launcher expects (it resolves `node`/`lib` relative to its own parent).
# Not `python3 -m tarfile`: that drops the executable bits off `node` and the
# launcher, which is exactly what makes them runnable.
tar -xzf "${TMP_TGZ}" -C "${BUNDLE_DIR}" --strip-components=1
chmod +x "${BUNDLE_DIR}/node" "${BUNDLE_DIR}/bin/codegraph"

write_shim

# Self-verify — a broken install fails this (set -e) and thus fails the
# install_system_cli call / the healer's next re-run.
INSTALLED="$("${SHIM}" --version | tr -d '[:space:]')"
if [ "${INSTALLED}" != "${CODEGRAPH_VERSION}" ]; then
  echo "install_codegraph.sh: installed ${CODEGRAPH_VERSION} but the shim reports '${INSTALLED}'" >&2
  exit 1
fi
echo "install_codegraph.sh: codegraph ${INSTALLED} installed (${BUNDLE_DIR}, shim ${SHIM})"
