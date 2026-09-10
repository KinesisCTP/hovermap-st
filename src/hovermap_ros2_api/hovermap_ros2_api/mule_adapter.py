"""rclpy adapter around the separately installed ROS-agnostic Mule core.

The Hovermap side still emits ROS 1 serialized buffers. This module keeps all
Mule I/O on its Tornado thread, transfers complete buffers through a bounded
queue, decodes them explicitly, and publishes native ROS 2 messages.
"""

from dataclasses import dataclass, fields
import logging
import math
import queue
import threading
import time
import uuid
from typing import Dict, Mapping

from builtin_interfaces.msg import Time as TimeMessage
from nav_msgs.msg import Odometry
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import String
from tf2_msgs.msg import TFMessage

from hovermap_ros2_msgs.msg import (
    BridgeStatus,
    LogIndex,
    Manifest,
    PeerStatus,
    TopicStatus,
)
from hovermap_ros2_msgs.srv import SetOverlay

from .event_buffer import DropOldestBuffer
from .latest_payload import LatestPayloadMailbox
from .mule_config import (
    MuleSettings,
    PERSISTENT_PUBLICATIONS,
    ROS1_TYPE_INFO,
    VOLATILE_PUBLICATIONS,
    WIRE_CONFIGURATION,
    WIRE_LIDAR,
    WIRE_OCCUPANCY,
    WIRE_ODOMETRY,
    WIRE_TF,
    WIRE_TF_STATIC,
    build_core_config,
    load_mule_core,
    validate_mule_settings,
)
from .ros1_wire import (
    WireDecodeError,
    decode_ros1_odometry,
    decode_ros1_point_cloud2,
    decode_ros1_tf_message,
    encode_ros1_string,
    to_ros2_odometry,
    to_ros2_point_cloud2,
    to_ros2_tf_message,
)
from .static_tf import StaticTransformCache


def _event_payload_size(event) -> int:
    return len(event[3]) if event[0] == "wire_message" else 0


def _normalized_unicast_addresses(values) -> tuple:
    """Map the Jazzy string-array sentinel to no unicast addresses."""
    return tuple(value for value in values if value)


@dataclass
class TopicCounters:
    volatile_sub_msg_count: int = 0
    volatile_sub_raw_size: int = 0
    volatile_sub_bridged_size: int = 0
    volatile_pub_msg_count: int = 0
    volatile_pub_raw_size: int = 0
    volatile_pub_bridged_size: int = 0
    volatile_pub_drop_count: int = 0
    persistent_sub_msg_count: int = 0
    persistent_sub_raw_size: int = 0
    persistent_sub_bridged_size: int = 0
    persistent_pub_msg_count: int = 0
    persistent_pub_raw_size: int = 0
    persistent_pub_bridged_size: int = 0
    persistent_pub_drop_count: int = 0


