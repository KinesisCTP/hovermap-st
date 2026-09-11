#!/usr/bin/env bash
set -euo pipefail

required_topics=(
    /cortex/lidar/corrected:sensor_msgs/PointCloud2
    /cortex/occupancy_grid_map/data:sensor_msgs/PointCloud2
    /cortex/odometry:nav_msgs/Odometry
    /cortex/tf:tf2_msgs/TFMessage
    /cortex/tf_static:tf2_msgs/TFMessage
    /cortex/mule_bridge/status:mule_bridge_msgs/Status
    /cortex/hovermap_status:hovermap_api_msgs/HovermapStatus
    /cortex/scan_names_response:hovermap_api_msgs/ScanInformationList
    /cortex/scan_download_successful:std_msgs/Bool
    /cortex/scan_names_request:std_msgs/Empty
    /cortex/set_scan_prefix:std_msgs/String
    /cortex/start_scan:std_msgs/Empty
    /cortex/stop_scan:std_msgs/Empty
    /cortex/download_scan:std_msgs/String
    /tf:tf2_msgs/TFMessage
    /tf_static:tf2_msgs/TFMessage
)

required_services=(
    /cortex/mule_bridge/set_overlay:mule_bridge_msgs/SetOverlay
)

failed=0
for item in "${required_topics[@]}"; do
    topic="${item%%:*}"
    expected="${item#*:}"
    actual="$(rostopic type "${topic}" 2>/dev/null || true)"
    if [ "${actual}" = "${expected}" ]; then
        printf 'PASS %-42s %s\n' "${topic}" "${actual}"
    else
        printf 'FAIL %-42s expected %s, got %s\n' \
            "${topic}" "${expected}" "${actual:-<missing>}" >&2
        failed=1
    fi
done

for item in "${required_services[@]}"; do
    service="${item%%:*}"
    expected="${item#*:}"
    actual="$(rosservice type "${service}" 2>/dev/null || true)"
    if [ "${actual}" = "${expected}" ]; then
        printf 'PASS %-42s %s\n' "${service}" "${actual}"
    else
        printf 'FAIL %-42s expected %s, got %s\n' \
            "${service}" "${expected}" "${actual:-<missing>}" >&2
        failed=1
    fi
done

# The perception-configuration topic is a subscriber on the remote Hovermap,
# so it is advertised in Mule's peer manifest rather than registered in the
# local ROS graph until a local publisher is started. Verify it, and the peer
# connection itself, through the read-only Mule status message.
if ! python3 - <<'PY'
import sys

import rospy
from mule_bridge_msgs.msg import Status

rospy.init_node("hovermap_ros1_verify", anonymous=True, disable_signals=True)
try:
    status = rospy.wait_for_message("/cortex/mule_bridge/status", Status, timeout=10)
except rospy.ROSException as error:
    print(f"FAIL /cortex/mule_bridge/status              {error}", file=sys.stderr)
    raise SystemExit(1)

peer_names = [peer.node_name for peer in status.peers]
if peer_names:
    print(f"PASS {'Mule peer discovery':42} {', '.join(peer_names)}")
else:
    print(f"FAIL {'Mule peer discovery':42} no peers", file=sys.stderr)
    raise SystemExit(1)

expected_remote_topics = {
    "/cortex/occupancy_grid_map/configuration": "std_msgs/String",
}
advertised_topics = {topic.topic_name: topic.type_name for topic in status.topics}
failed = False
for topic_name, expected_type in expected_remote_topics.items():
    actual_type = advertised_topics.get(topic_name)
    if actual_type == expected_type:
        print(f"PASS {topic_name:42} {actual_type} (remote)")
    else:
        print(
            f"FAIL {topic_name:42} expected {expected_type}, "
            f"got {actual_type or '<missing>'} in Mule status",
            file=sys.stderr,
        )
        failed = True

raise SystemExit(1 if failed else 0)
PY
then
    failed=1
fi

if [ "${failed}" -ne 0 ]; then
    exit 1
fi

echo
echo "ROS 1 registrations and Mule peer manifest are present."
echo "Observe live rates without changing device state:"
echo "  rostopic hz /cortex/lidar/corrected"
echo "  rostopic hz /cortex/occupancy_grid_map/data"
echo "  rostopic hz /cortex/odometry"
