"""Golden and malformed-buffer tests for ROS 1 serialization codecs."""

import struct
import unittest

from hovermap_ros2_api.ros1_wire import (
    Header,
    Odometry,
    PointCloud2,
    PointField,
    Quaternion,
    RosTime,
    TFMessage,
    TransformStamped,
    Vector3,
    WireDecodeError,
    WireEncodeError,
    WireLimits,
    decode_ros1_odometry,
    decode_ros1_point_cloud2,
    decode_ros1_string,
    decode_ros1_tf_message,
    encode_ros1_odometry,
    encode_ros1_point_cloud2,
    encode_ros1_string,
    encode_ros1_tf_message,
)


def _golden_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _golden_header(seq: int, sec: int, nanosec: int, frame_id: str) -> bytes:
    return struct.pack("<III", seq, sec, nanosec) + _golden_string(frame_id)


# This fixture is deliberately assembled from the ROS 1 field specification,
# independently of the production encoder.
GOLDEN_POINT_CLOUD = b"".join(
    (
        _golden_header(7, 12, 34, "odom"),
        struct.pack("<II", 1, 1),
        struct.pack("<I", 1),
        _golden_string("x"),
        struct.pack("<IBI", 0, PointField.FLOAT32, 1),
        struct.pack("<BII", 0, 4, 4),
        struct.pack("<I", 4),
        struct.pack("<f", 1.5),
        struct.pack("<B", 1),
    )
)


POSE_COVARIANCE = tuple(float(index) for index in range(36))
TWIST_COVARIANCE = tuple(float(-index) for index in range(36))
GOLDEN_ODOMETRY = b"".join(
    (
        _golden_header(9, 100, 200, "odom"),
        _golden_string("hovermap_base"),
        struct.pack("<3d", 1.0, 2.0, 3.0),
        struct.pack("<4d", 0.1, 0.2, 0.3, 0.9),
        struct.pack("<36d", *POSE_COVARIANCE),
        struct.pack("<3d", -1.0, -2.0, -3.0),
        struct.pack("<3d", 0.4, 0.5, 0.6),
        struct.pack("<36d", *TWIST_COVARIANCE),
    )
)


GOLDEN_TF = b"".join(
    (
        struct.pack("<I", 1),
        _golden_header(4, 5, 6, "odom"),
        _golden_string("hovermap_base"),
        struct.pack("<3d", 10.0, 20.0, 30.0),
        struct.pack("<4d", 0.0, 0.0, 0.0, 1.0),
    )
)


class StringCodecTests(unittest.TestCase):
    def test_golden_ascii(self):
        golden = bytes.fromhex("030000006d6170")
        self.assertEqual(decode_ros1_string(golden), "map")
        self.assertEqual(encode_ros1_string("map"), golden)

    def test_utf8_length_is_bytes(self):
        golden = bytes.fromhex("05000000636166c3a9")
        self.assertEqual(decode_ros1_string(golden), "café")
        self.assertEqual(encode_ros1_string("café"), golden)

    def test_rejects_trailing_and_invalid_utf8(self):
        with self.assertRaises(WireDecodeError):
            decode_ros1_string(bytes.fromhex("010000006100"))
        with self.assertRaises(WireDecodeError):
            decode_ros1_string(bytes.fromhex("01000000ff"))

    def test_rejects_declared_oversize_before_allocation(self):
        limits = WireLimits(max_string_bytes=3)
        with self.assertRaises(WireDecodeError):
            decode_ros1_string(struct.pack("<I", 4), limits)


