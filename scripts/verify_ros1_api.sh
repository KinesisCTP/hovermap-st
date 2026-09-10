#!/usr/bin/env bash
set -euo pipefail

required_topics=(
    /cortex/lidar/corrected:sensor_msgs/PointCloud2
    /cortex/occupancy_grid_map/data:sensor_msgs/PointCloud2
    /cortex/odometry:nav_msgs/Odometry
    /cortex/tf:tf2_msgs/TFMessage
    /cortex/tf_static:tf2_msgs/TFMessage
    /cortex/hovermap_status:hovermap_api_msgs/HovermapStatus
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

if [ "${failed}" -ne 0 ]; then
    exit 1
fi

echo
echo "Core API topics are present. Observe rates without changing device state:"
echo "  rostopic hz /cortex/lidar/corrected"
echo "  rostopic hz /cortex/occupancy_grid_map/data"
echo "  rostopic hz /cortex/odometry"
