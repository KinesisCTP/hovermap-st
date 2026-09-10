# Kinesis Hovermap ST — ROS 2 Jazzy

This private `codex/ros2-jazzy` branch is the KINESIS native ROS 2 prototype
for the Emesent Hovermap ST on Ubuntu 24.04 / ROS 2 Jazzy. It provides:

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

This is an engineering prototype, not a claim of hardware parity. Physical
Hovermap acceptance remains mandatory before operational use.

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

## Build and launch

Run from the repository root on Ubuntu 24.04 with ROS 2 Jazzy:

```bash
source /opt/ros/jazzy/setup.bash
vcs import . < hovermap_ros2_core_https.repos
sudo apt-get update
sudo apt-get install -y python3-avro
rosdep install --from-paths src/hovermap_ros2_msgs src/hovermap_ros2_api \
  --ignore-src -r -y
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

## Current validation boundary

Offline tests validate wire-format round trips and malformed buffers, HTTP
response/redirect/download safety, concurrency bounds, FIFO device-control
ordering, newest-only configuration buffering, static-TF aggregation, and
configuration parsing. Jazzy CI also
builds the interfaces and nodes and constructs the real pinned Mule Client on
loopback.

Real hardware must still validate discovery/reconnect, rates and loss, message
bytes and timestamps against the ROS 1 oracle, transform topology and late
static subscribers, every control path, ZIP downloads, overlay behavior,
perception persistence, rosbag2/Nav2 interoperability, and a sustained soak.
Perception configuration has no device acknowledgement and is never applied
automatically.
