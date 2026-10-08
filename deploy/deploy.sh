#!/usr/bin/env bash
# Read-only CT117 planning/preflight. Apply is deliberately manual and approved.
set -euo pipefail
if [[ $# -eq 0 ]]; then
    printf '%s\n' 'Read-only mode and explicit service selection required; see --help.' >&2
    exit 2
fi
exec python3 "$(dirname "${BASH_SOURCE[0]}")/preflight.py" "$@"
