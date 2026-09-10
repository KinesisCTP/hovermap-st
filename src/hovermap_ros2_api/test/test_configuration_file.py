"""Tests for explicit perception configuration loading."""

from pathlib import Path
import tempfile
import unittest

from hovermap_ros2_api.configuration_file import read_configuration


class ConfigurationFileTests(unittest.TestCase):
    def test_reads_exact_utf8_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "perception.yaml"
            expected = "voxel_size: 0.2\nlabel: café\n"
            path.write_bytes(expected.encode("utf-8"))
            self.assertEqual(read_configuration(path), expected)

    def test_rejects_empty_oversize_and_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty.yaml"
            empty.write_text("  \n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_configuration(empty)

            large = root / "large.yaml"
            large.write_bytes(b"12345")
            with self.assertRaises(ValueError):
                read_configuration(large, max_bytes=4)

            invalid = root / "invalid.yaml"
            invalid.write_bytes(b"\xff")
            with self.assertRaises(ValueError):
                read_configuration(invalid)


if __name__ == "__main__":
    unittest.main()
