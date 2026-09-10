"""Concurrency tests for the bounded daemon HTTP worker pool."""

import threading
import unittest

from hovermap_ros2_api.worker_pool import BoundedDaemonWorkerPool, WorkQueueFull


class BoundedDaemonWorkerPoolTests(unittest.TestCase):
    def test_rejects_shutdown_capacity_smaller_than_worker_count(self):
        with self.assertRaisesRegex(ValueError, "at least the worker count"):
            BoundedDaemonWorkerPool(2, 1, "test-worker")

    def test_queue_is_bounded_while_a_worker_is_busy(self):
        pool = BoundedDaemonWorkerPool(1, 1, "test-worker")
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(timeout=2.0)
            return "first"

        first = pool.submit(blocking)
        self.assertTrue(started.wait(timeout=1.0))
        second = pool.submit(lambda: "second")
        with self.assertRaises(WorkQueueFull):
            pool.submit(lambda: "third")
        release.set()
        self.assertEqual(first.result(timeout=1.0), "first")
        self.assertEqual(second.result(timeout=1.0), "second")
        pool.shutdown(wait=True)

    def test_shutdown_cancels_pending_without_waiting_for_running_call(self):
        pool = BoundedDaemonWorkerPool(1, 2, "test-worker")
        started = threading.Event()
        release = threading.Event()

        def blocking():
            started.set()
            release.wait(timeout=2.0)

        running = pool.submit(blocking)
        self.assertTrue(started.wait(timeout=1.0))
        pending = pool.submit(lambda: None)
        pool.shutdown(cancel_pending=True, wait=False)
        self.assertTrue(pending.cancelled())
        self.assertFalse(running.done())
        release.set()
        self.assertIsNone(running.result(timeout=1.0))

    def test_single_worker_preserves_submission_order(self):
        pool = BoundedDaemonWorkerPool(1, 8, "test-control")
        observed = []
        futures = [
            pool.submit(lambda value=value: observed.append(value))
            for value in ("start", "prefix", "stop")
        ]
        for future in futures:
            self.assertIsNone(future.result(timeout=1.0))
        self.assertEqual(observed, ["start", "prefix", "stop"])
        pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
