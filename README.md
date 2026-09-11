# Kinesis Hovermap ST Workspace

This private repository is the onboarding workspace for Kinesis CTP users
working with the Emesent Hovermap ST. The default branch provides a
reproducible ROS 1 Noetic baseline; native ROS 2 Jazzy development is isolated
on the `codex/ros2-jazzy` branch until it reaches hardware parity.

## ROS 1 first steps (Wi-Fi)

Before starting, power the Hovermap, enable **Publish external API messages**
and **Use Wi-Fi for external API**, connect the workstation to its isolated
Wi-Fi network, and configure that interface as `10.9.0.99/24`. Keep the
Hovermap securely held or mounted before starting a Mapping mission.

In terminal 1:

```bash
git clone https://github.com/KinesisCTP/hovermap-st.git ~/hovermap-st_ws
cd ~/hovermap-st_ws
./scripts/preflight_network.sh wifi <wifi-interface>
./containers/ros1_noetic/run.sh

# Inside the container:
./scripts/bootstrap_ros1.sh
source devel/setup.bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=10.9.0.0 ip_netmask:=255.255.255.0
```

In terminal 2:

```bash
cd ~/hovermap-st_ws
./containers/ros1_noetic/enter.sh

# Inside the container:
source devel/setup.bash
./scripts/verify_ros1_api.sh
rviz -d "$(rospack find hovermap_st_bringup)/rviz/hovermap.rviz"
```

After securing the Hovermap, start and stop a Mapping mission from Commander,
the Web UI, or the ROS topics below. Always stop the mission before closing
the node:

```bash
rostopic pub -1 /cortex/start_scan std_msgs/Empty '{}'
# Move the secured Hovermap slowly while viewing the live cloud in RViz.
rostopic pub -1 /cortex/stop_scan std_msgs/Empty '{}'
```

