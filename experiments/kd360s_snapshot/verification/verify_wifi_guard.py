#!/usr/bin/env python3
"""Run the production Wi-Fi consumer splice against stock code in ESP32-S3 QEMU."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import verify_hooks as runner

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import kd360s_firmware as firmware
import kd360s_test_image as builder
import kd360s_wifi_guard as guard


def build(stock, toolchain, output, wifi_hooks=False):
    with tempfile.TemporaryDirectory(prefix='wifi-guard-qemu-') as temporary:
        work = Path(temporary)
        entry = work / 'entry.S'
        entry.write_text('.section .text.entry,"ax"\n.align 4\n.global snapshot_entry\n'
                         '.type snapshot_entry,@function\nsnapshot_entry:\nentry a1,32\nretw\n'
                         '.size snapshot_entry,.-snapshot_entry\n')
        payload = work / 'payload.elf'
        extra = []
        if wifi_hooks:
            wrappers = work / 'wrappers.c'
            wrappers.write_text('''extern unsigned original_explicit_begin(unsigned,unsigned,unsigned,unsigned,unsigned,unsigned);
extern unsigned original_saved_begin(unsigned);
extern unsigned original_disconnect(unsigned,unsigned,unsigned);
extern unsigned original_mode(unsigned);
unsigned wrap_wifi_explicit(unsigned a,unsigned b,unsigned c,unsigned d,unsigned e,unsigned f) { return original_explicit_begin(a,b,c,d,e,f); }
unsigned wrap_wifi_saved(unsigned a) { return original_saved_begin(a); }
unsigned wrap_wifi_disconnect(unsigned a,unsigned b,unsigned c) { return original_disconnect(a,b,c); }
unsigned wrap_wifi_mode(unsigned a) { return original_mode(a); }
''')
            extra = ['-DWIFI_HOOKS=1', '-Os', '-mlongcalls', '-fno-optimize-sibling-calls',
                     '-fno-ipa-icf', str(wrappers), str(ROOT / 'experiments/kd360s_snapshot/wifi_hooks.S')]
        subprocess.run([runner.tool(toolchain, 'gcc')] + extra + ['-nostdlib', '-Wl,--build-id=none',
                        '-Wl,-T,' + str(ROOT / 'experiments/kd360s_snapshot/snapshot.ld'),
                        str(entry), str(ROOT / 'experiments/kd360s_snapshot/wifi_guard.S'),
                        '-o', str(payload)], check=True)
        image, manifest = builder.build_image(stock, payload.read_bytes(), wifi_guard=True, wifi_hooks=wifi_hooks)
        original = firmware.verify_bytes(stock)
        code = original.segments[3]
        size = manifest['changes']['code_segment_size']['after']
        binary = bytearray(image[code.offset:code.offset + size])
        linker = work / 'qemu.ld'
        linker.write_text('SECTIONS { .stock 0x42000020 : { *(.stock) } }\n')
        for label in ('positive', 'negative'):
            if label == 'negative':
                # Suppress the owned error path. The OTA nonzero case must fail.
                start = int(manifest['changes']['wifi_guard']['patches'][0]['guard_address'], 16)
                position = start - guard.LITERAL_BYTES + 20 - code.address
                binary[position:position + 4] = (0x4201d43d).to_bytes(4, 'little')
            raw, obj = work / 'code.bin', work / 'code.o'
            raw.write_bytes(binary)
            subprocess.run([runner.tool(toolchain, 'objcopy'), '-I', 'binary', '-O', 'elf32-xtensa-le',
                            '-B', 'xtensa', '--rename-section', '.data=.stock,alloc,load,readonly,code,contents',
                            str(raw), str(obj)], check=True)
            subprocess.run([runner.tool(toolchain, 'ld'), '-T', str(linker), str(obj),
                            '-o', str(output / (label + '.elf'))], check=True)
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        shutil.copyfile(payload, output / 'payload.elf')
    return manifest


def debugger_script(elf, port, wifi_hooks=False):
    path = str(elf).replace('\\', '\\\\').replace('"', '\\"')
    lines = ['set pagination off', 'set confirm off', f'file "{path}"',
             f'target remote 127.0.0.1:{port}', f'hbreak *{0x4201d3e1 if wifi_hooks else 0x4201d3db}', 'hbreak *0x4201d43d']

    def check(expression, value, label):
        lines.extend([f'if (unsigned int)({expression}) != {value}',
                      f'printf "FAIL {label}: got 0x%x expected 0x%x\\n", '
                      f'(unsigned int)({expression}), {value}', 'quit 1', 'end'])

    for name, screen in [('ota', 7), ('wifi', 8), ('password', 9), ('normal', 10),
                         ('dfu', 11), ('null', 0), ('settings', 12),
                         ('preferences', 13), ('normal_repeat', 10)]:
        for pending in (0, 1, 255):
            label = name + '_' + str(pending)
            owned = screen in (7, 8, 9)
            expected_pending = pending if owned else 0
            lines.extend(['set $windowbase=0', 'set $windowstart=1', 'set $ps=0x40020',
                          'set $sar=13', 'set $pc=0x4201d3c9',
                          'set $ar1=0x3fce0000', 'set {unsigned int}0x3fcdfff4=0x3fce0080'])
            for index in range(16):
                lines.append(f'set $ar{index}={0x3fce0000 if index == 1 else 0x11000000 + index}')
            for kind, address, value in [
                    ('unsigned int', 0x3fc9d104, screen), ('unsigned int', 0x3fca8564, 7 if screen else 0),
                    ('unsigned int', 0x3fca84cc, 8 if screen else 0),
                    ('unsigned int', 0x3fca849c, 9 if screen else 0),
                    ('unsigned int', 0x3fca8060, 11), ('unsigned char', 0x3fca1514, pending),
                    ('unsigned char', 0x3fca1515, 201), ('unsigned short', 0x3fc9ca9f, 0xa55a),
                    ('unsigned char', 0x3fc9caa1, 0x23), ('unsigned short', 0x3fca1512, 0x7777),
                    ('unsigned char', 0x3fca1516, 1), ('unsigned int', 0x3fca1518, 0x12345678)]:
                lines.append(f'set {{{kind}}}{address}={value}')
            lines.append('continue')
            check('$pc', (0x4201d3e1 if wifi_hooks else 0x4201d3db) if owned and pending else 0x4201d43d, label + '_continuation')
            for index in [0, 1, 5, 6, 7, 10, 11, 12, 13, 14, 15]:
                check('$ar' + str(index), (0 if wifi_hooks and owned and pending and index in (11, 12) else 0x3fce0000 if index == 1 else 0x11000000 + index),
                      label + '_a' + str(index))
            for expression, value in [('$ar2', 0x3fc9ca60), ('$ar3', 0 if wifi_hooks and owned and pending else expected_pending),
                                      ('$ar4', 0x3fca1514), ('$sar', 13), ('$windowbase', 0),
                                      ('$windowstart', 1),
                                      ('*(unsigned char*)0x3fca1514', expected_pending),
                                      ('*(unsigned char*)0x3fca1515', 201),
                                      ('*(unsigned short*)0x3fc9ca9f', 0xa55a),
                                      ('*(unsigned char*)0x3fc9caa1', 0x23),
                                      ('*(unsigned short*)0x3fca1512', 0x7777),
                                      ('*(unsigned char*)0x3fca1516', 1),
                                      ('*(unsigned int*)0x3fca1518', 0x12345678)]:
                check(expression, value, label + '_' + expression)
            lines.append(f'printf "PASS {label}\\n"')
    lines.append('quit 0')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', type=Path, required=True)
    parser.add_argument('--qemu', type=Path, required=True)
    parser.add_argument('--stock', type=Path, required=True)
    parser.add_argument('--wifi-hooks', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = build(args.stock.read_bytes(), args.toolchain, args.output, args.wifi_hooks)
    results = {label: runner.execute(args.qemu, args.toolchain, args.output / (label + '.elf'),
                                    args.output, label, script_factory=lambda elf, port: debugger_script(elf, port, args.wifi_hooks))
               for label in ('positive', 'negative')}
    passed = (results['positive']['exit_code'] == 0 and len(results['positive']['passed']) == 27
              and not results['positive']['failures'] and results['negative']['exit_code'] != 0
              and any('ota_1_continuation' in message for message in results['negative']['failures']))
    report = {'passed': passed, 'hardware_verified': False, 'ready_to_flash': False,
              'cases': results, 'source_sha256': manifest['source']['sha256'],
              'assembly_sha256': hashlib.sha256((ROOT / 'experiments/kd360s_snapshot/wifi_guard.S').read_bytes()).hexdigest(),
              'elf_sha256': manifest['elf']['sha256'],
              'compiler': subprocess.check_output([runner.tool(args.toolchain, 'gcc'), '--version'], text=True).splitlines()[0],
              'qemu': subprocess.check_output([str(args.qemu), '--version'], text=True).splitlines()[0],
              'scope': 'Real production guard and packaged stock splice; stops at both original continuations. '
                       'No SDK, callback, full scheduler, RTOS, asynchronous event timing or hardware execution.'}
    (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
