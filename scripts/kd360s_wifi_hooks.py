"""Four fixed stock Wi-Fi API detours and their exact original-call trampolines."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
from typing import TYPE_CHECKING

from kd360s_firmware import FirmwareError, VerifiedImage
from kd360s_instrumentation import mapped_bytes

if TYPE_CHECKING:
    from kd360s_test_image import Payload


@dataclass(frozen=True)
class WifiHook:
    name: str
    address: int
    expected: bytes
    continuation: int | None
    original: str
    original_offset: int
    original_size: int


HOOKS = (
    WifiHook('explicit', 0x420561C0, bytes.fromhex('36c10291549e0c1b2d05705074c02000'),
             0x420561D0, 'original_explicit_begin', 28, 22),
    WifiHook('saved', 0x420564F4, bytes.fromhex('36c10131879d0c1bc020002803a1659e'),
             0x42056504, 'original_saved_begin', 52, 22),
    WifiHook('disconnect', 0x42056688, bytes.fromhex('36610140607441219d0c050c080c17'),
             0x42056697, 'original_disconnect', 76, 21),
    WifiHook('mode', 0x42054F88, bytes.fromhex('3661004d0265fdff'),
             0x42054F9A, 'original_mode', 100, 24),
)
# Literal pool, relocated original bodies and alignment padding from the reviewed
# assembly. Relative operands remain identical when this block is relocated.
TRAMPOLINE_BLOCK = bytes.fromhex(
    'a031ca3f2821ca3fd06105420465054297660542644f05429a4f054236c1'
    '0291f8ff0c1b2d05705074c02000f1f7ffa00f00000036c10131f2ff0c1b'
    'c020002803a1f0fff1f2ffa00f00000036610140607441ebff0c050c080c'
    '17f1edffa00f000000003661004d0281eaffe00800279a030c121df0f1e8'
    'ffa00f00')
LITERALS = (
    (0x4203DB14, 0x3FCA31A0), (0x4203DE98, 0x3FCA2128),
    (0x4203E628, 0x403805AC), (0x40374EB4, 0x3C3791A6),
    (0x40374E8C, 0x3C3791BF), (0x40374D24, 0xB33FFFFF),
    (0x42000AE0, 0x4200BDA8), (0x42002C60, 0x4202655C), (0x4203EC00, 0x42067014),
)
IMPORTS = (
    ('current_task', 0x40380120, bytes.fromhex('364100a0630080eb03808d04e098118134d29a88c020002228008156d3e008001df0')),
    ('get_mode', 0x42054F64, bytes.fromhex('3661002136a48202000c02271812813fa4820800271809cba125dc63279a0128311df0')),

    ('mode_preserved_epilogues', 0x42054F90, bytes.fromhex('279a060c121df0')),

    ('create_mutex', 0x4037EC88, bytes.fromhex(
        '36410020c074b2a000a2a00165cdffa02a209c6a8122d80c0bb92ab90ab93a826a13b26a14dd0bcd0b65ddff1df0')),
    ('take_recursive', 0x4037F13C, bytes.fromhex(
        '36410056e200d151d7c15bd7a139d7b2a2cda546054822e5fc00a7940c88320c1a1b8889322d0a1df00030b32020a220a5e4ff16eafe8222031b88893206f9ff')),
    ('give_recursive', 0x4037EC50, bytes.fromhex(
        '36410056e200d18cd8c18cd8a174d8b2a2956595054822a54b0132a000a79413b8320c130bbbb932cc8bdd0bcd0b20a22065e0ff2d031df0')),
)


def plan_wifi_hooks(image: VerifiedImage, payload: Payload) -> dict:
    text = next((section for section in payload.sections if section.name == '.text'), None)
    if text is None:
        raise FirmwareError('Wi-Fi hooks require executable payload text')

    def function(name):
        matches = [symbol for symbol in payload.symbols if symbol.name == name]
        if len(matches) != 1:
            raise FirmwareError(f'Wi-Fi hooks require exactly one symbol: {name}')
        symbol = matches[0]
        if (symbol.info != 0x12 or symbol.section_name != '.text' or symbol.address % 4
                or symbol.size < 5 or not text.address <= symbol.address
                < symbol.address + symbol.size <= text.address + len(text.data)):
            raise FirmwareError(f'Wi-Fi hook requires a bounded aligned global text function: {name}')
        return symbol

    originals = [function(hook.original) for hook in HOOKS]
    start = originals[0].address - HOOKS[0].original_offset
    if not text.address <= start < start + len(TRAMPOLINE_BLOCK) <= text.address + len(text.data):
        raise FirmwareError('Wi-Fi original trampoline literal block is outside executable payload')
    for hook, original in zip(HOOKS, originals):
        if original.address != start + hook.original_offset or original.size != hook.original_size:
            raise FirmwareError(f'Wi-Fi original trampoline layout differs: {hook.original}')
    position = start - text.address
    if text.data[position:position + len(TRAMPOLINE_BLOCK)] != TRAMPOLINE_BLOCK:
        raise FirmwareError('Wi-Fi original trampoline instructions/literals differ from reviewed bytes')

    witnesses = []
    for name, address, expected in IMPORTS + tuple(
            (f'literal_{address:08x}', address, struct.pack('<I', value)) for address, value in LITERALS):
        offset, actual = mapped_bytes(image, address, len(expected))
        if actual != expected:
            raise FirmwareError(f'Wi-Fi hook stock witness differs: {name}')
        witnesses.append({'name': name, 'address': f'0x{address:08x}',
                          'file_offset': offset, 'bytes_hex': actual.hex()})

    patches = []
    ranges = [(start, start + len(TRAMPOLINE_BLOCK))]
    code = image.segments[3]
    for hook in HOOKS:
        if not code.address <= hook.address < hook.address + len(hook.expected) <= code.address + code.size:
            raise FirmwareError(f'Wi-Fi hook lies outside stock IROM: {hook.name}')
        offset, actual = mapped_bytes(image, hook.address, len(hook.expected))
        if actual != hook.expected:
            raise FirmwareError(f'Wi-Fi hook stock span differs: {hook.name}')
        wrapper = function('wrap_wifi_' + hook.name)
        if any(wrapper.address < end and begin < wrapper.address + wrapper.size for begin, end in ranges):
            raise FirmwareError('Wi-Fi wrappers and original trampoline block must not overlap')
        ranges.append((wrapper.address, wrapper.address + wrapper.size))
        entry = text.data[wrapper.address - text.address:wrapper.address - text.address + 3]
        if entry[0] != 0x36 or entry[1] & 15 != 1:
            raise FirmwareError(f'Wi-Fi wrapper requires windowed ENTRY a1: {wrapper.name}')
        frame = ((entry[1] >> 4) | entry[2] << 4) * 8
        if frame < 32 or frame % 16:
            raise FirmwareError(f'Wi-Fi wrapper frame must be aligned and at least 32 bytes: {wrapper.name}')
        replacement = (bytes.fromhex('06010000') + struct.pack('<I', wrapper.address)
                       + bytes.fromhex('91ffffa00900') + bytes(max(0, len(hook.expected) - 14)))
        if hook.name == 'mode':
            # The literal belongs to the coupled 21-byte pending-consumer patch.
            displacement = (0x4201D3DC - hook.address) // 4
            replacement = b'\x91' + struct.pack('<H', displacement & 0xffff) + bytes.fromhex('a009000000')
        patches.append({'name': hook.name, 'address': f'0x{hook.address:08x}', 'file_offset': offset,
                        'before_hex': actual.hex(), 'after_hex': replacement.hex(),
                        'wrapper_symbol': wrapper.name, 'wrapper_address': f'0x{wrapper.address:08x}',
                        'wrapper_stack_bytes': frame, 'original_symbol': hook.original,
                        'original_address': f'0x{start + hook.original_offset:08x}',
                        'continuation': f'0x{hook.continuation:08x}'})
    return {'scheme': 'kd360s_1.0.13_hk_wifi_call8_detours_v1', 'patches': patches,
            'stock_witnesses': witnesses, 'trampoline_block_address': f'0x{start:08x}',
            'trampoline_block_sha256': hashlib.sha256(TRAMPOLINE_BLOCK).hexdigest(),
            'inbound_abi': 'call8_only', 'return_abi': 'one_integer_word',
            'mode_literal_address': '0x4201d3dc', 'requires_guard_variant': 'wifi_hooks',
            'pre_entry_scratch': 'a9', 'trampoline_jump_scratch': 'a15',
            'runtime_verified': False}