Expected results are an `st_0200` Mule peer, all `PASS` lines from the
verification script, corrected LiDAR near 20 Hz, odometry near 100 Hz, and a
live cloud in RViz. Synchronize the **host** clock to the Hovermap NTP server
before timestamp-sensitive navigation or measurement. See [Network
profiles](#network-profiles), [Device and mission
controls](#device-control-topics), and [Validation status](#validation-status)
for the detailed operating boundaries.

It provides:

- HTTPS and SSH `.repos` manifests for the Kinesis tracking fork of Emesent's
  ROS API (the HTTPS onboarding manifest is pinned to the audited commit);
- a containerized ROS 1 Noetic environment for current Ubuntu hosts;
- Kinesis bringup with explicit network-profile arguments;
- conventional `/tf` and latched `/tf_static` relays;
- read-only network and topic verification scripts; and
- an evidence-backed source audit and hardware acceptance checklist.

The device-bound feature file, credentials, vendor media, and downloaded scans
are deliberately excluded from Git.

## Repository layout

```text
.
├── containers/ros1_noetic/       # containerized Noetic environment
├── docs/device-activation.md      # entitlement and API activation runbook
├── docs/ros1-audit.md             # source, interface, risk, and test audit
├── hovermap_kinesis_https.repos   # simple/read-only dependency import
├── hovermap_kinesis_ssh.repos     # Kinesis contributor import
├── scripts/                       # bootstrap and non-mutating checks
└── src/hovermap_st_bringup/       # Kinesis launch and TF relay package
```

The implementation remains in the separate public tracking fork:

- `KinesisCTP/hovermap_ros_api`
- upstream `Emesent/hovermap_ros_api`

The reproducible HTTPS onboarding baseline is commit
`1608fb74784977b69936590b9cda8340a1fe3013`; at audit time the Kinesis fork was
identical to Emesent `main`.

## Activation boundary

API use requires all of the following:

- an API-enabled Hovermap with Cortex `4.0.2` or newer;
- the device-specific feature entitlement installed successfully;
- **Publish external API messages** enabled in the Hovermap Web UI; and
- a Linux host connected through Wi-Fi, USB Ethernet, or the ST
  Fischer-to-Ethernet interface.

MENA3D supplied Kinesis with an Emesent feature image and the
[Hovermap feature-upgrade guide](https://knowledge.emesent.com/docs/hovermap-feature-upgrade).
The file is intentionally not in this repository. The current guide names
Hovermap ST-X as its prerequisite even though the supplied entitlement targets
the Kinesis Hovermap ST; confirm the Web UI recognizes the package and allow
its built-in integrity check to complete before selecting **Install**. Do not
rename, edit, publish, or commit the file.

The feature upgrade is a physical-device operation. A successful GitHub build
does not prove entitlement installation or API activation.

Follow [`docs/device-activation.md`](docs/device-activation.md) for the exact
install sequence, stop conditions, transport selection, and post-install
checks.

## Network profiles

Configure exactly one Hovermap-facing host interface with the client address:

| Connection | Prefix | Hovermap | Client | Netmask |
|---|---|---|---|---|
| Wi-Fi | `10.9.0.0` | `10.9.0.1` | `10.9.0.99` | `255.255.255.0` |
| ST Fischer Ethernet | `192.168.2.0` | `192.168.2.115` | `192.168.2.100` | `255.255.255.0` |
| USB Ethernet | `192.168.3.0` | `192.168.3.115` | `192.168.3.100` | `255.255.255.0` |

The vendor YAML's `mule_network` value is currently ignored by the code. Mule
chooses the first IPv4 interface matching `ip_prefix`, so avoid configuring
the same Hovermap subnet on multiple host interfaces.

The API transport is selected on the Hovermap at boot. Keep **Use Wi-Fi for
external API** enabled for Wi-Fi. Disable it for Fischer or USB Ethernet, then
power-cycle the Hovermap before testing that connection.

Before launching, run a read-only preflight on the Linux host:

```bash
./scripts/preflight_network.sh wifi wlan0
# or: ./scripts/preflight_network.sh fischer enp4s0
# or: ./scripts/preflight_network.sh usb enx001122334455
```

The Mule path uses UDP `8123`, multicast `225.0.0.250`, and dynamically selected
TCP ports `49172-49191`. The configured upper bound, `49192`, is exclusive.
The HTTP API is unencrypted and unauthenticated; use
only an isolated, trusted Hovermap network.

## Detailed container setup

Install Docker Engine on a Linux workstation and verify it is usable without
`sudo`, then clone this repository:

```bash
git clone https://github.com/KinesisCTP/hovermap-st.git ~/hovermap-st_ws
cd ~/hovermap-st_ws
./containers/ros1_noetic/run.sh
```

Inside the container, import dependencies and build:

```bash
./scripts/bootstrap_ros1.sh
source devel/setup.bash
```

The container image bakes in the vendor's pinned Python requirements because
its nonstandard package metadata is invisible to `rosdep`. Re-run
`./containers/ros1_noetic/build.sh` after changing the mirrored requirements.

## Start the API client

Use the matching prefix. Wi-Fi is the default:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=10.9.0.0 ip_netmask:=255.255.255.0
```

For the ST Fischer interface:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=192.168.2.0 ip_netmask:=255.255.255.0
```

For USB Ethernet:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=192.168.3.0 ip_netmask:=255.255.255.0
```

Expected logs include discovery of a peer named for the Hovermap serial and a
successful ZeroMQ connection. The Kinesis launch also relays namespaced
transforms to standard `/tf` and latched `/tf_static`. Set
`relay_standard_tf:=false` only if another relay owns those topics.

## Live data check

Start a Mapping mission from Commander or the Web UI, then run:

```bash
./scripts/verify_ros1_api.sh
rostopic hz /cortex/lidar/corrected
rostopic hz /cortex/occupancy_grid_map/data
rostopic hz /cortex/odometry
rviz -d "$(rospack find hovermap_st_bringup)/rviz/hovermap.rviz"
```

Nominal rates are approximately 20 Hz for corrected LiDAR, 1 Hz for the local
occupancy grid, 100 Hz for odometry, and 1 Hz for Hovermap status. Inspect the
actual point fields, frame IDs, transform tree, and clock offset before using
the data for navigation or measurement.

Synchronize the **host** clock to the Hovermap's NTP server. Do not assume that
running Chrony in the unprivileged development container can adjust host time.

## Device-control topics

These commands change device or mission state. Run them only after the
read-only baseline is healthy and the operator is ready:

```bash
# Set an alphanumeric prefix of at most 20 characters.
rostopic pub -1 /cortex/set_scan_prefix std_msgs/String "data: 'Kinesis'"

# Start and stop a Mapping scan.
rostopic pub -1 /cortex/start_scan std_msgs/Empty '{}'
rostopic pub -1 /cortex/stop_scan std_msgs/Empty '{}'

# List stored scans.
rostopic pub -1 /cortex/scan_names_request std_msgs/Empty '{}'
rostopic echo -n 1 /cortex/scan_names_response

# Download one exact returned scan name into ./downloads.
rostopic pub -1 /cortex/download_scan std_msgs/String "data: 'Kinesis_01'"
rostopic echo -n 1 /cortex/scan_download_successful
```

Do not run the vendor `configure_perception` helper during onboarding. Its YAML
changes persistent device state, has no acknowledgement, and differs from the
README's stated defaults.

## Contributor setup

On a Linux host with `vcstool` and an authenticated GitHub SSH connection,
Kinesis contributors can import through SSH:

```bash
vcs import src < hovermap_kinesis_ssh.repos
./scripts/use_ssh_remotes.sh
```

For the container path, first use the pinned HTTPS bootstrap. Then run
`./scripts/use_ssh_remotes.sh` on the host; it refuses dirty/divergent source,
fetches `origin/main`, and leaves the source on a local `main` branch. SSH
credentials are deliberately not mounted into the development container.

Recommended imported-repository remotes:

```text
origin   -> git@github.com:KinesisCTP/hovermap_ros_api.git
upstream -> git@github.com:Emesent/hovermap_ros_api.git
```

Keep vendor changes in the tracking fork and device-specific onboarding in
this repository. Never commit feature entitlements, scans, local `.env` files,
or network credentials.

The ROS 1 bootstrap is intentionally container-only. The upstream HTTP node
hard-codes `/data/downloads`; `run.sh` supplies a writable bind mount there.
A native non-root launch requires an equivalent writable path or a future
tracking-fork change that makes the download directory configurable.

No open-source licence is granted for Kinesis-authored code while this
repository remains private. Resolve the upstream metadata conflict and select
an explicit licence before any external publication.

## Validation status

The repository CI checks shell/XML/Python syntax, builds the documented
container, imports the pinned public fork, builds all catkin packages, and
runs the vendor's available tests. Real-device acceptance is still required
for:

- entitlement/API activation and Cortex version;
- Wi-Fi and intended Ethernet discovery/reconnect;
- topic names, types, rates, point fields, timestamps, and frame tree;
- status and stored-scan parsing;
- prefix, start, stop, list, and one small ZIP-verified download; and
- perception configuration only under an explicitly approved test.

See [`docs/ros1-audit.md`](docs/ros1-audit.md) for the detailed audit and known
limitations.
