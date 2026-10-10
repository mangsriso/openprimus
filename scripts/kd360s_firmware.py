#!/usr/bin/env python3
"""Fetch, verify, and extract the pinned KD360S 1.0.13 HK ESP image."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.request


@dataclass(frozen=True)
class Target:
    filename: str
    size: int
    sha256: str
    chip_id: int
    segment_count: int


TARGET = Target(
    "KD360S_OTA_1.0.13_HK_20260630.bin",
    3829152,
    "ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4",
    9,
    5,
)
BASE_URLS = (
    "https://wpmfirmwareprod.blob.core.windows.net/firmware/",
    "https://wpm-firmware-updates-g9hfehacbzaphxfj.a01.azurefd.net/firmware/",
)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / "firmware_binary" / "kd360s" / TARGET.filename
DEFAULT_OUTPUT = ROOT / "analysis" / "extracted" / "kd360s" / "1.0.13_HK"
SOCKET_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120


class FirmwareError(ValueError):
    """A firmware image or output does not meet the pinned contract."""


@dataclass(frozen=True)
class Segment:
    index: int
    address: int
    offset: int
    size: int

    @property
    def filename(self) -> str:
        return f"seg{self.index:02d}_0x{self.address:08x}.bin"


@dataclass(frozen=True)
class VerifiedImage:
    data: bytes
    target: Target
    segments: tuple[Segment, ...]
    payload_end: int
    checksum_end: int


def verify_bytes(data: bytes, target: Target = TARGET) -> VerifiedImage:
    """Return immutable bytes and segment boundaries only after all checks pass."""
    data = bytes(data)
    if len(data) != target.size:
        raise FirmwareError(f"size mismatch: expected {target.size}, got {len(data)}")
    if hashlib.sha256(data).hexdigest() != target.sha256:
        raise FirmwareError("pinned SHA-256 mismatch")
    if len(data) < 24:
        raise FirmwareError("truncated ESP header")
    if data[0] != 0xE9:
        raise FirmwareError("invalid ESP magic")
    if data[1] != target.segment_count:
        raise FirmwareError(f"segment count mismatch: expected {target.segment_count}")
    if struct.unpack_from("<H", data, 12)[0] != target.chip_id:
        raise FirmwareError(f"chip ID mismatch: expected {target.chip_id}")
    if data[23] != 1:
        raise FirmwareError("ESP appended SHA-256 flag must be 1")

    offset = 24
    checksum = 0xEF
    segments = []
    for index in range(target.segment_count):
        if offset + 8 > len(data):
            raise FirmwareError(f"truncated segment {index} header")
        address, size = struct.unpack_from("<II", data, offset)
        offset += 8
        if address + size > 0x100000000:
            raise FirmwareError(f"segment {index} address overflow")
        if size > len(data) - offset:
            raise FirmwareError(f"truncated segment {index} payload")
        segments.append(Segment(index, address, offset, size))
        for value in data[offset : offset + size]:
            checksum ^= value
        offset += size

    checksum_end = (offset + 1 + 15) & ~15
    if checksum_end > len(data):
        raise FirmwareError("truncated checksum block")
    if data[checksum_end - 1] != checksum:
        raise FirmwareError("ESP XOR checksum mismatch")
    if checksum_end + 32 > len(data):
        raise FirmwareError("truncated appended SHA-256")
    if checksum_end + 32 != len(data):
        raise FirmwareError("unexpected trailing bytes")
    if hashlib.sha256(data[:checksum_end]).digest() != data[checksum_end:]:
        raise FirmwareError("ESP appended SHA-256 mismatch")
    return VerifiedImage(data, target, tuple(segments), offset, checksum_end)


def verify_file(path: Path, target: Target = TARGET) -> VerifiedImage:
    with Path(path).open("rb") as source:
        return verify_bytes(source.read(target.size + 1), target)


def fetch(
    cache_path: Path = DEFAULT_CACHE,
    *,
    target: Target = TARGET,
    opener=urllib.request.urlopen,
) -> Path:
    """Reuse a verified cache or atomically replace it with a verified download."""
    cache_path = Path(cache_path)
    try:
        verify_file(cache_path, target)
        return cache_path
    except (OSError, FirmwareError):
        pass
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    failures = []
    for base_url in BASE_URLS:
        url = f"{base_url}KD360S/{target.filename}"
        temporary_path = None
        try:
            started = time.monotonic()
            with opener(url, timeout=SOCKET_TIMEOUT) as response:
                if not response.geturl().startswith("https://"):
                    raise FirmwareError("download redirected away from HTTPS")
                length = response.headers.get("Content-Length")
                if length is not None and length != str(target.size):
                    raise FirmwareError("download Content-Length mismatch")
                with tempfile.NamedTemporaryFile(
                    dir=cache_path.parent, prefix=f".{target.filename}.", delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    received = 0
                    while True:
                        if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                            raise FirmwareError("download time limit exceeded")
                        chunk = response.read1(min(65536, target.size + 1 - received))
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > target.size:
                            raise FirmwareError("download exceeds pinned size")
                        temporary.write(chunk)
            verify_file(temporary_path, target)
            os.replace(temporary_path, cache_path)
            return cache_path
        except (OSError, urllib.error.URLError, http.client.HTTPException, FirmwareError) as error:
            failures.append(f"{base_url}: {error}")
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
    raise FirmwareError("all firmware sources failed; " + "; ".join(failures))


def metadata(image: VerifiedImage) -> dict:
    return {
        "filename": image.target.filename,
        "size": len(image.data),
        "sha256": image.target.sha256,
        "chip_id": image.target.chip_id,
        "entry_point": f"0x{struct.unpack_from('<I', image.data, 4)[0]:08x}",
        "payload_end": image.payload_end,
        "checksum_offset": image.checksum_end - 1,
        "appended_sha256_offset": image.checksum_end,
        "segments": [
            {
                "index": segment.index,
                "filename": segment.filename,
                "address": f"0x{segment.address:08x}",
                "offset": segment.offset,
                "size": segment.size,
                "sha256": hashlib.sha256(
                    image.data[segment.offset : segment.offset + segment.size]
                ).hexdigest(),
            }
            for segment in image.segments
        ],
    }


def extract(image: VerifiedImage, output_dir: Path = DEFAULT_OUTPUT) -> Path:
    """Publish all segments together; an existing result must match exactly."""
    output_dir = Path(output_dir)
    files = {
        segment.filename: image.data[segment.offset : segment.offset + segment.size]
        for segment in image.segments
    }
    files["metadata.json"] = (json.dumps(metadata(image), indent=2) + "\n").encode()
    if output_dir.exists() or output_dir.is_symlink():
        if output_dir.is_symlink() or not output_dir.is_dir():
            raise FirmwareError(f"refusing to overwrite existing output: {output_dir}")
        matches = {item.name for item in output_dir.iterdir()} == set(files)
        for name, expected in files.items():
            path = output_dir / name
            if not matches or path.is_symlink() or not path.is_file():
                matches = False
                break
            with path.open("rb") as existing:
                if existing.read(len(expected) + 1) != expected:
                    matches = False
                    break
        if matches:
            return output_dir
        raise FirmwareError(f"existing output differs; refusing to overwrite: {output_dir}")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir.parent, prefix=".kd360s-") as stage:
        staging_dir = Path(stage)
        for name, contents in files.items():
            (staging_dir / name).write_bytes(contents)
        if output_dir.exists() or output_dir.is_symlink():
            raise FirmwareError(f"output appeared during extraction: {output_dir}")
        staging_dir.rename(output_dir)
    return output_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch", help="download the pinned image or verify its cache")
    for command in ("verify", "extract"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args(argv)
    try:
        if args.command == "fetch":
            print(fetch())
        else:
            image = verify_file(args.path)
            if args.command == "extract":
                print(extract(image))
            else:
                print(json.dumps(metadata(image), indent=2))
    except (OSError, FirmwareError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
