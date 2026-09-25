"""Install the checksum-locked FFmpeg tools using Python 3.11+ standard library.

Run ``python scripts/install_ffmpeg.py`` before the shared check gate. Only the
two executable members are copied; archive paths are never used as destinations.
An intact installation can be reused. Other existing destinations are preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = Path(__file__).with_name("ffmpeg-lock.json")
RECEIPT = "installation.json"


class InstallError(Exception):
    """An actionable installation failure."""


def platform_key(system: str, machine: str, multiarch: str = "") -> str:
    machine = machine.lower()
    if system == "Windows" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    if system == "Linux":
        if machine in {"amd64", "x86_64"}:
            return "linux-x86_64"
        if "gnueabihf" in multiarch and machine in {"armv7l", "armv8l", "aarch64"}:
            return "linux-armhf"
        if machine in {"aarch64", "arm64"}:
            return "linux-aarch64"
    raise InstallError(
        f"No locked binary for {system}/{machine} ({multiarch or 'unknown ABI'}). "
        "Use a native FFmpeg/ffprobe 7.0.2 build with libx264, AAC and libass; "
        "see docs/checks.md."
    )


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        if hasattr(hashlib, "file_digest"):  # Python 3.11+
            return hashlib.file_digest(stream, "sha256").hexdigest()
        digest = hashlib.sha256()  # Python 3.10 fallback (e.g. Ubuntu 22.04 system python)
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
        return digest.hexdigest()


def verify_archive(path: Path, spec: dict) -> None:
    if path.stat().st_size != spec["size"] or sha256(path) != spec["sha256"]:
        raise InstallError("FFmpeg archive checksum/size mismatch; nothing installed.")


def download_archive(spec: dict, path: Path) -> None:
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "video-editor-bot"})
    with urllib.request.urlopen(request, timeout=60) as response, path.open("xb") as output:
        received = 0
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > spec["size"]:
                raise InstallError("FFmpeg archive exceeds its locked size; nothing installed.")
            output.write(chunk)
    verify_archive(path, spec)


def extract_tools(archive: Path, spec: dict, destination: Path) -> None:
    members = spec["members"]
    if set(members) not in ({"ffmpeg", "ffprobe"}, {"ffmpeg.exe", "ffprobe.exe"}):
        raise InstallError("Lock file must select exactly the two FFmpeg executable basenames.")
    if spec["archive"] == "zip":
        with zipfile.ZipFile(archive) as bundle:
            for name, member_name in members.items():
                matches = [item for item in bundle.infolist() if item.filename == member_name]
                if len(matches) != 1:
                    raise InstallError(f"Archive must contain exactly one {member_name}.")
                member = matches[0]
                kind = stat.S_IFMT(member.external_attr >> 16)
                if member.is_dir() or kind not in {0, stat.S_IFREG}:
                    raise InstallError(f"Archive member is not a regular file: {member_name}")
                with bundle.open(member) as source, (destination / name).open("xb") as output:
                    shutil.copyfileobj(source, output)
    elif spec["archive"] == "tar.xz":
        with tarfile.open(archive, "r:xz") as bundle:
            entries = bundle.getmembers()
            for name, member_name in members.items():
                matches = [item for item in entries if item.name == member_name]
                if len(matches) != 1 or not matches[0].isfile():
                    raise InstallError(f"Archive needs one regular file: {member_name}")
                source = bundle.extractfile(matches[0])
                if source is None:
                    raise InstallError(f"Cannot read archive member: {member_name}")
                with source, (destination / name).open("xb") as output:
                    shutil.copyfileobj(source, output)
    else:
        raise InstallError(f"Unsupported archive type: {spec['archive']}")
    for name in members:
        (destination / name).chmod(0o755)


def verify_versions(directory: Path, names: dict, version: str) -> None:
    for name in names:
        result = subprocess.run(
            [str(directory / name), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        tool = Path(name).stem
        pattern = rf"^{tool} version {re.escape(version)}(?:[-\s]|$)"
        if result.returncode or not re.match(pattern, result.stdout):
            detail = (result.stderr or result.stdout).strip()[:500]
            raise InstallError(f"{name} did not run as version {version}: {detail}")


def valid_installation(destination: Path, spec: dict, version: str, key: str) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    try:
        receipt = json.loads((destination / RECEIPT).read_text(encoding="utf-8"))
        if (
            receipt["version"] != version
            or receipt["platform"] != key
            or receipt["archive_sha256"] != spec["sha256"]
            or set(receipt["binaries"]) != set(spec["members"])
        ):
            return False
        return all(
            not (destination / name).is_symlink()
            and sha256(destination / name) == digest
            for name, digest in receipt["binaries"].items()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def install(lock: dict, destination: Path, key: str) -> bool:
    """Return True for a new installation, False for an intact existing one."""
    version = lock["version"]
    spec = lock["platforms"][key]
    if destination.exists() or destination.is_symlink():
        if valid_installation(destination, spec, version, key):
            verify_versions(destination, spec["members"], version)
            return False
        raise InstallError(
            f"Preserving existing destination {destination}; it is not an intact locked "
            "installation. Choose a new --dest directory, or move that directory aside."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="veb-ffmpeg-download-") as downloaded:
        archive = Path(downloaded) / ("ffmpeg." + spec["archive"])
        print(f"Downloading FFmpeg {version} for {key}...", flush=True)
        download_archive(spec, archive)
        # A sibling staging directory permits one atomic rename on the same drive.
        with tempfile.TemporaryDirectory(prefix=".ffmpeg-install-", dir=destination.parent) as staged:
            payload = Path(staged) / "tools"
            payload.mkdir()
            extract_tools(archive, spec, payload)
            verify_versions(payload, spec["members"], version)
            receipt = {
                "version": version,
                "platform": key,
                "archive_sha256": spec["sha256"],
                "binaries": {name: sha256(payload / name) for name in spec["members"]},
            }
            (payload / RECEIPT).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            # Never replace an existing installation or merge arbitrary directories.
            if destination.exists() or destination.is_symlink():
                raise InstallError(f"Destination appeared during install: {destination}")
            payload.rename(destination)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=ROOT / ".tools" / "ffmpeg")
    args = parser.parse_args()
    destination = args.dest.expanduser().absolute()
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        key = platform_key(platform.system(), platform.machine(), sysconfig.get_config_var("MULTIARCH") or "")
        created = install(lock, destination, key)
    except (
        InstallError, OSError, ValueError, KeyError, tarfile.TarError,
        zipfile.BadZipFile, subprocess.TimeoutExpired, urllib.error.URLError,
    ) as error:
        print(f"FFmpeg setup: FAIL: {error}", file=sys.stderr)
        return 1
    state = "installed" if created else "already installed and verified"
    print(f"FFmpeg/ffprobe {lock['version']} {state}: {destination}")
    print("The shared check gate automatically uses the default .tools/ffmpeg directory.")
    if destination != ROOT / ".tools" / "ffmpeg":
        print("For this custom destination, add the directory to PATH before running the gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
