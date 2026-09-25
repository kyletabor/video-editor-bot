"""Offline checks for archive integrity and non-destructive tool installation."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

MODULE_PATH = Path(__file__).resolve().parents[1] / "install_ffmpeg.py"
MODULE_SPEC = importlib.util.spec_from_file_location("install_ffmpeg", MODULE_PATH)
installer = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "tools.zip"
        self.dest = self.root / "installed"
        with zipfile.ZipFile(self.archive, "w") as bundle:
            bundle.writestr("release/bin/ffmpeg.exe", b"ffmpeg fixture")
            bundle.writestr("release/bin/ffprobe.exe", b"ffprobe fixture")
            bundle.writestr("../../escaped.txt", b"must never escape")
        self.spec = {
            "url": "https://example.invalid/locked.zip",
            "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "size": self.archive.stat().st_size,
            "archive": "zip",
            "members": {
                "ffmpeg.exe": "release/bin/ffmpeg.exe",
                "ffprobe.exe": "release/bin/ffprobe.exe",
            },
        }
        self.lock = {"version": "7.0.2", "platforms": {"test": self.spec}}

    def mocked_download(self):
        return patch.object(
            installer.urllib.request,
            "urlopen",
            side_effect=lambda *args, **kwargs: io.BytesIO(self.archive.read_bytes()),
        )

    def install_fixture(self):
        with self.mocked_download(), patch.object(installer, "verify_versions"):
            with contextlib.redirect_stdout(io.StringIO()):
                return installer.install(self.lock, self.dest, "test")

    def test_platforms_and_unsupported_abi(self):
        for system, machine, multiarch, key in [
            ("Windows", "AMD64", "", "windows-x86_64"),
            ("Linux", "x86_64", "", "linux-x86_64"),
            ("Linux", "aarch64", "aarch64-linux-gnu", "linux-aarch64"),
            ("Linux", "armv7l", "arm-linux-gnueabihf", "linux-armhf"),
            ("Linux", "aarch64", "arm-linux-gnueabihf", "linux-armhf"),
        ]:
            with self.subTest(key=key, machine=machine):
                self.assertEqual(installer.platform_key(system, machine, multiarch), key)
        for system, machine in [("Darwin", "arm64"), ("Windows", "ARM64"), ("Linux", "armv7l")]:
            with self.subTest(system=system, machine=machine):
                with self.assertRaises(installer.InstallError):
                    installer.platform_key(system, machine)

    def test_checksum_failure_prevents_extraction_and_publication(self):
        self.spec["sha256"] = "0" * 64
        with self.mocked_download(), patch.object(installer, "extract_tools") as extract:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(installer.InstallError, "checksum"):
                    installer.install(self.lock, self.dest, "test")
        extract.assert_not_called()
        self.assertFalse(self.dest.exists())

    def test_oversize_download_is_rejected(self):
        self.spec["size"] = 1
        with self.mocked_download():
            with self.assertRaisesRegex(installer.InstallError, "exceeds"):
                installer.download_archive(self.spec, self.root / "download.zip")

    def test_only_expected_basenames_are_published(self):
        self.assertTrue(self.install_fixture())
        self.assertEqual(
            {item.name for item in self.dest.iterdir()},
            {"ffmpeg.exe", "ffprobe.exe", installer.RECEIPT},
        )
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertEqual(list(self.root.glob(".ffmpeg-install-*")), [])

    def test_existing_intact_install_is_reused_without_download(self):
        self.install_fixture()
        with patch.object(installer, "download_archive") as download:
            with patch.object(installer, "verify_versions"):
                self.assertFalse(installer.install(self.lock, self.dest, "test"))
        download.assert_not_called()

    def test_existing_changed_install_is_preserved(self):
        self.install_fixture()
        binary = self.dest / "ffmpeg.exe"
        binary.write_bytes(b"changed")
        with self.assertRaisesRegex(installer.InstallError, "Preserving existing"):
            installer.install(self.lock, self.dest, "test")
        self.assertEqual(binary.read_bytes(), b"changed")

    def test_unrelated_destination_is_preserved(self):
        self.dest.mkdir()
        owned_file = self.dest / "user-file.txt"
        owned_file.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "Preserving existing"):
            installer.install(self.lock, self.dest, "test")
        self.assertEqual(owned_file.read_text(encoding="utf-8"), "preserve")

    def test_wrong_binary_version_does_not_publish_or_keep_staging(self):
        with self.mocked_download():
            with patch.object(installer, "verify_versions", side_effect=installer.InstallError("wrong version")):
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(installer.InstallError, "wrong version"):
                        installer.install(self.lock, self.dest, "test")
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.root.glob(".ffmpeg-install-*")), [])

    def test_version_check_requires_exact_patch_version(self):
        for reported in ("7.0.20", "7.1", "9.0.2"):
            result = subprocess.CompletedProcess([], 0, f"ffmpeg version {reported}-build\n", "")
            with patch.object(installer.subprocess, "run", return_value=result):
                with self.assertRaises(installer.InstallError):
                    installer.verify_versions(self.root, {"ffmpeg.exe": "unused"}, "7.0.2")
        result = subprocess.CompletedProcess([], 0, "ffmpeg version 7.0.2-essentials_build\n", "")
        with patch.object(installer.subprocess, "run", return_value=result):
            installer.verify_versions(self.root, {"ffmpeg.exe": "unused"}, "7.0.2")

    def test_zip_symlink_or_duplicate_selected_member_is_rejected(self):
        for duplicate in (False, True):
            with self.subTest(duplicate=duplicate):
                archive = self.root / ("duplicate.zip" if duplicate else "symlink.zip")
                with zipfile.ZipFile(archive, "w") as bundle:
                    info = zipfile.ZipInfo("release/bin/ffmpeg.exe")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    if duplicate:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", UserWarning)
                            bundle.writestr(info.filename, b"one")
                            bundle.writestr(info.filename, b"two")
                    else:
                        bundle.writestr(info, "../../target")
                    bundle.writestr("release/bin/ffprobe.exe", b"probe")
                output = self.root / archive.stem
                output.mkdir()
                with self.assertRaises(installer.InstallError):
                    installer.extract_tools(archive, self.spec, output)

    def test_tar_regular_files_only_and_no_path_extraction(self):
        spec = {"archive": "tar.xz", "members": {"ffmpeg": "release/ffmpeg", "ffprobe": "release/ffprobe"}}
        for link in (False, True):
            with self.subTest(link=link):
                archive = self.root / ("link.tar.xz" if link else "regular.tar.xz")
                with tarfile.open(archive, "w:xz") as bundle:
                    for name in ["release/ffmpeg", "release/ffprobe", "../../escaped"]:
                        info = tarfile.TarInfo(name)
                        if link and name == "release/ffprobe":
                            info.type = tarfile.SYMTYPE
                            info.linkname = "../../target"
                            bundle.addfile(info)
                        else:
                            info.size = 4
                            bundle.addfile(info, io.BytesIO(b"tool"))
                output = self.root / ("tar-link" if link else "tar-regular")
                output.mkdir()
                if link:
                    with self.assertRaises(installer.InstallError):
                        installer.extract_tools(archive, spec, output)
                else:
                    installer.extract_tools(archive, spec, output)
                    self.assertEqual({p.name for p in output.iterdir()}, {"ffmpeg", "ffprobe"})


if __name__ == "__main__":
    unittest.main()
