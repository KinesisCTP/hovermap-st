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

image_name="${ROS_IMAGE:-local/kinesis-hovermap-st-noetic:desktop}"
video_gid="$(getent group video | cut -d: -f3 || true)"
render_gid="$(getent group render | cut -d: -f3 || true)"

docker build \
    --build-arg USER_UID="$(id -u)" \
    --build-arg USER_GID="$(id -g)" \
    --build-arg VIDEO_GID="${video_gid:-44}" \
    --build-arg RENDER_GID="${render_gid:-109}" \
    -t "${image_name}" \
    "${script_dir}"

echo "Built ${image_name}"
