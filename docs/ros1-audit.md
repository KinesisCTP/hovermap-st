# ROS 1 source audit

Audit date: 2026-09-10

Audited source: `KinesisCTP/hovermap_ros_api` `main` at
`1608fb74784977b69936590b9cda8340a1fe3013`

Upstream: `Emesent/hovermap_ros_api` at the same commit (0 ahead, 0 behind)

## What the source contains

The fork is a complete ROS 1 Noetic catkin workspace, not an onboarding
repository. It contains:

- `hovermap_api`: launch/config plus HTTP control and perception-config nodes;
- `hovermap_api_msgs`: Hovermap status and stored-scan messages;
- `mule_bridge`: CSIRO-derived peer discovery and message transport; and
- `mule_bridge_msgs`: transport telemetry and overlay-control interfaces.

There are two separate data paths:

1. The Mule data plane discovers the Hovermap with UDP multicast/unicast,
   carries live data over ZeroMQ TCP, wraps control records with Avro, and uses
   zlib compression. Message payloads are opaque ROS 1 serialized bytes.
2. The HTTP path calls the Hovermap directly for status, scan prefix,
   start/stop, scan listing, and scan download.

The HTTP interface is plain and unauthenticated. Use it only on a dedicated,
trusted Hovermap network.

## Interface inventory

### Received from Hovermap

| Topic | Type | Nominal rate | Purpose |
|---|---|---:|---|
| `/cortex/lidar/corrected` | `sensor_msgs/PointCloud2` | 20 Hz | SLAM-corrected LiDAR |
| `/cortex/occupancy_grid_map/data` | `sensor_msgs/PointCloud2` | 1 Hz | Local 3-D navigation grid |
| `/cortex/odometry` | `nav_msgs/Odometry` | 100 Hz | Corrected local odometry |
| `/cortex/tf` | `tf2_msgs/TFMessage` | variable | Dynamic transforms |
| `/cortex/tf_static` | `tf2_msgs/TFMessage` | once | Static transforms |
| `/cortex/mule_bridge/status` | `mule_bridge_msgs/Status` | 1 Hz | Transport diagnostics |
| `/cortex/hovermap_status` | `hovermap_api_msgs/HovermapStatus` | 1 Hz | Scan/storage state |
| `/cortex/scan_names_response` | `hovermap_api_msgs/ScanInformationList` | on request | Stored scans |
| `/cortex/scan_download_successful` | `std_msgs/Bool` | on completion | Download result |

### Sent from the client

| Topic | Type | Purpose |
|---|---|---|
| `/cortex/occupancy_grid_map/configuration` | `std_msgs/String` | Persistent occupancy YAML |
| `/cortex/scan_names_request` | `std_msgs/Empty` | Request stored scans |
| `/cortex/set_scan_prefix` | `std_msgs/String` | Set persistent name prefix |
| `/cortex/start_scan` | `std_msgs/Empty` | Start Mapping mission |
| `/cortex/stop_scan` | `std_msgs/Empty` | Stop Mapping mission |
| `/cortex/download_scan` | `std_msgs/String` | Download scan by name |

### Local bridge service

| Service | Type | Purpose |
|---|---|---|
| `/cortex/mule_bridge/set_overlay` | `mule_bridge_msgs/SetOverlay` | Restrict outgoing persistent synchronization requests to named Mule peers; an empty list removes the restriction |

The exposed API does not include every sensor or product feature. In
particular, it does not expose raw LiDAR, IMU, cameras, GPS, scan deletion,
Aura processing, or autonomy controls.

## Network protocol

- default discovery: multicast `225.0.0.250`, UDP `8123`;
- data ports: TCP `49172` through `49191` (`49192` is the configured exclusive
  upper bound);
- topic frames: two-byte topic-name length, topic name, compressed ROS 1
  message bytes;
- device operations: `/status`, `/files`, `/startsystem?mission_type=1`,
  `/stopsystem`, `/setprefix`, and `/downloadscan`.

## Important defects and limitations

- `mule_network` is documented but absent from the Mule `Config` type, so it
  is ignored. Interface choice is actually the first IPv4 interface matching
  `ip_prefix`.
- `ip_netmask` is parsed but unused by interface selection.
- The vendor `/cortex/tf_static` publisher is not latched despite the README
  saying it is. `hovermap_st_bringup` adds a correctly latched `/tf_static`
  relay and a conventional `/tf` relay.
- The README refers to `tf_msgs/TFMessage`; the configuration actually uses
  `tf2_msgs/TFMessage`.
- The one-shot perception-config node neither waits for a subscriber nor
  receives an acknowledgement. This is consistent with the vendor note that
  it may need to run twice.
- Device commands are fire-and-forget. Only downloads receive a Boolean result,
  and that result is not correlated with a scan name or request ID.
- Concurrent downloads of the same scan can race on the same `.part` path.
- Downloads do not verify `Content-Length` or ZIP integrity.
- The Mule publishers use `queue_size=0`, risking unbounded buffering for
  high-rate point clouds.
- `mule_network`, `ip_netmask`, and `swarm_name` are not security boundaries;
  there is no transport authentication or encryption.
- The checked-in perception config is `160 x 160 x 100` at `0.2 m`; the README
  describes the device default as `160 x 160 x 160` at `0.25 m`. Since the
  setting persists across boots, do not apply it as part of onboarding.

## Build and maintenance findings

- ROS 1 Noetic and Ubuntu 20.04 are end-of-life; retain a containerized legacy
  runtime and pin the imported source.
- The vendor Dockerfile requires BuildKit `RUN --mount`, runs as root, and
  builds source into an image while later bind-mounting a different source
  tree over it.
- The vendor `package.xml` omits direct imports including `rospy`, `rospkg`,
  and `std_msgs`.
- Pip requirements are exported with a nonstandard tag, so `rosdep` cannot
  install them automatically.
- The Python pins include Tornado 5.1.1 and netifaces 0.10.9, both too old for
  a native Python 3.12 / ROS 2 Jazzy runtime without validation.
- Chrony must discipline the host clock. Installing it in an ordinary
  unprivileged container does not synchronize the host.

## Existing test coverage

The vendor tests cover Avro protocol round trips, Mule history/database
behavior, and a two-master bridge flow. They do not cover HTTP operations,
perception delivery, TF latching, Docker, point-cloud/odometry fidelity, or a
real Hovermap. This repository's CI establishes that the imported source still
builds and its existing tests still run; hardware acceptance remains separate.

## Licensing boundary

The repository-level MIT/REUSE declarations conflict with `Proprietary`
entries in two package manifests. The Mule subtree has a separate CSIRO
permissive licence. See `THIRD_PARTY_NOTICES.md`; preserve upstream notices and
clarify the manifest discrepancy before publishing a derivative port.
