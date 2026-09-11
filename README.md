# Kinesis Hovermap ST — ROS 2 Jazzy

This private `codex/ros2-jazzy` branch is the KINESIS native ROS 2 prototype
for the Emesent Hovermap ST on Ubuntu 24.04 / ROS 2 Jazzy.

## ROS 2 first steps (Wi-Fi)

Before starting, power the Hovermap, enable **Publish external API messages**
and **Use Wi-Fi for external API**, connect the workstation to its isolated
Wi-Fi network, and configure that interface as `10.9.0.99/24`. Keep the
Hovermap securely held or mounted before starting a Mapping mission.

In terminal 1, from a ROS 2 Jazzy environment:

```bash
git clone --branch codex/ros2-jazzy \
  https://github.com/KinesisCTP/hovermap-st.git ~/hovermap-st_ros2_ws
cd ~/hovermap-st_ros2_ws
source /opt/ros/jazzy/setup.bash
vcs import . < hovermap_ros2_core_https.repos
sudo apt-get update && sudo apt-get install -y python3-avro
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

# Only after securing the Hovermap:
ros2 topic pub --once /cortex/start_scan std_msgs/msg/Empty '{}'
# Move it slowly while viewing the live cloud, then stop before closing ROS:
ros2 topic pub --once /cortex/stop_scan std_msgs/msg/Empty '{}'
```

Expected results are peer `st_0200`, a live corrected cloud near 20 Hz,
occupancy near 1 Hz, odometry near 100 Hz, and motion visible in RViz2.
Synchronize the host clock before timestamp-sensitive navigation or
measurement. See [Device and network gate](#device-and-network-gate) and
[Current validation status](#current-validation-status) for the operating
boundaries.

It provides:

- `hovermap_ros2_msgs`, with ROS 2 Hovermap status, scan, Mule diagnostic, and
  overlay-service interfaces;
- `hovermap_ros2_api`, with hardened HTTP control/download behavior and a
  native `rclpy` adapter around the separately imported Mule transport core;
- explicit, tested ROS 1 wire codecs for `PointCloud2`, `Odometry`,
  `TFMessage`, and `String`;
- original `/cortex/*` compatibility topics plus enabled-by-default `/tf` and
  `/tf_static` output for tf2 and RViz2; and
- offline codec, HTTP, queue, ordering, and configuration tests plus a Jazzy
  build and real-core compatibility smoke in CI.

Physical Wi-Fi validation against KINESIS Hovermap `st_0200` passed on
2026-09-11 for discovery, live native ROS 2 data, TF, RViz2, and start/stop
control. Ethernet, downloads, perception configuration, rosbag2/Nav2, and
long-duration parity testing remain open before operational release.

## Source and license boundary

No Emesent or CSIRO source is copied into the ROS 2 packages. The public
KINESIS tracking fork is imported at the audited commit
`1608fb74784977b69936590b9cda8340a1fe3013`; only its ROS-agnostic
`mule_bridge` package is added to `PYTHONPATH` at runtime.

The new KINESIS packages are marked `Proprietary` for private/internal
evaluation. Do not publish them until KINESIS selects an explicit license and
the conflicting upstream license metadata is resolved. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Repository layout

```text
.
├── src/hovermap_ros2_msgs/       # KINESIS ROS 2 interfaces
├── src/hovermap_ros2_api/        # HTTP node, Mule adapter, codecs, tests
├── third_party/COLCON_IGNORE     # prevents imported ROS 1 package discovery
├── hovermap_ros2_core_https.repos
├── scripts/source_mule_core.sh
├── tools/smoke_mule_core.py
├── docs/                         # activation and audited ROS 1 reference
└── containers/ros1_noetic/       # retained baseline/reference, not ROS 2 src
```

The former ROS 1 catkin bringup package is deliberately absent on this branch
so `rosdep` and `colcon` see only the two native ROS 2 packages. The container,
audit, activation, and verification files remain as parity evidence and a
reference for the `main` ROS 1 baseline.

## Detailed build and launch

Run from the repository root on Ubuntu 24.04 with ROS 2 Jazzy:

```bash
source /opt/ros/jazzy/setup.bash
vcs import . < hovermap_ros2_core_https.repos
sudo apt-get update
sudo apt-get install -y python3-avro
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

Native downloads default to the user-writable `~/hovermap_downloads`. Both
adapters are required by default: if either exits, the other node and launch
shut down instead of leaving a degraded partial API running.

Full topic, parameter, QoS, security, perception-configuration, test, and
hardware-acceptance details are in
[`src/hovermap_ros2_api/README.md`](src/hovermap_ros2_api/README.md).

## Device and network gate

API use requires the device-specific entitlement, **Publish external API
messages** enabled in the Web UI, and an isolated Hovermap-facing host
interface. Follow [`docs/device-activation.md`](docs/device-activation.md)
before testing.

| Connection | Prefix | Hovermap | Client | Netmask |
|---|---|---|---|---|
| Wi-Fi | `10.9.0.0` | `10.9.0.1` | `10.9.0.99` | `255.255.255.0` |
| ST Fischer Ethernet | `192.168.2.0` | `192.168.2.115` | `192.168.2.100` | `255.255.255.0` |
| USB Ethernet | `192.168.3.0` | `192.168.3.115` | `192.168.3.100` | `255.255.255.0` |

Mule uses UDP 8123 and TCP ports 49172–49191 (`max_port=49192` is exclusive).
The upstream Beacon receive socket binds UDP 8123 on all host interfaces even
though `ip_prefix` selects the ZMQ/outbound interface. Scope both UDP and TCP
with the host firewall to the isolated Hovermap interface/subnet. The HTTP and
Mule protocols are unauthenticated; never expose them to an untrusted network.

## Current validation status

Offline tests validate wire-format round trips and malformed buffers, HTTP
response/redirect/download safety, concurrency bounds, FIFO device-control
ordering, newest-only configuration buffering, static-TF aggregation, and
configuration parsing. Jazzy CI also
builds the interfaces and nodes and constructs the real pinned Mule Client on
loopback.

The 2026-09-11 Wi-Fi test on `st_0200` validated peer discovery and TCP
connection, ROS 2 start/stop control, RViz2 visualization, expected frames and
six PointFields, late static-TF delivery, and live rates of approximately 19.8
Hz corrected LiDAR, 0.98 Hz occupancy, and 99-100 Hz odometry. The bridge
reported zero queue drops, zero decode failures, and zero peer RTT failures.
The test used `ros:jazzy-ros-base-noble` with host networking on the Ubuntu
24.04 workstation because the host ROS installation is Kilted.

The workstation clock was approximately 101 seconds behind the Hovermap
timestamps; clock synchronization remains required before timestamp-sensitive
navigation or measurement. Ethernet discovery/reconnect, exact byte-for-byte
ROS 1 bag comparison, prefix/list/download controls, overlays, perception
persistence, rosbag2/Nav2 interoperability, and a sustained soak remain open.
Perception configuration has no device acknowledgement and is never applied
automatically.
