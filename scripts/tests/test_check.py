"""Regression checks for failures that must never produce a green gate."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check


class GateTests(unittest.TestCase):
    def setUp(self):
        self.media = {
            "format": {"duration": "5.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
                 "width": 640, "height": 360, "nb_read_frames": "150", "duration": "5.0"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "duration": "5.0"},
            ],
        }

    def assert_valid(self, media):
        check.assert_media(media, seconds=5, width=640, height=360, frames=150)

    def test_expected_media(self):
        self.assert_valid(self.media)

    def test_bad_media_rejected_even_with_correct_container_duration(self):
        for field, value in (("duration", "1"), ("duration", "NaN"), ("duration", None),
                             ("width", 320), ("nb_read_frames", "149"), ("pix_fmt", "yuv444p")):
            with self.subTest(field=field, value=value):
                media = deepcopy(self.media)
                media["streams"][0][field] = value
                with self.assertRaises(check.CheckError):
                    self.assert_valid(media)
        self.media["streams"].pop()
        with self.assertRaises(check.CheckError):
            self.assert_valid(self.media)

    def test_command_failure_is_propagated(self):
        failed = subprocess.CompletedProcess(["ffmpeg"], 7, "", "encoder failed")
        with patch("check.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(check.CheckError, "encoder failed"):
                check.run("ffmpeg")

    def test_whitespace_failure_does_not_fall_back(self):
        with patch.dict("check.os.environ", {"CHECK_BASE": "known-base"}):
            with patch("check.run", side_effect=check.CheckError("bad whitespace")) as command:
                with self.assertRaises(check.CheckError):
                    check.check_whitespace()
                self.assertEqual(command.call_count, 1)

    def test_staged_whitespace_cannot_be_hidden_by_unstaged_fix(self):
        with tempfile.TemporaryDirectory(prefix="veb git test ") as directory:
            root = Path(directory)
            def git(*args):
                subprocess.run(["git", "-C", directory, *args], check=True, capture_output=True)
            git("init")
            git("-c", "user.name=Gate Test", "-c", "user.email=gate@example.invalid",
                "commit", "--allow-empty", "-m", "initial")
            source = root / "example.txt"
            source.write_text("staged whitespace   \n", encoding="utf-8")
            git("add", "example.txt")
            source.write_text("fixed only in working tree\n", encoding="utf-8")
            with patch("check.ROOT", root), patch.dict("check.os.environ", {"CHECK_BASE": "HEAD"}):
                with self.assertRaisesRegex(check.CheckError, "trailing whitespace"):
                    check.check_whitespace()

    def test_wrong_or_mismatched_tool_versions_fail(self):
        for versions in (("ffmpeg version 4.3.2-test",),
                         ("ffmpeg version 7.0.2-static", "ffprobe version 9.0.2-static")):
            with patch("check.run", side_effect=versions):
                with self.assertRaises(check.CheckError):
                    check.check_versions()

    def test_reference_run_rejects_other_compatible_version(self):
        with patch("check.run", side_effect=["ffmpeg version 9.0.2-static", "ffprobe version 9.0.2-static"]):
            with self.assertRaisesRegex(check.CheckError, "reference run requires"):
                check.check_versions(require_pinned=True)

    def test_missing_required_filter_fails(self):
        with patch("check.run", side_effect=["ffmpeg version 7.0.2-static", "ffprobe version 7.0.2-static",
                                             " V..... libx264 encoder\n A..... aac encoder", ""]):
            with self.assertRaisesRegex(check.CheckError, "missing required capability: subtitles"):
                check.check_versions()


if __name__ == "__main__":
    unittest.main()
