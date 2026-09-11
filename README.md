# KINESIS Hovermap ST — ROS 2 Jazzy

A native ROS 2 Jazzy workspace for KINESIS users operating the Hovermap ST on
Ubuntu 24.04. For ROS 1 Noetic, use the [`main`](https://github.com/KinesisCTP/hovermap-st/tree/main)
branch.

## Quick start (Wi-Fi)

Power the Hovermap, connect the Linux workstation to its isolated Wi-Fi
network, and configure that interface as `10.9.0.99/24`. Keep the Hovermap
securely held or mounted before starting a Mapping mission.

In terminal 1:

```bash
git clone --branch codex/ros2-jazzy \
  https://github.com/KinesisCTP/hovermap-st.git ~/hovermap-st_ros2_ws
cd ~/hovermap-st_ros2_ws
./scripts/preflight_network.sh wifi wlan0

source /opt/ros/jazzy/setup.bash
sudo apt-get update
sudo apt-get install -y python3-avro ros-dev-tools
vcs import . < hovermap_ros2_core_https.repos
rosdep update --rosdistro jazzy
rosdep install --from-paths src/hovermap_ros2_msgs src/hovermap_ros2_api \
  --ignore-src --rosdistro jazzy -y
colcon build --base-paths src/hovermap_ros2_msgs src/hovermap_ros2_api \
  --symlink-install

source install/setup.bash
source scripts/source_mule_core.sh
ros2 launch hovermap_ros2_api hovermap_api.launch.py \
  ip_prefix:=10.9.0.0 hovermap_address:=10.9.0.1
```

In terminal 2:

```bash
cd ~/hovermap-st_ros2_ws
source /opt/ros/jazzy/setup.bash
source install/setup.bash
rviz2 -d "$(ros2 pkg prefix hovermap_ros2_api)/share/hovermap_ros2_api/rviz/hovermap.rviz"
```

In another sourced shell, start and stop a Mapping mission only after securing
the Hovermap:

```bash
ros2 topic pub --once /cortex/start_scan std_msgs/msg/Empty '{}'
# Move the Hovermap slowly while viewing the live cloud in RViz2.
ros2 topic pub --once /cortex/stop_scan std_msgs/msg/Empty '{}'
```

Expect a Hovermap Mule peer, corrected LiDAR near 20 Hz, occupancy near 1 Hz,
odometry near 100 Hz, and motion in RViz2. Synchronize the **host** clock to
the Hovermap NTP server before timestamp-sensitive navigation or measurement.

## Network profiles

Configure exactly one Hovermap-facing host interface:

| Connection | Prefix | Hovermap | Client | Netmask |
|---|---|---|---|---|
| Wi-Fi | `10.9.0.0` | `10.9.0.1` | `10.9.0.99` | `255.255.255.0` |
| ST Fischer Ethernet | `192.168.2.0` | `192.168.2.115` | `192.168.2.100` | `255.255.255.0` |
| USB Ethernet | `192.168.3.0` | `192.168.3.115` | `192.168.3.100` | `255.255.255.0` |

Keep **Use Wi-Fi for external API** enabled for Wi-Fi. Disable it for Fischer
or USB Ethernet, then power-cycle the Hovermap. Run the matching preflight:

```bash
./scripts/preflight_network.sh wifi wlan0
# or: ./scripts/preflight_network.sh fischer enp4s0
# or: ./scripts/preflight_network.sh usb enx001122334455
```

Launch with the table's Prefix and Hovermap values. For Wi-Fi:

```bash
ros2 launch hovermap_ros2_api hovermap_api.launch.py \
  ip_prefix:=10.9.0.0 hovermap_address:=10.9.0.1
```

Mule uses UDP `8123`, multicast `225.0.0.250`, and TCP ports `49172-49191`;
`49192` is the exclusive upper bound. The HTTP and Mule interfaces are
unauthenticated, so keep them on an isolated, trusted Hovermap network.

## Runtime details

In each new launch shell, source the environments in this order:

```bash
source /opt/ros/jazzy/setup.bash
source install/setup.bash
source scripts/source_mule_core.sh
```

Downloads go to `~/hovermap_downloads`. If either API adapter exits, the launch
shuts down instead of leaving a partial client running. Advanced parameters,
QoS, topic types, and diagnostics are in
[`src/hovermap_ros2_api/README.md`](src/hovermap_ros2_api/README.md).

This branch has no ROS 2 container helper; `containers/ros1_noetic` belongs to
the ROS 1 workflow.

## Live data check

During a Mapping mission, run these individually or in separate terminals:

```bash
ros2 topic echo --once /cortex/mule_bridge/status
ros2 topic hz /cortex/lidar/corrected
ros2 topic hz /cortex/occupancy_grid_map/data
ros2 topic hz /cortex/odometry
rviz2 -d "$(ros2 pkg prefix hovermap_ros2_api)/share/hovermap_ros2_api/rviz/hovermap.rviz"
```

Check the actual point fields, frame IDs, transform tree, diagnostics, and
clock offset before using the data for navigation or measurement.

## Device-control topics

Run state-changing commands only after the read-only checks are healthy:

```bash
ros2 topic pub --once /cortex/set_scan_prefix \
  std_msgs/msg/String "{data: 'Kinesis'}"
ros2 topic pub --once /cortex/start_scan std_msgs/msg/Empty '{}'
ros2 topic pub --once /cortex/stop_scan std_msgs/msg/Empty '{}'
```

Scan-list and download responses are volatile. Start the matching `ros2 topic
echo --once` listener in another shell before publishing each request:

```bash
# List scans.
ros2 topic echo --once /cortex/scan_names_response
ros2 topic pub --once /cortex/scan_names_request std_msgs/msg/Empty '{}'

# Download an exact returned scan name.
ros2 topic echo --once /cortex/scan_download_successful
ros2 topic pub --once /cortex/download_scan \
  std_msgs/msg/String "{data: 'Kinesis_01'}"
```

Avoid `configure_perception` unless deliberately changing persistent settings;
the device does not acknowledge configuration changes.

## Contributor setup

Contributors can clone through SSH and then follow the setup above:

```bash
git clone --branch codex/ros2-jazzy \
  git@github.com:KinesisCTP/hovermap-st.git ~/hovermap-st_ros2_ws
```

Run the pure-Python tests and, in a built and sourced workspace, `colcon test`
plus `colcon test-result --verbose` before contributing:

```bash
PYTHONPATH=src/hovermap_ros2_api python3 -m unittest discover \
  -s src/hovermap_ros2_api/test -v
colcon test --base-paths src/hovermap_ros2_msgs src/hovermap_ros2_api
colcon test-result --verbose
```

Keep the Mule dependency pinned through `hovermap_ros2_core_https.repos` and
never commit scans, `.env` files, device-specific files, or credentials.

KINESIS-authored code remains proprietary; no open-source license is granted.
Public availability does not change those rights. Third-party software remains
governed by its own licenses; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
