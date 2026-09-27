import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import procutil, worker
from app.stages import ingest, upscale


class PreflightTests(unittest.TestCase):
    def test_fat_volume_skipped_even_with_space(self):
        with patch.dict(ingest.CONFIG, staging_drives=["R:/", "Z:/"]), \
             patch.object(ingest.staging, "check_filesystem", side_effect=[RuntimeError("FAT32"), None]), \
             patch.object(ingest.shutil, "disk_usage", return_value=SimpleNamespace(free=100 * 1024**3)):
            self.assertEqual(ingest._pick_staging_drive(1024), "Z:/")

    def test_no_unsafe_fallback_when_drives_full(self):
        with patch.dict(ingest.CONFIG, staging_drives=["Z:/"]), \
             patch.object(ingest.staging, "check_filesystem"), \
             patch.object(ingest.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(RuntimeError, "no suitable drive"):
                ingest._pick_staging_drive(1024)

    def test_encoder_disk_error_not_hidden_by_broken_pipe(self):
        producer = Mock(stdout=io.BytesIO(), stderr=io.BytesIO(b"fwrite failed errno: 32"), returncode=1)
        consumer = Mock(stdout=io.StringIO("No space left on device\n"), returncode=1)
        with patch.object(procutil.subprocess, "Popen", side_effect=[producer, consumer]), \
             patch.object(procutil.db, "log"):
            with self.assertRaises(procutil.CommandError) as caught:
                procutil.run_piped_logged("job", "stage", ["vspipe"], ["ffmpeg"])
        self.assertEqual(caught.exception.cmd, ["ffmpeg"])
        self.assertEqual(worker._classify_failure(str(caught.exception), ""), "disk_full")

    def test_generative_oom_does_not_enter_output_recovery(self):
        preset = {"output_tier_height": 1080, "model": "astrasharp", "max_gpu_mem_gb": 19,
                  "ffmpeg_encoding": "-c:v h264_nvenc"}
        error = procutil.CommandError(["neuroserver"], 1, "torch.OutOfMemoryError: CUDA out of memory")
        with patch.object(upscale, "_current_dims", return_value=(640, 480)), \
             patch.object(upscale, "_current_fps", return_value=60), \
             patch.object(upscale.db, "log"), \
             patch.object(upscale, "run_logged", side_effect=error), \
             patch.object(upscale, "_find_recovered_output") as recover:
            with self.assertRaises(procutil.CommandError):
                upscale._run_generative_segment("job", preset, Path("input.mp4"), 0, None, 30, 30, 0)
            recover.assert_not_called()
        self.assertEqual(worker._classify_failure(str(error), ""), "oom")


if __name__ == "__main__":
    unittest.main()
