"""Offline tests for the boundary between the ROS 2 adapter and Mule core."""

from types import SimpleNamespace
import unittest

from hovermap_ros2_api.mule_config import (
    MuleSettings,
    PERSISTENT_PUBLICATIONS,
    VOLATILE_PUBLICATIONS,
    WIRE_CONFIGURATION,
    WIRE_LIDAR,
    WIRE_OCCUPANCY,
    WIRE_ODOMETRY,
    WIRE_TF,
    WIRE_TF_STATIC,
    build_core_config,
    validate_mule_settings,
)


class _Builder:
    def __call__(self, **values):
        return SimpleNamespace(**values)


def _fake_core():
    config = _Builder()
    config.Bridge = _Builder()
    config.Bridge.Volatile = _Builder()
    config.Bridge.Volatile.Flow = _Builder()
    config.Bridge.Volatile.Flow.Subscription = _Builder()
    config.Bridge.Volatile.Publication = _Builder()
    config.Bridge.Persistent = _Builder()
    config.Bridge.Persistent.Publication = _Builder()
    return SimpleNamespace(Config=config)


class MuleConfigTests(unittest.TestCase):
    def test_fixed_wire_publications_match_hovermap_contract(self):
        self.assertEqual(
            VOLATILE_PUBLICATIONS,
            (WIRE_LIDAR, WIRE_OCCUPANCY, WIRE_TF, WIRE_ODOMETRY),
        )
        self.assertEqual(PERSISTENT_PUBLICATIONS, (WIRE_TF_STATIC,))
        for topic in VOLATILE_PUBLICATIONS + PERSISTENT_PUBLICATIONS:
            self.assertTrue(topic.startswith("/cortex/"))

    def test_core_config_has_configuration_flow_and_fixed_publications(self):
        settings = MuleSettings(
            swarm_name="hvm",
            node_name="test_client",
            ip_prefix="192.168.2.0",
            ping_ucast_addrs=("192.168.2.115",),
        )
        result = build_core_config(_fake_core(), settings)
        self.assertEqual(result.swarm_name, "hvm")
        self.assertEqual(result.node_name, "test_client")
        self.assertEqual(result.ip_prefix, "192.168.2.0")
        self.assertEqual(result.ping_ucast_addrs, ["192.168.2.115"])
        flow = result.bridge.volatile.flows["configuration"]
        self.assertEqual(len(flow.subscriptions), 1)
        self.assertEqual(flow.subscriptions[0].topic, WIRE_CONFIGURATION)
        self.assertEqual(flow.subscriptions[0].type, "std_msgs/String")
        self.assertEqual(
            [publication.topic for publication in result.bridge.volatile.publications],
            list(VOLATILE_PUBLICATIONS),
        )
        self.assertEqual(
            [publication.topic for publication in result.bridge.persistent.publications],
            list(PERSISTENT_PUBLICATIONS),
        )

    def test_rejects_invalid_network_and_port_settings(self):
        for settings in (
            MuleSettings(ip_prefix="10.9.0.1"),
            MuleSettings(ping_mcast_group="10.9.0.1"),
            MuleSettings(ping_ucast_addrs=("225.0.0.250",)),
            MuleSettings(min_port=50000, max_port=40000),
            MuleSettings(min_port=50000, max_port=50000),
            MuleSettings(compression_level=10),
        ):
            with self.assertRaises(ValueError):
                validate_mule_settings(settings)

    def test_empty_multicast_string_maps_to_unicast_only_core_config(self):
        settings = MuleSettings(
            ping_mcast_group="",
            ping_ucast_addrs=("10.9.0.2",),
        )
        validate_mule_settings(settings)
        result = build_core_config(_fake_core(), settings)
        self.assertIsNone(result.ping_mcast_group)
        self.assertEqual(result.ping_ucast_addrs, ["10.9.0.2"])


if __name__ == "__main__":
    unittest.main()
