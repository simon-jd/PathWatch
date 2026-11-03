#!/usr/bin/env bash
set -euo pipefail
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/dev-env.sh"
[[ $# -le 1 ]] || { echo "Only reap or down; no scope overrides" >&2; exit 2; }
exec python3 "$PATHWATCH_ROOT/scripts/azure_reaper.py" "${1:-reap}"
