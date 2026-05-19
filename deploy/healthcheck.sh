#!/usr/bin/env bash
# Healthcheck for the Knowledge SQLite, Qdrant, and REST API dependencies.

set -euo pipefail

REPO_DIR="${MCP_REPO:-/opt/mcp-servers}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/lib-env.sh"

curl_headers=()
add_auth_headers() {
    [[ -z "${QDRANT_API_KEY:-}" ]] || curl_headers+=(-H "api-key: ${QDRANT_API_KEY}")
}

check_sqlite() {
    local db_path="${KNOWLEDGE_DB_PATH:-${REPO_DIR}/data/knowledge.db}"
    [[ -s "$db_path" ]] || { echo "sqlite missing_or_empty path=${db_path}" >&2; return 1; }
    [[ "$(sqlite3 "$db_path" 'PRAGMA quick_check;')" == "ok" ]] || {
        echo "sqlite quick_check failed path=${db_path}" >&2
        return 1
    }
    echo "sqlite ok path=${db_path}"
}

check_qdrant() {
    local url="${QDRANT_URL:-http://127.0.0.1:6333}"
    local collection="${KNOWLEDGE_QDRANT_COLLECTION:-knowledge}"
    curl -fsS --max-time 10 "${curl_headers[@]}" \
        "${url%/}/collections/${collection}" >/dev/null
    echo "qdrant ok collection=${collection}"
}

check_api() {
    local base="${KNOWLEDGE_API_BASE:-${API_BASE:-}}"
    local url="${KNOWLEDGE_API_HEALTH_URL:-}"
    [[ -n "$url" || -z "$base" ]] || url="${base%/}/api/health"
    url="${url:-http://127.0.0.1:9018/api/health}"
    local args=(-fsS --max-time 10)
    [[ -z "${KNOWLEDGE_API_TOKEN:-}" ]] || args+=(-H "Authorization: Bearer ${KNOWLEDGE_API_TOKEN}")
    curl "${args[@]}" "$url" >/dev/null
    echo "knowledge_api ok url=${url}"
}

main() {
    load_mcp_env "$REPO_DIR"
    add_auth_headers
    check_sqlite
    check_qdrant
    check_api
}

main "$@"
