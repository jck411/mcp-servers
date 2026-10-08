#!/usr/bin/env bash
# Existing CT117 units/private configuration must not be overwritten by bootstrap.
set -euo pipefail
printf '%s\n' 'Bootstrap disabled. CT117 uses existing units; see README.md deployment procedure.' >&2
exit 2
