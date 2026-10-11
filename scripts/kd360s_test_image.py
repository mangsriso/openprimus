#!/usr/bin/env python3
"""Build an offline experimental image; this does not establish flash readiness."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile

from kd360s_firmware import DEFAULT_CACHE, TARGET, FirmwareError, Target, verify_bytes
from kd360s_instrumentation import EXPECTED_SEGMENTS, audit_bytes, mapped_bytes
from kd360s_hooks import plan_recorder_hooks
from kd360s_wifi_guard import plan_wifi_guard
from kd360s_wifi_hooks import plan_wifi_hooks

PAYLOAD_START = 0x42106EA0
PAYLOAD_LIMIT = 0x42110000
TASK_POINTER = 0x4203EC00
_TRIAL_FUNCTION = 0x42100F70
_TRIAL_EXPECTED = bytes.fromhex("3641000c021df000")
_TRIAL_BYTE_OFFSET = 0x390F74
_TRIAL_REPLACEMENT = 0x12
MAX_ELF_SIZE = 16 * 1024 * 1024
ELF_HEADER = struct.Struct("<16sHHIIIIIHHHHHH")
SECTION_HEADER = struct.Struct("<IIIIIIIIII")
SYMBOL = struct.Struct("<IIIBBH")
HARDWARE_GATES = [
    "Device bootloader, partition table and OTA slot capacity are unverified.",
    "Device eFuse security state and image acceptance are unverified.",
    "Executable MMU mapping, runtime ABI and stack/heap headroom are unverified.",
    "Recovery independent of the stock application's Wi-Fi/DFU screen is unverified.",
    "Payload behavior, fixed-address imports and effects on machine control require review and hardware tests.",
]


@dataclass(frozen=True)
class Section:
    name: str
    address: int
    flags: int
    data: bytes


@dataclass(frozen=True)
class ElfSymbol:
    name: str
    address: int
    size: int
    info: int
    section_name: str


@dataclass(frozen=True)
class Payload:
    entry: int
    flags: int
    sections: tuple[Section, ...]
    data: bytes
    entry_stack_bytes: int
    symbols: tuple[ElfSymbol, ...]


def _slice(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise FirmwareError(f"ELF {label} is outside the file")
    return data[offset : offset + size]


def _name(strings: bytes, offset: int) -> str:
    if offset >= len(strings):
        raise FirmwareError("ELF string index is outside its table")
    end = strings.find(b"\0", offset)
    if end < 0:
        raise FirmwareError("ELF string is not terminated")
    try:
        return strings[offset:end].decode("ascii")
    except UnicodeDecodeError as error:
        raise FirmwareError("ELF section/symbol name is not ASCII") from error


def parse_elf(data: bytes) -> Payload:
    """Accept a narrow resolved ELF, copying allocated sections, never PT_LOAD headers."""
    if len(data) > MAX_ELF_SIZE:
        raise FirmwareError("ELF exceeds the 16 MiB input limit")
    fields = ELF_HEADER.unpack(_slice(data, 0, ELF_HEADER.size, "header"))
    ident, kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, shstr = fields
    if ident[:9] != b"\x7fELF\x01\x01\x01\x00\x00" or any(ident[9:]):
        raise FirmwareError("ELF must be ELF32 little-endian System V version 1")
    if (kind, machine, version, ehsize) != (2, 94, 1, 52):
        raise FirmwareError("ELF must be an Xtensa ET_EXEC with a standard ELF32 header")
    # esp-2021r2 binutils defines only machine, XT_INSN and XT_LIT flags; no ABI bit.
    if flags & ~0x300:
        raise FirmwareError("ELF contains unsupported Xtensa machine/flags")
    if shsize != 40 or not 1 < shnum <= 4096 or not 0 < shstr < shnum:
        raise FirmwareError("ELF requires an ordinary section table and section-name table")
    section_table = _slice(data, shoff, shnum * shsize, "section table")
    headers = [SECTION_HEADER.unpack_from(section_table, i * shsize) for i in range(shnum)]
    if any(headers[0]):
        raise FirmwareError("ELF null section must be zero")
    if headers[shstr][1] != 3:
        raise FirmwareError("ELF section-name table must be STRTAB")
    names = _slice(data, headers[shstr][4], headers[shstr][5], "section names")
    if not names or names[0] != 0:
        raise FirmwareError("ELF section-name table lacks its null string")
    if phnum:
        if phsize != 32 or phnum > 4096:
            raise FirmwareError("ELF program-header table is malformed")
        table = _slice(data, phoff, phsize * phnum, "program headers")
        for i in range(phnum):
            ptype, offset, _, _, filesz, memsz, _, _ = struct.unpack_from("<IIIIIIII", table, i * phsize)
            if ptype not in (0, 1, 0x6474E551):
                raise FirmwareError("ELF dynamic/interpreter/unsupported program header")
            if filesz > memsz:
                raise FirmwareError("ELF program file size exceeds memory size")
            _slice(data, offset, filesz, "program contents")
    sections = []
    symbols = []
    symbol_tables = 0
    for header in headers[1:]:
        name_index, stype, sflags, address, offset, size, link, _, alignment, entsize = header
        name = _name(names, name_index)
        contents = b"" if stype == 8 else _slice(data, offset, size, name)
        if stype in (4, 9) and size:
            raise FirmwareError("ELF must have no remaining relocations")
        if stype in (6, 11):
            raise FirmwareError("ELF dynamic linking is not supported")
        if stype == 2:
            symbol_tables += 1
            if entsize != SYMBOL.size or size < SYMBOL.size or size % SYMBOL.size or not 0 < link < shnum:
                raise FirmwareError("ELF symbol table is malformed")
            string_header = headers[link]
            if string_header[1] != 3:
                raise FirmwareError("ELF symbol names must use STRTAB")
            strings = _slice(data, string_header[4], string_header[5], "symbol names")
            if not strings or strings[0] or any(contents[:SYMBOL.size]):
                raise FirmwareError("ELF symbol table lacks its null symbol/string")
            for position in range(SYMBOL.size, size, SYMBOL.size):
                symbol_name, symbol_address, symbol_size, symbol_info, _, section_index = SYMBOL.unpack_from(contents, position)
                label = _name(strings, symbol_name)
                if section_index == 0:
                    raise FirmwareError(f"ELF unresolved symbol: {label}")
                if section_index not in (0xFFF1,) and not 0 < section_index < shnum:
                    raise FirmwareError("ELF common/extended/invalid symbol section")
                symbols.append(ElfSymbol(label, symbol_address, symbol_size, symbol_info,
                                         _name(names, headers[section_index][0]) if section_index < shnum else ""))
        if sflags & 2:
            if name not in (".text", ".rodata") or stype != 1 or sflags not in (2, 6) or not size:
                raise FirmwareError(f"ELF allocated section is not nonwritable .text/.rodata: {name}")
            if (name == ".text" and sflags != 6) or (name == ".rodata" and sflags != 2):
                raise FirmwareError("ELF text/rodata permissions are invalid")
            if alignment and (alignment & (alignment - 1) or address % alignment):
                raise FirmwareError("ELF allocated section alignment is invalid")
            if address < PAYLOAD_START or address + size > PAYLOAD_LIMIT:
                raise FirmwareError("ELF allocated section lies outside the candidate tail")
            sections.append(Section(name, address, sflags, contents))
        elif stype == 8 and size:
            raise FirmwareError("ELF NOBITS storage is not supported")
    if symbol_tables != 1:
        raise FirmwareError("ELF requires exactly one symbol table to check unresolved symbols")
    sections.sort(key=lambda item: item.address)
    if not sections or len({section.name for section in sections}) != len(sections):
        raise FirmwareError("ELF requires unique allocated section names")
    end = PAYLOAD_START
    for section in sections:
        if section.address < end:
            raise FirmwareError("ELF allocated sections overlap")
        end = section.address + len(section.data)
    code = next((s for s in sections if s.flags & 4 and s.address <= entry < s.address + len(s.data)), None)
    if code is None:
        raise FirmwareError("ELF entry must lie in an allocated executable section")
    instruction = code.data[entry - code.address : entry - code.address + 3]
    if len(instruction) != 3 or instruction[0] != 0x36 or instruction[1] & 15 != 1:
        raise FirmwareError("ELF entry must begin with a windowed ENTRY a1 instruction")
    stack_bytes = ((instruction[1] >> 4) | instruction[2] << 4) * 8
    if stack_bytes < 16 or stack_bytes % 16:
        raise FirmwareError("ELF entry stack frame must preserve 16-byte alignment")
    blob = bytearray(end - PAYLOAD_START)
    for section in sections:
        start = section.address - PAYLOAD_START
        blob[start : start + len(section.data)] = section.data
    return Payload(entry, flags, tuple(sections), bytes(blob), stack_bytes, tuple(symbols))


def _seal(body: bytes) -> bytes:
    checksum = 0xEF
    offset = 24
    for _ in range(body[1]):
        _, size = struct.unpack_from("<II", body, offset)
        offset += 8
        for value in body[offset : offset + size]:
            checksum ^= value
        offset += size
    if offset != len(body):
        raise FirmwareError("rebuilt ESP segment boundaries disagree with body size")
    checksum_end = (len(body) + 16) & ~15
    sealed = body + bytes(checksum_end - len(body) - 1) + bytes([checksum])
    return sealed + hashlib.sha256(sealed).digest()


def build_image(stock: bytes, elf: bytes, *, trial_defer_confirm: bool = False,
                recorder_hooks: bool = False, wifi_guard: bool = False,
                wifi_hooks: bool = False) -> tuple[bytes, dict]:
    """Build only from the exact stock pin, then undo the edits and verify that pin."""
    source_audit = audit_bytes(stock)
    image = verify_bytes(stock)
    payload = parse_elf(elf)
    hooks = plan_recorder_hooks(image, payload) if recorder_hooks else None
    if wifi_hooks and not wifi_guard:
        raise FirmwareError("Wi-Fi hooks require their coupled pending-consumer guard")
    wifi_hook_plan = plan_wifi_hooks(image, payload) if wifi_hooks else None
    guard = plan_wifi_guard(image, payload, wifi_hook_plan=wifi_hook_plan) if wifi_guard else None
    patches = [patch for plan in (hooks, guard, wifi_hook_plan) if plan for patch in plan["patches"]]
    code = image.segments[3]
    insertion_offset = code.offset + code.size
    if trial_defer_confirm:
        function_offset, function_bytes = mapped_bytes(image, _TRIAL_FUNCTION, len(_TRIAL_EXPECTED))
        if not (0x42000000 <= code.address <= _TRIAL_FUNCTION
                and _TRIAL_FUNCTION + len(_TRIAL_EXPECTED) <= code.address + code.size <= 0x44000000):
            raise FirmwareError("trial confirmation function must lie wholly in the IROM code segment")
        if function_offset + 4 != _TRIAL_BYTE_OFFSET or function_offset + len(_TRIAL_EXPECTED) > insertion_offset:
            raise FirmwareError("trial confirmation function has an unexpected file offset")
        if function_bytes != _TRIAL_EXPECTED:
            raise FirmwareError("trial confirmation function bytes differ from the stock witness")
    padding = PAYLOAD_START - (code.address + code.size)
    inserted = bytes(padding) + payload.data
    # ESP segment lengths are word-aligned, including a payload whose last opcode is short.
    inserted += bytes(-(code.size + len(inserted)) % 4)
    if code.address + code.size + len(inserted) > PAYLOAD_LIMIT:
        raise FirmwareError("aligned payload exceeds the candidate tail")
    pointer_offset, old_pointer = mapped_bytes(image, TASK_POINTER, 4)
    body = bytearray(stock[:insertion_offset] + inserted + stock[insertion_offset:image.payload_end])
    struct.pack_into("<I", body, code.offset - 4, code.size + len(inserted))
    struct.pack_into("<I", body, pointer_offset, payload.entry)
    if trial_defer_confirm:
        body[_TRIAL_BYTE_OFFSET] = _TRIAL_REPLACEMENT
    for patch in patches:
        offset = patch["file_offset"]
        replacement = bytes.fromhex(patch["after_hex"])
        body[offset:offset + len(replacement)] = replacement
    result = _seal(bytes(body))
    result_target = Target("firmware.experimental.bin", len(result), hashlib.sha256(result).hexdigest(), 9, 5)
    rebuilt = verify_bytes(result, result_target)
    expected_segments = list(EXPECTED_SEGMENTS)
    expected_segments[3] = type(code)(3, code.address, code.offset, code.size + len(inserted))
    last = image.segments[4]
    expected_segments[4] = type(last)(4, last.address, last.offset + len(inserted), last.size)
    if rebuilt.segments != tuple(expected_segments):
        raise FirmwareError("rebuilt image segment map differs from the planned insertion")
    restored = bytearray(result[:insertion_offset] + result[insertion_offset + len(inserted):rebuilt.payload_end])
    struct.pack_into("<I", restored, code.offset - 4, code.size)
    restored[pointer_offset:pointer_offset + 4] = old_pointer
    if trial_defer_confirm:
        restored[_TRIAL_BYTE_OFFSET] = _TRIAL_EXPECTED[4]
    for patch in patches:
        offset = patch["file_offset"]
        original = bytes.fromhex(patch["before_hex"])
        restored[offset:offset + len(original)] = original
    # Preserve the original checksum padding too, so the comparison covers every stock byte.
    restored.extend(stock[image.payload_end:])
    verify_bytes(bytes(restored))
    if bytes(restored) != stock:
        raise FirmwareError("reverse verification did not reproduce the exact stock image")
    manifest = {
        "schema_version": 1,
        "status": "experimental_image_built_offline",
        "ready_to_flash": False,
        "source": {"filename": TARGET.filename, "sha256": TARGET.sha256, "size": len(stock)},
        "elf": {"sha256": hashlib.sha256(elf).hexdigest(), "size": len(elf), "entry": f"0x{payload.entry:08x}",
                "e_flags": f"0x{payload.flags:08x}", "entry_stack_bytes": payload.entry_stack_bytes,
                "windowed_entry_instruction_checked": True, "whole_payload_abi_proven": False,
                "sections": [{"name": s.name, "address": f"0x{s.address:08x}", "size": len(s.data),
                              "sha256": hashlib.sha256(s.data).hexdigest()} for s in payload.sections]},
        "output": {"filename": result_target.filename, "sha256": result_target.sha256, "size": len(result)},
        "changes": {
            "insertion": {"file_offset": insertion_offset, "address": f"0x{code.address + code.size:08x}",
                          "size": len(inserted), "leading_padding": padding,
                          "sha256": hashlib.sha256(inserted).hexdigest()},
            "code_segment_size": {"file_offset": code.offset - 4, "before": code.size, "after": code.size + len(inserted)},
            "task_pointer": {"address": f"0x{TASK_POINTER:08x}", "file_offset": pointer_offset,
                             "before_hex": old_pointer.hex(), "after_hex": struct.pack("<I", payload.entry).hex()},
            "last_segment": {"payload_offset_before": last.offset, "payload_offset_after": last.offset + len(inserted),
                             "payload_unchanged": True},
            "checksum": {"offset_before": image.checksum_end - 1, "offset_after": rebuilt.checksum_end - 1,
                         "value_before": stock[image.checksum_end - 1], "value_after": result[rebuilt.checksum_end - 1]},
            "appended_sha256": {"offset_before": image.checksum_end, "offset_after": rebuilt.checksum_end,
                                "before": stock[image.checksum_end:].hex(), "after": result[rebuilt.checksum_end:].hex()},
        },
        "checks": {"source_audit": source_audit["status"], "esp_integrity_valid": True,
                   "reverse_to_pinned_stock": True, "payload_runtime_tested": False},
        "unresolved_hardware_gates": HARDWARE_GATES,
    }
    if trial_defer_confirm:
        manifest["changes"]["trial_defer_confirm"] = {
            "function_address": f"0x{_TRIAL_FUNCTION:08x}",
            "byte_address": f"0x{_TRIAL_FUNCTION + 4:08x}",
            "file_offset": _TRIAL_BYTE_OFFSET,
            "before_hex": _TRIAL_EXPECTED[4:5].hex(),
            "after_hex": bytes([_TRIAL_REPLACEMENT]).hex(),
            "expected_function_hex": _TRIAL_EXPECTED.hex(),
            "intent": "defer_stock_early_ota_confirmation",
            "mark_valid_added": False,
            "rollback_verified_on_device": False,
            "rollback_condition": "Rollback depends on the device bootloader configuration and OTA slot state.",
        }
    if hooks:
        manifest["changes"]["recorder_hooks"] = hooks
    if guard:
        manifest["changes"]["wifi_guard"] = guard
    if wifi_hook_plan:
        manifest["changes"]["wifi_hooks"] = wifi_hook_plan
    return result, manifest


def write_package(output: Path, binary: bytes, manifest: dict, *, elf: bytes | None = None) -> Path:
    """Publish matching files without replacing paths; write a new manifest last."""
    files = {"firmware.experimental.bin": binary}
    if elf is not None:
        if (manifest.get("elf", {}).get("size") != len(elf)
                or manifest.get("elf", {}).get("sha256") != hashlib.sha256(elf).hexdigest()):
            raise FirmwareError("retained ELF differs from the package manifest")
        files["payload.elf"] = elf
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    created_directory = False
    present = set()
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir() or not {p.name for p in output.iterdir()} <= set(files):
            raise FirmwareError("existing output is not the identical package")
        present = {p.name for p in output.iterdir()}
        for name in present:
            contents = files[name]
            path = output / name
            if path.is_symlink() or not path.is_file():
                raise FirmwareError("existing output differs; refusing to overwrite")
            with path.open("rb") as stream:
                if stream.read(len(contents) + 1) != contents:
                    raise FirmwareError("existing output differs; refusing to overwrite")
        if present == set(files):
            return output
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".kd360s-build-") as temporary:
        staging = Path(temporary)
        for name, contents in files.items():
            with (staging / name).open("xb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
        if not output.exists():
            output.mkdir()
            created_directory = True
        published = []
        try:
            for name in files:
                if name in present:
                    continue
                os.link(staging / name, output / name)
                published.append(output / name)
        except BaseException:
            for path in published:
                path.unlink()
            if created_directory:
                output.rmdir()
            raise
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--elf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new local package directory")
    parser.add_argument("--trial-defer-confirm", action="store_true",
                        help="offline trial that suppresses early stock OTA confirmation; rollback is not guaranteed")
    parser.add_argument("--recorder-hooks", action="store_true",
                        help="install the four pinned recorder entry detours from ELF wrapper symbols")
    parser.add_argument("--wifi-guard", action="store_true",
                        help="install the pinned OTA disconnect pending-flag consumer guard")
    parser.add_argument("--wifi-hooks", action="store_true",
                        help="install the four pinned Wi-Fi API detours and original trampolines")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in (args.stock.resolve(), args.elf.resolve()):
            raise FirmwareError("output cannot replace an input")
        with args.stock.open("rb") as source:
            stock = source.read(TARGET.size + 1)
        with args.elf.open("rb") as source:
            elf = source.read(MAX_ELF_SIZE + 1)
        binary, manifest = build_image(stock, elf, trial_defer_confirm=args.trial_defer_confirm,
                                       recorder_hooks=args.recorder_hooks, wifi_guard=args.wifi_guard,
                                       wifi_hooks=args.wifi_hooks)
        write_package(args.output, binary, manifest, elf=elf)
        print(json.dumps({"status": manifest["status"], "ready_to_flash": False,
                          "output": str(args.output), "sha256": manifest["output"]["sha256"]}, indent=2))
    except (OSError, FirmwareError) as error:
        print(json.dumps({"status": "build_rejected", "ready_to_flash": False, "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