class _MuleLogHandler(logging.Handler):
    """Move Mule/Tornado log records onto the ROS executor thread."""

    def __init__(self, enqueue) -> None:
        super().__init__()
        self._enqueue = enqueue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._enqueue(("transport_log", record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


class MuleAdapter(Node):
    """Convert fixed Hovermap ROS 1 wire topics into native ROS 2 topics."""

    def __init__(self) -> None:
        super().__init__("mule_adapter")
        self._declare_parameters()
        self._settings = self._read_settings()
        event_queue_depth = int(self.get_parameter("event_queue_depth").value)
        event_queue_bytes = int(self.get_parameter("event_queue_bytes").value)
        self._event_queue = DropOldestBuffer(
            max_items=event_queue_depth,
            max_bytes=event_queue_bytes,
            size_of=_event_payload_size,
            on_drop=self._record_dropped_event,
        )
        # Lifecycle transitions must not be lost behind high-rate sensor data.
        self._lifecycle_event_queue = queue.SimpleQueue()
        self._transport_error_queue = queue.SimpleQueue()
        self._persistent_event_queue = queue.SimpleQueue()
        self._shutdown_on_transport_error = bool(
            self.get_parameter("shutdown_on_transport_error").value
        )
        self._transport_loop = None
        self._transport_client = None
        self._transport_ready = threading.Event()
        self._transport_stop_requested = threading.Event()
        self._node_uuid = uuid.uuid4().bytes
        self._stats_lock = threading.Lock()
        self._topic_counters: Dict[str, TopicCounters] = {
            topic: TopicCounters() for topic in ROS1_TYPE_INFO
        }
        self._dropped_events = 0
        self._reported_dropped_events = 0
        self._decode_failures = 0
        self._configuration_mailbox = LatestPayloadMailbox()
        self._configuration_retry_count = 0
        self._configuration_retry_limit = int(
            self.get_parameter("configuration_retry_limit").value
        )
        self._configuration_retry_delay = float(
            self.get_parameter("configuration_retry_delay").value
        )
        if self._configuration_retry_limit < 0 or self._configuration_retry_limit > 10:
            raise ValueError("configuration_retry_limit must be in 0..10")
        if self._configuration_retry_delay <= 0 or self._configuration_retry_delay > 30:
            raise ValueError("configuration_retry_delay must be in (0, 30]")
        self._static_tf_cache = StaticTransformCache()
        self._drain_max_events = int(
            self.get_parameter("drain_max_events").value
        )
        self._drain_time_budget = (
            float(self.get_parameter("drain_time_budget_ms").value) / 1000.0
        )
        if self._drain_max_events < 1 or self._drain_max_events > 1024:
            raise ValueError("drain_max_events must be in 1..1024")
        if self._drain_time_budget <= 0 or self._drain_time_budget > 0.1:
            raise ValueError("drain_time_budget_ms must be in (0, 100]")
        drain_callback_group = MutuallyExclusiveCallbackGroup()
        status_callback_group = MutuallyExclusiveCallbackGroup()
        control_callback_group = MutuallyExclusiveCallbackGroup()

        sensor_reliability_name = self.get_parameter(
            "sensor_reliability"
        ).value
        if sensor_reliability_name == "best_effort":
            sensor_reliability = ReliabilityPolicy.BEST_EFFORT
        elif sensor_reliability_name == "reliable":
            sensor_reliability = ReliabilityPolicy.RELIABLE
        else:
            raise ValueError(
                "sensor_reliability must be 'best_effort' or 'reliable'"
            )
        sensor_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=5,
            reliability=sensor_reliability,
            durability=DurabilityPolicy.VOLATILE,
        )
        tf_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=100,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )
        static_tf_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        command_qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
        )

        self._publishers = {
            WIRE_LIDAR: [
                self.create_publisher(
                    PointCloud2, self.get_parameter("lidar_topic").value, sensor_qos
                )
            ],
            WIRE_OCCUPANCY: [
                self.create_publisher(
                    PointCloud2, self.get_parameter("occupancy_topic").value, sensor_qos
                )
            ],
            WIRE_ODOMETRY: [
                self.create_publisher(
                    Odometry, self.get_parameter("odometry_topic").value, sensor_qos
                )
            ],
            WIRE_TF: [
                self.create_publisher(
                    TFMessage, self.get_parameter("tf_topic").value, tf_qos
                )
            ],
            WIRE_TF_STATIC: [
                self.create_publisher(
                    TFMessage,
                    self.get_parameter("tf_static_topic").value,
                    static_tf_qos,
                )
            ],
        }
        if bool(self.get_parameter("publish_standard_tf").value):
            self._append_distinct_tf_publisher(
                WIRE_TF,
                self.get_parameter("standard_tf_topic").value,
                tf_qos,
            )
            self._append_distinct_tf_publisher(
                WIRE_TF_STATIC,
                self.get_parameter("standard_tf_static_topic").value,
                static_tf_qos,
            )
        self._status_pub = self.create_publisher(
            BridgeStatus,
            self.get_parameter("bridge_status_topic").value,
            QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE),
        )
        self._configuration_sub = self.create_subscription(
            String,
            self.get_parameter("configuration_topic").value,
            self._on_configuration,
            command_qos,
            callback_group=control_callback_group,
        )
        self._overlay_service = self.create_service(
            SetOverlay,
            self.get_parameter("set_overlay_service").value,
            self._on_set_overlay,
            callback_group=control_callback_group,
        )

        self.create_timer(
            0.005,
            self._drain_transport_events,
            callback_group=drain_callback_group,
        )
        self.create_timer(
            self._settings.status_period,
            self._request_status_snapshot,
            callback_group=status_callback_group,
        )
        self._transport_thread = threading.Thread(
            target=self._transport_main,
            name="hovermap-mule",
            daemon=True,
        )
        self._transport_thread.start()
        self.get_logger().info(
            "Starting Mule transport; fixed wire namespace is /cortex and ROS 2 outputs "
            "may be remapped independently"
        )

    def _declare_parameters(self) -> None:
        defaults = MuleSettings()
        self.declare_parameter("swarm_name", defaults.swarm_name)
        self.declare_parameter("node_name", defaults.node_name)
        self.declare_parameter("ip_prefix", defaults.ip_prefix)
        self.declare_parameter("ip_netmask", defaults.ip_netmask)
        self.declare_parameter("ping_mcast_group", defaults.ping_mcast_group)
        # Jazzy infers [] as a byte array even when a string-array type is
        # requested. A one-element empty-string default fixes the static type;
        # _read_settings normalizes the sentinel back to an empty tuple.
        self.declare_parameter("ping_ucast_addrs", [""])
        self.declare_parameter("ping_port", defaults.ping_port)
        self.declare_parameter("min_port", defaults.min_port)
        self.declare_parameter("max_port", defaults.max_port)
        self.declare_parameter("initial_wait", defaults.initial_wait)
        self.declare_parameter("status_period", defaults.status_period)
        self.declare_parameter("compression_level", defaults.compression_level)
        self.declare_parameter("event_queue_depth", 512)
        self.declare_parameter("event_queue_bytes", 256 * 1024 * 1024)
        self.declare_parameter("drain_max_events", 32)
        self.declare_parameter("drain_time_budget_ms", 5.0)
        self.declare_parameter("configuration_retry_limit", 3)
        self.declare_parameter("configuration_retry_delay", 1.0)
        self.declare_parameter("shutdown_on_transport_error", True)
        self.declare_parameter("sensor_reliability", "best_effort")
        self.declare_parameter("lidar_topic", WIRE_LIDAR)
        self.declare_parameter("occupancy_topic", WIRE_OCCUPANCY)
        self.declare_parameter("odometry_topic", WIRE_ODOMETRY)
        self.declare_parameter("tf_topic", WIRE_TF)
        self.declare_parameter("tf_static_topic", WIRE_TF_STATIC)
        self.declare_parameter("publish_standard_tf", True)
        self.declare_parameter("standard_tf_topic", "/tf")
        self.declare_parameter("standard_tf_static_topic", "/tf_static")
        self.declare_parameter("configuration_topic", WIRE_CONFIGURATION)
        self.declare_parameter("bridge_status_topic", "/cortex/mule_bridge/status")
        self.declare_parameter(
            "set_overlay_service", "/cortex/mule_bridge/set_overlay"
        )

    def _read_settings(self) -> MuleSettings:
        ping_ucast_addrs = _normalized_unicast_addresses(
            self.get_parameter("ping_ucast_addrs").value
        )
        settings = MuleSettings(
            swarm_name=self.get_parameter("swarm_name").value,
            node_name=self.get_parameter("node_name").value,
            ip_prefix=self.get_parameter("ip_prefix").value,
            ip_netmask=self.get_parameter("ip_netmask").value,
            ping_mcast_group=self.get_parameter("ping_mcast_group").value,
            ping_ucast_addrs=ping_ucast_addrs,
            ping_port=int(self.get_parameter("ping_port").value),
            min_port=int(self.get_parameter("min_port").value),
            max_port=int(self.get_parameter("max_port").value),
            initial_wait=float(self.get_parameter("initial_wait").value),
            status_period=float(self.get_parameter("status_period").value),
            compression_level=int(self.get_parameter("compression_level").value),
        )
        validate_mule_settings(settings)
        depth = int(self.get_parameter("event_queue_depth").value)
        if depth < 128:
            raise ValueError("event_queue_depth must be at least 128")
        byte_limit = int(self.get_parameter("event_queue_bytes").value)
        if byte_limit < 16 * 1024 * 1024:
            raise ValueError("event_queue_bytes must be at least 16 MiB")
        return settings

    def _append_distinct_tf_publisher(
        self, wire_topic: str, output_topic: str, qos: QoSProfile
    ) -> None:
        compatibility_topic = (
            self.get_parameter("tf_topic").value
            if wire_topic == WIRE_TF
            else self.get_parameter("tf_static_topic").value
        )
        if output_topic != compatibility_topic:
            self._publishers[wire_topic].append(
                self.create_publisher(TFMessage, output_topic, qos)
            )

    def _transport_main(self) -> None:
        loop = None
        log_handler = _MuleLogHandler(self._enqueue_event)
        log_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        transport_loggers = [logging.getLogger(name) for name in ("mule_bridge", "tornado")]
        previous_logger_state = [
            (logger.level, logger.propagate) for logger in transport_loggers
        ]
        for logger in transport_loggers:
            logger.addHandler(log_handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        try:
            if self._transport_stop_requested.wait(self._settings.initial_wait):
                return
            core = load_mule_core()
            import zmq
            from tornado.ioloop import IOLoop

            loop = IOLoop()
            loop.make_current()
            config = build_core_config(core, self._settings)
            client = core.Client(
                time.time,
                config,
                loop,
                zmq.Context.instance(),
                self._node_uuid,
                list(VOLATILE_PUBLICATIONS),
                list(PERSISTENT_PUBLICATIONS),
            )
            client.on_volatile(
                lambda args: self._enqueue_transport_message("volatile", args)
            )
            client.on_persistent(
                lambda args: self._enqueue_transport_message("persistent", args)
            )
            self._transport_loop = loop
            self._transport_client = client
            self._transport_ready.set()
            if self._configuration_mailbox.arm():
                loop.add_callback(self._drain_configuration_mailbox)
            self._enqueue_event(("transport_ready",))
            loop.start()
        except Exception as exc:
            self._enqueue_event(("transport_stopped", repr(exc)))
        finally:
            self._transport_ready.clear()
            self._transport_client = None
            self._transport_loop = None
            for logger, (level, propagate) in zip(
                transport_loggers, previous_logger_state
            ):
                logger.removeHandler(log_handler)
                logger.setLevel(level)
                logger.propagate = propagate
            if loop is not None:
                try:
                    loop.close(all_fds=True)
                except Exception as exc:
                    self._enqueue_event(("transport_error", f"loop close failed: {exc!r}"))

    def _enqueue_transport_message(self, direction: str, args) -> None:
        self._enqueue_event(
            (
                "wire_message",
                direction,
                args.topic_name,
                bytes(args.msg_buff),
                int(args.bridged_size),
            )
        )

    def _enqueue_event(self, event) -> None:
        if event[0] in ("transport_ready", "transport_stopped"):
            self._lifecycle_event_queue.put(event)
            return
        if event[0] == "transport_error":
            self._transport_error_queue.put(event)
            return
        if event[0] == "wire_message" and event[1] == "persistent":
            self._persistent_event_queue.put(event)
            return
        self._event_queue.put(event)

    def _record_dropped_event(self, event) -> None:
        with self._stats_lock:
            self._dropped_events += 1
            if event[0] == "wire_message":
                direction, topic_name = event[1], event[2]
                prefix = (
                    "volatile_pub" if direction == "volatile" else "persistent_pub"
                )
                counters = self._topic_counters.get(topic_name)
                if counters is not None:
                    name = f"{prefix}_drop_count"
                    setattr(counters, name, getattr(counters, name) + 1)

    def _drain_transport_events(self) -> None:
        for _ in range(16):
            try:
                event = self._lifecycle_event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_transport_event(event)

        for _ in range(32):
            try:
                event = self._transport_error_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_transport_event(event)

        deadline = time.monotonic() + self._drain_time_budget
        for _ in range(self._drain_max_events):
            try:
                event = self._persistent_event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_transport_event(event)
            if time.monotonic() >= deadline:
                break

        for _ in range(self._drain_max_events):
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_transport_event(event)
            if time.monotonic() >= deadline:
                break

        with self._stats_lock:
            dropped = self._dropped_events
        if dropped != self._reported_dropped_events:
            self._reported_dropped_events = dropped
            self.get_logger().error(
                f"Mule adapter event queue overflow; dropped events total: {dropped}"
            )

    def _handle_transport_event(self, event) -> None:
        kind = event[0]
        if kind == "wire_message":
            self._publish_wire_message(*event[1:])
        elif kind == "status_snapshot":
            self._publish_status(event[1])
        elif kind == "transport_ready":
            self.get_logger().info("Mule transport is ready")
        elif kind == "transport_error":
            self.get_logger().error(f"Mule transport error: {event[1]}")
        elif kind == "transport_stopped":
            self.get_logger().fatal(f"Mule transport stopped: {event[1]}")
            if self._shutdown_on_transport_error and rclpy.ok(context=self.context):
                rclpy.shutdown(context=self.context)
        elif kind == "transport_log":
            self._forward_transport_log(event[1], event[2])

    def _forward_transport_log(self, level: int, message: str) -> None:
        logger = self.get_logger()
        if level >= logging.CRITICAL:
            logger.fatal(message)
        elif level >= logging.ERROR:
            logger.error(message)
        elif level >= logging.WARNING:
            logger.warning(message)
        elif level >= logging.INFO:
            logger.info(message)
        else:
            logger.debug(message)

    def _publish_wire_message(
        self, direction: str, topic_name: str, payload: bytes, bridged_size: int
    ) -> None:
        publishers = self._publishers.get(topic_name)
        if publishers is None:
            self.get_logger().warning(f"Ignoring unexpected Mule topic {topic_name!r}")
            return
        try:
            if topic_name in (WIRE_LIDAR, WIRE_OCCUPANCY):
                message = to_ros2_point_cloud2(decode_ros1_point_cloud2(payload))
            elif topic_name == WIRE_ODOMETRY:
                message = to_ros2_odometry(decode_ros1_odometry(payload))
            elif topic_name == WIRE_TF_STATIC:
                decoded = decode_ros1_tf_message(payload)
                message = to_ros2_tf_message(self._static_tf_cache.update(decoded))
            else:
                message = to_ros2_tf_message(decode_ros1_tf_message(payload))
        except (WireDecodeError, ValueError, TypeError) as exc:
            self._decode_failures += 1
            if self._decode_failures <= 5 or self._decode_failures % 100 == 0:
                self.get_logger().error(
                    f"Rejected malformed ROS 1 payload on {topic_name}: {exc}; "
                    f"failures={self._decode_failures}"
                )
            return
        for publisher in publishers:
            publisher.publish(message)
        prefix = "volatile_pub" if direction == "volatile" else "persistent_pub"
        self._increment_topic(topic_name, prefix, len(payload), bridged_size)

    def _on_configuration(self, message: String) -> None:
        try:
            payload = encode_ros1_string(message.data)
        except (ValueError, TypeError) as exc:
            self.get_logger().error(f"Cannot encode occupancy configuration: {exc}")
            return
        should_schedule = self._configuration_mailbox.offer(
            payload, self._transport_ready.is_set
        )
        with self._stats_lock:
            self._configuration_retry_count = 0
        if not should_schedule:
            if self._transport_ready.is_set():
                return
            self.get_logger().info(
                "Queued latest occupancy configuration until Mule transport is ready"
            )
            return
        loop = self._transport_loop
        if loop is None:
            self._configuration_mailbox.cancel_schedule()
            return
        try:
            loop.add_callback(self._drain_configuration_mailbox)
        except Exception as exc:
            self._configuration_mailbox.cancel_schedule()
            self._enqueue_event(("transport_error", f"configuration queue: {exc!r}"))

    def _drain_configuration_mailbox(self) -> None:
        while True:
            payload = self._configuration_mailbox.take_or_disarm()
            if payload is None:
                return
            client = self._transport_client
            if client is None:
                self._configuration_mailbox.restore_after_failure(payload)
                return
            try:
                bridged_size = client.bridge_volatile(
                    payload, "configuration", WIRE_CONFIGURATION
                )
                self._increment_topic(
                    WIRE_CONFIGURATION, "volatile_sub", len(payload), bridged_size
                )
                with self._stats_lock:
                    self._configuration_retry_count = 0
            except Exception as exc:
                with self._stats_lock:
                    self._configuration_retry_count += 1
                    retry_count = self._configuration_retry_count
                can_retry = (
                    retry_count <= self._configuration_retry_limit
                    and self._transport_ready.is_set()
                )
                retry_reserved = self._configuration_mailbox.restore_after_failure(
                    payload, schedule_retry=can_retry
                )
                self._enqueue_event(("transport_error", f"configuration send: {exc!r}"))
                if retry_reserved:
                    loop = self._transport_loop
                    if loop is not None:
                        try:
                            loop.call_later(
                                self._configuration_retry_delay,
                                self._drain_configuration_mailbox,
                            )
                            return
                        except Exception as retry_exc:
                            self._configuration_mailbox.cancel_schedule()
                            self._enqueue_event(
                                (
                                    "transport_error",
                                    f"configuration retry queue: {retry_exc!r}",
                                )
                            )
                self._enqueue_event(
                    (
                        "transport_error",
                        "configuration remains pending after retry limit",
                    )
                )
                return

    def _on_set_overlay(self, request, response):
        loop = self._transport_loop
        client = self._transport_client
        if not self._transport_ready.is_set() or loop is None or client is None:
            self.get_logger().warning(
                "Ignoring set_overlay request because Mule transport is not ready"
            )
            return response
        node_names = list(request.node_names)

        def apply_overlay() -> None:
            try:
                client.set_overlay(node_names)
            except Exception as exc:
                self._enqueue_event(("transport_error", f"set_overlay: {exc!r}"))

        loop.add_callback(apply_overlay)
        return response

    def _increment_topic(
        self, topic_name: str, prefix: str, raw_size: int, bridged_size: int
    ) -> None:
        with self._stats_lock:
            counters = self._topic_counters[topic_name]
            setattr(counters, f"{prefix}_msg_count", getattr(counters, f"{prefix}_msg_count") + 1)
            setattr(
                counters,
                f"{prefix}_raw_size",
                getattr(counters, f"{prefix}_raw_size") + raw_size,
            )
            setattr(
                counters,
                f"{prefix}_bridged_size",
                getattr(counters, f"{prefix}_bridged_size") + bridged_size,
            )

    def _request_status_snapshot(self) -> None:
        loop = self._transport_loop
        if self._transport_ready.is_set() and loop is not None:
            loop.add_callback(self._snapshot_transport_state)

    def _snapshot_transport_state(self) -> None:
        try:
            client = self._transport_client
            if client is None:
                return
            manifest_time, manifest = client.get_history().peek()
            snapshot = {
                "manifest_time": float(manifest_time),
                "manifest": _snapshot_manifest(manifest),
                "peers": [_snapshot_peer(peer) for peer in client.get_peers()],
            }
            self._enqueue_event(("status_snapshot", snapshot))
        except Exception as exc:
            self._enqueue_event(("transport_error", f"status snapshot: {exc!r}"))

    def _publish_status(self, snapshot: Mapping) -> None:
        with self._stats_lock:
            dropped_events = self._dropped_events
        message = BridgeStatus()
        message.swarm_name = self._settings.swarm_name
        message.node_uuid = list(self._node_uuid)
        message.node_name = self._settings.node_name
        message.peers = [_peer_message(peer) for peer in snapshot["peers"]]
        message.topics = self._topic_status_messages()
        message.manifest = _manifest_message(snapshot["manifest"])
        message.manifest_time = _float_time_message(snapshot["manifest_time"])
        message.dropped_event_count = dropped_events
        message.decode_failure_count = self._decode_failures
        self._status_pub.publish(message)

    def _topic_status_messages(self):
        with self._stats_lock:
            snapshot = {
                topic: {field.name: getattr(counters, field.name) for field in fields(counters)}
                for topic, counters in self._topic_counters.items()
            }
        result = []
        for topic_name, values in snapshot.items():
            message = TopicStatus()
            message.topic_name = topic_name
            message.type_name, message.type_md5sum = ROS1_TYPE_INFO[topic_name]
            for name, value in values.items():
                setattr(message, name, value)
            result.append(message)
        return result

    def destroy_node(self) -> bool:
        self._transport_stop_requested.set()
        loop = self._transport_loop
        if loop is not None:
            try:
                loop.add_callback(loop.stop)
            except Exception:
                pass
        if self._transport_thread.is_alive():
            self._transport_thread.join(timeout=3.0)
        return super().destroy_node()


def _snapshot_manifest(manifest) -> Mapping:
    return {
        "seq": int(manifest.seq),
        "tails": [
            {
                "node_uuid": bytes(tail.node_uuid),
                "log_name": str(tail.log_name),
                "trunc": bool(tail.trunc),
                "index": int(tail.index),
            }
            for tail in manifest.tails
        ],
    }


def _snapshot_peer(peer) -> Mapping:
    return {
        "node_uuid": bytes(peer.node_uuid),
        "node_name": str(peer.node_name),
        "generation": int(peer.generation),
        "rtt_mean_secs": float(peer.rtt_mean_secs),
        "rtt_success_count": int(peer.rtt_success_count),
        "rtt_failure_count": int(peer.rtt_failure_count),
        "manifest_time": float(peer.manifest_time),
        "manifest": _snapshot_manifest(peer.manifest),
        "sync_up_time": float(peer.sync_up_time),
        "sync_down_time": float(peer.sync_down_time),
        "sync_up_size": int(peer.sync_up_size),
        "sync_down_size": int(peer.sync_down_size),
        "sync_down_used_size": int(peer.sync_down_used_size),
        "sync_down_budget_size": int(peer.sync_down_budget_size),
        "send_hello_request_count": int(peer.send_hello_request_count),
        "recv_hello_request_count": int(peer.recv_hello_request_count),
        "send_hello_reply_count": int(peer.send_hello_reply_count),
        "recv_hello_reply_count": int(peer.recv_hello_reply_count),
        "send_sync_request_count": int(peer.send_sync_request_count),
        "recv_sync_request_count": int(peer.recv_sync_request_count),
        "send_sync_reply_count": int(peer.send_sync_reply_count),
        "recv_sync_reply_count": int(peer.recv_sync_reply_count),
    }


def _manifest_message(snapshot: Mapping) -> Manifest:
    message = Manifest()
    message.seq = snapshot["seq"]
    tails = []
    for tail in snapshot["tails"]:
        item = LogIndex()
        item.node_uuid = list(tail["node_uuid"])
        item.log_name = tail["log_name"]
        item.trunc = tail["trunc"]
        item.index = tail["index"]
        tails.append(item)
    message.tails = tails
    return message


def _peer_message(snapshot: Mapping) -> PeerStatus:
    message = PeerStatus()
    message.node_uuid = list(snapshot["node_uuid"])
    message.node_name = snapshot["node_name"]
    message.generation = snapshot["generation"]
    message.rtt_mean_secs = snapshot["rtt_mean_secs"]
    message.rtt_success_count = snapshot["rtt_success_count"]
    message.rtt_failure_count = snapshot["rtt_failure_count"]
    message.manifest_time = _float_time_message(snapshot["manifest_time"])
    message.manifest = _manifest_message(snapshot["manifest"])
    message.sync_up_time = _float_time_message(snapshot["sync_up_time"])
    message.sync_down_time = _float_time_message(snapshot["sync_down_time"])
    for name in (
        "sync_up_size",
        "sync_down_size",
        "sync_down_used_size",
        "sync_down_budget_size",
        "send_hello_request_count",
        "recv_hello_request_count",
        "send_hello_reply_count",
        "recv_hello_reply_count",
        "send_sync_request_count",
        "recv_sync_request_count",
        "send_sync_reply_count",
        "recv_sync_reply_count",
    ):
        setattr(message, name, snapshot[name])
    return message


def _float_time_message(value: float) -> TimeMessage:
    message = TimeMessage()
    if not math.isfinite(value) or value <= 0:
        return message
    seconds = math.floor(value)
    nanoseconds = int(round((value - seconds) * 1_000_000_000))
    if nanoseconds == 1_000_000_000:
        seconds += 1
        nanoseconds = 0
    if seconds > (1 << 31) - 1:
        seconds = (1 << 31) - 1
        nanoseconds = 999_999_999
    message.sec = seconds
    message.nanosec = nanoseconds
    return message


def main(args=None) -> None:
    rclpy.init(args=args)
    node = MuleAdapter()
    executor = MultiThreadedExecutor(num_threads=2)
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
