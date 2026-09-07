import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.sync_lock import acquire_sync_lock, SyncAlreadyRunning


class NativeSyncLockTests(unittest.TestCase):
    def test_same_process_exclusion_and_exception_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "fixture.db"
            with self.assertRaises(ValueError):
                with acquire_sync_lock(db):
                    with self.assertRaises(SyncAlreadyRunning):
                        with acquire_sync_lock(db):
                            self.fail("lock must exclude a second owner")
                    raise ValueError("test cleanup")
            with acquire_sync_lock(db):
                pass

    def test_separate_process_is_excluded_then_can_acquire(self):
        code = """import sys
from core.sync_lock import acquire_sync_lock, SyncAlreadyRunning
try:
    with acquire_sync_lock(sys.argv[1]):
        pass
except SyncAlreadyRunning:
    sys.exit(23)
"""
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "fixture.db")
            def child():
                return subprocess.run([sys.executable, "-c", code, db], capture_output=True, timeout=15)
            with acquire_sync_lock(db):
                result = child()
                self.assertEqual(result.returncode, 23, result.stderr.decode())
            result = child()
            self.assertEqual(result.returncode, 0, result.stderr.decode())
