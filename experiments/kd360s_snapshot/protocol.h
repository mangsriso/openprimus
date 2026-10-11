#ifndef SNAPSHOT_PROTOCOL_H
#define SNAPSHOT_PROTOCOL_H
#include <stdint.h>
#define SNAPSHOT_CAPACITY 128u
#define SNAPSHOT_BYTES 128u
struct snapshot_ring {
    uint32_t head;
    uint32_t tail;
    uint32_t sequence;
    uint32_t dropped;
    uint8_t records[SNAPSHOT_CAPACITY][SNAPSHOT_BYTES];
    uint8_t *external_records;
    uint32_t external_capacity;
    uint32_t retention_initialized;
};
static inline uint32_t load_acquire(const uint32_t *p) {
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}
static inline void store_release(uint32_t *p, uint32_t value) {
    __atomic_store_n(p, value, __ATOMIC_RELEASE);
}
static inline void put32(uint8_t *p, uint32_t value) {
    for (unsigned i = 0; i < 4; ++i) p[i] = value >> (i * 8);
}
static inline uint32_t get32(const uint8_t *p) {
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
static inline int bytes_equal(const uint8_t *a, const uint8_t *b, unsigned n) {
    for (unsigned i = 0; i < n; ++i) if (a[i] != b[i]) return 0;
    return 1;
}
static inline uint32_t ring_capacity(const struct snapshot_ring *ring) {
    return ring->external_records ? ring->external_capacity : SNAPSHOT_CAPACITY;
}
static inline uint8_t *ring_slot(struct snapshot_ring *ring, uint32_t index) {
    index &= ring_capacity(ring) - 1;
    return ring->external_records ? ring->external_records + index * SNAPSHOT_BYTES : ring->records[index];
}
static inline int enqueue_reserved(struct snapshot_ring *ring, uint8_t packet[SNAPSHOT_BYTES], uint32_t reserve) {
    uint32_t capacity = ring_capacity(ring);
    if (reserve > capacity) return 0;
    if (ring->sequence == UINT32_MAX) return 0;
    put32(packet + 16, ring->sequence++);
    if ((uint32_t)(ring->head - load_acquire(&ring->tail)) >= capacity - reserve) {
        if (ring->dropped != UINT32_MAX) ++ring->dropped;
        return 0;
    }
    put32(packet + 24, ring->dropped);
    uint8_t *slot = ring_slot(ring, ring->head);
    for (unsigned i = 0; i < SNAPSHOT_BYTES; ++i) slot[i] = packet[i];
    store_release(&ring->head, ring->head + 1);
    return 1;
}
static inline int enqueue(struct snapshot_ring *ring, uint8_t packet[SNAPSHOT_BYTES]) {
    return enqueue_reserved(ring, packet, 0);
}
static inline const uint8_t *pending(struct snapshot_ring *ring) {
    return ring->tail == load_acquire(&ring->head) ? 0 : ring_slot(ring, ring->tail);
}
static inline int acknowledge(struct snapshot_ring *ring, const uint8_t *ack, unsigned size) {
    const uint8_t *packet = pending(ring);
    if (!packet || size != 16 || ack[0] != 'W' || ack[1] != 'P' || ack[2] != 'A' || ack[3] != '1'
        || !bytes_equal(ack + 4, packet + 8, 8) || get32(ack + 12) != get32(packet + 16)) return 0;
    store_release(&ring->tail, ring->tail + 1);
    return 1;
}
#endif
