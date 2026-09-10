"""Race-order regression tests for the latest configuration mailbox."""

import unittest

from hovermap_ros2_api.latest_payload import LatestPayloadMailbox


class LatestPayloadMailboxTests(unittest.TestCase):
    def test_new_value_replaces_startup_pending_before_first_drain(self):
        mailbox = LatestPayloadMailbox()
        ready = False
        self.assertFalse(mailbox.offer(b"old", lambda: ready))
        ready = True
        self.assertTrue(mailbox.arm())
        self.assertFalse(mailbox.offer(b"new", lambda: ready))
        self.assertEqual(mailbox.take_or_disarm(), b"new")
        self.assertIsNone(mailbox.take_or_disarm())

    def test_value_arriving_during_drain_is_sent_after_older_value(self):
        mailbox = LatestPayloadMailbox()
        self.assertTrue(mailbox.offer(b"first", lambda: True))
        self.assertEqual(mailbox.take_or_disarm(), b"first")
        self.assertFalse(mailbox.offer(b"latest", lambda: True))
        self.assertEqual(mailbox.take_or_disarm(), b"latest")
        self.assertIsNone(mailbox.take_or_disarm())

    def test_failure_does_not_overwrite_newer_pending_value(self):
        mailbox = LatestPayloadMailbox()
        self.assertTrue(mailbox.offer(b"first", lambda: True))
        first = mailbox.take_or_disarm()
        mailbox.offer(b"latest", lambda: True)
        self.assertTrue(
            mailbox.restore_after_failure(first, schedule_retry=True)
        )
        self.assertEqual(mailbox.take_or_disarm(), b"latest")


if __name__ == "__main__":
    unittest.main()
