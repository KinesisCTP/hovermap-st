"""Explicit codecs for ROS 1 messages transported as opaque Mule payloads.

Mule does not carry ROS type negotiation. Hovermap publishes ROS 1 wire bytes,
so a native ROS 2 adapter must decode the ROS 1 serialization deliberately and
then populate ROS 2 messages. Keeping the parser independent of ROS makes it
possible to test malformed and golden payloads without either ROS runtime.
"""

from array import array
from dataclasses import dataclass
import struct
from typing import ClassVar, Sequence, Tuple, Union


BytesLike = Union[bytes, bytearray, memoryview]


class WireCodecError(ValueError):
    """Base class for a ROS 1 wire-format error."""


class WireDecodeError(WireCodecError):
    """Raised when a payload is malformed, truncated, or exceeds a limit."""


class WireEncodeError(WireCodecError):
    """Raised when an in-memory value cannot be represented on the wire."""


@dataclass(frozen=True)
class WireLimits:
    """Resource limits applied before allocating from untrusted wire lengths."""

    max_string_bytes: int = 1024 * 1024
    max_blob_bytes: int = 512 * 1024 * 1024
    max_point_fields: int = 4096
    max_transforms: int = 100_000


DEFAULT_LIMITS = WireLimits()


@dataclass(frozen=True)
class RosTime:
    sec: int
    nanosec: int


@dataclass(frozen=True)
class Header:
    seq: int
    stamp: RosTime
    frame_id: str


@dataclass(frozen=True)
class PointField:
    INT8: ClassVar[int] = 1
    UINT8: ClassVar[int] = 2
    INT16: ClassVar[int] = 3
    UINT16: ClassVar[int] = 4
    INT32: ClassVar[int] = 5
    UINT32: ClassVar[int] = 6
    FLOAT32: ClassVar[int] = 7
    FLOAT64: ClassVar[int] = 8

    name: str
    offset: int
    datatype: int
    count: int


@dataclass(frozen=True)
class PointCloud2:
    header: Header
    height: int
    width: int
    fields: Tuple[PointField, ...]
    is_bigendian: bool
    point_step: int
    row_step: int
    data: bytes
    is_dense: bool


@dataclass(frozen=True)
class Vector3:
    x: float
    y: float
    z: float


@dataclass(frozen=True)
class Quaternion:
    x: float
    y: float
    z: float
    w: float


@dataclass(frozen=True)
class Pose:
    position: Vector3
    orientation: Quaternion


@dataclass(frozen=True)
class Twist:
    linear: Vector3
    angular: Vector3


@dataclass(frozen=True)
class Odometry:
    header: Header
    child_frame_id: str
    pose: Pose
    pose_covariance: Tuple[float, ...]
    twist: Twist
    twist_covariance: Tuple[float, ...]


@dataclass(frozen=True)
class TransformStamped:
    header: Header
    child_frame_id: str
    translation: Vector3
    rotation: Quaternion


@dataclass(frozen=True)
class TFMessage:
    transforms: Tuple[TransformStamped, ...]


_POINT_FIELD_WIDTHS = {
    PointField.INT8: 1,
    PointField.UINT8: 1,
    PointField.INT16: 2,
    PointField.UINT16: 2,
    PointField.INT32: 4,
    PointField.UINT32: 4,
    PointField.FLOAT32: 4,
    PointField.FLOAT64: 8,
}


