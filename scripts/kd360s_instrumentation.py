#!/usr/bin/env python3
"""Audit pinned stock firmware and an optional inert probe without writing an image."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys

from kd360s_firmware import DEFAULT_CACHE, TARGET, FirmwareError, Segment, VerifiedImage, verify_bytes


EXPECTED_SEGMENTS = (
    Segment(0, 0x3C110020, 32, 2651708),
    Segment(1, 0x3FC96990, 2651748, 24316),
    Segment(2, 0x40374000, 2676072, 10928),
    Segment(3, 0x42000020, 2687008, 1076852),
    Segment(4, 0x40376AB0, 3763868, 65244),
)
PROBE_TEXT = bytes.fromhex("14 70 06 42 36 41 00 20 a2 20 81 fd ff e0 08 00 1d f0")


@dataclass(frozen=True)
class Witness:
    name: str
    address: int
    expected: bytes


WITNESSES = (
    Witness("loop_task_pointer", 0x4203EC00, bytes.fromhex("14 70 06 42")),
    Witness("wifi_object_literal_a", 0x42000790, bytes.fromhex("28 21 ca 3f")),
    Witness("wifi_object_literal_b", 0x4203DE98, bytes.fromhex("28 21 ca 3f")),
    Witness("bss_start_literal", 0x403746D4, bytes.fromhex("90 c8 c9 3f")),
    Witness("bss_end_literal", 0x403746D0, bytes.fromhex("78 93 ca 3f")),
    Witness("loop_task_entry_bytes", 0x42067014, bytes.fromhex("36 41 00 10 11 20 25 e4 a4")),
    Witness("task_pointer_load_bytes", 0x42067065, bytes.fromhex("a1 e6 5e")),
)


def mapped_bytes(image: VerifiedImage, address: int, size: int) -> tuple[int, bytes]:
    """Read one unambiguous, initialized segment range by virtual address."""
    if size <= 0 or address < 0 or address + size > 0x100000000:
        raise FirmwareError("invalid virtual address range")
    overlaps = [
        segment for segment in image.segments
        if address < segment.address + segment.size and address + size > segment.address
    ]
    if len(overlaps) != 1:
        raise FirmwareError("virtual address range is unmapped or ambiguous")
    segment = overlaps[0]
    if address < segment.address or address + size > segment.address + segment.size:
        raise FirmwareError("virtual address range crosses a segment boundary")
    offset = segment.offset + address - segment.address
    if segment.offset < 0 or segment.offset + segment.size > len(image.data):
        raise FirmwareError("segment payload is outside the verified image")
    return offset, image.data[offset : offset + size]


def fact(value, evidence_class: str, **details) -> dict:
    return {"value": value, "evidence_class": evidence_class, **details}


def _audit_verified(image: VerifiedImage, probe_text: bytes | None) -> dict:
    if image.target != TARGET or len(image.data) != TARGET.size:
        raise FirmwareError("audit requires the pinned KD360S target")
    if image.data[1] != len(image.segments) or image.segments != EXPECTED_SEGMENTS:
        raise FirmwareError("segment layout differs from the audited layout")
    for segment in image.segments:
        if struct.unpack_from("<II", image.data, segment.offset - 8) != (segment.address, segment.size):
            raise FirmwareError("segment header disagrees with the verified map")

    witnesses = {}
    for witness in WITNESSES:
        offset, observed = mapped_bytes(image, witness.address, len(witness.expected))
        if observed != witness.expected:
            raise FirmwareError(f"source witness mismatch: {witness.name}")
        details = {
            "address": f"0x{witness.address:08x}",
            "file_offset": offset,
        }
        if len(observed) == 4:
            details["uint32_le"] = f"0x{struct.unpack('<I', observed)[0]:08x}"
        witnesses[witness.name] = fact(observed.hex(), "verified_image_bytes", **details)

    code = image.segments[3]
    following = image.segments[4]
    code_end = code.address + code.size
    proposed_start = (code_end + 15) & ~15
    page_end = (code_end + 0xFFFF) & ~0xFFFF
    proposed_offset = code.offset + proposed_start - code.address
    overlaps_following = (
        proposed_offset < following.offset + following.size
        and proposed_offset + len(PROBE_TEXT) > following.offset
    )
    if following.offset != code.offset + code.size + 8 or not overlaps_following:
        raise FirmwareError("candidate placement no longer has the audited overlap")

    probe = fact(None, "not_supplied")
    if probe_text is not None:
        if probe_text != PROBE_TEXT:
            raise FirmwareError("probe text must match the exact 18-byte inert prototype")
        probe = fact(
            {
                "size": len(probe_text),
                "sha256": hashlib.sha256(probe_text).hexdigest(),
                "entry_offset": 4,
                "proposed_entry": f"0x{proposed_start + 4:08x}",
                "additional_stack_frame_bytes": 32,
                "general_elf_validation": False,
                "runtime_tested": False,
            },
            "exact_prototype_bytes",
            interpretation=(
                "The fixed prototype forwards its argument through a windowed call to the "
                "old loopTask. It adds no telemetry. Matching raw text does not validate an "
                "ELF, its relocations, runtime ABI behavior, or available task stack."
            ),
        )

    return {
        "schema_version": 1,
        "status": "offline_audit_passed",
        "ready_to_flash": False,
        "source": fact(
            {"filename": TARGET.filename, "sha256": TARGET.sha256, "size": len(image.data)},
            "pinned_image_verification",
        ),
        "image_structure": fact(
            {
                "segment_count": len(image.segments),
                "code_segment": {
                    "index": code.index,
                    "address": f"0x{code.address:08x}",
                    "file_offset": code.offset,
                    "size": code.size,
                    "end_address_exclusive": f"0x{code_end:08x}",
                    "end_file_offset_exclusive": code.offset + code.size,
                },
                "next_segment_payload_offset": following.offset,
            },
            "verified_image_headers",
        ),
        "witnesses": witnesses,
        "task_pointer_interpretation": fact(
            "Ghidra static analysis identifies the literal loaded at 0x42067065 as the "
            "loopTask pointer passed during task creation. The literal at 0x4203ec00 "
            "contains 0x42067014.",
            "external_static_ghidra_analysis",
            independently_decoded_by_this_tool=False,
        ),
        "candidate_placement": fact(
            {
                "alignment": 16,
                "proposed_start_address": f"0x{proposed_start:08x}",
                "prospective_file_offset": proposed_offset,
                "assumed_page_size": 65536,
                "page_end_address_exclusive": f"0x{page_end:08x}",
                "bytes_until_assumed_page_end": page_end - proposed_start,
                "overlaps_existing_segment_payload": overlaps_following,
                "requires_insertion_and_shift": True,
                "runtime_mmu_mapping_proven": False,
            },
            "layout_arithmetic_only",
            limitation=(
                "The page remainder is arithmetic, not verified free executable memory. "
                "Writing the probe at the prospective file offset would overwrite segment 4. "
                "No bytes are inserted, shifted, or overwritten by this tool."
            ),
        ),
        "probe_text": probe,
        "blockers": [
            "Device bootloader, partition table, eFuse security state, and recovery are unverified.",
            "Actual OTA slot capacity and bootloader MMU mapping are unverified.",
            "Probe runtime ABI behavior and loopTask stack headroom are unverified.",
            "No persistent Wi-Fi worker, reconnect logic, telemetry, or shot archive exists in this probe.",
        ],
    }


def audit_bytes(data: bytes, probe_text: bytes | None = None) -> dict:
    """Require the production pin before interpreting any address or optional probe."""
    return _audit_verified(verify_bytes(data), probe_text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--probe-text", type=Path, help="raw .text of the exact inert prototype")
    args = parser.parse_args(argv)
    try:
        with args.path.open("rb") as source:
            data = source.read(TARGET.size + 1)
        probe_text = None
        if args.probe_text is not None:
            with args.probe_text.open("rb") as source:
                probe_text = source.read(len(PROBE_TEXT) + 1)
        report = audit_bytes(data, probe_text)
    except (OSError, FirmwareError) as error:
        print(json.dumps({"schema_version": 1, "status": "audit_rejected", "ready_to_flash": False, "error": str(error)}))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
