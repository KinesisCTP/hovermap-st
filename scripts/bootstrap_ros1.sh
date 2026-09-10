#!/usr/bin/env bash
set -euo pipefail

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${workspace_root}"

# shellcheck disable=SC1091
source /opt/ros/noetic/setup.bash

if [ ! -d src/hovermap_ros_api/.git ]; then
    vcs import src < hovermap_kinesis_https.repos
fi

# These are baked into the Kinesis container because upstream exposes them
# through a nonstandard package.xml export that rosdep cannot install.
python3 -c 'import avro, catkin_pkg, netifaces, tornado, zmq'

rosdep update --rosdistro noetic
rosdep install --from-paths src --ignore-src -r -y --rosdistro noetic

catkin init 2>/dev/null || true
catkin config --extend /opt/ros/noetic --cmake-args -DCMAKE_BUILD_TYPE=Release
catkin build

echo
echo "ROS 1 workspace built. Run: source ${workspace_root}/devel/setup.bash"
