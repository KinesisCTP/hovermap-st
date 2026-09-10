# hovermap_ros2_api

KINESIS-authored native ROS 2 Jazzy nodes for the Hovermap API. This package
does not contain or relicense the Emesent/CSIRO Mule implementation.

This package is marked `Proprietary` for private/internal evaluation pending an
explicit licensing decision. Do not publish it until the conflicting upstream
license metadata has been reconciled and KINESIS has selected a license.

## Architecture and runtime dependency

The Hovermap Mule endpoint transports opaque ROS 1 serialized messages. It
does not require a ROS 1 master and does not negotiate a ROS version. This
package therefore:

1. imports the ROS-agnostic `mule_bridge` transport core from the separate
   KINESIS fork at runtime;
2. receives complete ROS 1 buffers on the Mule/Tornado I/O thread;
3. transfers them through a bounded queue;
4. explicitly decodes `sensor_msgs/PointCloud2`, `nav_msgs/Odometry`, and
   `tf2_msgs/TFMessage`; and
5. publishes equivalent native ROS 2 messages with purpose-specific QoS.

The occupancy-map configuration path works in reverse: a ROS 2
`std_msgs/String` is explicitly encoded in ROS 1 wire format and sent through
Mule. ROS 1 `Header.seq` has no ROS 2 equivalent and is intentionally dropped;
timestamps, frame IDs, covariance arrays, PointField metadata, and point-cloud
payload bytes are preserved.

The volatile cross-thread wire queue is bounded by both event count and
aggregate payload bytes (`event_queue_depth` and `event_queue_bytes`) and drops
oldest work first, so a stalled ROS executor keeps recent live samples instead
of publishing an ever-staler backlog. Low-rate persistent static transforms use
a separate lossless queue so a unique static frame cannot be discarded behind
LiDAR. Mule lifecycle and transport-error events also use critical queues. Wire
drops are reported in logs, the aggregate `BridgeStatus` counter, and per-topic
drop counters; codec failures have their own `BridgeStatus` counter.
Transport draining is count- and time-budgeted on its own callback group;
configuration/overlay and status callbacks use separate groups so a sensor
backlog cannot monopolize every executor callback lane.

These adapter limits begin after the imported core callback. The pinned core
performs unauthenticated `zlib.decompress` without an output bound before the
adapter can enforce its queue and codec limits. Treat the Hovermap network as
trusted and isolated. Bounded streaming decompression and authentication, if
introduced, belong in the separately maintained core and require upstream
license review.
The ROS 1 wrapper's `initial_wait` behavior is retained as an interruptible
delay after ROS 2 endpoints are created and before Mule sockets are opened.
The launch shuts down both adapters when either process exits, matching the
ROS 1 `required` behavior. `shutdown_on_mule_exit:=false` and
`shutdown_on_http_exit:=false` are available only for deliberate diagnostics.

HTTP work is also bounded (`http_workers` and `max_pending_requests`). Mutating
start, stop, and prefix commands use a separate bounded single-worker lane, so
they execute in submission order without being blocked by status, listing, or
download work. Only one scan download may be queued or running. HTTP worker
threads are daemons, so an unresponsive 450-second archive-preparation request
cannot hold process exit; shutdown asks streaming downloads to stop and leaves
only a `.part` file.
Redirects are rejected before urllib follows them, and environment proxy
settings are bypassed, preventing device traffic from reaching another host or
corporate proxy.
The compatibility control topics are separate DDS streams, so ordering across
different topic names is not a protocol guarantee. Once submitted, mutations
run serially; a stop can wait behind already accepted controls. A future
correlated service/action is required for strict multi-command transactions.

Install the Mule core from the separately checked-out KINESIS fork into the
same Python environment. The fork must expose `mule_bridge.Client` and
`mule_bridge.Config`:

```bash
vcs import . < hovermap_ros2_core_https.repos
source scripts/source_mule_core.sh
python3 -c 'import mule_bridge; print(mule_bridge.__version__)'
PYTHONPATH="src/hovermap_ros2_api:${PYTHONPATH}" \
  python3 tools/smoke_mule_core.py \
    --expected-root third_party/hovermap_ros_api \
    --expected-commit 1608fb74784977b69936590b9cda8340a1fe3013 \
    --instantiate-client
```