class _Reader:
    def __init__(self, payload: BytesLike, limits: WireLimits) -> None:
        try:
            self._view = memoryview(payload).cast("B")
        except (TypeError, ValueError) as exc:
            raise WireDecodeError("payload must be a contiguous byte buffer") from exc
        self._offset = 0
        self.limits = limits

    def _take(self, size: int, label: str) -> memoryview:
        if size < 0 or size > len(self._view) - self._offset:
            remaining = len(self._view) - self._offset
            raise WireDecodeError(
                f"truncated {label}: need {size} bytes, have {remaining}"
            )
        start = self._offset
        self._offset += size
        return self._view[start : start + size]

    def u8(self, label: str) -> int:
        return self._take(1, label)[0]

    def boolean(self, label: str) -> bool:
        value = self.u8(label)
        if value not in (0, 1):
            raise WireDecodeError(f"invalid ROS bool for {label}: {value}")
        return bool(value)

    def u32(self, label: str) -> int:
        return struct.unpack_from("<I", self._take(4, label))[0]

    def f64(self, label: str) -> float:
        return struct.unpack_from("<d", self._take(8, label))[0]

    def string(self, label: str) -> str:
        size = self.u32(f"{label} length")
        if size > self.limits.max_string_bytes:
            raise WireDecodeError(
                f"{label} length {size} exceeds limit {self.limits.max_string_bytes}"
            )
        raw = self._take(size, label)
        try:
            return raw.tobytes().decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise WireDecodeError(f"{label} is not valid UTF-8") from exc

    def blob(self, label: str) -> bytes:
        size = self.u32(f"{label} length")
        if size > self.limits.max_blob_bytes:
            raise WireDecodeError(
                f"{label} length {size} exceeds limit {self.limits.max_blob_bytes}"
            )
        return self._take(size, label).tobytes()

    def array_count(self, label: str, maximum: int) -> int:
        count = self.u32(f"{label} count")
        if count > maximum:
            raise WireDecodeError(f"{label} count {count} exceeds limit {maximum}")
        return count

    def finish(self) -> None:
        trailing = len(self._view) - self._offset
        if trailing:
            raise WireDecodeError(f"payload has {trailing} trailing bytes")


