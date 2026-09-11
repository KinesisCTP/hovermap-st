"""ROS-aware conversion tests; skipped in the pure-Python test job."""

import unittest

try:
    import geometry_msgs.msg  # noqa: F401
    import nav_msgs.msg  # noqa: F401
    import sensor_msgs.msg  # noqa: F401
    import tf2_msgs.msg  # noqa: F401
    import rclpy
    from rclpy.context import Context
    from rclpy.node import Node
    from rclpy.parameter import Parameter

    ROS_MESSAGES_AVAILABLE = True
except ImportError:
    ROS_MESSAGES_AVAILABLE = False

from hovermap_ros2_api.ros1_wire import (
    Header,
    Odometry,
    PointCloud2,
    PointField,
    Pose,
    Quaternion,
    RosTime,
    TFMessage,
    TransformStamped,
    Twist,
    Vector3,
    to_ros2_odometry,
    to_ros2_point_cloud2,
    to_ros2_tf_message,
)


@unittest.skipUnless(ROS_MESSAGES_AVAILABLE, "ROS 2 message packages are not installed")
class Ros2ConversionTests(unittest.TestCase):
    def test_mule_adapter_constructs_without_clobbering_rclpy_publishers(self):
        from hovermap_ros2_api.mule_adapter import MuleAdapter

        rclpy.init(args=["--ros-args", "-p", "initial_wait:=60.0"])
        node = MuleAdapter()
        try:
            self.assertIsInstance(node._publishers, list)
            self.assertIsInstance(node._wire_publishers, dict)
        finally:
            node.destroy_node()
            if rclpy.ok():
                rclpy.shutdown()

    def test_string_array_parameter_supports_sentinel_and_nonempty(self):
        from hovermap_ros2_api.mule_adapter import _normalized_unicast_addresses

        for override, expected in (
            (None, ()),
            ([""], ()),
            (["10.9.0.2"], ("10.9.0.2",)),
        ):
            with self.subTest(override=override):
                context = Context()
                rclpy.init(context=context)
                parameter_overrides = []
                if override is not None:
                    parameter_overrides.append(
                        Parameter(
                            "ping_ucast_addrs",
                            Parameter.Type.STRING_ARRAY,
                            override,
                        )
                    )
                node = Node(
                    "string_array_test",
                    context=context,
                    parameter_overrides=parameter_overrides,
                )
                try:
                    node.declare_parameter("ping_ucast_addrs", [""])
                    self.assertEqual(
                        _normalized_unicast_addresses(
                            node.get_parameter("ping_ucast_addrs").value
                        ),
                        expected,
                    )
                finally:
                    node.destroy_node()
                    context.shutdown()

    def test_point_cloud_metadata_and_payload(self):
        value = PointCloud2(
            header=Header(99, RosTime(12, 34), "odom"),
            height=1,
            width=1,
            fields=(PointField("x", 0, PointField.FLOAT32, 1),),
            is_bigendian=False,
            point_step=4,
            row_step=4,
            data=b"\x00\x00\xc0\x3f",
            is_dense=True,
        )
        result = to_ros2_point_cloud2(value)
        self.assertEqual(result.header.stamp.sec, 12)
        self.assertEqual(result.header.stamp.nanosec, 34)
        self.assertEqual(result.header.frame_id, "odom")
        self.assertEqual(result.fields[0].name, "x")
        self.assertEqual(bytes(result.data), value.data)

    def test_odometry_covariance_and_frames(self):
        covariance = tuple(float(index) for index in range(36))
        value = Odometry(
            header=Header(1, RosTime(2, 3), "odom"),
            child_frame_id="hovermap_base",
            pose=Pose(Vector3(1.0, 2.0, 3.0), Quaternion(0.0, 0.0, 0.0, 1.0)),
            pose_covariance=covariance,
            twist=Twist(Vector3(4.0, 5.0, 6.0), Vector3(7.0, 8.0, 9.0)),
            twist_covariance=covariance,
        )
        result = to_ros2_odometry(value)
        self.assertEqual(result.header.frame_id, "odom")
        self.assertEqual(result.child_frame_id, "hovermap_base")
        self.assertEqual(tuple(result.pose.covariance), covariance)
        self.assertEqual(tuple(result.twist.covariance), covariance)

    def test_transform_array(self):
        value = TFMessage(
            (
                TransformStamped(
                    Header(5, RosTime(6, 7), "odom"),
                    "hovermap_base",
                    Vector3(1.0, 2.0, 3.0),
                    Quaternion(0.0, 0.0, 0.0, 1.0),
                ),
            )
        )
        result = to_ros2_tf_message(value)
        self.assertEqual(len(result.transforms), 1)
        self.assertEqual(result.transforms[0].header.frame_id, "odom")
        self.assertEqual(result.transforms[0].child_frame_id, "hovermap_base")
        self.assertEqual(result.transforms[0].transform.translation.z, 3.0)

    def test_bridge_diagnostic_and_overlay_interfaces(self):
        from hovermap_ros2_api.mule_adapter import (
            _float_time_message,
            _manifest_message,
        )
        from hovermap_ros2_msgs.srv import SetOverlay

        manifest = _manifest_message(
            {
                "seq": 3,
                "tails": [
                    {
                        "node_uuid": bytes(range(16)),
                        "log_name": "static_tf",
                        "trunc": True,
                        "index": 42,
                    }
                ],
            }
        )
        self.assertEqual(manifest.seq, 3)
        self.assertEqual(list(manifest.tails[0].node_uuid), list(range(16)))
        self.assertEqual(manifest.tails[0].index, 42)
        timestamp = _float_time_message(10.25)
        self.assertEqual((timestamp.sec, timestamp.nanosec), (10, 250_000_000))
        request = SetOverlay.Request()
        request.node_names = ["hovermap"]
        self.assertEqual(list(request.node_names), ["hovermap"])
        from hovermap_ros2_msgs.msg import BridgeStatus

        status = BridgeStatus()
        status.dropped_event_count = 2
        status.decode_failure_count = 3
        self.assertEqual(status.dropped_event_count, 2)
        self.assertEqual(status.decode_failure_count, 3)


if __name__ == "__main__":
    unittest.main()
