import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('cc'), 'host C compiler required')
class SnapshotRingTests(unittest.TestCase):
    def test_overflow_replay_wrap_and_concurrent_delivery(self):
        source = r'''
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include "protocol.h"
static struct snapshot_ring ring;
static void ack_for(const uint8_t *p, uint8_t *a) {
    memcpy(a, "WPA1", 4); memcpy(a + 4, p + 8, 8); memcpy(a + 12, p + 16, 4);
}
static void *producer(void *unused) {
    (void)unused;
    for (uint32_t i = 0; i < 50000; ++i) {
        uint8_t p[SNAPSHOT_BYTES] = {0}; put32(p + 32, i);
        while ((uint32_t)(ring.head - load_acquire(&ring.tail)) == SNAPSHOT_CAPACITY) sched_yield();
        assert(enqueue(&ring, p));
    }
    return 0;
}
int main(void) {
    uint8_t p[SNAPSHOT_BYTES] = {0}, a[16];
    p[8] = 0xfe;
    for (unsigned i = 0; i < SNAPSHOT_CAPACITY; ++i) assert(enqueue(&ring, p));
    assert(!enqueue(&ring, p)); assert(ring.dropped == 1); assert(ring.sequence == 129);
    assert(get32(pending(&ring) + 16) == 0);
    ack_for(pending(&ring), a); a[4] ^= 1;
    assert(!acknowledge(&ring, a, 16)); a[4] ^= 1;
    assert(!acknowledge(&ring, a, 15)); assert(acknowledge(&ring, a, 16));
    assert(!acknowledge(&ring, a, 16));
    assert(enqueue(&ring, p));
    for (unsigned i = 1; i < 128; ++i) {
        assert(get32(pending(&ring) + 16) == i); ack_for(pending(&ring), a); assert(acknowledge(&ring, a, 16));
    }
    assert(get32(pending(&ring) + 16) == 129); assert(get32(pending(&ring) + 24) == 1);
    ack_for(pending(&ring), a); assert(acknowledge(&ring, a, 16)); assert(!pending(&ring));
    ring.head = ring.tail = UINT32_MAX;
    assert(enqueue(&ring, p)); assert(ring.head == 0);
    ack_for(pending(&ring), a); assert(acknowledge(&ring, a, 16)); assert(ring.tail == 0);
    ring.sequence = UINT32_MAX; assert(!enqueue(&ring, p)); assert(!pending(&ring));
    memset(&ring, 0, sizeof(ring));
    pthread_t thread; assert(!pthread_create(&thread, 0, producer, 0));
    for (uint32_t i = 0; i < 50000; ++i) {
        const uint8_t *item; while (!(item = pending(&ring))) sched_yield();
        assert(get32(item + 16) == i); assert(get32(item + 32) == i);
        ack_for(item, a); assert(acknowledge(&ring, a, 16));
    }
    assert(!pthread_join(thread, 0)); assert(!pending(&ring)); assert(!ring.dropped);
    puts("overflow, replay, wrap and 50000 concurrent deliveries passed");
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            c = pathlib.Path(temporary) / 'ring.c'
            binary = pathlib.Path(temporary) / 'ring'
            c.write_text(source)
            subprocess.run(['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread',
                            '-I', str(ROOT / 'experiments/kd360s_snapshot'), str(c), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], check=True, text=True, capture_output=True, timeout=20)
            self.assertIn('50000 concurrent deliveries passed', result.stdout)
