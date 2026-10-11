import csv
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / 'scripts/kd360s_live.py'


@unittest.skipUnless(os.name == 'posix' and shutil.which('cc'), 'POSIX host and C compiler required')
class ProcessRecoveryTests(unittest.TestCase):
    def test_c_ring_through_udp_survives_collector_kill_and_exports_exact_bytes(self):
        source = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "protocol.h"
int main(void) {
    struct snapshot_ring ring = {0};
    uint8_t packet[SNAPSHOT_BYTES] = {0}, ack[16];
    memcpy(packet, "WPS2", 4); packet[4] = 2; packet[5] = 1; packet[6] = SNAPSHOT_BYTES;
    put32(packet + 8, 0x76543210); put32(packet + 12, 0xfedcba98);
    put32(packet + 32, 0x41100000); put32(packet + 36, 0x3f800000);
    put32(packet + 56, 1);
    for (unsigned i = 0; i < 160; ++i) {
        put32(packet + 20, i * 100);
        assert(enqueue(&ring, packet) == (i < 128));
    }
    assert(ring.dropped == 32);
    for (unsigned i = 0; i < 129; ++i) {
        const uint8_t *next = pending(&ring); assert(next);
        assert(fwrite(next, 1, SNAPSHOT_BYTES, stdout) == SNAPSHOT_BYTES); assert(!fflush(stdout));
        assert(fread(ack, 1, 16, stdin) == 16);
        assert(acknowledge(&ring, ack, 16));
        if (i == 0) {
            put32(packet + 20, 16000);
            assert(enqueue(&ring, packet));
        }
    }
    assert(!pending(&ring));
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix='kd360s-process-test-') as directory:
            temporary = Path(directory)
            cfile, executable = temporary / 'producer.c', temporary / 'producer'
            database = temporary / 'capture.sqlite'
            cfile.write_text(source)
            subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'experiments/kd360s_snapshot'), str(cfile),
                            '-o', str(executable)], check=True, capture_output=True)
            processes = []
            with (temporary / 'children.log').open('wb') as errors, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                client.bind(('127.0.0.1', 0))
                client.settimeout(3)
                try:
                    collector, address = self.start_collector(database, 0, errors, processes)
                    producer = subprocess.Popen([str(executable)], stdin=subprocess.PIPE,
                                                stdout=subprocess.PIPE, stderr=errors)
                    processes.append(producer)
                    originals = {}
                    for index in range(129):
                        packet = self.read_exact(producer.stdout, 128)
                        seq = struct.unpack_from('<I', packet, 16)[0]
                        originals[seq] = packet.hex()
                        expected = b'WPA1' + packet[8:16] + packet[16:20]
                        client.sendto(packet, address)
                        self.assertEqual(client.recvfrom(64)[0], expected)
                        if index in (0, 63):
                            if index == 63:
                                collector.kill()
                                collector.wait(timeout=5)
                                collector, restarted = self.start_collector(database, address[1], errors, processes)
                                self.assertEqual(restarted, address)
                            client.sendto(packet, address)
                            self.assertEqual(client.recvfrom(64)[0], expected)
                        if index == 0:
                            conflict = bytearray(packet)
                            conflict[20] ^= 1
                            client.settimeout(0.15)
                            for rejected in (bytes(conflict), packet + b'x'):
                                client.sendto(rejected, address)
                                with self.assertRaises(socket.timeout):
                                    client.recvfrom(64)
                            client.settimeout(3)
                        producer.stdin.write(expected)
                        producer.stdin.flush()
                    producer.stdin.close()
                    self.assertEqual(producer.wait(timeout=5), 0)
                    collector.kill()
                    collector.wait(timeout=5)
                    self.assertEqual(list(originals), list(range(128)) + [160])
                    rows = [json.loads(line) for line in subprocess.check_output(
                        [sys.executable, str(LIVE), 'export', str(database)], text=True).splitlines()]
                    self.assertEqual({row['seq']: row['raw_hex'] for row in rows}, originals)
                    self.assertEqual(len(rows), 129)
                    self.assertTrue(all(row['session'] == 'fedcba9876543210' for row in rows))
                    self.assertTrue(all(row['pressure'] == 9.0 and row['flow'] == 1.0 for row in rows))
                    self.assertEqual([row['dropped_cumulative'] for row in rows], [0] * 128 + [32])
                    self.assertEqual(rows[-1]['observation_ms'], 16000)
                    csv_text = subprocess.check_output([sys.executable, str(LIVE), 'export', str(database),
                                                   '--format', 'csv'], text=True)
                    csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
                    self.assertEqual(len(csv_rows), 129)
                    self.assertEqual({int(row['seq']): row['raw_hex'] for row in csv_rows}, originals)
                    self.assertEqual(int(csv_rows[-1]['dropped_cumulative']), 32)
                    with sqlite3.connect(database) as connection:
                        self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone(), ('ok',))
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
                        if process.stdin and not process.stdin.closed:
                            process.stdin.close()
                        if process.stdout:
                            process.stdout.close()

    @staticmethod
    def read_exact(stream, size):
        result = bytearray()
        deadline = time.monotonic() + 10
        with selectors.DefaultSelector() as ready:
            ready.register(stream, selectors.EVENT_READ)
            while len(result) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise AssertionError('child output timed out')
                chunk = os.read(stream.fileno(), size - len(result))
                if not chunk:
                    raise AssertionError('child closed output before completing record')
                result.extend(chunk)
        return bytes(result)

    def start_collector(self, database, port, errors, processes):
        process = subprocess.Popen([sys.executable, str(LIVE), 'serve', str(database),
                                    '--port', str(port)], stdout=subprocess.PIPE, stderr=errors)
        processes.append(process)
        line = bytearray()
        deadline = time.monotonic() + 10
        while not line.endswith(b'\n'):
            line.extend(self.read_exact(process.stdout, 1))
            if len(line) > 4096 or time.monotonic() > deadline:
                raise AssertionError('invalid collector startup output')
        return process, tuple(json.loads(line)['listening'])
