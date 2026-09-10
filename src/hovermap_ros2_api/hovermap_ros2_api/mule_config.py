"""ROS-independent configuration boundary for the imported Mule core."""

from dataclasses import dataclass
import importlib
import ipaddress


WIRE_CONFIGURATION = "/cortex/occupancy_grid_map/configuration"
WIRE_LIDAR = "/cortex/lidar/corrected"
WIRE_OCCUPANCY = "/cortex/occupancy_grid_map/data"
WIRE_ODOMETRY = "/cortex/odometry"
WIRE_TF = "/cortex/tf"
WIRE_TF_STATIC = "/cortex/tf_static"

VOLATILE_PUBLICATIONS = (WIRE_LIDAR, WIRE_OCCUPANCY, WIRE_TF, WIRE_ODOMETRY)
PERSISTENT_PUBLICATIONS = (WIRE_TF_STATIC,)

ROS1_TYPE_INFO = {
    WIRE_CONFIGURATION: ("std_msgs/String", "992ce8a1687cec8c8bd883ec73ca41d1"),
    WIRE_LIDAR: ("sensor_msgs/PointCloud2", "1158d486dd51d683ce2f1be655c3c181"),
    WIRE_OCCUPANCY: (
        "sensor_msgs/PointCloud2",
        "1158d486dd51d683ce2f1be655c3c181",
    ),
    WIRE_ODOMETRY: ("nav_msgs/Odometry", "cd5e73d190d741a2f92e81eda573aca7"),
    WIRE_TF: ("tf2_msgs/TFMessage", "94810edda583a504dfda3829e70d7eec"),
    WIRE_TF_STATIC: ("tf2_msgs/TFMessage", "94810edda583a504dfda3829e70d7eec"),
}


@dataclass(frozen=True)
class MuleSettings:
    swarm_name: str = "hvm"
    node_name: str = "hvm_ros2_client"
    ip_prefix: str = "10.9.0.0"
    ip_netmask: str = "255.255.255.0"
    ping_mcast_group: str = "225.0.0.250"
    ping_ucast_addrs: tuple = ()
    ping_port: int = 8123
    min_port: int = 49172
    max_port: int = 49192
    initial_wait: float = 5.0
    status_period: float = 1.0
    compression_level: int = -1


def validate_mule_settings(settings: MuleSettings) -> MuleSettings:
    """Validate settings before the Mule core opens sockets or database files."""

    if not settings.swarm_name or not settings.node_name:
        raise ValueError("swarm_name and node_name must be non-empty")
    try:
        ipaddress.IPv4Network(
            f"{settings.ip_prefix}/{settings.ip_netmask}", strict=True
        )
        multicast = (
            None
            if settings.ping_mcast_group == ""
            else ipaddress.IPv4Address(settings.ping_mcast_group)
        )
        unicast = [ipaddress.IPv4Address(value) for value in settings.ping_ucast_addrs]
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"invalid Mule IPv4 setting: {exc}") from exc
    except ipaddress.NetmaskValueError as exc:
        raise ValueError(f"invalid Mule IPv4 netmask: {exc}") from exc
    if multicast is not None and not multicast.is_multicast:
        raise ValueError("ping_mcast_group must be an IPv4 multicast address")
    if any(address.is_multicast for address in unicast):
        raise ValueError("ping_ucast_addrs must contain only unicast addresses")
    if settings.min_port < 1 or settings.max_port > 65535:
        raise ValueError("Mule port range must be within 1..65535")
    if settings.min_port >= settings.max_port:
        raise ValueError("min_port must be less than exclusive max_port")
    if settings.ping_port < 1 or settings.ping_port > 65535:
        raise ValueError("ping_port must be within 1..65535")
    if settings.status_period <= 0 or settings.initial_wait < 0:
        raise ValueError("status_period must be positive and initial_wait non-negative")
    if settings.compression_level < -1 or settings.compression_level > 9:
        raise ValueError("compression_level must be in -1..9")
    return settings


def load_mule_core():
    """Load the separately installed KINESIS Mule core with a clear diagnostic."""

    try:
        core = importlib.import_module("mule_bridge")
    except ImportError as exc:
        raise RuntimeError(
            "The ROS-agnostic 'mule_bridge' Python package is not installed. "
            "Install the KINESIS fork separately; do not vendor it into this package."
        ) from exc
    for required in ("Client", "Config"):
        if not hasattr(core, required):
            raise RuntimeError(f"mule_bridge core does not export required {required}")
    return core


def build_core_config(core, settings: MuleSettings):
    """Build the legacy core's typed config without ROS 1 parameter machinery."""

    validate_mule_settings(settings)
    config_type = core.Config
    volatile_type = config_type.Bridge.Volatile
    persistent_type = config_type.Bridge.Persistent
    subscription = volatile_type.Flow.Subscription(
        topic=WIRE_CONFIGURATION,
        type="std_msgs/String",
    )
    configuration_flow = volatile_type.Flow(
        subscriptions=[subscription],
        ip_tos=0x30,
        sndhwm=1000,
    )
    volatile_publications = [
        volatile_type.Publication(topic=topic, type=ROS1_TYPE_INFO[topic][0])
        for topic in VOLATILE_PUBLICATIONS
    ]
    persistent_publications = [
        persistent_type.Publication(topic=topic, type=ROS1_TYPE_INFO[topic][0])
        for topic in PERSISTENT_PUBLICATIONS
    ]
    bridge = config_type.Bridge(
        volatile=volatile_type(
            flows={"configuration": configuration_flow},
            publications=volatile_publications,
        ),
        persistent=persistent_type(logs={}, publications=persistent_publications),
    )
    return config_type(
        bridge=bridge,
        swarm_name=settings.swarm_name,
        node_name=settings.node_name,
        ip_prefix=settings.ip_prefix,
        ip_netmask=settings.ip_netmask,
        ping_mcast_group=settings.ping_mcast_group or None,
        ping_ucast_addrs=list(settings.ping_ucast_addrs),
        ping_port=settings.ping_port,
        min_port=settings.min_port,
        max_port=settings.max_port,
        initial_wait=settings.initial_wait,
        status_period=settings.status_period,
        compression_level=settings.compression_level,
    )
