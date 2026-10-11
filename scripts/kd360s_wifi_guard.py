"""One pinned OTA pending-flag splice; the stock disconnect callback is unchanged."""
from __future__ import annotations

import hashlib
import struct
from typing import TYPE_CHECKING

from kd360s_firmware import FirmwareError, VerifiedImage
from kd360s_instrumentation import mapped_bytes

if TYPE_CHECKING:
    from kd360s_test_image import Payload

ADDRESS = 0x4201D3CC
EXPECTED = bytes.fromhex("c0200032040030307421158b161306")
SYMBOL = "wifi_pending_guard"
LITERAL_BYTES = 28
FUNCTION_BYTES = 72
# Fixed relative instructions and absolute stock data/continuation literals.
# This block is position independent when its start is aligned to four bytes.
GUARD_BLOCK = bytes.fromhex(
    "04d1c93f6485ca3fcc84ca3f9c84ca3f60cac93fdbd301423dd40142"
    "c0200032040030307421faff16c30281f5ff82280016a80191f4ff922900971820"
    "91f2ff92290097181791f1ff92290097180e32a000324400c0200081f0ffa00800"
    "81edffa00800")
WITNESSES = (
    ("predecessor_load_a4", 0x4201D3C9, bytes.fromhex("41b491")),
    ("pending", 0x42001A9C, struct.pack("<I", 0x3FCA1514)),
    ("reason", 0x42001A98, struct.pack("<I", 0x3FCA1515)),
    ("flags", 0x4200002C, struct.pack("<I", 0x3FC9CA60)),
    ("active_screen", 0x420000E4, struct.pack("<I", 0x3FC9D104)),
    ("ota_screen", 0x42000800, struct.pack("<I", 0x3FCA8564)),
    ("wifi_screen", 0x42000804, struct.pack("<I", 0x3FCA84CC)),
    ("password_screen", 0x42000808, struct.pack("<I", 0x3FCA849C)),
    ("callback_pointer", 0x42001BD8, struct.pack("<I", 0x4201BA94)),
)
CALLBACK_ADDRESS = 0x4201BA94
CALLBACK_SIZE = 153
CALLBACK_SHA256 = "49b9765fe4343e84ed8843f2a2d144f3dbb374fea7ba38a62f43b676f91558a7"


GUARD_HOOKS_BLOCK = bytes.fromhex(
    "04d1c93f6485ca3fcc84ca3f9c84ca3f60cac93fe1d301423dd40142c020"
    "0032040030307421faff16c30281f5ff82280016a80191f4ff9229009718"
    "2091f2ff92290097181791f1ff92290097180e32a000324400c0200081f0"
    "ffa008000c030c0cbd0c81ecffa00800")


def plan_wifi_guard(image: VerifiedImage, payload: Payload, *, wifi_hook_plan: dict | None = None) -> dict:
    # The caller supplies only the independently validated fixed Wi-Fi hook plan.
    coupled = wifi_hook_plan is not None
    expected = EXPECTED + bytes.fromhex("0c030c0cbd0c") if coupled else EXPECTED
    block = GUARD_HOOKS_BLOCK if coupled else GUARD_BLOCK
    function_bytes = FUNCTION_BYTES + 6 if coupled else FUNCTION_BYTES
    code = image.segments[3]
    if not code.address <= ADDRESS < ADDRESS + len(expected) <= code.address + code.size:
        raise FirmwareError("Wi-Fi guard splice lies outside stock IROM")
    offset, actual = mapped_bytes(image, ADDRESS, len(expected))
    if actual != expected:
        raise FirmwareError("Wi-Fi guard stock span differs")
    witnesses = []
    for name, address, expected in WITNESSES:
        position, value = mapped_bytes(image, address, len(expected))
        if value != expected:
            raise FirmwareError(f"Wi-Fi guard stock witness differs: {name}")
        witnesses.append({"name": name, "address": f"0x{address:08x}",
                          "file_offset": position, "bytes_hex": value.hex()})
    callback_offset, callback = mapped_bytes(image, CALLBACK_ADDRESS, CALLBACK_SIZE)
    if hashlib.sha256(callback).hexdigest() != CALLBACK_SHA256:
        raise FirmwareError("Wi-Fi guard stock callback differs")
    matches = [symbol for symbol in payload.symbols if symbol.name == SYMBOL]
    if len(matches) != 1:
        raise FirmwareError(f"Wi-Fi guard requires exactly one symbol: {SYMBOL}")
    symbol = matches[0]
    text = next((section for section in payload.sections if section.name == ".text"), None)
    start = symbol.address - LITERAL_BYTES
    if (symbol.info != 0x12 or symbol.section_name != ".text"
            or symbol.size != function_bytes or symbol.address % 4 or text is None
            or not text.address <= start < symbol.address + symbol.size <= text.address + len(text.data)):
        raise FirmwareError("Wi-Fi guard must be a bounded global text function with its literal block")
    position = start - text.address
    if text.data[position:position + len(block)] != block:
        raise FirmwareError("Wi-Fi guard instructions/literals differ from the reviewed interior splice")
    replacement = (bytes.fromhex("06010000") + struct.pack("<I", symbol.address)
                   + bytes.fromhex("91ffffa0090000"))
    if coupled:
        mode = next(patch for patch in wifi_hook_plan['patches'] if patch['name'] == 'mode')
        replacement += b'\x00' + struct.pack('<I', int(mode['wrapper_address'], 16)) + b'\x00'
    return {
        "scheme": "kd360s_1.0.13_hk_pending_consumer_v2" if coupled else "kd360s_1.0.13_hk_pending_consumer_v1",
        "patches": [{"address": f"0x{ADDRESS:08x}", "file_offset": offset,
                     "before_hex": actual.hex(), "after_hex": replacement.hex(),
                     "guard_symbol": SYMBOL, "guard_address": f"0x{symbol.address:08x}"}],
        "stock_witnesses": witnesses,
        "preserved_callback": {"address": f"0x{CALLBACK_ADDRESS:08x}",
                               "file_offset": callback_offset, "size": CALLBACK_SIZE,
                               "sha256": CALLBACK_SHA256},
        "guard_block_sha256": hashlib.sha256(block).hexdigest(),
        "continuations": ["0x4201d3e1" if coupled else "0x4201d3db", "0x4201d43d"],
        "scratch": ["a8", "a9"], "runtime_verified": False,
    }
