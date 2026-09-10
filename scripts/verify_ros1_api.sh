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
    /cortex/occupancy_grid_map/configuration:std_msgs/String
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

if [ "${failed}" -ne 0 ]; then
    exit 1
fi

echo
echo "ROS 1 topic and service registrations are present."
echo "Observe live rates without changing device state:"
echo "  rostopic hz /cortex/lidar/corrected"
echo "  rostopic hz /cortex/occupancy_grid_map/data"
echo "  rostopic hz /cortex/odometry"
