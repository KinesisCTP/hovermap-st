# KINESIS Hovermap ST — ROS 1 Noetic

A containerized ROS 1 Noetic workspace for KINESIS users operating the
Hovermap ST. The native ROS 2 Jazzy version is available on the
[`ros2-jazzy`](https://github.com/KinesisCTP/hovermap-st/tree/ros2-jazzy)
branch.

## Quick start (Wi-Fi)

Power the Hovermap, connect the Linux workstation to its isolated Wi-Fi
network, and configure that interface as `10.9.0.99/24`. Keep the Hovermap
securely held or mounted before starting a Mapping mission.

In terminal 1:

```bash
git clone https://github.com/KinesisCTP/hovermap-st.git ~/hovermap-st_ws
cd ~/hovermap-st_ws
./scripts/preflight_network.sh wifi wlan0
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

Expected results are discovery of a Hovermap Mule peer, all `PASS` lines from
the verification script, corrected LiDAR near 20 Hz, odometry near 100 Hz,
and a live cloud in RViz. Synchronize the **host** clock to the Hovermap NTP
server before timestamp-sensitive navigation or measurement.

## Network profiles

Configure exactly one Hovermap-facing host interface with the client address:

| Connection | Prefix | Hovermap | Client | Netmask |
|---|---|---|---|---|
| Wi-Fi | `10.9.0.0` | `10.9.0.1` | `10.9.0.99` | `255.255.255.0` |
| ST Fischer Ethernet | `192.168.2.0` | `192.168.2.115` | `192.168.2.100` | `255.255.255.0` |
| USB Ethernet | `192.168.3.0` | `192.168.3.115` | `192.168.3.100` | `255.255.255.0` |

The API currently ignores the `mule_network` value in its YAML. Mule chooses
the first IPv4 interface matching `ip_prefix`, so avoid configuring the same
Hovermap subnet on multiple host interfaces.

The API transport is selected on the Hovermap at boot. Keep **Use Wi-Fi for
external API** enabled for Wi-Fi. Disable it for Fischer or USB Ethernet, then
power-cycle the Hovermap before using that connection.

Run the matching read-only preflight before launching:

```bash
./scripts/preflight_network.sh wifi wlan0
# or: ./scripts/preflight_network.sh fischer enp4s0
# or: ./scripts/preflight_network.sh usb enx001122334455
```

Mule uses UDP `8123`, multicast `225.0.0.250`, and dynamically selected TCP
ports `49172-49191`; the configured upper bound, `49192`, is exclusive. The
HTTP API is unencrypted and unauthenticated, so use only an isolated, trusted
Hovermap network.

## Container setup

Install Docker Engine on a Linux workstation and verify it is usable without
`sudo`, then clone and start the workspace:

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

Rebuild the image after changing
`containers/ros1_noetic/requirements-ros1.txt`:

```bash
./containers/ros1_noetic/build.sh
```

The bootstrap is container-only. The API downloads to `/data/downloads`, and
`run.sh` supplies a writable bind mount there.

## Start the API client

Use the launch arguments for the active network profile. Wi-Fi is the default:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=10.9.0.0 ip_netmask:=255.255.255.0
```

For ST Fischer Ethernet:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=192.168.2.0 ip_netmask:=255.255.255.0
```

For USB Ethernet:

```bash
roslaunch hovermap_st_bringup hovermap_api.launch \
  ip_prefix:=192.168.3.0 ip_netmask:=255.255.255.0
```

Expected logs include discovery of a peer named for the Hovermap and a
successful ZeroMQ connection. The KINESIS launch also relays namespaced
transforms to standard `/tf` and latched `/tf_static`. Set
`relay_standard_tf:=false` only if another relay owns those topics.

## Live data check

Start a Mapping mission from Commander, the Web UI, or the control topic below,
then run these checks individually or in separate terminals:

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

Synchronize the **host** clock to the Hovermap NTP server. Chrony inside the
unprivileged development container cannot adjust the host clock.

## Device-control topics

These commands change device or mission state. Run them only after the
read-only checks are healthy and the operator is ready:

```bash
# Set an alphanumeric prefix of at most 20 characters.
rostopic pub -1 /cortex/set_scan_prefix std_msgs/String "data: 'Kinesis'"

# Start and stop a Mapping scan.
rostopic pub -1 /cortex/start_scan std_msgs/Empty '{}'
rostopic pub -1 /cortex/stop_scan std_msgs/Empty '{}'

# In a listener terminal, then a request terminal: list stored scans.
rostopic echo -n 1 /cortex/scan_names_response
rostopic pub -1 /cortex/scan_names_request std_msgs/Empty '{}'

# In a listener terminal, then a request terminal: download one exact name.
rostopic echo -n 1 /cortex/scan_download_successful
rostopic pub -1 /cortex/download_scan std_msgs/String "data: 'Kinesis_01'"
```

Avoid `configure_perception` unless deliberately changing persistent device
settings; it has no acknowledgement.

## Contributor setup

On a Linux host with `vcstool` and an authenticated GitHub SSH connection,
KINESIS contributors can import through SSH:

```bash
vcs import src < hovermap_kinesis_ssh.repos
./scripts/use_ssh_remotes.sh
```

For the container path, first use the pinned HTTPS bootstrap. Then run
`./scripts/use_ssh_remotes.sh` on the host; it refuses dirty or divergent
source, fetches `origin/main`, and leaves the imported source on a local
`main` branch. SSH credentials are not mounted into the development container.

Never commit scans, local `.env` files, device-specific files, or network
credentials. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the
terms that govern imported software.

KINESIS-authored code remains proprietary; no open-source license is granted.
Public availability does not change those rights.
