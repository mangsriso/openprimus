#!/usr/bin/env python3
"""Reproduce an experimental package and inspect a private flash dump offline."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import struct
import sys

from kd360s_firmware import DEFAULT_CACHE, TARGET, FirmwareError, verify_bytes
from kd360s_test_image import HARDWARE_GATES, MAX_ELF_SIZE, PAYLOAD_LIMIT, PAYLOAD_START, build_image

SECTOR_SIZE = 4096
MAX_BACKUP_SIZE = 64 * 1024 * 1024
MAX_IMAGE_SIZE = TARGET.size + PAYLOAD_LIMIT - PAYLOAD_START + SECTOR_SIZE
PARTITION_ENTRY = struct.Struct('<HBBII16sI')
MD5_MARKER = b'\xeb\xeb' + b'\xff' * 14


@dataclass(frozen=True)
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise FirmwareError(f'{path.name} exceeds the {limit}-byte input limit')
    return data


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise FirmwareError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(',', ':'))


def verify_package(stock: bytes, elf: bytes, binary: bytes, manifest: dict,
                   *, trial_defer_confirm: bool = False) -> dict:
    if not isinstance(manifest, dict):
        raise FirmwareError('manifest must be a JSON object')
    _canonical(manifest)
    changes = manifest.get('changes')
    if not isinstance(changes, dict):
        raise FirmwareError('manifest changes must be an object')
    recorder_hooks = 'recorder_hooks' in changes
    wifi_guard = 'wifi_guard' in changes
    wifi_hooks = 'wifi_hooks' in changes
    expected, expected_manifest = build_image(stock, elf, trial_defer_confirm=trial_defer_confirm,
                                               recorder_hooks=recorder_hooks, wifi_guard=wifi_guard,
                                               wifi_hooks=wifi_hooks)
    if binary != expected:
        raise FirmwareError('package bytes differ from the image rebuilt from stock and ELF')
    base = {key: value for key, value in manifest.items() if key != 'experiment'}
    if _canonical(base) != _canonical(expected_manifest):
        raise FirmwareError('manifest differs from the rebuilt image manifest')
    experiment = manifest.get('experiment')
    if 'experiment' in manifest and not isinstance(experiment, dict):
        raise FirmwareError('experiment metadata must be an object')
    return {
        'status': 'offline_package_verified',
        'ready_to_flash': False,
        'package_reproduced': True,
        'source_sha256': TARGET.sha256,
        'elf_sha256': expected_manifest['elf']['sha256'],
        'output_sha256': expected_manifest['output']['sha256'],
        'output_size': len(binary),
        'sector_rounded_size': (len(binary) + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE,
        'trial_defer_confirm': trial_defer_confirm,
        'recorder_hooks': recorder_hooks,
        'wifi_guard': wifi_guard,
        'wifi_hooks': wifi_hooks,
        'unverified_metadata': {'experiment': experiment},
        'source_to_elf_verified': False,
        'backup_inspection': None,
        'unresolved_hardware_gates': list(HARDWARE_GATES),
        'limitations': [
            'Reconstruction does not prove that this ELF came from reviewed source.',
            'Experiment metadata, including collector address, is not verified against the ELF.',
            'Image validity does not establish every-shot capture or device readiness.',
        ],
    }


def _partitions(backup: bytes, offset: int) -> tuple[list[Partition], str]:
    if offset < SECTOR_SIZE or offset % SECTOR_SIZE or offset + SECTOR_SIZE > len(backup):
        raise FirmwareError('partition table must be sector-aligned and lie within the offset-zero dump')
    table = backup[offset:offset + 0xc00]
    partitions = []
    digest = hashlib.md5()
    md5_status = 'not_present'
    for position in range(0, len(table), PARTITION_ENTRY.size):
        record = table[position:position + PARTITION_ENTRY.size]
        if record == b'\xff' * PARTITION_ENTRY.size:
            if any(value != 0xff for value in table[position:]):
                raise FirmwareError('unexpected partition data after the table terminator')
            break
        if record[:2] == b'\xeb\xeb':
            if md5_status != 'not_present' or record[:16] != MD5_MARKER or record[16:] != digest.digest():
                raise FirmwareError('partition table MD5 mismatch or malformed checksum record')
            md5_status = 'verified'
            continue
        if md5_status == 'verified':
            raise FirmwareError('partition entry follows the MD5 record')
        magic, kind, subtype, start, size, label, flags = PARTITION_ENTRY.unpack(record)
        if magic != 0x50aa:
            raise FirmwareError('invalid partition entry magic; check the dump and table offset')
        if flags & ~1:
            raise FirmwareError('unsupported partition flags')
        try:
            name = label.split(b'\0', 1)[0].decode('ascii')
        except UnicodeDecodeError as error:
            raise FirmwareError('partition label must be ASCII') from error
        if not name or any(ord(char) < 32 or ord(char) > 126 for char in name):
            raise FirmwareError('partition label is empty or contains control characters')
        if size == 0 or start < offset + SECTOR_SIZE or start + size > len(backup):
            raise FirmwareError('partition lies outside the dump or overlaps the partition table')
        if start % (0x10000 if kind == 0 else 4):
            raise FirmwareError('partition offset has invalid alignment')
        partitions.append(Partition(name, kind, subtype, start, size, flags))
        digest.update(record)
    else:
        raise FirmwareError('partition table has no terminator')
    if not partitions:
        raise FirmwareError('partition table is empty')
    if len({part.name for part in partitions}) != len(partitions):
        raise FirmwareError('partition names are not unique')
    ordered = sorted(partitions, key=lambda part: part.offset)
    for previous, current in zip(ordered, ordered[1:]):
        if current.offset < previous.offset + previous.size:
            raise FirmwareError('partitions overlap')
    ota = [part for part in partitions if part.type == 1 and part.subtype == 0]
    if len(ota) > 1 or any(part.size != 0x2000 for part in ota):
        raise FirmwareError('OTA data partition must be unique and exactly 0x2000 bytes')
    return partitions, md5_status


def inspect_flash_backup(backup: bytes, *, partition_table_offset: int,
                         stock: bytes, candidate_size: int) -> dict:
    verify_bytes(stock)
    if len(backup) > MAX_BACKUP_SIZE:
        raise FirmwareError('flash dump exceeds the 64 MiB input limit')
    partitions, md5_status = _partitions(backup, partition_table_offset)
    required = (candidate_size + SECTOR_SIZE - 1) // SECTOR_SIZE * SECTOR_SIZE
    records = []
    matches = []
    for part in partitions:
        record = asdict(part)
        if part.type == 0:
            matched = part.size >= len(stock) and backup[part.offset:part.offset + len(stock)] == stock
            record.update(candidate_fits=part.size >= required, pinned_stock_prefix_match=matched)
            if matched:
                matches.append(part.name)
        records.append(record)
    return {
        'dump_sha256': hashlib.sha256(backup).hexdigest(),
        'provided_dump_size': len(backup),
        'partition_table_offset': partition_table_offset,
        'partition_table_md5': md5_status,
        'partitions': records,
        'pinned_stock_app_partitions': matches,
        'required_app_capacity': required,
        'selected_ota_destination': None,
        'complete_device_backup_verified': False,
        'active_application_verified': False,
        'security_state_verified': False,
        'bootloader_rollback_verified': False,
        'independent_recovery_verified': False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--stock', type=Path, default=DEFAULT_CACHE)
    parser.add_argument('--elf', type=Path, help='defaults to payload.elf in the package')
    parser.add_argument('--trial-defer-confirm', action='store_true')
    parser.add_argument('--flash-backup', type=Path, help='private dump starting at flash offset zero')
    parser.add_argument('--partition-table-offset', type=lambda value: int(value, 0))
    args = parser.parse_args(argv)
    if (args.flash_backup is None) != (args.partition_table_offset is None):
        parser.error('--flash-backup and --partition-table-offset must be supplied together')
    try:
        stock = read_bounded(args.stock, TARGET.size)
        elf = read_bounded(args.elf or args.package / 'payload.elf', MAX_ELF_SIZE)
        binary = read_bounded(args.package / 'firmware.experimental.bin', MAX_IMAGE_SIZE)
        manifest = json.loads(read_bounded(args.package / 'manifest.json', 128 * 1024),
                              object_pairs_hook=_unique_object)
        report = verify_package(stock, elf, binary, manifest, trial_defer_confirm=args.trial_defer_confirm)
        if args.flash_backup is not None:
            backup = read_bounded(args.flash_backup, MAX_BACKUP_SIZE)
            report['backup_inspection'] = inspect_flash_backup(
                backup, partition_table_offset=args.partition_table_offset, stock=stock,
                candidate_size=len(binary))
        print(json.dumps(report, indent=2, allow_nan=False))
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        print(json.dumps({'status': 'preflight_rejected', 'ready_to_flash': False, 'error': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
