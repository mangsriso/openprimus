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
from kd360s_test_image import build_image, parse_elf, write_package

ROOT = Path(__file__).resolve().parents[1]


def verify_marker(elf):
    payload = parse_elf(elf)
    symbols = [s for s in payload.symbols if s.name == 'recorder_key_marker']
    if len(symbols) != 1:
        raise FirmwareError('recorder requires one pthread key marker')
    marker = symbols[0]
    text = next(s for s in payload.sections if s.name == '.text')
    if (marker.section_name != '.text' or marker.info & 15 != 2 or marker.size != 5
            or marker.address % 4 or not text.address <= marker.address < marker.address + marker.size <= text.address + len(text.data)
            or any(s.name != marker.name and s.info & 15 == 2 and s.address == marker.address for s in payload.symbols)):
        raise FirmwareError('pthread key marker must be a distinct bounded executable function')
    offset = marker.address - text.address
    if text.data[offset:offset + marker.size] != bytes.fromhex('3641001df0'):
        raise FirmwareError('pthread key marker must be an inert ENTRY/RETW function')


def build(toolchain, stock, collector, port, output, *, trial_defer_confirm=False):
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
        command = [str(compiler), '-std=c11', '-DWIFI_HOOKS=1', '-Os', '-Wall', '-Wextra', '-Werror', '-mlongcalls',
                   '-ffreestanding', '-fno-builtin', '-fno-stack-protector', '-fno-optimize-sibling-calls',
                   '-fno-ipa-icf',
                   '-nostdlib', '-Wl,--abi-windowed', '-Wl,--build-id=none',
                   '-Wl,-T,' + str(directory / 'snapshot.ld'),
                   '-DCOLLECTOR_IPV4_LE=' + hex(int.from_bytes(address.packed, 'little')) + 'u',
                   '-DCOLLECTOR_PORT_BE=' + hex(int.from_bytes(port.to_bytes(2, 'big'), 'little')) + 'u',
                   str(directory / 'snapshot.c'), str(directory / 'hooks.S'),
                   str(directory / 'wifi_guard.S'), str(directory / 'wifi_hooks.S'), '-o', str(elf)]
        subprocess.run(command, check=True, cwd=temporary)
        with stock.open('rb') as stream:
            original = stream.read(TARGET.size + 1)
        elf_bytes = elf.read_bytes()
        verify_marker(elf_bytes)
        binary, manifest = build_image(original, elf_bytes, trial_defer_confirm=trial_defer_confirm,
                                       recorder_hooks=True, wifi_guard=True, wifi_hooks=True)
        manifest['experiment'] = {
            'name': 'lifecycle_recorder_v2', 'collector_ipv4': str(address), 'collector_port': port,
            'compiler': version, 'protocol': 'WPS2', 'observation_interval_min_ms': 100,
            'transport_window_records': 8, 'transport_retry_ms': 250,
            'ram_record_capacity_min': 128, 'ram_record_capacity_max': 8192,
            'external_ram_limit_bytes': 1048576, 'boundary_reserve_records': 16,
            'idle_observation_interval_min_ms': 5000, 'idle_queue_empty_only': True,
            'power_loss_retention': False,
            'freshness': 'hook_invocations_with_source_generation_and_time',
            'every_shot_verified': False, 'wifi_policy': 'task_scoped_recursive_gate_with_owner_checked_worker_retry',
            'retry_interval_min_ms': 10000, 'native_sdk_tasks_bypass_gate': True,
            'UIwait_latency_bounded': False,
            'sources': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in ('snapshot.c', 'stock_runtime.h', 'protocol.h', 'snapshot.ld',
                                     'hooks.S', 'wifi_guard.S', 'wifi_hooks.S', 'wifi_gate.h', 'recorder_context.h',
                                     'retention.h', 'transport.h')},
        }
        write_package(output, binary, manifest, elf=elf_bytes)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--toolchain', required=True, type=Path, help='directory containing xtensa-esp32s3-elf-gcc')
    parser.add_argument('--stock', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--collector-ip', required=True)
    parser.add_argument('--port', type=int, default=47653)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--trial-defer-confirm', action='store_true',
                        help='offline trial that suppresses early stock OTA confirmation; rollback is not guaranteed')
    args = parser.parse_args(argv)
    try:
        result = build(args.toolchain, args.stock, args.collector_ip, args.port, args.output,
                       trial_defer_confirm=args.trial_defer_confirm)
    except (ValueError, OSError, FirmwareError, subprocess.CalledProcessError) as error:
        print(json.dumps({'status': 'build_rejected', 'ready_to_flash': False, 'error': str(error)}))
        return 1
    print(json.dumps({'status': result['status'], 'ready_to_flash': False,
                      'output': str(args.output), 'sha256': result['output']['sha256']}, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
