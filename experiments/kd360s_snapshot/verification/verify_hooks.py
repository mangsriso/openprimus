#!/usr/bin/env python3
"""Execute production detours and stock continuations in isolated ESP32-S3 QEMU."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
STOCK_SHA256 = 'ac94398fde154cd8d29709e82990fd6dce53faa69b28e713f2c2a5dd4c65f5b4'
CODE_ADDRESS = 0x42000020
CODE_OFFSET = 2687008
CODE_SIZE = 1076852
HOOKS = [('start', 0x42005918, 16, 2), ('stop', 0x420031b0, 16, 3),
         ('pressure', 0x42003da4, 15, 4), ('rx', 0x42005430, 16, 5)]
LINKER_SCRIPT = '''SECTIONS {
 .vectors 0x40370000 : { *(.vectors) }
 .stock 0x42000020 : { *(.stock) }
 .hooks 0x42106ea0 : { *(.text.hooks) }
 .callback 0x42109000 : { *(.test_callback) }
 /DISCARD/ : { *(.comment) *(.xtensa.info) *(.xt.prop) *(.xt.lit) }
}
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tool(directory, name):
    return str(directory / ('xtensa-esp32s3-elf-' + name))


def build(stock, toolchain, output, negative=False):
    """Only start's original suffix is substituted; other original bodies remain."""
    with tempfile.TemporaryDirectory(prefix='hook-qemu-build-') as temporary:
        work = Path(temporary)
        code = bytearray(stock[CODE_OFFSET:CODE_OFFSET + CODE_SIZE])
        source, obj, elf = work / 'stock.bin', work / 'stock.o', work / 'hooks.elf'
        linker = work / 'hooks.ld'
        linker.write_text(LINKER_SCRIPT)
        objcopy = [tool(toolchain, 'objcopy'), '-I', 'binary', '-O', 'elf32-xtensa-le',
                   '-B', 'xtensa', '--rename-section',
                   '.data=.stock,alloc,load,readonly,code,contents', str(source), str(obj)]
        compile_command = [tool(toolchain, 'gcc'), '-mno-target-align',
                           '-mno-text-section-literals', '-Wa,--no-transform', '-nostdlib',
                           '-Wl,--no-relax', '-Wl,-T,' + str(linker),
                           str(HERE.parent / 'hooks.S'), str(HERE / 'callbacks.S'),
                           str(HERE / 'vectors.S'), str(obj), '-o', str(elf)]
        source.write_bytes(code)
        subprocess.run(objcopy, check=True)
        subprocess.run(compile_command, check=True)
        symbols = subprocess.check_output([tool(toolchain, 'nm'), str(elf)], text=True)
        wrappers = {match[2]: int(match[1], 16)
                    for match in re.finditer(r'^([0-9a-f]+) T (wrap_\w+)$', symbols, re.M)}
        for name, address, span, _ in HOOKS:
            patch = (bytes.fromhex('06010000') + struct.pack('<I', wrappers['wrap_' + name])
                     + bytes.fromhex('91ffffa00900') + bytes(span - 14))
            offset = address - CODE_ADDRESS
            code[offset:offset + span] = patch
        # mov a3,a7; retw: retains first argument and exposes sixth as return word 2.
        offset = 0x42005928 - CODE_ADDRESS
        code[offset:offset + 6] = bytes.fromhex('703720900000')
        if negative:
            # Deliberately replace scratch a9 with argument register a10.
            offset = 0x42005918 - CODE_ADDRESS
            code[offset + 8], code[offset + 12] = 0xa1, 0x0a
        source.write_bytes(code)
        subprocess.run(objcopy, check=True)
        subprocess.run(compile_command, check=True)
        shutil.copyfile(elf, output)
    return wrappers


