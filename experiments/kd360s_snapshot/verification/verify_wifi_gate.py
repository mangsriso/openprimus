#!/usr/bin/env python3
"""Execute production-header C Wi-Fi wrappers and stock-entry dispatch in Xtensa QEMU.

Original API bodies are bounded noinline C mocks, not the SDK or wifi_hooks.S.
The fixture uses production compiler flags but does not claim byte equivalence
with the packaged snapshot translation unit, whose callbacks can inline.

Run with --toolchain /path/to/xtensa-esp32s3-elf/bin --qemu /path/to/qemu-wrapper
--stock firmware_binary/kd360s/KD360S_OTA_1.0.13_HK_20260630.bin
--output /path/to/private/results. A passing run reports 11 positive cases and
an argument-corruption negative control in results.json. Output ELFs contain
private stock code and must not be committed or flashed.

This tests CALL8 entries and actual window spills. The mode consumer, original
trampolines, SDK bodies, scheduler and mutex concurrency are outside this proof.
Entry patches are constructed independently of the production image builder.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile

from verify_hooks import CODE_ADDRESS, CODE_OFFSET, CODE_SIZE, STOCK_SHA256, execute, sha256, tool

HERE = Path(__file__).resolve().parent
SITES = [('explicit', 0x420561c0, 16), ('saved', 0x420564f4, 16),
         ('disconnect', 0x42056688, 15), ('mode', 0x42054f88, 8)]
CASES = ['native_explicit', 'worker_explicit', 'producer_explicit', 'explicit_take_fail',
         'nested_mode', 'saved', 'disconnect', 'mode', 'saved_take_fail',
         'disconnect_take_fail', 'mode_take_fail']
FLAGS = ['-std=c11', '-DWIFI_HOOKS=1', '-Os', '-Wall', '-Wextra', '-Werror', '-mlongcalls',
         '-ffreestanding', '-fno-builtin', '-fno-stack-protector',
         '-fno-optimize-sibling-calls', '-fno-ipa-icf', '-nostdlib',
         '-Wl,--abi-windowed', '-Wl,--build-id=none']
LINKER = '''SECTIONS {
 .vectors 0x40370000 : { *(.vectors) }
 .stock 0x42000020 : { *(.stock) }
 .text 0x42110000 : { *(.literal .literal.*) *(.text .text.*) *(.rodata .rodata.*) }
 .fixture_state 0x3fce1000 : { *(.fixture_state) *(.data .data.*) *(.bss .bss.*) }
 /DISCARD/ : { *(.comment) *(.xtensa.info) *(.xt.prop) *(.xt.lit) }
}
'''


def build(stock, toolchain, output, negative=False):
    with tempfile.TemporaryDirectory(prefix='wifi-gate-qemu-') as temporary:
        work = Path(temporary)
        raw, obj, elf = work / 'stock.bin', work / 'stock.o', work / 'gate.elf'
        linker = work / 'gate.ld'
        linker.write_text(LINKER)
        code = bytearray(stock[CODE_OFFSET:CODE_OFFSET + CODE_SIZE])
        copy_command = [tool(toolchain, 'objcopy'), '-I', 'binary', '-O', 'elf32-xtensa-le',
                        '-B', 'xtensa', '--rename-section',
                        '.data=.stock,alloc,load,readonly,code,contents', str(raw), str(obj)]
        command = [tool(toolchain, 'gcc'), *FLAGS, '-Wl,-T,' + str(linker),
                   str(HERE / 'wifi_gate_fixture.c'), str(HERE / 'vectors.S'), str(obj), '-o', str(elf)]
        raw.write_bytes(code)
        subprocess.run(copy_command, check=True)
        subprocess.run(command, check=True)
        symbols = dict((m[2], int(m[1], 16)) for m in re.finditer(
            r'^([0-9a-f]+) [TtDdBb] (\w+)$', subprocess.check_output([tool(toolchain, 'nm'), str(elf)], text=True), re.M))
        patches = []
        for name, address, span in SITES:
            wrapper = symbols['wrap_wifi_' + name]
            patch = bytes.fromhex('06010000') + struct.pack('<I', wrapper) + bytes.fromhex('91ffffa00900') + bytes(span - 14) if span >= 14 else b''
            if name == 'mode':
                displacement = (0x4201d3dc - address) // 4
                patch = b'\x91' + struct.pack('<H', displacement & 0xffff) + bytes.fromhex('a009000000')
                code[0x4201d3dc - CODE_ADDRESS:0x4201d3e0 - CODE_ADDRESS] = struct.pack('<I', wrapper)
            if negative and name == 'explicit':
                patch = bytearray(patch)
                patch[8], patch[12] = 0xa1, 0x0a
                patch = bytes(patch)
            offset = address - CODE_ADDRESS
            code[offset:offset + span] = patch
            patches.append({'name': name, 'address': hex(address), 'patch_hex': patch.hex(), 'wrapper': hex(wrapper)})
        raw.write_bytes(code)
        subprocess.run(copy_command, check=True)
        subprocess.run(command, check=True)
        shutil.copyfile(elf, output)
        output.with_suffix('.asm').write_text(subprocess.check_output([tool(toolchain, 'objdump'), '-d',
            '--start-address=0x42110000', str(elf)], text=True))
    return {'symbols': symbols, 'patches': patches}


def debugger_script(metadata):
    def script(elf, port):
        path = str(elf).replace('\\', '\\\\').replace('"', '\\"')
        lines = ['set pagination off', 'set confirm off', f'file "{path}"',
                 f'target remote 127.0.0.1:{port}', 'set $ov=0', 'set $un=0',
                 'hbreak *0x42118000', 'hbreak *0x40370080', 'commands 2', 'silent',
                 'set $ov=$ov+1', 'continue', 'end', 'hbreak *0x403700c0', 'commands 3',
                 'silent', 'set $un=$un+1', 'continue', 'end']
        def check(expression, expected, label):
            lines.extend([f'if (unsigned int)({expression}) != {expected}',
                          f'printf "FAIL {label}: got 0x%x expected 0x%x\\n", '
                          f'(unsigned int)({expression}), {expected}', 'quit 1', 'end'])
        for index, name in enumerate(CASES):
            lines.extend(['set $windowbase=0', 'set $windowstart=1', 'set $ps=0x60020',
                          'set $vecbase=0x40370000', 'set $cpenable=1',
                          'set $ar1=0x3fce0000', 'set {unsigned int}0x3fcdfff4=0x3fce0080',
                          'set $ar8=0x82118000', 'set $ar0=0', f'set $ar10={index}'])
            for register in range(2, 8):
                lines.append(f'set $ar{register}={0xabc00000 + register}')
            lines.extend([f'set $pc={metadata["symbols"]["run_wifi_case"]}', 'continue'])
            for expression, expected in [('$pc', 0x42118000), ('$ar1', 0x3fce0000),
                                         ('$ar10', 0), ('$windowbase', 0), ('$windowstart', 1)]:
                check(expression, expected, name)
            for register in range(2, 8):
                check('$ar' + str(register), 0xabc00000 + register, name + '_caller_canary')
            lines.append(f'printf "PASS {name} overflow=%u underflow=%u\\n",$ov,$un')
        lines.extend(['if $ov==0 || $un==0', 'printf "FAIL no executed window spill\\n"',
                      'quit 1', 'end', 'quit 0'])
        return '\n'.join(lines) + '\n'
    return script


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', required=True, type=Path)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--stock', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    output, toolchain, qemu = args.output.resolve(), args.toolchain.resolve(), args.qemu.resolve()
    stock = args.stock.read_bytes()
    if sha256(stock) != STOCK_SHA256:
        parser.error('stock SHA-256 differs from reviewed image')
    compiler = subprocess.check_output([tool(toolchain, 'gcc'), '--version'], text=True).splitlines()[0]
    if '8.4.0' not in compiler or 'esp-2021r2-patch5' not in compiler:
        parser.error('requires reviewed ESP-2021r2-patch5 GCC 8.4.0')
    output.mkdir(parents=True, exist_ok=True)
    report = {'compiler': compiler, 'flags': FLAGS, 'stock_sha256': sha256(stock),
              'scope': 'production-header fixture plus stock-entry detours; original API bodies are noinline C mocks',
              'production_elf_byte_equivalence': False, 'hardware_verified': False,
              'ready_to_flash': False,
              'qemu': subprocess.check_output([str(qemu), '--version'], text=True).splitlines()[0],
              'sources': {str(p.relative_to(HERE.parent)): sha256(p.read_bytes()) for p in
                          [HERE.parent / 'wifi_gate.h', HERE / 'wifi_gate_fixture.c', Path(__file__),
                           HERE / 'verify_hooks.py', HERE / 'vectors.S']}}
    for label, negative in [('positive', False), ('negative', True)]:
        elf = output / (label + '.elf')
        metadata = build(stock, toolchain, elf, negative)
        report[label] = execute(qemu, toolchain, elf, output, label, script_factory=debugger_script(metadata))
        report[label]['elf_sha256'] = sha256(elf.read_bytes())
        report[label]['patches'] = metadata['patches']
    report['passed'] = (report['positive']['exit_code'] == 0 and len(report['positive']['passed']) == len(CASES)
                        and not report['positive']['failures'] and report['negative']['exit_code'] != 0
                        and any(f.startswith('native_explicit: got 0x1 expected 0x0') for f in report['negative']['failures']))
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