class PointCloudCodecTests(unittest.TestCase):
    def test_decodes_and_reencodes_golden_fixture(self):
        value = decode_ros1_point_cloud2(GOLDEN_POINT_CLOUD)
        self.assertEqual(value.header, Header(7, RosTime(12, 34), "odom"))
        self.assertEqual(value.height, 1)
        self.assertEqual(value.width, 1)
        self.assertEqual(value.fields, (PointField("x", 0, 7, 1),))
        self.assertEqual(value.data, struct.pack("<f", 1.5))
        self.assertTrue(value.is_dense)
        self.assertEqual(encode_ros1_point_cloud2(value), GOLDEN_POINT_CLOUD)

    def test_rejects_truncation_invalid_bool_and_trailing_bytes(self):
        with self.assertRaises(WireDecodeError):
            decode_ros1_point_cloud2(GOLDEN_POINT_CLOUD[:-1])
        with self.assertRaises(WireDecodeError):
            decode_ros1_point_cloud2(GOLDEN_POINT_CLOUD[:-1] + b"\x02")
        with self.assertRaises(WireDecodeError):
            decode_ros1_point_cloud2(GOLDEN_POINT_CLOUD + b"\x00")

    def test_rejects_out_of_range_nanoseconds(self):
        invalid = bytearray(GOLDEN_POINT_CLOUD)
        invalid[8:12] = struct.pack("<I", 1_000_000_000)
        with self.assertRaises(WireDecodeError):
            decode_ros1_point_cloud2(invalid)

    def test_rejects_inconsistent_dimensions_on_encode(self):
        value = decode_ros1_point_cloud2(GOLDEN_POINT_CLOUD)
        invalid = PointCloud2(
            header=value.header,
            height=2,
            width=value.width,
            fields=value.fields,
            is_bigendian=value.is_bigendian,
            point_step=value.point_step,
            row_step=value.row_step,
            data=value.data,
            is_dense=value.is_dense,
        )
        with self.assertRaises(WireEncodeError):
            encode_ros1_point_cloud2(invalid)


class OdometryCodecTests(unittest.TestCase):
    def test_decodes_and_reencodes_golden_fixture(self):
        value = decode_ros1_odometry(GOLDEN_ODOMETRY)
        self.assertEqual(value.header, Header(9, RosTime(100, 200), "odom"))
        self.assertEqual(value.child_frame_id, "hovermap_base")
        self.assertEqual(value.pose.position, Vector3(1.0, 2.0, 3.0))
        self.assertEqual(value.pose.orientation, Quaternion(0.1, 0.2, 0.3, 0.9))
        self.assertEqual(value.pose_covariance, POSE_COVARIANCE)
        self.assertEqual(value.twist.linear, Vector3(-1.0, -2.0, -3.0))
        self.assertEqual(value.twist.angular, Vector3(0.4, 0.5, 0.6))
        self.assertEqual(value.twist_covariance, TWIST_COVARIANCE)
        self.assertEqual(encode_ros1_odometry(value), GOLDEN_ODOMETRY)

    def test_rejects_truncation_and_bad_covariance_length(self):
        with self.assertRaises(WireDecodeError):
            decode_ros1_odometry(GOLDEN_ODOMETRY[:-8])
        value = decode_ros1_odometry(GOLDEN_ODOMETRY)
        invalid = Odometry(
            header=value.header,
            child_frame_id=value.child_frame_id,
            pose=value.pose,
            pose_covariance=(0.0,),
            twist=value.twist,
            twist_covariance=value.twist_covariance,
        )
        with self.assertRaises(WireEncodeError):
            encode_ros1_odometry(invalid)


class TfCodecTests(unittest.TestCase):
    def test_decodes_and_reencodes_golden_fixture(self):
        value = decode_ros1_tf_message(GOLDEN_TF)
        self.assertEqual(len(value.transforms), 1)
        transform = value.transforms[0]
        self.assertEqual(transform.header, Header(4, RosTime(5, 6), "odom"))
        self.assertEqual(transform.child_frame_id, "hovermap_base")
        self.assertEqual(transform.translation, Vector3(10.0, 20.0, 30.0))
        self.assertEqual(transform.rotation, Quaternion(0.0, 0.0, 0.0, 1.0))
        self.assertEqual(encode_ros1_tf_message(value), GOLDEN_TF)

    def test_empty_array_and_count_limit(self):
        self.assertEqual(decode_ros1_tf_message(b"\x00\x00\x00\x00"), TFMessage(()))
        with self.assertRaises(WireDecodeError):
            decode_ros1_tf_message(struct.pack("<I", 2), WireLimits(max_transforms=1))

    def test_encode_model_is_explicit(self):
        value = TFMessage(
            (
                TransformStamped(
                    header=Header(0, RosTime(1, 2), "a"),
                    child_frame_id="b",
                    translation=Vector3(0.0, 0.0, 0.0),
                    rotation=Quaternion(0.0, 0.0, 0.0, 1.0),
                ),
            )
        )
        self.assertEqual(decode_ros1_tf_message(encode_ros1_tf_message(value)), value)


if __name__ == "__main__":
    unittest.main()
