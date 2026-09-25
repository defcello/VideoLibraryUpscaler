import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import staging


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.directory = root / "staging" / "job1"
        self.directory.mkdir(parents=True)
        self.source = root / "source.mkv"
        self.source.write_bytes(b"source")
        self.current = self.directory / "input.mkv"
        self.current.write_bytes(b"retry input")
        self.partial = self.directory / "partial.mkv"
        self.partial.write_bytes(b"incomplete")
        self.job = dict(id="job1", staging_dir=str(self.directory),
                        original_nas_path=str(self.source), current_file=str(self.current),
                        status="failed", stage="probed")
        self.settings = {}
        for target, kwargs in [
            ("CONFIG", {"new": {"staging_drives": [str(root)], "staging_subdir": "staging"}}),
            ("db.get_job", {"return_value": self.job}),
            ("db.get_settings", {"return_value": self.settings}),
            ("db.log", {}),
        ]:
            mock = patch("app.staging." + target, **kwargs)
            value = mock.start()
            self.addCleanup(mock.stop)
            if target == "db.log":
                self.log = value

    def test_failed_job_keeps_retry_input_only(self):
        staging.cleanup("job1")
        self.assertEqual(list(self.directory.iterdir()), [self.current])
        self.assertEqual(self.source.read_bytes(), b"source")

    def test_successful_stage_keeps_committed_output(self):
        self.job["status"] = "pending"
        self.test_failed_job_keeps_retry_input_only()

    def test_done_and_cancelled_remove_staging(self):
        for status in ("done", "cancelled"):
            self.directory.mkdir(exist_ok=True)
            self.partial.write_bytes(b"leftover")
            self.job["status"] = status
            staging.cleanup("job1")
            self.assertFalse(self.directory.exists())
            self.assertTrue(self.source.exists())

    def test_failed_ingest_removes_partial_copy(self):
        self.job.update(stage="queued", current_file=None)
        staging.cleanup("job1")
        self.assertFalse(self.directory.exists())

    def test_missing_retry_input_preserves_files(self):
        self.current.unlink()
        staging.cleanup("job1")
        self.assertTrue(self.partial.exists())
        self.log.assert_called()

    def test_rejects_staging_root_as_job_directory(self):
        self.job["staging_dir"] = str(self.directory.parent)
        staging.cleanup("job1")
        self.assertTrue(self.partial.exists())
        self.log.assert_called()

    def test_checkpoint_files_are_preserved(self):
        self.settings["checkpoint_completed_segments"] = [{"path": str(self.partial)}]
        staging.cleanup("job1")
        self.assertTrue(self.partial.exists())
        self.assertTrue(self.current.exists())

    def test_locked_file_is_logged_and_retried(self):
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            staging.cleanup("job1")
        self.assertTrue(self.partial.exists())
        self.log.assert_called()
        staging.cleanup("job1")
        self.assertFalse(self.partial.exists())


if __name__ == "__main__":
    unittest.main()
