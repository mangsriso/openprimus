#ifndef SNAPSHOT_RETENTION_H
#define SNAPSHOT_RETENTION_H
#include "protocol.h"
#define RETENTION_CAPS 0x404u
#define RETENTION_MIN_BYTES (32u * 1024u)
#define RETENTION_MAX_BYTES (1024u * 1024u)
_Static_assert((SNAPSHOT_CAPACITY & (SNAPSHOT_CAPACITY - 1)) == 0, "Ring capacity must be a power of two");
_Static_assert((SNAPSHOT_BYTES & (SNAPSHOT_BYTES - 1)) == 0, "Record size must be a power of two");
static inline uint32_t retention_bytes(uint32_t free_bytes, uint32_t largest_bytes) {
    uint32_t limit = RETENTION_MAX_BYTES;
    if (free_bytes / 4 < limit) limit = free_bytes / 4;
    if (largest_bytes / 2 < limit) limit = largest_bytes / 2;
    if (limit < RETENTION_MIN_BYTES) return 0;
    uint32_t size = RETENTION_MIN_BYTES;
    while (size <= limit / 2) size *= 2;
    return size;
}
/* Call before publishing the zero-initialized ring to either task. */
static inline uint32_t retention_startup(struct snapshot_ring *ring,
        uint32_t (*free_size)(uint32_t), uint32_t (*largest_block)(uint32_t),
        void *(*malloc_caps)(uint32_t, uint32_t)) {
    if (ring->retention_initialized || ring->external_records || ring->head || ring->tail || ring->sequence || ring->dropped)
        return ring_capacity(ring);
    ring->retention_initialized = 1;
    uint32_t available = free_size(RETENTION_CAPS);
    uint32_t largest = largest_block(RETENTION_CAPS);
    uint32_t bytes = retention_bytes(available, largest);
    if (bytes) {
        uint8_t *records = malloc_caps(bytes, RETENTION_CAPS);
        if (records) {
            ring->external_records = records;
            ring->external_capacity = bytes / SNAPSHOT_BYTES;
        }
    }
    return ring_capacity(ring);
}
#endif
