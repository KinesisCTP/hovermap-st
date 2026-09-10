"""Tests for static transform aggregation before transient-local publish."""

import unittest

from hovermap_ros2_api.ros1_wire import (
    Header,
    Quaternion,
    RosTime,
    TFMessage,
    TransformStamped,
    Vector3,
)
from hovermap_ros2_api.static_tf import StaticTransformCache


def _transform(parent: str, child: str, x: float) -> TransformStamped:
    return TransformStamped(
        header=Header(0, RosTime(1, 0), parent),
        child_frame_id=child,
        translation=Vector3(x, 0.0, 0.0),
        rotation=Quaternion(0.0, 0.0, 0.0, 1.0),
    )


class StaticTransformCacheTests(unittest.TestCase):
    def test_accumulates_separate_samples(self):
        cache = StaticTransformCache()
        first = cache.update(TFMessage((_transform("odom", "base", 1.0),)))
        second = cache.update(TFMessage((_transform("base", "lidar", 2.0),)))
        self.assertEqual([item.child_frame_id for item in first.transforms], ["base"])
        self.assertEqual(
            [item.child_frame_id for item in second.transforms],
            ["base", "lidar"],
        )

    def test_replaces_latest_transform_by_child_frame(self):
        cache = StaticTransformCache()
        cache.update(
            TFMessage(
                (
                    _transform("odom", "base", 1.0),
                    _transform("base", "lidar", 2.0),
                )
            )
        )
        aggregate = cache.update(TFMessage((_transform("map", "base", 3.0),)))
        self.assertEqual(len(aggregate.transforms), 2)
        self.assertEqual(aggregate.transforms[0].header.frame_id, "map")
        self.assertEqual(aggregate.transforms[0].translation.x, 3.0)

    def test_rejects_empty_child_and_capacity_overflow(self):
        cache = StaticTransformCache(max_transforms=1)
        with self.assertRaises(ValueError):
            cache.update(TFMessage((_transform("odom", "", 0.0),)))
        cache.update(TFMessage((_transform("odom", "base", 0.0),)))
        with self.assertRaises(ValueError):
            cache.update(TFMessage((_transform("base", "lidar", 0.0),)))

    def test_rejected_sample_is_atomic(self):
        cache = StaticTransformCache(max_transforms=2)
        cache.update(TFMessage((_transform("odom", "base", 1.0),)))
        with self.assertRaises(ValueError):
            cache.update(
                TFMessage(
                    (
                        _transform("base", "lidar", 2.0),
                        _transform("lidar", "", 3.0),
                    )
                )
            )
        aggregate = cache.update(TFMessage(()))
        self.assertEqual(
            [item.child_frame_id for item in aggregate.transforms], ["base"]
        )


if __name__ == "__main__":
    unittest.main()
