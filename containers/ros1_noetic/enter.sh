#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${script_dir}/.env"

if [ -f "${env_file}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
fi

container_name="${CONTAINER_NAME:-kinesis_hovermap_st_noetic}"
container_workspace="${CONTAINER_WORKSPACE:-/home/ros/hovermap-st_ws}"

if ! docker ps --format '{{.Names}}' | grep -Fxq "${container_name}"; then
    echo "Container ${container_name} is not running." >&2
    echo "Start it with ${script_dir}/run.sh" >&2
    exit 1
fi

docker exec -it "${container_name}" bash -lc "cd '${container_workspace}' && exec bash"