Use current Ubuntu packages for Avro, pyzmq, Tornado, and netifaces. Ubuntu
Noble provides Avro as `python3-avro`, but the upstream rosdep database has no
matching key, so install that package explicitly before running `rosdep`. Do
not reintroduce the legacy `tornado==5.1.1` or `netifaces==0.10.9` pins on
Noble. The separate fork must be tested with those current dependencies. The
imported core currently creates `mule_<node_name>.db` in its working directory
even for this read-mostly configuration; that behavior should be corrected in
the fork before production deployment.

The CI real-core smoke verifies the pinned commit, imports the exact checkout,
builds its real `Config`, and constructs/releases `Client`, IOLoop, ZeroMQ, and
SQLite resources on loopback in a temporary directory. It deliberately does
not start discovery because the pinned core binds its UDP beacon to all host
interfaces; Tornado discovery/Avro exchange remains an isolated-network and
hardware acceptance item.

## Build and launch

Run these commands from the repository root:

```bash
source /opt/ros/jazzy/setup.bash
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

The Mule core selects a network interface by `ip_prefix`; the interface itself
must already have the required static address. Host networking must permit
multicast UDP 8123 and TCP ports 49172-49191 (configured as the half-open range
`[49172, 49192)`). Keep the unauthenticated HTTP and Mule
interfaces on the isolated Hovermap network. The Hovermap also requires the
external API feature entitlement and the API publication switch enabled.
`ip_prefix` selects the ZMQ bind and outbound multicast interface, but the
upstream Beacon receive socket binds UDP 8123 on all host interfaces. Firewall
UDP 8123 and the TCP range to the isolated Hovermap interface/subnet; do not
treat `ip_prefix` as an exposure boundary.
Unicast discovery addresses are additive by default. For a unicast-only
deployment, set `ping_mcast_group` to an empty string and provide a non-empty
typed string array in `ping_ucast_addrs`.

Native launches download to the user-writable `~/hovermap_downloads` by
default. Container deployments may explicitly override
`download_directory:=/data/downloads` when that path is bind-mounted writable.

## Compatibility topics

The fixed Mule wire names stay under `/cortex`; changing them would break the
device protocol. ROS 2 output topic parameters are independently remappable.

Published:

| Topic | ROS 2 type | QoS |
|---|---|---|
| `/cortex/lidar/corrected` | `sensor_msgs/msg/PointCloud2` | best effort, volatile |
| `/cortex/occupancy_grid_map/data` | `sensor_msgs/msg/PointCloud2` | best effort, volatile |
| `/cortex/odometry` | `nav_msgs/msg/Odometry` | best effort, volatile |
| `/cortex/tf` | `tf2_msgs/msg/TFMessage` | reliable, volatile |
| `/cortex/tf_static` | `tf2_msgs/msg/TFMessage` | reliable, transient local |
| `/tf` | `tf2_msgs/msg/TFMessage` | reliable, volatile |
| `/tf_static` | `tf2_msgs/msg/TFMessage` | reliable, transient local |
| `/cortex/mule_bridge/status` | `hovermap_ros2_msgs/msg/BridgeStatus` | reliable |
| `/cortex/hovermap_status` | `hovermap_ros2_msgs/msg/HovermapStatus` | reliable |
| `/cortex/scan_names_response` | `hovermap_ros2_msgs/msg/ScanInformationList` | reliable |
| `/cortex/scan_download_successful` | `std_msgs/msg/Bool` | reliable |

Consumed:

- `/cortex/occupancy_grid_map/configuration` (`std_msgs/msg/String`)
- `/cortex/scan_names_request` (`std_msgs/msg/Empty`)
- `/cortex/set_scan_prefix` (`std_msgs/msg/String`)
- `/cortex/start_scan` (`std_msgs/msg/Empty`)
- `/cortex/stop_scan` (`std_msgs/msg/Empty`)
- `/cortex/download_scan` (`std_msgs/msg/String`)

Service:

- `/cortex/mule_bridge/set_overlay`
  (`hovermap_ros2_msgs/srv/SetOverlay`, `string[] node_names` and an empty
  response). An empty list removes overlay restrictions, matching ROS 1.

These compatibility topics intentionally match the ROS 1 interface. Services
and a correlated download action can be added later without removing them.

Standard `/tf` and `/tf_static` relays are enabled by default for tf2 and RViz2
compatibility while the original `/cortex/tf*` topics remain available. Since
transient-local depth 1 retains only one message, the adapter accumulates the
latest static transform for each child frame and publishes the full aggregate
on both static topics. Set `publish_standard_tf:=false` only when another relay
already owns the standard topics.

The high-rate point-cloud and odometry outputs default to DDS best-effort QoS,
which is intentionally different from ROS 1 TCPROS reliability and avoids a
slow subscriber back-pressuring the live sensor bridge. Set
`sensor_reliability:=reliable` if a deployment requires reliable delivery, then
validate latency and queue-drop counters on the target network.

The type and units of the undocumented `/files.time` field remain
hardware-unconfirmed: neither the supplied MENA3D instructions nor the public
guide specifies them. The client normalizes finite numbers, numeric strings,
and ISO-8601 strings. If it sees another non-empty scalar string, it emits a
warning and preserves the device's stable order rather than inventing one.

For clarity, the ROS 2 diagnostic types rename the original generic Mule
`Status`, `Peer`, and `Topic` messages to `BridgeStatus`, `PeerStatus`, and
`TopicStatus`. Original fields are preserved; `BridgeStatus` adds KINESIS
adapter drop and decode-failure counters, and `TopicStatus` adds volatile and
persistent publication-drop counters. Persistent static TF uses the protected
non-dropping path, so its drop counter stays zero under the adapter policy.
`Manifest`, `LogIndex`, and `SetOverlay` keep their ROS 1 names and structures.

## Offline tests

The codec and HTTP client do not import ROS, so their unit tests can run before
Jazzy is installed:

```bash
PYTHONPATH=src/hovermap_ros2_api python3 -m unittest discover \
  -s src/hovermap_ros2_api/test -v
