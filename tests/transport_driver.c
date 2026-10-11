#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "transport.h"
static struct snapshot_ring ring;
static struct transmit_window window;
static void make_ack(const uint8_t *packet, uint8_t *ack) {
    assert(packet); memcpy(ack, "WPA1", 4); memcpy(ack + 4, packet + 8, 12);
}
static void enqueue_records(unsigned count) {
    for (unsigned i = 0; i < count; ++i) {
        uint8_t packet[SNAPSHOT_BYTES] = {0}; packet[8] = 0xab;
        ring.sequence += i & 1;
        assert(enqueue(&ring, packet));
    }
}
int main(void) {
    uint8_t ack[8][16], invalid[16];
    assert(window_count(&ring) == 0 && !window_packet(&ring, 0));
    enqueue_records(3); assert(window_count(&ring) == 3 && !window_packet(&ring, 3));
    make_ack(window_packet(&ring, 0), invalid);
    assert(!window_ack(&ring, &window, invalid, 16));
    enqueue_records(10); assert(window_count(&ring) == 8 && !window_packet(&ring, 8));
    for (unsigned i = 0; i < 8; ++i) {
        make_ack(window_packet(&ring, i), ack[i]);
        assert(window_due(&window, i, UINT32_MAX - 99));
        window_sent(&window, i, UINT32_MAX - 99);
        assert(!window_due(&window, i, 149));
        assert(window_due(&window, i, 150));
    }
    assert(!window_due(&window, 8, 0));
    memcpy(invalid, ack[0], 16); invalid[4] ^= 1; assert(!window_ack(&ring, &window, invalid, 16));
    memcpy(invalid, ack[0], 16); invalid[0] = 'X'; assert(!window_ack(&ring, &window, invalid, 16));
    memcpy(invalid, ack[0], 16); put32(invalid + 12, 0xabc); assert(!window_ack(&ring, &window, invalid, 16));
    assert(!window_ack(&ring, &window, ack[0], 15)); assert(!window_ack(&ring, &window, ack[0], 17));
    for (unsigned i = 1; i < 8; ++i) {
        assert(window_ack(&ring, &window, ack[i], 16));
        assert(!window_ack(&ring, &window, ack[i], 16));
        assert(ring.tail == 0 && !window_due(&window, i, 1000));
    }
    assert(window_due(&window, 0, 150)); window_sent(&window, 0, 150);
    assert(!window_due(&window, 0, 399)); assert(window_due(&window, 0, 400));
    assert(window_ack(&ring, &window, ack[0], 16));
    assert(ring.tail == 8 && !window.sentmask && !window.ackmask && window_count(&ring) == 5);
    assert(!window_ack(&ring, &window, ack[7], 16));
    make_ack(window_packet(&ring, 1), ack[1]); window_sent(&window, 1, 50);
    make_ack(window_packet(&ring, 0), ack[0]); window_sent(&window, 0, 60);
    assert(window_ack(&ring, &window, ack[0], 16));
    assert(window.sentmask == 1 && window.last_send[0] == 50);
    assert(window_ack(&ring, &window, ack[1], 16));
    make_ack(window_packet(&ring, 0), ack[0]); window_sent(&window, 0, 70);
    memset(&window, 0, sizeof(window));
    assert(!window_ack(&ring, &window, ack[0], 16));
    assert(window_due(&window, 0, 71)); window_sent(&window, 0, 71);
    assert(window_ack(&ring, &window, ack[0], 16));
    memset(&ring, 0, sizeof(ring)); memset(&window, 0, sizeof(window));
    ring.head = ring.tail = UINT32_MAX - 3; enqueue_records(SNAPSHOT_CAPACITY);
    uint8_t first[SNAPSHOT_BYTES]; memcpy(first, window_packet(&ring, 0), sizeof(first));
    for (unsigned i = 0; i < 8; ++i) { make_ack(window_packet(&ring, i), ack[i]); window_sent(&window, i, 0); }
    for (unsigned i = 1; i < 8; ++i) assert(window_ack(&ring, &window, ack[i], 16));
    uint8_t packet[SNAPSHOT_BYTES] = {0}; assert(!enqueue(&ring, packet));
    assert(!memcmp(first, window_packet(&ring, 0), sizeof(first)));
    assert(window_ack(&ring, &window, ack[0], 16)); assert(ring.tail == 4);
    enqueue_records(8); assert(window_count(&ring) == 8);
    assert(!window_ack(&ring, &window, ack[0], 16));
    puts("window partial startup, sparse sequence, out-of-order ACK, retry, reconnect and slot reuse passed");
}
