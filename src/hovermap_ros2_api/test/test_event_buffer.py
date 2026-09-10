"""Tests for live-data drop-oldest buffering."""

import queue
import unittest

from hovermap_ros2_api.event_buffer import DropOldestBuffer


class DropOldestBufferTests(unittest.TestCase):
    def test_full_buffer_evicts_oldest_and_keeps_newest(self):
        dropped = []
        buffer = DropOldestBuffer(
            max_items=2,
            max_bytes=10,
            size_of=len,
            on_drop=dropped.append,
        )
        buffer.put(b"old")
        buffer.put(b"middle")
        buffer.put(b"new")
        self.assertEqual(dropped, [b"old"])
        self.assertEqual(buffer.get_nowait(), b"middle")
        self.assertEqual(buffer.get_nowait(), b"new")
        with self.assertRaises(queue.Empty):
            buffer.get_nowait()

    def test_byte_limit_evicts_enough_old_entries(self):
        dropped = []
        buffer = DropOldestBuffer(
            max_items=4,
            max_bytes=5,
            size_of=len,
            on_drop=dropped.append,
        )
        buffer.put(b"aa")
        buffer.put(b"bb")
        buffer.put(b"cccc")
        self.assertEqual(dropped, [b"aa", b"bb"])
        self.assertEqual(buffer.get_nowait(), b"cccc")

    def test_single_oversize_item_is_rejected_and_reported(self):
        dropped = []
        buffer = DropOldestBuffer(
            max_items=1,
            max_bytes=2,
            size_of=len,
            on_drop=dropped.append,
        )
        self.assertFalse(buffer.put(b"large"))
        self.assertEqual(dropped, [b"large"])
        self.assertEqual(len(buffer), 0)


if __name__ == "__main__":
    unittest.main()
