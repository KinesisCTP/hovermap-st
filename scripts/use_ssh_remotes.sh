#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_repo="${workspace_root}/src/hovermap_ros_api"

if [ ! -d "${source_repo}/.git" ]; then
    echo "Skipping hovermap_ros_api: import it first with vcs." >&2
    exit 1
fi

if [ -n "$(git -C "${source_repo}" status --porcelain)" ]; then
    echo "Refusing to switch branches: hovermap_ros_api has uncommitted changes." >&2
    exit 1
fi

git -C "${source_repo}" remote set-url origin \
    git@github.com:KinesisCTP/hovermap_ros_api.git

if git -C "${source_repo}" remote get-url upstream >/dev/null 2>&1; then
    git -C "${source_repo}" remote set-url upstream \
        git@github.com:Emesent/hovermap_ros_api.git
else
    git -C "${source_repo}" remote add upstream \
        git@github.com:Emesent/hovermap_ros_api.git
fi

git -C "${source_repo}" fetch origin main

if ! git -C "${source_repo}" merge-base --is-ancestor HEAD origin/main; then
    echo "Current HEAD is not an ancestor of origin/main; refusing to discard work." >&2
    exit 1
fi

if git -C "${source_repo}" show-ref --verify --quiet refs/heads/main; then
    git -C "${source_repo}" switch main
    git -C "${source_repo}" merge --ff-only origin/main
else
    git -C "${source_repo}" switch --track -c main origin/main
fi

echo "Configured Kinesis origin, Emesent upstream, and a current local main branch."
