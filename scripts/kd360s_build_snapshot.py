#!/usr/bin/env python3
"""Compile and package an experimental snapshot sender without accessing hardware."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from kd360s_firmware import DEFAULT_CACHE, TARGET, FirmwareError
from kd360s_test_image import build_image, write_package

ROOT = Path(__file__).resolve().parents[1]


def build(toolchain, stock, collector, port, output):
    address = ipaddress.IPv4Address(collector)
    if address.is_multicast or address.is_unspecified or address == ipaddress.IPv4Address('255.255.255.255'):
        raise ValueError('collector must be an explicit unicast IPv4 address')
    if not 1 <= port <= 65535:
        raise ValueError('collector port must be in 1..65535')
    compiler = toolchain.resolve() / 'xtensa-esp32s3-elf-gcc'
    version = subprocess.check_output([str(compiler), '--version'], text=True).splitlines()[0]
    if '8.4.0' not in version or 'esp-2021r2-patch5' not in version:
        raise ValueError('use the reviewed ESP-2021r2-patch5 GCC 8.4.0 toolchain')
    directory = ROOT / 'experiments/kd360s_snapshot'
    with tempfile.TemporaryDirectory(prefix='kd360s-snapshot-') as temporary:
        elf = Path(temporary) / 'snapshot.elf'
        command = [str(compiler), '-std=c11', '-Os', '-Wall', '-Wextra', '-Werror', '-mlongcalls',
                   '-ffreestanding', '-fno-builtin', '-fno-stack-protector', '-fno-optimize-sibling-calls',
                   '-nostdlib', '-Wl,--abi-windowed', '-Wl,--build-id=none',
                   '-Wl,-T,' + str(directory / 'snapshot.ld'),
                   '-DCOLLECTOR_IPV4_LE=' + hex(int.from_bytes(address.packed, 'little')) + 'u',
                   '-DCOLLECTOR_PORT_BE=' + hex(int.from_bytes(port.to_bytes(2, 'big'), 'little')) + 'u',
                   str(directory / 'snapshot.c'), '-o', str(elf)]
        subprocess.run(command, check=True, cwd=temporary)
        with stock.open('rb') as stream:
            original = stream.read(TARGET.size + 1)
        binary, manifest = build_image(original, elf.read_bytes())
        manifest['experiment'] = {
            'name': 'normal_screen_snapshot_v1', 'collector_ipv4': str(address), 'collector_port': port,
            'compiler': version, 'protocol': 'WPS1', 'observation_interval_min_ms': 100,
            'ram_record_capacity': 128, 'power_loss_retention': False, 'freshness': 'unknown',
            'every_shot_verified': False, 'wifi_policy': 'begin_once_after_setup_then_stock_ownership',
            'sources': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in ('snapshot.c', 'stock_runtime.h', 'protocol.h', 'snapshot.ld')},
        }
        write_package(output, binary, manifest)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', required=True, type=Path, help='directory containing xtensa-esp32s3-elf-gcc')
    parser.add_argument('--stock', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--collector-ip', required=True)
    parser.add_argument('--port', type=int, default=47653)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build(args.toolchain, args.stock, args.collector_ip, args.port, args.output)
    except (ValueError, OSError, FirmwareError, subprocess.CalledProcessError) as error:
        print(json.dumps({'status': 'build_rejected', 'ready_to_flash': False, 'error': str(error)}))
        return 1
    print(json.dumps({'status': result['status'], 'ready_to_flash': False,
                      'output': str(args.output), 'sha256': result['output']['sha256']}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