def cases():
    result = []
    for value in (0, 1, 0xffffffff):
        result.append(('start_' + hex(value), 0x42005918, 2, value, value,
                       [], [('$ar11', 0x66666666)]))
    coefficients = [('unsigned int', 0x3fc9ff08, 0x41200000),
                    ('unsigned int', 0x3fc9ff0c, 0x3f000000),
                    ('unsigned int', 0x3fc9ff10, 0xc0000000)]
    for value, bits in ((5, 0), (10, 0x40400000), (100, 0x42400000), (0xffff8000, 0)):
        result.append(('pressure_' + hex(value), 0x42003da4, 4, value, bits,
                       coefficients, []))
    result.append(('pressure_negative_calibration', 0x42003da4, 4, 10, 0,
                   coefficients[:2] + [('unsigned int', 0x3fc9ff10, 0xc1000000)], []))
    for name, screen, flag in [('equal_first', 7, 1), ('equal_second', 8, 1),
                               ('unequal', 9, 0x5a)]:
        setup = [('unsigned int', 0x3fc9d104, screen), ('unsigned int', 0x3fca8084, 7),
                 ('unsigned int', 0x3fca8110, 8), ('unsigned char', 0x3fc9ca81, 0x5a)]
        result.append(('rx_' + name, 0x42005430, 5, 0x123, 0x123, setup,
                       [('*(unsigned char*)0x3fc9ca81', flag)]))
    for value in (0, 5):
        setup = [('unsigned short', 0x3fc9cb0e, value), ('unsigned char', 0x3fc9dc0a, 0x42),
                 ('unsigned char', 0x3fc9dc0c, 0x23), ('unsigned char', 0x3fc9ca66, 0xab),
                 ('unsigned char', 0x3fc9ca8e, 0xcd)]
        checks = [('*(unsigned short*)0x3fc9cb0e', value or 1),
                  ('*(unsigned short*)0x3fc9cb10', 1200),
                  ('*(unsigned char*)0x3fc9dc0c', 0x23 if value else 0x42),
                  ('*(unsigned char*)0x3fc9ca66', 0), ('*(unsigned char*)0x3fc9ca8e', 1)]
        result.append(('stop_' + str(value), 0x420031b0, 3, 0x123, 0x123, setup, checks))
    return result


def debugger_script(elf, port):
    path = str(elf).replace('\\', '\\\\').replace('"', '\\"')
    lines = ['set pagination off', 'set confirm off', f'file "{path}"',
             f'target remote 127.0.0.1:{port}', 'set $ov=0', 'set $un=0',
             'hbreak *0x42110000', 'hbreak *0x40370080', 'commands 2', 'silent',
             'set $ov=$ov+1', 'continue', 'end', 'hbreak *0x403700c0', 'commands 3',
             'silent', 'set $un=$un+1', 'continue', 'end']

    def check(expression, expected, label):
        lines.extend([f'if (unsigned int)({expression}) != {expected}',
                      f'printf "FAIL {label}: got 0x%x expected 0x%x\\n", '
                      f'(unsigned int)({expression}), {expected}', 'quit 1', 'end'])

    for name, site, event, argument, returned, setup, specific in cases():
        lines.extend(['set $windowbase=0', 'set $windowstart=1', 'set $ps=0x60020',
                      'set $vecbase=0x40370000', 'set $cpenable=1',
                      'set $ar1=0x3fce0000', 'set {unsigned int}0x3fcdfff4=0x3fce0080',
                      'set $ar8=0x82110000', 'set $ar0=0'])
        for index in range(2, 8):
            lines.append(f'set $ar{index}={0x11000000 + index}')
        for index in range(10, 16):
            lines.append(f'set $ar{index}={0x11111111 * (index - 9)}')
        lines.extend([f'set $ar10={argument}', f'set $pc={site}'])
        for typename, address, value in setup:
            lines.append(f'set {{{typename}}}{address}={value}')
        lines.append('continue')
        for index in range(2, 8):
            check('$ar' + str(index), 0x11000000 + index, name + '_caller_a' + str(index))
        common = [('$pc', 0x42110000), ('$ar1', 0x3fce0000), ('$windowbase', 0),
                  ('$windowstart', 1), ('$ar10', returned),
                  ('$ar12', 0x33333333), ('$ar13', 0x44444444),
                  ('*(unsigned int*)0x3fce1000', event),
                  ('*(unsigned int*)0x3fce1004', argument),
                  ('*(unsigned int*)0x3fce1008', returned)]
        if event != 2:
            common.append(('$ar11', 0 if event == 4 else 0x22222222))
        for expression, expected in common + specific:
            check(expression, expected, name + '_' + expression)
        lines.append(f'printf "PASS {name} overflow=%u underflow=%u\\n",$ov,$un')
    lines.extend(['if $ov==0 || $un==0', 'printf "FAIL no window spill execution\\n"',
                  'quit 1', 'end', 'quit 0'])
    return '\n'.join(lines) + '\n'


