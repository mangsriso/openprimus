#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "retention.h"
static struct snapshot_ring ring;
static uint32_t available, largest, requested;
static unsigned queries, allocations;
static int allocation_fails;
static uint32_t free_size(uint32_t caps) { assert(caps == 0x404); ++queries; return available; }
static uint32_t largest_block(uint32_t caps) { assert(caps == 0x404); ++queries; return largest; }
static void *malloc_caps(uint32_t bytes, uint32_t caps) {
    assert(caps == 0x404); ++allocations; requested = bytes;
    return allocation_fails ? NULL : malloc(bytes);
}
static void reset(void) {
    free(ring.external_records); memset(&ring, 0, sizeof(ring));
    available = largest = requested = 0; queries = allocations = allocation_fails = 0;
}
static void ack_for(const uint8_t *p, uint8_t *a) {
    memcpy(a, "WPA1", 4); memcpy(a + 4, p + 8, 8); memcpy(a + 12, p + 16, 4);
}
static void pop(uint32_t seq) {
    uint8_t ack[16]; const uint8_t *item = pending(&ring);
    assert(item && get32(item + 16) == seq); ack_for(item, ack);
    assert(acknowledge(&ring, ack, sizeof(ack)));
}
static void test_policy(void) {
    assert(!retention_bytes(0, UINT32_MAX));
    assert(!retention_bytes(UINT32_MAX, 65535));
    assert(!retention_bytes(131071, UINT32_MAX));
    assert(retention_bytes(131072, 65536) == 32768);
    assert(retention_bytes(UINT32_MAX, UINT32_MAX) == 1048576);
    assert(retention_bytes(1000000, 1000000) == 131072);
    assert(retention_bytes(4000000, 200000) == 65536);
    for (uint32_t limit = 32768; limit <= 1048576; limit *= 2) {
        assert(retention_bytes(limit * 4, limit * 2) == limit);
        assert(retention_bytes(limit * 4 - 1, UINT32_MAX) == (limit == 32768 ? 0 : limit / 2));
    }
}
static void test_startup(void) {
    reset(); assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 128);
    assert(queries == 2 && allocations == 0 && !ring.external_records);
    available = largest = UINT32_MAX;
    assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 128);
    assert(queries == 2 && allocations == 0);
    reset(); available = largest = UINT32_MAX; allocation_fails = 1;
    assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 128);
    assert(allocations == 1 && requested == 1048576 && !ring.external_records);
    allocation_fails = 0;
    assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 128);
    assert(queries == 2 && allocations == 1);
    reset(); ring.sequence = 1;
    assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 128);
    assert(queries == 0 && allocations == 0);
}
static void test_queue(int external) {
    reset();
    if (external) {
        available = 262144; largest = 131072;
        assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 512);
        assert(requested == 65536 && allocations == 1);
        assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 512);
        assert(queries == 2 && allocations == 1);
    }
    uint32_t capacity = ring_capacity(&ring);
    ring.head = ring.tail = UINT32_MAX - 7;
    uint8_t packet[SNAPSHOT_BYTES] = {0}, ack[16]; packet[8] = 0x43;
    assert(!enqueue_reserved(&ring, packet, capacity + 1));
    assert(!ring.sequence && !ring.dropped);
    for (uint32_t i = 0; i < capacity - 16; ++i) assert(enqueue_reserved(&ring, packet, 16));
    assert(!enqueue_reserved(&ring, packet, 16)); assert(ring.dropped == 1);
    for (uint32_t i = 0; i < 16; ++i) assert(enqueue(&ring, packet));
    assert(!enqueue(&ring, packet)); assert(ring.dropped == 2);
    const uint8_t *first = pending(&ring); assert(first == ring_slot(&ring, UINT32_MAX - 7));
    if (external) {
        assert(first >= ring.external_records && first < ring.external_records + requested);
        for (unsigned i = 0; i < sizeof(ring.records); ++i) assert(((uint8_t *)ring.records)[i] == 0);
    }
    ack_for(first, ack); ack[4] ^= 1; assert(!acknowledge(&ring, ack, sizeof(ack)));
    ack[4] ^= 1; assert(acknowledge(&ring, ack, sizeof(ack))); assert(!acknowledge(&ring, ack, sizeof(ack)));
    for (uint32_t i = 1; i < capacity - 16; ++i) pop(i);
    for (uint32_t i = capacity - 15; i <= capacity; ++i) pop(i);
    assert(!pending(&ring));
    assert(enqueue_reserved(&ring, packet, 16));
    assert(get32(pending(&ring) + 24) == 2); pop(capacity + 2);
    assert(!enqueue_reserved(&ring, packet, capacity)); assert(ring.dropped == 3);
    ring.sequence = UINT32_MAX; assert(!enqueue(&ring, packet)); assert(!pending(&ring));
}
static void *producer(void *unused) {
    (void)unused;
    for (uint32_t i = 0; i < 50000; ++i) {
        uint8_t p[SNAPSHOT_BYTES] = {0}; put32(p + 32, i);
        while ((uint32_t)(ring.head - load_acquire(&ring.tail)) == ring_capacity(&ring)) sched_yield();
        assert(enqueue(&ring, p));
    }
    return NULL;
}
static void test_concurrent_external(void) {
    reset(); available = largest = UINT32_MAX;
    assert(retention_startup(&ring, free_size, largest_block, malloc_caps) == 8192);
    ring.head = ring.tail = UINT32_MAX - 16;
    pthread_t thread; assert(!pthread_create(&thread, NULL, producer, NULL));
    for (uint32_t i = 0; i < 50000; ++i) {
        const uint8_t *item; while (!(item = pending(&ring))) sched_yield();
        assert(get32(item + 32) == i); pop(i);
    }
    assert(!pthread_join(thread, NULL)); assert(!pending(&ring) && !ring.dropped);
}
int main(void) {
    test_policy(); test_startup(); test_queue(0); test_queue(1); test_concurrent_external(); reset();
    puts("retention policy, one allocation, fallback, reserve, wrap, ACK and 50000 concurrent external deliveries passed");
}
