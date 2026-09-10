#!/usr/bin/env bash
# Source this file; it does not copy Mule into the ROS 2 package or workspace.

_hovermap_ros2_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_hovermap_mule_parent="${_hovermap_ros2_root}/third_party/hovermap_ros_api/src/mule_bridge/mule_bridge/src"

if [[ ! -d "${_hovermap_mule_parent}/mule_bridge" ]]; then
  echo "Pinned Mule core is absent. Run: vcs import . < hovermap_ros2_core_https.repos" >&2
  unset _hovermap_ros2_root _hovermap_mule_parent
  return 1 2>/dev/null || exit 1
fi

export PYTHONPATH="${_hovermap_mule_parent}${PYTHONPATH:+:${PYTHONPATH}}"
unset _hovermap_ros2_root _hovermap_mule_parent
