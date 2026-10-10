#include "protocol.h"
#if !defined(__XTENSA_WINDOWED_ABI__) || !defined(__XTENSA_EL__)
#error "Requires Xtensa little-endian windowed ABI"
#endif
#ifndef COLLECTOR_IPV4_LE
#error "Configure the collector IPv4 address explicitly"
#endif
#ifndef COLLECTOR_PORT_BE
#error "Configure the collector UDP port explicitly"
#endif
_Static_assert(sizeof(void *) == 4, "Stock pointers are 32 bit");
#define IMPORT(address, result, ...) ((result (*)(__VA_ARGS__))(address))
#define stock_setup IMPORT(0x4200be5cu, void, void)
#define stock_loop IMPORT(0x4200cc98u, void, void)
#define stock_serial IMPORT(0x4200cc6cu, void, void)
#define watchdog_reset IMPORT(0x42073928u, int, void)
#define millis IMPORT(0x4206617cu, uint32_t, void)
#define delay IMPORT(0x42066198u, void, uint32_t)
#define allocate IMPORT(0x403846c8u, void *, uint32_t)
#define release IMPORT(0x403846e8u, void, void *)
#define fill_random IMPORT(0x42094a18u, void, void *, uint32_t)
#define create_task IMPORT(0x42066150u, int, void (*)(void *), const char *, uint32_t, void *, uint32_t, void *, int)
#define wifi_begin IMPORT(0x420561c0u, int, void *, const char *, const char *, int, const void *, int)
#define wifi_status IMPORT(0x420561a0u, int, void *)
#define udp_socket IMPORT(0x42082b44u, int, int, int, int)
#define udp_close IMPORT(0x420826b0u, int, int)
#define udp_send IMPORT(0x42082978u, int, int, const void *, uint32_t, int, const void *, uint32_t)
#define udp_receive IMPORT(0x42082884u, int, int, void *, uint32_t, int, void *, uint32_t *)
#define WIFI_OBJECT ((void *)0x3fca2128u)

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
    char ssid[33];
    char password[65];
};
static uint8_t read8(uint32_t address) { return *(volatile uint8_t *)address; }
static uint16_t read16(uint32_t address) { return *(volatile uint16_t *)address; }
static uint32_t read32(uint32_t address) { return *(volatile uint32_t *)address; }
static void zero(void *memory, uint32_t size) {
    volatile uint8_t *p = memory;
    while (size--) *p++ = 0;
}
static int copy_string(char *destination, unsigned capacity, uint32_t object) {
    const char *source = (const char *)((read8(object + 15) & 0x80) ? object : read32(object));
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
    fill_random(c->session, sizeof(c->session));
    store_release(&c->ready, 1);
    struct ipv4_address destination;
    zero(&destination, sizeof(destination));
    destination.length = 16;
    destination.family = 2;
    destination.port = COLLECTOR_PORT_BE;
    destination.ip = COLLECTOR_IPV4_LE;
    int socket = -1;
    uint32_t last_send = 0;
    int awaiting_ack = 0;
    for (;;) {
        delay(10);
        if (wifi_status(WIFI_OBJECT) != 3) {
            if (socket >= 0) udp_close(socket);
            socket = -1;
            awaiting_ack = 0;
            delay(250);
            continue;
        }
        if (socket < 0) {
            socket = udp_socket(2, 2, 0);
            if (socket < 0) { delay(250); continue; }
        }
        const uint8_t *packet = pending(&c->ring);
        if (!packet) continue;
        uint32_t now = millis();
        if (!awaiting_ack || (uint32_t)(now - last_send) >= 250) {
            if (udp_send(socket, packet, 64, 8, &destination, 16) != 64) {
                udp_close(socket);
                socket = -1;
                awaiting_ack = 0;
                delay(250);
                continue;
            }
            awaiting_ack = 1;
            last_send = now;
        }
        uint8_t ack[17];
        struct ipv4_address sender;
        zero(&sender, sizeof(sender));
        uint32_t sender_size = sizeof(sender);
        int received = udp_receive(socket, ack, sizeof(ack), 8, &sender, &sender_size);
        if (received == 16 && sender_size == 16 && sender.family == 2
            && sender.ip == destination.ip && sender.port == destination.port
            && acknowledge(&c->ring, ack, 16)) awaiting_ack = 0;
    }
}
static void observe(struct context *c, uint32_t now, uint32_t duration) {
    uint8_t packet[64];
    zero(packet, sizeof(packet));
    packet[0] = 'W'; packet[1] = 'P'; packet[2] = 'S'; packet[3] = '1';
    packet[4] = 1; packet[5] = 1; packet[6] = 64;
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
    put32(packet + 56, 1);
    enqueue(&c->ring, packet);
}
void snapshot_entry(void *argument) {
    (void)argument;
    stock_setup();
    volatile char task_name[5];
    task_name[0] = 'w'; task_name[1] = 'p'; task_name[2] = 'm'; task_name[3] = 's'; task_name[4] = 0;
    struct context *c = allocate(sizeof(struct context));
    if (c) {
        zero(c, sizeof(*c));
        if (!copy_string(c->ssid, sizeof(c->ssid), 0x3fc9ff3cu) || !c->ssid[0]
            || !copy_string(c->password, sizeof(c->password), 0x3fc9ff2cu)
            || create_task(network_worker, (const char *)task_name, 4096, c, 1, 0, 0) != 1) {
            zero(c->password, sizeof(c->password));
            release(c);
            c = 0;
        }
    }
    uint32_t last_observation = millis();
    for (;;) {
        if (read8(0x3fca2e6cu)) watchdog_reset();
        uint32_t start = millis();
        stock_loop();
        stock_serial();
        uint32_t now = millis();
        if (c && load_acquire(&c->ready) && (uint32_t)(now - last_observation) >= 100) {
            observe(c, now, now - start);
            last_observation = now;
        }
    }
}