```

On Jazzy, also run `colcon test` and inspect
`colcon test-result --verbose`.

## Hardware acceptance gate

Do not claim parity until the following pass against an API-enabled Hovermap:

- Wi-Fi and Ethernet discovery, reconnect, and disconnect behavior;
- approximately 20 Hz corrected lidar, 1 Hz occupancy, 100 Hz odometry, and
  1 Hz status without queue drops;
- exact frame IDs, transform topology, timestamps, covariances, PointFields,
  row steps, and point bytes compared with a ROS 1 bag oracle;
- late-subscriber receipt of `/cortex/tf_static` and RViz2 visualization;
- start/stop, prefix persistence, newest-first scan listing, valid ZIP download,
  failure reporting, and concurrent-download rejection;
- occupancy configuration round trip and persistence/reboot behavior;
- rosbag2 record/replay, Nav2 PointCloud2 ingestion, and a 30-60 minute soak
  comparing loss, CPU, and memory against the ROS 1 node.

The existing perception configuration must not be applied silently: its checked
in dimensions/resolution differ from values described in upstream prose, and
the device persists configuration across reboot.

For that reason this repository does not ship or auto-launch a perception
payload. After reviewing an authoritative YAML file, publish it explicitly:

```bash
ros2 run hovermap_ros2_api configure_perception --ros-args \
  -p config_file:=/absolute/path/to/reviewed-perception-config.yaml
```

The command waits for both the ROS 2 adapter subscription and a stable Mule
peer reported by `/cortex/mule_bridge/status`, then sends a small configurable
burst to reduce the upstream PUB slow-join risk. Set `required_peer_name` when
the authoritative Hovermap Mule node name is known. This is still not an
acknowledgement: the upstream protocol provides no command response, so verify
occupancy output and persisted configuration on the physical device.
Adapter send failures retain the newest configuration and make up to
`configuration_retry_limit` delayed retries, but those retries are transport
attempts rather than device acknowledgements.
