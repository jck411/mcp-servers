#!/usr/bin/env bash
# Ordered Knowledge backup, maintenance, and wiki rebuild pipeline.

set -euo pipefail

REPO_DIR="${MCP_REPO:-/opt/mcp-servers}"
BACKUP_ROOT="${BACKUP_ROOT:-/mnt/backups}"
LOG_DIR="${LOG_DIR:-${REPO_DIR}/logs}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUN_HEALTHCHECK=1
RUN_QDRANT=1
RUN_MAINTAIN=1
RUN_WIKI=1
WIKI_ARGS=()
QDRANT_SNAPSHOT=""
QDRANT_COLLECTION="${KNOWLEDGE_QDRANT_COLLECTION:-knowledge}"
LATEST_MANIFEST="${BACKUP_ROOT}/knowledge.latest.manifest.json"

source "${SCRIPT_DIR}/lib-env.sh"

usage() {
    cat <<'EOF'
Usage: backup.sh [--skip-healthcheck] [--skip-qdrant] [--skip-maintain] [--skip-wiki]
                 [--wiki-domain DOMAIN] [--wiki-entity-slug SLUG] [--wiki-force-full]
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-healthcheck) RUN_HEALTHCHECK=0 ;;
        --skip-qdrant) RUN_QDRANT=0 ;;
        --skip-maintain) RUN_MAINTAIN=0 ;;
        --skip-wiki) RUN_WIKI=0 ;;
        --wiki-domain)
            [[ $# -ge 2 ]] || { echo "--wiki-domain requires a value" >&2; exit 2; }
            WIKI_ARGS+=(--domain "$2")
            shift
            ;;
        --wiki-entity-slug)
            [[ $# -ge 2 ]] || { echo "--wiki-entity-slug requires a value" >&2; exit 2; }
            WIKI_ARGS+=(--entity-slug "$2")
            shift
            ;;
        --wiki-force-full) WIKI_ARGS+=(--force-full) ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

mkdir -p "$LOG_DIR" "$BACKUP_ROOT"/{daily,weekly,monthly,pre-maintenance,staging}
exec > >(tee -a "${LOG_DIR}/backup.log") 2>&1

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

sqlite_backup() {
    local src="$1" dest="$2"
    sqlite3 "$src" <<SQL
.timeout 10000
.backup '$dest'
SQL
}

json_get_result_name() {
    python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["name"])'
}

curl_qdrant() {
    local args=(-fsS --max-time "${QDRANT_CURL_TIMEOUT:-600}")
    [[ -z "${QDRANT_API_KEY:-}" ]] || args+=(-H "api-key: ${QDRANT_API_KEY}")
    curl "${args[@]}" "$@"
}

snapshot_qdrant() {
    local stage="$1"
    local url="${QDRANT_URL:-http://127.0.0.1:6333}"
    local response name out_dir
    out_dir="${stage}/qdrant/${QDRANT_COLLECTION}"
    mkdir -p "$out_dir"
    log "Creating Qdrant snapshot collection=${QDRANT_COLLECTION}"
    response="$(curl_qdrant -X POST "${url%/}/collections/${QDRANT_COLLECTION}/snapshots?wait=true")"
    name="$(json_get_result_name <<<"$response")"
    QDRANT_SNAPSHOT="$name"
    printf '%s\n' "$response" > "${out_dir}/${name}.json"
    curl_qdrant -o "${out_dir}/${name}" \
        "${url%/}/collections/${QDRANT_COLLECTION}/snapshots/${name}"
    [[ -s "${out_dir}/${name}" ]] || die "Qdrant snapshot download is empty"
    log "Downloaded Qdrant snapshot ${name}"
}

cleanup_qdrant_snapshot() {
    [[ -n "$QDRANT_SNAPSHOT" ]] || return 0
    local url="${QDRANT_URL:-http://127.0.0.1:6333}"
    curl_qdrant -X DELETE \
        "${url%/}/collections/${QDRANT_COLLECTION}/snapshots/${QDRANT_SNAPSHOT}" >/dev/null ||
        log "Warning: could not delete Qdrant server snapshot ${QDRANT_SNAPSHOT}"
}

copy_metadata() {
    local stage="$1"
    mkdir -p "${stage}/metadata"
    if [[ -f "${REPO_DIR}/data/pending_maintenance.json" ]]; then
        cp "${REPO_DIR}/data/pending_maintenance.json" "${stage}/metadata/"
    fi
}

write_manifest() {
    local stage="$1" archive="$2"
    cat > "${stage}/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "host": "$(hostname)",
  "repo_dir": "${REPO_DIR}",
  "backup_root": "${BACKUP_ROOT}",
  "archive_path": "${archive}",
  "sqlite_primary": "${KNOWLEDGE_DB_PATH:-${REPO_DIR}/data/knowledge.db}",
  "qdrant_url": "${QDRANT_URL:-http://127.0.0.1:6333}",
  "qdrant_collection": "${QDRANT_COLLECTION}",
  "qdrant_snapshot": "${QDRANT_SNAPSHOT}",
  "wiki_args": "$(printf '%q ' "${WIKI_ARGS[@]}")"
}
EOF
    (cd "$stage" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
}

record_latest_manifest() {
    local stage="$1"
    cp "${stage}/manifest.json" "$LATEST_MANIFEST"
    cp "${stage}/SHA256SUMS" "${BACKUP_ROOT}/knowledge.latest.SHA256SUMS"
}

verify_archive() {
    local archive="$1" list="${archive}.list"
    [[ -s "$archive" ]] || die "archive is empty: $archive"
    tar -tzf "$archive" > "$list"
    grep -Fx 'data/knowledge.db' "$list" >/dev/null || die "archive missing data/knowledge.db"
    grep -Fx 'manifest.json' "$list" >/dev/null || die "archive missing manifest.json"
    grep -Fx 'SHA256SUMS' "$list" >/dev/null || die "archive missing SHA256SUMS"
    if [[ "$RUN_QDRANT" -eq 1 ]]; then
        grep -E "^qdrant/${QDRANT_COLLECTION}/.+\\.snapshot$" "$list" >/dev/null ||
            die "archive missing Qdrant snapshot"
    fi
    (cd "$(dirname "$archive")" && sha256sum "$(basename "$archive")" > "$(basename "$archive").sha256")
    log "Verified archive ${archive}"
}

prune_keep() {
    local dir="$1" keep="$2" pattern="${3:-knowledge.*.tar.gz}"
    mapfile -t files < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
    for ((i = keep; i < ${#files[@]}; i++)); do
        rm -f "${files[$i]}" "${files[$i]}.sha256" "${files[$i]}.list"
    done
}

promote_archive() {
    local archive="$1"
    local layer="$2"
    local dest="${BACKUP_ROOT}/${layer}/$(basename "$archive")"
    ln "$archive" "$dest" 2>/dev/null || cp -p "$archive" "$dest"
    ln "${archive}.sha256" "${dest}.sha256" 2>/dev/null || cp -p "${archive}.sha256" "${dest}.sha256"
    ln "${archive}.list" "${dest}.list" 2>/dev/null || cp -p "${archive}.list" "${dest}.list"
}

run_maintenance() {
    [[ "$RUN_MAINTAIN" -eq 1 ]] || { log "Skipping maintain.py"; return 0; }
    local py="${PYTHON:-${REPO_DIR}/.venv/bin/python}"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local maintain="${REPO_DIR}/deploy/maintain.py"
    [[ -f "$maintain" ]] || die "missing ${maintain}"

    # Drift detection: compare live hash against the sentinel written by
    # `make deploy-maintain`. A mismatch means either the file was edited
    # directly on the server, or a repo change was committed without
    # running the deploy target.
    local sentinel="${maintain}.sha256"
    if [[ -f "$sentinel" ]]; then
        local expected live_hash
        expected="$(awk '{print $1}' "$sentinel")"
        live_hash="$(sha256sum "$maintain" | awk '{print $1}')"
        if [[ "$expected" != "$live_hash" ]]; then
            log "WARNING: maintain.py drift detected — live hash ${live_hash:0:12}… ≠ deployed ${expected:0:12}…"
            log "  Run 'make deploy-maintain' from the Knowledge repo to resync."
            curl -fsS --max-time 8 \
                -H "Title: maintain.py drift detected" \
                -H "Priority: high" \
                -H "Tags: warning" \
                -d "Live maintain.py hash does not match deployed sentinel. Run 'make deploy-maintain' to resync." \
                "https://ntfy.sh/${NTFY_TOPIC:-jack-knowledge-system-42x7}" 2>/dev/null || true
        fi
    else
        log "NOTE: maintain.py sentinel hash missing — run 'make deploy-maintain' to create it"
    fi

    log "Running maintain.py"
    "$py" "$maintain"
}

run_wiki() {
    [[ "$RUN_WIKI" -eq 1 ]] || { log "Skipping maintain_wiki.py"; return 0; }
    local py="${PYTHON:-${REPO_DIR}/.venv/bin/python}"
    [[ -x "$py" ]] || py="$(command -v python3)"
    log "Running maintain_wiki.py ${WIKI_ARGS[*]:-}"
    (cd "$REPO_DIR" && "$py" -m maintain_wiki --backup-manifest "$LATEST_MANIFEST" "${WIKI_ARGS[@]}")
}

main() {
    load_mcp_env "$REPO_DIR"
    QDRANT_COLLECTION="${KNOWLEDGE_QDRANT_COLLECTION:-$QDRANT_COLLECTION}"
    trap cleanup_qdrant_snapshot EXIT

    local db_path="${KNOWLEDGE_DB_PATH:-${REPO_DIR}/data/knowledge.db}"
    local stage="${BACKUP_ROOT}/staging/${STAMP}"
    local archive="${BACKUP_ROOT}/daily/knowledge.${STAMP}.tar.gz"
    mkdir -p "$stage"/{data,qdrant,metadata}

    log "Starting Knowledge backup pipeline stamp=${STAMP}"
    [[ "$RUN_HEALTHCHECK" -eq 0 ]] || "${REPO_DIR}/deploy/healthcheck.sh"

    [[ -s "$db_path" ]] || die "missing primary SQLite database: ${db_path}"
    local pre="${BACKUP_ROOT}/pre-maintenance/knowledge.pre-maintenance.${STAMP}.db"
    sqlite_backup "$db_path" "$pre"
    prune_keep "${BACKUP_ROOT}/pre-maintenance" 2 'knowledge.pre-maintenance.*.db'
    log "Created pre-maintenance SQLite spare ${pre}"

    for db in "$db_path" "${REPO_DIR}/data/memory.db" "${REPO_DIR}/data/rag_index.db"; do
        [[ -s "$db" ]] || continue
        sqlite_backup "$db" "${stage}/data/$(basename "$db")"
    done
    [[ "$RUN_QDRANT" -eq 0 ]] || snapshot_qdrant "$stage"
    copy_metadata "$stage"
    write_manifest "$stage" "$archive"

    (cd "$stage" && tar -czf "$archive" data qdrant metadata manifest.json SHA256SUMS)
    verify_archive "$archive"
    [[ "$(date -u +%u)" == 7 ]] && promote_archive "$archive" weekly
    [[ "$(date -u +%d)" == 01 ]] && promote_archive "$archive" monthly
    prune_keep "${BACKUP_ROOT}/daily" 14
    prune_keep "${BACKUP_ROOT}/weekly" 12
    prune_keep "${BACKUP_ROOT}/monthly" 12

    run_maintenance
    record_latest_manifest "$stage"
    rm -rf "$stage"
    run_wiki
    log "Knowledge backup pipeline complete archive=${archive}"
}

main "$@"
