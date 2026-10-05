#!/usr/bin/env bash
# Reverse of scripts/install_codegraph.sh — replayed once by the framework's
# install journal when this app is uninstalled. Removes the bundle and the
# PATH shim.
#
# Deliberately does NOT delete the index (`<index_root>/.codegraph/`) or the
# project config (`<index_root>/codegraph.json`): index_root is the user's own
# tree, not this app's data, and a reinstall that has to re-index the whole
# workspace from scratch is an expensive punishment for an uninstall/install
# cycle. `codegraph uninit <index_root>` is the deliberate way to drop them.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

AW_BIN_DIR="${AW_WORKSPACE_HOME:-$HOME/.aw-workspace}/bin"

rm -f "${AW_BIN_DIR}/codegraph"
rm -rf "${PACKAGE_DIR}/.data"

echo "uninstall_codegraph.sh: removed the codegraph bundle and shim (index left in place)"
