"""One-shot ROS 2 publisher for an explicitly selected perception config."""

import time

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from hovermap_ros2_msgs.msg import BridgeStatus

from .configuration_file import read_configuration
from .mule_config import WIRE_CONFIGURATION
from .peer_gate import peer_requirement_satisfied


class PerceptionConfigurationPublisher(Node):
    """Publish a reviewed file only after discovering the Mule adapter."""

    def __init__(self) -> None:
        super().__init__("hovermap_perception_config")
        self.declare_parameter("config_file", "")
        self.declare_parameter("discovery_timeout", 10.0)
        self.declare_parameter("peer_timeout", 30.0)
        self.declare_parameter("peer_settle_time", 2.0)
        self.declare_parameter("required_peer_name", "")
        self.declare_parameter("publish_repetitions", 3)
        self.declare_parameter("publish_interval", 1.0)
        self.declare_parameter(
            "bridge_status_topic", "/cortex/mule_bridge/status"
        )
        self._peer_names = ()
        self.publisher = self.create_publisher(
            String,
            WIRE_CONFIGURATION,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            ),
        )
        self.create_subscription(
            BridgeStatus,
            self.get_parameter("bridge_status_topic").value,
            self._on_bridge_status,
            QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            ),
        )

    def _on_bridge_status(self, message: BridgeStatus) -> None:
        self._peer_names = tuple(peer.node_name for peer in message.peers)

    def publish_once(self) -> None:
        config_file = self.get_parameter("config_file").value
        if not isinstance(config_file, str) or not config_file:
            raise ValueError(
                "config_file is required; no perception configuration is applied by default"
            )
        timeout = float(self.get_parameter("discovery_timeout").value)
        if timeout <= 0:
            raise ValueError("discovery_timeout must be positive")
        peer_timeout = float(self.get_parameter("peer_timeout").value)
        settle_time = float(self.get_parameter("peer_settle_time").value)
        required_peer = self.get_parameter("required_peer_name").value
        repetitions = int(self.get_parameter("publish_repetitions").value)
        publish_interval = float(self.get_parameter("publish_interval").value)
        if peer_timeout <= 0:
            raise ValueError("peer_timeout must be positive")
        if settle_time < 0:
            raise ValueError("peer_settle_time must be non-negative")
        if not isinstance(required_peer, str):
            raise ValueError("required_peer_name must be a string")
        if repetitions < 1 or repetitions > 10:
            raise ValueError("publish_repetitions must be in 1..10")
        if publish_interval < 0 or publish_interval > 30:
            raise ValueError("publish_interval must be in 0..30 seconds")
        configuration = read_configuration(config_file)
        deadline = time.monotonic() + timeout
        while self.count_subscribers(WIRE_CONFIGURATION) == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"no subscriber discovered on {WIRE_CONFIGURATION} within {timeout}s"
                )
            rclpy.spin_once(self, timeout_sec=0.1)

        self._wait_for_stable_peer(required_peer, peer_timeout, settle_time)
        for index in range(repetitions):
            if not peer_requirement_satisfied(self._peer_names, required_peer):
                raise RuntimeError("Mule peer disappeared before configuration send")
            self.publisher.publish(String(data=configuration))
            if index + 1 < repetitions:
                self._spin_for(publish_interval)
        self.get_logger().info(
            "Published explicitly selected perception configuration "
            f"{config_file!r} {repetitions} time(s)"
        )
        self.get_logger().warning(
            "Mule provides no acknowledgement for this configuration command; "
            "validate occupancy output and device persistence on hardware"
        )
        rclpy.spin_once(self, timeout_sec=0.25)

    def _wait_for_stable_peer(
        self, required_peer: str, timeout: float, settle_time: float
    ) -> None:
        deadline = time.monotonic() + timeout
        stable_since = None
        while True:
            rclpy.spin_once(self, timeout_sec=0.1)
            now = time.monotonic()
            if peer_requirement_satisfied(self._peer_names, required_peer):
                if stable_since is None:
                    stable_since = now
                if now - stable_since >= settle_time:
                    return
            else:
                stable_since = None
            if now >= deadline:
                description = repr(required_peer) if required_peer else "any peer"
                raise TimeoutError(
                    f"no stable Mule peer matching {description} within {timeout}s"
                )

    def _spin_for(self, duration: float) -> None:
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            rclpy.spin_once(
                self,
                timeout_sec=min(0.1, max(0.0, deadline - time.monotonic())),
            )


def main(args=None) -> None:
    rclpy.init(args=args)
    node = PerceptionConfigurationPublisher()
    exit_code = 0
    try:
        node.publish_once()
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        node.get_logger().error(str(exc))
        exit_code = 1
    finally:
        node.destroy_node()
        rclpy.shutdown()
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
