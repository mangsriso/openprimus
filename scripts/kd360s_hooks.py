"""Fixed entry detours for the pinned KD360S recorder; no arbitrary patch API."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import TYPE_CHECKING

from kd360s_firmware import FirmwareError, VerifiedImage
from kd360s_instrumentation import mapped_bytes

if TYPE_CHECKING:
    from kd360s_test_image import Payload


@dataclass(frozen=True)
class Hook:
    name: str
    address: int
    expected: bytes
    continuations: tuple[int, ...]


HOOKS = (
    Hook("rx", 0x42005430, bytes.fromhex("364100812ceb91d2eb88089809971808"),
         (0x42005440, 0x42005449)),
    Hook("start", 0x42005918, bytes.fromhex("368100e1b3eac2a1a10c4a0c06d1b1ea"),
         (0x42005928,)),
    Hook("stop", 0x420031B0, bytes.fromhex("36410081b8f3921807ccf90c19925807"),
         (0x420031C0, 0x420031CC)),
    Hook("pressure", 0x42003DA4, bytes.fromhex("3641008022230012ca21f2f0030201"),
         (0x42003DB3,)),
)

# Values read by the displaced L32R instructions, not their literal addresses.
LITERALS = (
    ("rx_lit8", 0x420000E4, 0x3FC9D104),
    ("rx_lit9", 0x42000380, 0x3FCA8084),
    ("start_lit14", 0x420003E8, 0x3C11152E),
    ("start_lit13", 0x420003EC, 0x3C11170F),
    ("stop_lit8", 0x42000094, 0x3FC9CB00),
    ("pressure_lit2", 0x42000178, 0x3FC9FF04),
)


def plan_recorder_hooks(image: VerifiedImage, payload: Payload) -> dict:
    """Check stock witnesses and ELF entry targets before constructing fixed edits."""
    code = image.segments[3]
    patches = []
    destinations = set()
    for hook in HOOKS:
        if not code.address <= hook.address < hook.address + len(hook.expected) <= code.address + code.size:
            raise FirmwareError(f"recorder hook is outside stock IROM: {hook.name}")
        offset, actual = mapped_bytes(image, hook.address, len(hook.expected))
        if actual != hook.expected:
            raise FirmwareError(f"recorder hook stock bytes differ: {hook.name}")
        name = "wrap_" + hook.name
        matches = [symbol for symbol in payload.symbols if symbol.name == name]
        if len(matches) != 1:
            raise FirmwareError(f"recorder ELF requires exactly one symbol: {name}")
        symbol = matches[0]
        if symbol.info >> 4 != 1 or symbol.info & 15 not in (0, 2) or symbol.section_name != ".text":
            raise FirmwareError(f"recorder wrapper must be a global text function: {name}")
        section = next((section for section in payload.sections if section.name == ".text"), None)
        size = max(3, symbol.size)
        if (section is None or symbol.address % 4
                or not section.address <= symbol.address < symbol.address + size <= section.address + len(section.data)):
            raise FirmwareError(f"recorder wrapper lies outside executable payload: {name}")
        if symbol.address in destinations:
            raise FirmwareError("recorder wrappers must have distinct entry addresses")
        destinations.add(symbol.address)
        position = symbol.address - section.address
        instruction = section.data[position:position + 3]
        if instruction[0] != 0x36 or instruction[1] & 15 != 1:
            raise FirmwareError(f"recorder wrapper must begin with windowed ENTRY a1: {name}")
        frame = ((instruction[1] >> 4) | instruction[2] << 4) * 8
        if frame < 80 or frame % 16:
            raise FirmwareError(f"recorder wrapper needs an aligned frame of at least 80 bytes: {name}")
        # J +8; padding; target literal; L32R a9, entry+4; JX a9; unreachable padding.
        # All pinned entries are 4-byte aligned. a9 is replaced by CALL8 ENTRY.
        replacement = (bytes.fromhex("06010000") + struct.pack("<I", symbol.address)
                       + bytes.fromhex("91ffffa00900") + bytes(len(hook.expected) - 14))
        patches.append({
            "name": hook.name, "address": f"0x{hook.address:08x}", "file_offset": offset,
            "before_hex": actual.hex(), "after_hex": replacement.hex(),
            "wrapper_symbol": name, "wrapper_address": f"0x{symbol.address:08x}",
            "wrapper_stack_bytes": frame,
            "continuations": [f"0x{address:08x}" for address in hook.continuations],
        })
    literals = []
    for name, address, value in LITERALS:
        offset, actual = mapped_bytes(image, address, 4)
        if actual != struct.pack("<I", value):
            raise FirmwareError(f"recorder literal differs from stock witness: {name}")
        literals.append({"name": name, "address": f"0x{address:08x}", "file_offset": offset,
                         "value_hex": f"0x{value:08x}", "bytes_hex": actual.hex()})
    return {"scheme": "kd360s_1.0.13_hk_call8_entry_detours_v1", "patches": patches,
            "literal_witnesses": literals, "runtime_verified": False,
            "inbound_abi": "call8_only", "pre_entry_scratch": "a9"}
