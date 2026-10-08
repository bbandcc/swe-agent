"""Static archive evidence rejects unpinned bytes before tar parsing."""
from pathlib import Path
import tempfile
import unittest
from research.audit_fuzzysearch_sdist import SIZE, audit


class StaticEvidenceTests(unittest.TestCase):
    def test_invalid_bytes_fail_closed(self):
        for raw in (b"not a tar", b"x" * SIZE, b"x" * (SIZE + 1)):
            with self.subTest(size=len(raw)), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "archive.tar.gz"
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "PINNED_ARCHIVE_MISMATCH"):
                    audit(path)
                self.assertEqual(path.read_bytes(), raw)

    def test_missing_input_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileNotFoundError):
                audit(Path(root) / "missing.tar.gz")
