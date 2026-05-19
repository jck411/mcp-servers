#!/usr/bin/env bash

load_mcp_env() {
    local repo_dir="${1:-/opt/mcp-servers}"
    local env_file line key value
    for env_file in "${repo_dir}/.env" "${repo_dir}/.env.network" "${repo_dir}/.env.config"; do
        [[ -f "$env_file" ]] || continue
        while IFS= read -r line; do
            line="${line#"${line%%[![:space:]]*}"}"
            [[ -z "$line" || "$line" == \#* || "$line" != *=* ]] && continue
            key="${line%%=*}"
            key="${key#export }"
            key="${key%"${key##*[![:space:]]}"}"
            [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ! -v "$key" ]] || continue
            value="${line#*=}"
            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"
            [[ "$value" == \"*\" && "$value" == *\" ]] && value="${value:1:${#value}-2}"
            [[ "$value" == \'*\' && "$value" == *\' ]] && value="${value:1:${#value}-2}"
            export "$key=$value"
        done < "$env_file"
    done
}
