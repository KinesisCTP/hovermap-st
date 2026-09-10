"""Tests for the Mule peer-readiness predicate."""

import unittest

from hovermap_ros2_api.peer_gate import peer_requirement_satisfied


class PeerGateTests(unittest.TestCase):
    def test_any_peer_mode_requires_at_least_one_named_peer(self):
        self.assertFalse(peer_requirement_satisfied([]))
        self.assertFalse(peer_requirement_satisfied(["", None]))
        self.assertTrue(peer_requirement_satisfied(["hovermap"]))

    def test_named_peer_mode_requires_exact_match(self):
        self.assertFalse(
            peer_requirement_satisfied(["another-device"], "hovermap")
        )
        self.assertTrue(peer_requirement_satisfied(["hovermap"], "hovermap"))

    def test_rejects_non_string_requirement(self):
        with self.assertRaises(TypeError):
            peer_requirement_satisfied(["hovermap"], None)


if __name__ == "__main__":
    unittest.main()
