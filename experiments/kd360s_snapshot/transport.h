#ifndef SNAPSHOT_TRANSPORT_H
#define SNAPSHOT_TRANSPORT_H
#include "protocol.h"
#define TRANSMIT_WINDOW_SIZE 8u
#define TRANSMIT_RETRY_MS 250u
struct transmit_window {
    uint32_t sentmask;
    uint32_t ackmask;
    uint32_t last_send[TRANSMIT_WINDOW_SIZE];
};
static inline uint32_t window_count(const struct snapshot_ring *ring) {
    uint32_t count = load_acquire(&ring->head) - ring->tail;
    return count < TRANSMIT_WINDOW_SIZE ? count : TRANSMIT_WINDOW_SIZE;
}
static inline const uint8_t *window_packet(struct snapshot_ring *ring, uint32_t index) {
    return index < window_count(ring) ? ring_slot(ring, ring->tail + index) : 0;
}
static inline int window_due(const struct transmit_window *window, uint32_t index, uint32_t now) {
    if (index >= TRANSMIT_WINDOW_SIZE) return 0;
    uint32_t bit = 1u << index;
    return !(window->ackmask & bit) && (!(window->sentmask & bit)
           || (uint32_t)(now - window->last_send[index]) >= TRANSMIT_RETRY_MS);
}
static inline void window_sent(struct transmit_window *window, uint32_t index, uint32_t now) {
    if (index >= TRANSMIT_WINDOW_SIZE || window->ackmask & (1u << index)) return;
    window->sentmask |= 1u << index;
    window->last_send[index] = now;
}
static inline int window_ack(struct snapshot_ring *ring, struct transmit_window *window,
                             const uint8_t *ack, uint32_t size) {
    if (size != 16 || ack[0] != 'W' || ack[1] != 'P' || ack[2] != 'A' || ack[3] != '1') return 0;
    uint32_t count = window_count(ring);
    for (uint32_t i = 0; i < count; ++i) {
        uint32_t bit = 1u << i;
        if (!(window->sentmask & bit) || window->ackmask & bit) continue;
        const uint8_t *packet = ring_slot(ring, ring->tail + i);
        if (!bytes_equal(ack + 4, packet + 8, 8) || get32(ack + 12) != get32(packet + 16)) continue;
        window->ackmask |= bit;
        uint32_t released = 0;
        while (released < count && window->ackmask & (1u << released)) ++released;
        if (released) {
            window->sentmask >>= released;
            window->ackmask >>= released;
            for (uint32_t j = 0; j < TRANSMIT_WINDOW_SIZE; ++j)
                window->last_send[j] = j + released < TRANSMIT_WINDOW_SIZE ? window->last_send[j + released] : 0;
            store_release(&ring->tail, ring->tail + released);
        }
        return 1;
    }
    return 0;
}
#endif
