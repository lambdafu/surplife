#!/bin/bash
# Sync src/surplife_core into the vendored integration core.
# Single source of truth: src/surplife_core — never edit custom_components/.../core.
set -euo pipefail
cd "$(dirname "$0")/.."
cp src/surplife_core/*.py custom_components/surplife_matrix/core/
cp -r src/surplife_core/fonts/. custom_components/surplife_matrix/core/fonts/
echo "core synced: $(ls custom_components/surplife_matrix/core/*.py | wc -l) modules"
