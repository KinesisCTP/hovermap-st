#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
env_file="${script_dir}/.env"

if [ -f "${env_file}" ]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
fi

HOVERMAP_WS="${HOVERMAP_WS:-${repo_root}}"
ROS_IMAGE="${ROS_IMAGE:-local/kinesis-hovermap-st-noetic:desktop}"
CONTAINER_NAME="${CONTAINER_NAME:-kinesis_hovermap_st_noetic}"
CONTAINER_WORKSPACE="${CONTAINER_WORKSPACE:-/home/ros/hovermap-st_ws}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-${repo_root}/downloads}"

if ! docker info >/dev/null 2>&1; then
    echo "Docker is not reachable from this shell." >&2
    echo "Install Docker Engine or refresh your docker-group session, then retry." >&2
    exit 1
fi

if ! docker image inspect "${ROS_IMAGE}" >/dev/null 2>&1; then
    "${script_dir}/build.sh"
fi

mkdir -p "${HOVERMAP_WS}/src" "${DOWNLOAD_DIR}"
xhost_granted=0
if command -v xhost >/dev/null 2>&1 \
    && xhost +local:docker >/dev/null 2>&1; then
    xhost_granted=1
fi

cleanup_xhost() {
    if [ "${xhost_granted}" -eq 1 ]; then
        xhost -local:docker >/dev/null 2>&1 || true
    fi
}
trap cleanup_xhost EXIT

docker_args=(
    -it --rm
    --name "${CONTAINER_NAME}"
    --network=host
    --ipc=host
    -e DISPLAY="${DISPLAY:-}"
    -e XDG_RUNTIME_DIR=/tmp/runtime-ros
    -e ROS_WORKSPACE="${CONTAINER_WORKSPACE}"
    -e QT_X11_NO_MITSHM=1
    -v /tmp/.X11-unix:/tmp/.X11-unix:rw
    -v "${HOVERMAP_WS}:${CONTAINER_WORKSPACE}:rw"
    -v "${DOWNLOAD_DIR}:/data/downloads:rw"
    -w "${CONTAINER_WORKSPACE}"
)

if [ -n "${XAUTHORITY:-}" ] && [ -f "${XAUTHORITY}" ]; then
    docker_args+=(
        -e XAUTHORITY=/tmp/.docker.xauth
        -v "${XAUTHORITY}:/tmp/.docker.xauth:ro"
    )
fi

if [ -d /dev/dri ]; then
    docker_args+=(--device /dev/dri)
    video_gid="$(getent group video | cut -d: -f3 || true)"
    render_gid="$(getent group render | cut -d: -f3 || true)"
    [ -z "${video_gid}" ] || docker_args+=(--group-add "${video_gid}")
    [ -z "${render_gid}" ] || docker_args+=(--group-add "${render_gid}")
fi

# Expansion is intentionally deferred until the shell inside the container.
# shellcheck disable=SC2016
docker run "${docker_args[@]}" "${ROS_IMAGE}" \
    bash -lc 'mkdir -p "${XDG_RUNTIME_DIR}" && chmod 700 "${XDG_RUNTIME_DIR}" && exec bash'