def execute(qemu, toolchain, elf, output, label):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = [str(qemu), '-machine', 'esp32s3', '-nographic', '-S',
               '-gdb', f'tcp:127.0.0.1:{port}', '-kernel', str(elf), '-global',
               'driver=timer.esp32c3.timg,property=wdt_disable,value=true']
    with (output / (label + '-qemu.log')).open('w') as logfile:
        process = subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('QEMU exited before its debugger opened; inspect QEMU log')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.02)
            else:
                raise RuntimeError('QEMU debugger did not open')
            script = output / (label + '.gdb')
            script.write_text(debugger_script(elf, port))
            run = subprocess.run([tool(toolchain, 'gdb'), '-q', '-batch', '-x', str(script)],
                                 capture_output=True, text=True, timeout=60)
            log = run.stdout + run.stderr
            (output / (label + '-gdb.log')).write_text(log)
            return {'exit_code': run.returncode,
                    'passed': re.findall(r'^PASS (.+)$', log, re.M),
                    'failures': re.findall(r'^FAIL (.+)$', log, re.M)}
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', required=True, type=Path)
    parser.add_argument('--qemu', required=True, type=Path)
    parser.add_argument('--stock', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    toolchain, qemu, output = (path.resolve() for path in
                               (args.toolchain, args.qemu, args.output))
    stock = args.stock.read_bytes()
    if sha256(stock) != STOCK_SHA256:
        parser.error('stock SHA-256 does not match the reviewed KD360S image')
    compiler = subprocess.check_output([tool(toolchain, 'gcc'), '--version'], text=True).splitlines()[0]
    if '8.4.0' not in compiler or 'esp-2021r2-patch5' not in compiler:
        parser.error('use the reviewed ESP-2021r2-patch5 GCC 8.4.0 toolchain')
    qemu_version = subprocess.check_output([str(qemu), '--version'], text=True).splitlines()[0]
    output.mkdir(parents=True, exist_ok=True)
    report = {'stock_sha256': sha256(stock), 'compiler': compiler, 'qemu': qemu_version,
              'sources': {name: sha256(path.read_bytes()) for name, path in
                          [('hooks.S', HERE.parent / 'hooks.S'), ('callbacks.S', HERE / 'callbacks.S'),
                           ('vectors.S', HERE / 'vectors.S'), ('verify_hooks.py', Path(__file__))]},
              'full_original_bodies': ['pressure', 'rx', 'stop'],
              'start_body': 'actual relocated prefix with synthetic return suffix',
              'hardware_verified': False, 'ready_to_flash': False}
    for label, negative in [('production', False), ('negative', True)]:
        elf = output / (label + '.elf')
        build(stock, toolchain, elf, negative)
        report[label] = execute(qemu, toolchain, elf, output, label)
        report[label]['elf_sha256'] = sha256(elf.read_bytes())
    production, negative = report['production'], report['negative']
    report['passed'] = (production['exit_code'] == 0 and len(production['passed']) == len(cases())
                        and not production['failures'] and negative['exit_code'] != 0
                        and any(failure.startswith('start_0x0_$ar10:') for failure in negative['failures']))
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
