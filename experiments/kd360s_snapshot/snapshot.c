#include "protocol.h"
#include "stock_runtime.h"
#include "recorder_context.h"
#include "retention.h"
#include "transport.h"

#define BOUNDARY_RESERVE 16u
#define IDLE_OBSERVATION_MS 5000u

struct ipv4_address {
    uint8_t length;
    uint8_t family;
    uint16_t port;
    uint32_t ip;
    uint8_t zero[8];
};
_Static_assert(sizeof(struct ipv4_address) == 16, "lwIP sockaddr_in layout");
struct context {
    struct snapshot_ring ring;
    uint32_t ready;
    uint8_t session[8];
    uint32_t shot_id, shot_open, start_argument;
    uint32_t pressure_generation, pressure_ms, pressure_bits, pressure_adc;
    uint32_t rx_generation, rx_ms, source_flags;
    uint8_t rx[28];
    char ssid[33];
    char password[65];
};
static void zero(void *memory, uint32_t size) {
    volatile uint8_t *p = memory;
    while (size--) *p++ = 0;
}
static int copy_string(char *destination, unsigned capacity, uint32_t object) {
    const char *source = string_address((read8(object + 15) & 0x80) ? object : read32(object));
    if (!source) return 0;
    for (unsigned i = 0; i < capacity; ++i) {
        destination[i] = source[i];
        if (!source[i]) return 1;
    }
    destination[capacity - 1] = 0;
    return 0;
}
static void network_worker(void *argument) {
    struct context *c = argument;
    /* Only the initial connection is ours; stock settings retain later Wi-Fi ownership. */
    wifi_begin(WIFI_OBJECT, c->ssid, c->password, 0, 0, 1);
    zero(c->password, sizeof(c->password));
    struct ipv4_address destination;
    zero(&destination, sizeof(destination));
    destination.length = 16;
    destination.family = 2;
    destination.port = COLLECTOR_PORT_BE;
    destination.ip = COLLECTOR_IPV4_LE;
    int socket = -1;
    struct transmit_window window;
    zero(&window, sizeof(window));
    for (;;) {
        delay(10);
        if (wifi_status(WIFI_OBJECT) != 3) {
            if (socket >= 0) udp_close(socket);
            socket = -1;
            zero(&window, sizeof(window));
            delay(250);
            continue;
        }
        if (socket < 0) {
            socket = udp_socket(2, 2, 0);
            if (socket < 0) { delay(250); continue; }
        }
        uint32_t count = window_count(&c->ring);
        if (!count) continue;
        uint32_t now = millis();
        for (uint32_t index = 0; index < count; ++index) {
            if (!window_due(&window, index, now)) continue;
            const uint8_t *packet = window_packet(&c->ring, index);
            if (udp_send(socket, packet, SNAPSHOT_BYTES, 8, &destination, 16) != SNAPSHOT_BYTES) {
                udp_close(socket);
                socket = -1;
                zero(&window, sizeof(window));
                break;
            }
            window_sent(&window, index, now);
        }
        if (socket < 0) { delay(250); continue; }
        for (uint32_t received_count = 0; received_count < TRANSMIT_WINDOW_SIZE; ++received_count) {
            uint8_t ack[17];
            struct ipv4_address sender;
            zero(&sender, sizeof(sender));
            uint32_t sender_size = sizeof(sender);
            int received = udp_receive(socket, ack, sizeof(ack), 8, &sender, &sender_size);
            if (received < 0) break;
            if (received == 16 && sender_size == 16 && sender.family == 2
                && sender.ip == destination.ip && sender.port == destination.port)
                window_ack(&c->ring, &window, ack, 16);
        }
    }
}
static void observe(struct context *c, uint32_t now, uint32_t duration, uint32_t kind, uint32_t flags) {
    uint8_t packet[SNAPSHOT_BYTES];
    zero(packet, sizeof(packet));
    packet[0] = 'W'; packet[1] = 'P'; packet[2] = 'S'; packet[3] = '2';
    packet[4] = 2; packet[5] = kind; packet[6] = SNAPSHOT_BYTES;
    for (unsigned i = 0; i < 8; ++i) packet[8 + i] = c->session[i];
    put32(packet + 20, now);
    put32(packet + 28, duration);
    put32(packet + 32, read32(0x3fc9d0c0u));
    put32(packet + 36, read32(0x3fc9d0c8u));
    put32(packet + 40, read32(0x3fc9d0bcu));
    put32(packet + 44, read32(0x3fc9d0c4u));
    uint16_t volume = read16(0x3fc9d0a0u);
    packet[48] = volume; packet[49] = volume >> 8;
    packet[50] = read8(0x3fc9cfe1u);
    packet[51] = read8(0x3fc9ce52u);
    packet[52] = read8(0x3fc9ca66u);
    packet[53] = read8(0x3fc9ca6au);
    uint16_t diagnostic = read16(0x3fc9ff64u);
    packet[54] = diagnostic; packet[55] = diagnostic >> 8;
    put32(packet + 56, 1 | c->source_flags | flags);
    put32(packet + 60, c->shot_id);
    put32(packet + 64, c->pressure_generation);
    put32(packet + 68, c->pressure_ms);
    put32(packet + 72, c->rx_generation);
    put32(packet + 76, c->rx_ms);
    put32(packet + 80, c->start_argument);
    put32(packet + 84, c->pressure_bits);
    put32(packet + 88, c->pressure_adc);
    if (c->source_flags & 4) {
        put32(packet + 92, sizeof(c->rx));
        for (unsigned i = 0; i < sizeof(c->rx); ++i) packet[96 + i] = c->rx[i];
    }
    put32(packet + 124, ring_capacity(&c->ring));
    enqueue_reserved(&c->ring, packet, kind == 2 || kind == 3 ? 0 : BOUNDARY_RESERVE);
}
static void increment(struct context *c, uint32_t *counter) {
    if (*counter == UINT32_MAX) c->source_flags |= 32;
    else ++*counter;
}
void recorder_after(uint32_t kind, uint32_t argument, uint32_t result) {
    struct context *c = recorder_context();
    if (!c || !load_acquire(&c->ready)) return;
    uint32_t now = millis(), flags = 0;
    switch (kind) {
    case 2:
        if (c->shot_open) flags |= 16;
        increment(c, &c->shot_id);
        c->shot_open = 1;
        c->start_argument = argument;
        break;
    case 3:
        if (!c->shot_open) flags |= 8;
        c->shot_open = 0;
        break;
    case 4:
        increment(c, &c->pressure_generation);
        c->pressure_ms = now;
        c->pressure_bits = result;
        c->pressure_adc = argument;
        c->source_flags |= 2;
        if (!c->shot_open) return;
        break;
    case 5:
        increment(c, &c->rx_generation);
        c->rx_ms = now;
        for (unsigned i = 0; i < sizeof(c->rx); ++i) c->rx[i] = read8(0x3fc9ce56u + i);
        c->source_flags |= 4;
        if (!c->shot_open) return;
        break;
    default:
        return;
    }
    observe(c, now, 0, kind, flags);
}
void snapshot_entry(void *argument) {
    (void)argument;
    stock_setup();
    volatile char task_name[5];
    task_name[0] = 'w'; task_name[1] = 'p'; task_name[2] = 'm'; task_name[3] = 's'; task_name[4] = 0;
    struct context *c = allocate(sizeof(struct context));
    if (c) {
        zero(c, sizeof(*c));
        int bound = RECORDER_BIND_FAILED;
        if (copy_string(c->ssid, sizeof(c->ssid), 0x3fc9ff3cu) && c->ssid[0]
            && copy_string(c->password, sizeof(c->password), 0x3fc9ff2cu)) {
            retention_startup(&c->ring, caps_free_size, caps_largest_block, caps_allocate);
            fill_random(c->session, sizeof(c->session));
            bound = recorder_bind(c);
        }
        if (bound != RECORDER_BIND_OK || create_task(network_worker, (const char *)task_name, 4096, c, 1, 0, 0) != 1) {
            zero(c->password, sizeof(c->password));
            if (bound == RECORDER_BIND_FAILED) {
                if (c->ring.external_records) release(c->ring.external_records);
                release(c);
            }
            c = 0;
        } else {
            store_release(&c->ready, 1);
        }
    }
    uint32_t last_observation = millis();
    for (;;) {
        if (read8(0x3fca2e6cu)) watchdog_reset();
        uint32_t start = millis();
        stock_loop();
        stock_serial();
        uint32_t now = millis();
        if (c && load_acquire(&c->ready)
            && (uint32_t)(now - last_observation) >= (c->shot_open ? 100u : IDLE_OBSERVATION_MS)
            && (c->shot_open || c->ring.head == load_acquire(&c->ring.tail))) {
            observe(c, now, now - start, 1, 0);
            last_observation = now;
        }
    }
}