class _Writer:
    def __init__(self, limits: WireLimits) -> None:
        self._data = bytearray()
        self.limits = limits

    def u8(self, value: int, label: str) -> None:
        _require_uint(value, 8, label)
        self._data.extend(struct.pack("<B", value))

    def boolean(self, value: bool, label: str) -> None:
        if not isinstance(value, bool):
            raise WireEncodeError(f"{label} must be bool")
        self.u8(int(value), label)

    def u32(self, value: int, label: str) -> None:
        _require_uint(value, 32, label)
        self._data.extend(struct.pack("<I", value))

    def f64(self, value: float, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WireEncodeError(f"{label} must be a number")
        self._data.extend(struct.pack("<d", float(value)))

    def string(self, value: str, label: str) -> None:
        if not isinstance(value, str):
            raise WireEncodeError(f"{label} must be str")
        try:
            raw = value.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise WireEncodeError(f"{label} is not valid UTF-8 text") from exc
        if len(raw) > self.limits.max_string_bytes:
            raise WireEncodeError(
                f"{label} length {len(raw)} exceeds limit "
                f"{self.limits.max_string_bytes}"
            )
        self.u32(len(raw), f"{label} length")
        self._data.extend(raw)

    def blob(self, value: BytesLike, label: str) -> None:
        try:
            raw = memoryview(value).cast("B")
        except (TypeError, ValueError) as exc:
            raise WireEncodeError(f"{label} must be a contiguous byte buffer") from exc
        if len(raw) > self.limits.max_blob_bytes:
            raise WireEncodeError(
                f"{label} length {len(raw)} exceeds limit {self.limits.max_blob_bytes}"
            )
        self.u32(len(raw), f"{label} length")
        self._data.extend(raw)

    def build(self) -> bytes:
        return bytes(self._data)


def _require_uint(value: int, bits: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WireEncodeError(f"{label} must be an unsigned {bits}-bit integer")
    if value < 0 or value > (1 << bits) - 1:
        raise WireEncodeError(f"{label} is outside unsigned {bits}-bit range")


def _decode_header(reader: _Reader) -> Header:
    seq = reader.u32("header.seq")
    sec = reader.u32("header.stamp.sec")
    nanosec = reader.u32("header.stamp.nanosec")
    if nanosec >= 1_000_000_000:
        raise WireDecodeError("header.stamp.nanosec must be less than 1,000,000,000")
    return Header(seq, RosTime(sec, nanosec), reader.string("header.frame_id"))


def _encode_header(writer: _Writer, value: Header) -> None:
    if not isinstance(value, Header):
        raise WireEncodeError("header must be Header")
    writer.u32(value.seq, "header.seq")
    writer.u32(value.stamp.sec, "header.stamp.sec")
    if value.stamp.nanosec >= 1_000_000_000:
        raise WireEncodeError("header.stamp.nanosec must be less than 1,000,000,000")
    writer.u32(value.stamp.nanosec, "header.stamp.nanosec")
    writer.string(value.frame_id, "header.frame_id")


def _decode_vector3(reader: _Reader, label: str) -> Vector3:
    return Vector3(
        reader.f64(f"{label}.x"),
        reader.f64(f"{label}.y"),
        reader.f64(f"{label}.z"),
    )


def _encode_vector3(writer: _Writer, value: Vector3, label: str) -> None:
    writer.f64(value.x, f"{label}.x")
    writer.f64(value.y, f"{label}.y")
    writer.f64(value.z, f"{label}.z")


def _decode_quaternion(reader: _Reader, label: str) -> Quaternion:
    return Quaternion(
        reader.f64(f"{label}.x"),
        reader.f64(f"{label}.y"),
        reader.f64(f"{label}.z"),
        reader.f64(f"{label}.w"),
    )


def _encode_quaternion(writer: _Writer, value: Quaternion, label: str) -> None:
    writer.f64(value.x, f"{label}.x")
    writer.f64(value.y, f"{label}.y")
    writer.f64(value.z, f"{label}.z")
    writer.f64(value.w, f"{label}.w")


def _decode_covariance(reader: _Reader, label: str) -> Tuple[float, ...]:
    return tuple(reader.f64(f"{label}[{index}]") for index in range(36))


def _encode_covariance(
    writer: _Writer, values: Sequence[float], label: str
) -> None:
    if len(values) != 36:
        raise WireEncodeError(f"{label} must contain exactly 36 values")
    for index, value in enumerate(values):
        writer.f64(value, f"{label}[{index}]")


def _validate_point_cloud(value: PointCloud2, *, decoding: bool = False) -> None:
    error_type = WireDecodeError if decoding else WireEncodeError
    try:
        _require_uint(value.height, 32, "height")
        _require_uint(value.width, 32, "width")
        _require_uint(value.point_step, 32, "point_step")
        _require_uint(value.row_step, 32, "row_step")
    except WireEncodeError as exc:
        raise error_type(str(exc)) from exc
    minimum_row_step = value.point_step * value.width
    if value.row_step < minimum_row_step:
        raise error_type(
            f"row_step {value.row_step} is smaller than point_step * width "
            f"({minimum_row_step})"
        )
    expected_data = value.row_step * value.height
    if len(value.data) != expected_data:
        raise error_type(
            f"data length {len(value.data)} does not equal row_step * height "
            f"({expected_data})"
        )
    for field in value.fields:
        width = _POINT_FIELD_WIDTHS.get(field.datatype)
        if width is None:
            raise error_type(f"field {field.name!r} has invalid datatype {field.datatype}")
        if field.offset < 0 or field.count < 0:
            raise error_type(f"field {field.name!r} has a negative offset or count")
        if field.offset + width * field.count > value.point_step:
            raise error_type(f"field {field.name!r} extends beyond point_step")


def decode_ros1_string(
    payload: BytesLike, limits: WireLimits = DEFAULT_LIMITS
) -> str:
    """Decode a ROS 1 ``std_msgs/String`` serialized payload."""

    reader = _Reader(payload, limits)
    value = reader.string("data")
    reader.finish()
    return value


def encode_ros1_string(value: str, limits: WireLimits = DEFAULT_LIMITS) -> bytes:
    """Encode text as a ROS 1 ``std_msgs/String`` serialized payload."""

    writer = _Writer(limits)
    writer.string(value, "data")
    return writer.build()


def decode_ros1_point_cloud2(
    payload: BytesLike, limits: WireLimits = DEFAULT_LIMITS
) -> PointCloud2:
    """Decode a ROS 1 ``sensor_msgs/PointCloud2`` serialized payload."""

    reader = _Reader(payload, limits)
    header = _decode_header(reader)
    height = reader.u32("height")
    width = reader.u32("width")
    field_count = reader.array_count("fields", limits.max_point_fields)
    fields = []
    for index in range(field_count):
        fields.append(
            PointField(
                name=reader.string(f"fields[{index}].name"),
                offset=reader.u32(f"fields[{index}].offset"),
                datatype=reader.u8(f"fields[{index}].datatype"),
                count=reader.u32(f"fields[{index}].count"),
            )
        )
    result = PointCloud2(
        header=header,
        height=height,
        width=width,
        fields=tuple(fields),
        is_bigendian=reader.boolean("is_bigendian"),
        point_step=reader.u32("point_step"),
        row_step=reader.u32("row_step"),
        data=reader.blob("data"),
        is_dense=reader.boolean("is_dense"),
    )
    reader.finish()
    _validate_point_cloud(result, decoding=True)
    return result


def encode_ros1_point_cloud2(
    value: PointCloud2, limits: WireLimits = DEFAULT_LIMITS
) -> bytes:
    """Encode a value as ROS 1 ``sensor_msgs/PointCloud2`` bytes."""

    if not isinstance(value, PointCloud2):
        raise WireEncodeError("value must be PointCloud2")
    if len(value.fields) > limits.max_point_fields:
        raise WireEncodeError("fields exceeds configured count limit")
    _validate_point_cloud(value)
    writer = _Writer(limits)
    _encode_header(writer, value.header)
    writer.u32(value.height, "height")
    writer.u32(value.width, "width")
    writer.u32(len(value.fields), "fields count")
    for index, field in enumerate(value.fields):
        writer.string(field.name, f"fields[{index}].name")
        writer.u32(field.offset, f"fields[{index}].offset")
        writer.u8(field.datatype, f"fields[{index}].datatype")
        writer.u32(field.count, f"fields[{index}].count")
    writer.boolean(value.is_bigendian, "is_bigendian")
    writer.u32(value.point_step, "point_step")
    writer.u32(value.row_step, "row_step")
    writer.blob(value.data, "data")
    writer.boolean(value.is_dense, "is_dense")
    return writer.build()


def decode_ros1_odometry(
    payload: BytesLike, limits: WireLimits = DEFAULT_LIMITS
) -> Odometry:
    """Decode a ROS 1 ``nav_msgs/Odometry`` serialized payload."""

    reader = _Reader(payload, limits)
    result = Odometry(
        header=_decode_header(reader),
        child_frame_id=reader.string("child_frame_id"),
        pose=Pose(
            position=_decode_vector3(reader, "pose.pose.position"),
            orientation=_decode_quaternion(reader, "pose.pose.orientation"),
        ),
        pose_covariance=_decode_covariance(reader, "pose.covariance"),
        twist=Twist(
            linear=_decode_vector3(reader, "twist.twist.linear"),
            angular=_decode_vector3(reader, "twist.twist.angular"),
        ),
        twist_covariance=_decode_covariance(reader, "twist.covariance"),
    )
    reader.finish()
    return result


def encode_ros1_odometry(
    value: Odometry, limits: WireLimits = DEFAULT_LIMITS
) -> bytes:
    """Encode a value as ROS 1 ``nav_msgs/Odometry`` bytes."""

    if not isinstance(value, Odometry):
        raise WireEncodeError("value must be Odometry")
    writer = _Writer(limits)
    _encode_header(writer, value.header)
    writer.string(value.child_frame_id, "child_frame_id")
    _encode_vector3(writer, value.pose.position, "pose.pose.position")
    _encode_quaternion(writer, value.pose.orientation, "pose.pose.orientation")
    _encode_covariance(writer, value.pose_covariance, "pose.covariance")
    _encode_vector3(writer, value.twist.linear, "twist.twist.linear")
    _encode_vector3(writer, value.twist.angular, "twist.twist.angular")
    _encode_covariance(writer, value.twist_covariance, "twist.covariance")
    return writer.build()


def decode_ros1_tf_message(
    payload: BytesLike, limits: WireLimits = DEFAULT_LIMITS
) -> TFMessage:
    """Decode a ROS 1 ``tf2_msgs/TFMessage`` serialized payload."""

    reader = _Reader(payload, limits)
    count = reader.array_count("transforms", limits.max_transforms)
    transforms = []
    for index in range(count):
        label = f"transforms[{index}]"
        transforms.append(
            TransformStamped(
                header=_decode_header(reader),
                child_frame_id=reader.string(f"{label}.child_frame_id"),
                translation=_decode_vector3(reader, f"{label}.transform.translation"),
                rotation=_decode_quaternion(reader, f"{label}.transform.rotation"),
            )
        )
    reader.finish()
    return TFMessage(tuple(transforms))


def encode_ros1_tf_message(
    value: TFMessage, limits: WireLimits = DEFAULT_LIMITS
) -> bytes:
    """Encode a value as ROS 1 ``tf2_msgs/TFMessage`` bytes."""

    if not isinstance(value, TFMessage):
        raise WireEncodeError("value must be TFMessage")
    if len(value.transforms) > limits.max_transforms:
        raise WireEncodeError("transforms exceeds configured count limit")
    writer = _Writer(limits)
    writer.u32(len(value.transforms), "transforms count")
    for index, transform in enumerate(value.transforms):
        label = f"transforms[{index}]"
        _encode_header(writer, transform.header)
        writer.string(transform.child_frame_id, f"{label}.child_frame_id")
        _encode_vector3(writer, transform.translation, f"{label}.transform.translation")
        _encode_quaternion(writer, transform.rotation, f"{label}.transform.rotation")
    return writer.build()


def to_ros2_point_cloud2(value: PointCloud2):
    """Create ``sensor_msgs.msg.PointCloud2`` without importing ROS at module load."""

    from sensor_msgs.msg import PointCloud2 as RosPointCloud2
    from sensor_msgs.msg import PointField as RosPointField

    result = RosPointCloud2()
    _copy_header_to_ros2(value.header, result.header)
    result.height = value.height
    result.width = value.width
    result.fields = []
    for field in value.fields:
        ros_field = RosPointField()
        ros_field.name = field.name
        ros_field.offset = field.offset
        ros_field.datatype = field.datatype
        ros_field.count = field.count
        result.fields.append(ros_field)
    result.is_bigendian = value.is_bigendian
    result.point_step = value.point_step
    result.row_step = value.row_step
    result.data = array("B", value.data)
    result.is_dense = value.is_dense
    return result


def to_ros2_odometry(value: Odometry):
    """Create ``nav_msgs.msg.Odometry`` from decoded ROS 1 data."""

    from nav_msgs.msg import Odometry as RosOdometry

    result = RosOdometry()
    _copy_header_to_ros2(value.header, result.header)
    result.child_frame_id = value.child_frame_id
    _copy_vector3(value.pose.position, result.pose.pose.position)
    _copy_quaternion(value.pose.orientation, result.pose.pose.orientation)
    result.pose.covariance = list(value.pose_covariance)
    _copy_vector3(value.twist.linear, result.twist.twist.linear)
    _copy_vector3(value.twist.angular, result.twist.twist.angular)
    result.twist.covariance = list(value.twist_covariance)
    return result


def to_ros2_tf_message(value: TFMessage):
    """Create ``tf2_msgs.msg.TFMessage`` from decoded ROS 1 data."""

    from geometry_msgs.msg import TransformStamped as RosTransformStamped
    from tf2_msgs.msg import TFMessage as RosTFMessage

    result = RosTFMessage()
    transforms = []
    for value_transform in value.transforms:
        transform = RosTransformStamped()
        _copy_header_to_ros2(value_transform.header, transform.header)
        transform.child_frame_id = value_transform.child_frame_id
        _copy_vector3(value_transform.translation, transform.transform.translation)
        _copy_quaternion(value_transform.rotation, transform.transform.rotation)
        transforms.append(transform)
    result.transforms = transforms
    return result


def _copy_header_to_ros2(source: Header, target) -> None:
    # ROS 2 intentionally removed Header.seq; timestamp and frame remain exact.
    target.stamp.sec = source.stamp.sec
    target.stamp.nanosec = source.stamp.nanosec
    target.frame_id = source.frame_id


def _copy_vector3(source: Vector3, target) -> None:
    target.x = source.x
    target.y = source.y
    target.z = source.z


def _copy_quaternion(source: Quaternion, target) -> None:
    target.x = source.x
    target.y = source.y
    target.z = source.z
    target.w = source.w
