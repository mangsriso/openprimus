#include <assert.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SNAPSHOT_STOCK_RUNTIME_H
#define KD360S_RECORDER_CONTEXT_H
enum { RECORDER_BIND_OK = 1, RECORDER_BIND_FAILED = 0, RECORDER_BIND_RETAIN = -1 };
static int recorder_bind(void *context);
static void *recorder_context(void);
#define COLLECTOR_IPV4_LE 0x010200c0u
#define COLLECTOR_PORT_BE 0x25bau
#define WIFI_OBJECT ((void *)0x3fca2128u)
static void stock_setup(void);
static void stock_loop(void);
static void stock_serial(void);
static int watchdog_reset(void);
static uint32_t millis(void);
static void delay(uint32_t ms);
static void *allocate(uint32_t size);
static void release(void *memory);
static uint32_t caps_free_size(uint32_t caps);
static uint32_t caps_largest_block(uint32_t caps);
static void *caps_allocate(uint32_t size, uint32_t caps);
static void fill_random(void *memory, uint32_t size);
static int create_task(void (*entry)(void *), const char *name, uint32_t stack,
                       void *argument, uint32_t priority, void *handle, int core);
static int wifi_begin(void *object, const char *ssid, const char *password,
                      int channel, const void *bssid, int connect);
static int wifi_status(void *object);
static int udp_socket(int domain, int type, int protocol);
static int udp_close(int socket);
static int udp_send(int socket, const void *data, uint32_t size, int flags,
                    const void *destination, uint32_t destination_size);
static int udp_receive(int socket, void *data, uint32_t size, int flags,
                       void *sender, uint32_t *sender_size);
static uint8_t read8(uint32_t address);
static uint16_t read16(uint32_t address);
static uint32_t read32(uint32_t address);
static const char *string_address(uint32_t address);
#include "../experiments/kd360s_snapshot/snapshot.c"

enum ack_kind { NO_ACK, GOOD_ACK, BAD_IP, BAD_PORT, BAD_FAMILY, BAD_ADDRESS_SIZE,
                SHORT_ACK, LONG_ACK, BAD_MAGIC, BAD_SESSION, BAD_SEQUENCE, DUPLICATE_ACK };
struct network_step { int connected, socket_result, send_result; enum ack_kind ack; };
struct sent_record { uint32_t time; uint8_t bytes[SNAPSHOT_BYTES]; };
static struct {
    uint8_t memory[0x8000], heap[256];
    struct context context;
    jmp_buf finished;
    uint32_t clock, loop_durations[8];
    unsigned loops, loop_limit, brewing;
    int allocation_ok, task_result, bind_result, bound, other_task;
    unsigned allocations, releases, tasks, begins, sockets, closes;
    char calls[128]; unsigned call_count;
    struct network_step steps[64]; unsigned step_count, step;
    struct sent_record sent[64]; unsigned sends;
    int begin_result;
    unsigned ack_count[64], ack_source[64][8], ack_used, fail_send_number;
    uint32_t caps_free, caps_largest;
    unsigned caps_allocations, caps_releases;
    uint8_t external_records[32768];
} mock;
static void call(char event) { assert(mock.call_count + 1 < sizeof(mock.calls)); mock.calls[mock.call_count++] = event; }
static uint8_t *memory_at(uint32_t address) {
    if (address >= 0x20000000u && address < 0x20000100u) return mock.heap + (address - 0x20000000u);
    assert(address >= 0x3fc9c000u && address < 0x3fca4000u);
    return mock.memory + (address - 0x3fc9c000u);
}
static uint8_t read8(uint32_t address) { return *memory_at(address); }
static uint16_t read16(uint32_t address) { return read8(address) | (uint16_t)read8(address + 1) << 8; }
static uint32_t read32(uint32_t address) { return get32(memory_at(address)); }
static const char *string_address(uint32_t address) { return address ? (const char *)memory_at(address) : NULL; }
static void stock_setup(void) { call('S'); }
static void stock_loop(void) {
    if (mock.loops == mock.loop_limit) longjmp(mock.finished, 1);
    call('L'); mock.clock += mock.loop_durations[mock.loops++];
    if (mock.brewing) mock.context.shot_open = 1;
}
static void stock_serial(void) { call('R'); }
static int watchdog_reset(void) { call('W'); return 0; }
static uint32_t millis(void) { return mock.clock; }
static void delay(uint32_t ms) {
    assert(ms == 10 || ms == 250);
    if (ms == 10 && mock.step++ == mock.step_count) longjmp(mock.finished, 1);
    if (ms == 10) mock.ack_used = 0;
    mock.clock += ms;
}
static void *allocate(uint32_t size) {
    assert(size == sizeof(struct context)); call('A'); ++mock.allocations;
    return mock.allocation_ok ? &mock.context : NULL;
}
static void release(void *memory) {
    if (memory == mock.external_records) { ++mock.caps_releases; return; }
    assert(memory == &mock.context);
    for (unsigned i = 0; i < sizeof(mock.context.password); ++i) assert(mock.context.password[i] == 0);
    ++mock.releases; call('F');
}
static uint32_t caps_free_size(uint32_t caps) { assert(caps == 0x404); return mock.caps_free; }
static uint32_t caps_largest_block(uint32_t caps) { assert(caps == 0x404); return mock.caps_largest; }
static void *caps_allocate(uint32_t size, uint32_t caps) {
    assert(caps == 0x404 && size == sizeof(mock.external_records));
    ++mock.caps_allocations;
    return mock.external_records;
}
static void fill_random(void *memory, uint32_t size) {
    assert(size == 8 && mock.begins == 0);
    for (unsigned i = 0; i < size; ++i) ((uint8_t *)memory)[i] = (uint8_t)(i + 1);
}
static int recorder_bind(void *context) {
    assert(context == &mock.context);
    if (mock.bind_result == 1) mock.bound = 1;
    return mock.bind_result;
}
static void *recorder_context(void) {
    return mock.bound && !mock.other_task ? &mock.context : NULL;
}
static int create_task(void (*entry)(void *), const char *name, uint32_t stack,
                       void *argument, uint32_t priority, void *handle, int core) {
    assert(entry == network_worker && !strcmp(name, "wpms"));
    assert(stack == 4096 && argument == &mock.context && priority == 1 && !handle && core == 0);
    ++mock.tasks; call('C');
    return mock.task_result;
}
static int wifi_begin(void *object, const char *ssid, const char *password,
                      int channel, const void *bssid, int connect) {
    assert(object == WIFI_OBJECT && !strcmp(ssid, "network") && !strcmp(password, "secret"));
    assert(channel == 0 && !bssid && connect == 1); ++mock.begins; return mock.begin_result;
}
static struct network_step *step(void) { assert(mock.step > 0 && mock.step <= mock.step_count); return &mock.steps[mock.step - 1]; }
static int wifi_status(void *object) { assert(object == WIFI_OBJECT); return step()->connected ? 3 : 0; }
static int udp_socket(int domain, int type, int protocol) {
    assert(domain == 2 && type == 2 && protocol == 0); ++mock.sockets; return step()->socket_result;
}
static int udp_close(int socket) { assert(socket == 7); ++mock.closes; return 0; }
static int udp_send(int socket, const void *data, uint32_t size, int flags,
                    const void *destination, uint32_t destination_size) {
    const struct ipv4_address *target = destination;
    assert(socket == 7 && size == SNAPSHOT_BYTES && flags == 8 && destination_size == 16);
    assert(target->length == 16 && target->family == 2 && target->port == COLLECTOR_PORT_BE && target->ip == COLLECTOR_IPV4_LE);
    assert(mock.sends < 64); mock.sent[mock.sends].time = mock.clock;
    memcpy(mock.sent[mock.sends++].bytes, data, SNAPSHOT_BYTES); return mock.fail_send_number == mock.sends ? -1 : step()->send_result;
}
static int udp_receive(int socket, void *data, uint32_t size, int flags,
                       void *sender, uint32_t *sender_size) {
    assert(socket == 7 && size == 17 && flags == 8 && *sender_size == 16);
    enum ack_kind kind = step()->ack;
    if (kind == NO_ACK || mock.ack_used >= mock.ack_count[mock.step - 1]) return -1;
    unsigned source = mock.ack_source[mock.step - 1][mock.ack_used++];
    assert(source < mock.sends);
    struct ipv4_address *address = sender;
    *address = (struct ipv4_address){16, 2, COLLECTOR_PORT_BE, COLLECTOR_IPV4_LE, {0}};
    uint8_t *ack = data;
    const uint8_t *packet = mock.sent[source].bytes;
    memcpy(ack, "WPA1", 4); memcpy(ack + 4, packet + 8, 8); memcpy(ack + 12, packet + 16, 4);
    switch (kind) {
    case BAD_IP: ++address->ip; break;
    case BAD_PORT: ++address->port; break;
    case BAD_FAMILY: ++address->family; break;
    case BAD_ADDRESS_SIZE: --*sender_size; break;
    case SHORT_ACK: return 15;
    case LONG_ACK: ack[16] = 0; return 17;
    case BAD_MAGIC: ack[0] = 'X'; break;
    case BAD_SESSION: ++ack[4]; break;
    case BAD_SEQUENCE: put32(ack + 12, get32(ack + 12) + 1); break;
    case DUPLICATE_ACK: memcpy(ack + 4, mock.sent[0].bytes + 8, 8); memcpy(ack + 12, mock.sent[0].bytes + 16, 4); break;
    default: break;
    }
    return 16;
}
static void inline_string(uint32_t address, const char *value) {
    assert(strlen(value) < 15); memset(memory_at(address), 0, 16);
    strcpy((char *)memory_at(address), value); *memory_at(address + 15) = 0x80;
}
static void reset(void) {
    memset(&mock, 0, sizeof(mock)); mock.allocation_ok = 1; mock.task_result = 1; mock.bind_result = 1;
    mock.loop_limit = 1; mock.loop_durations[0] = 100;
    inline_string(0x3fc9ff3cu, "network"); inline_string(0x3fc9ff2cu, "secret");
}
static void run_entry(void) { if (!setjmp(mock.finished)) snapshot_entry(NULL); }
static void run_network(unsigned count) {
    mock.step_count = count;
    if (!setjmp(mock.finished)) network_worker(&mock.context);
    assert(mock.begins == 1 && mock.context.ready == 1);
    assert(!memcmp(mock.context.session, "\1\2\3\4\5\6\7\10", 8));
    for (unsigned i = 0; i < sizeof(mock.context.password); ++i) assert(mock.context.password[i] == 0);
}
static void network_fixture(unsigned records) {
    reset(); strcpy(mock.context.ssid, "network"); strcpy(mock.context.password, "secret");
    mock.context.ready = 1;
    for (unsigned i = 0; i < 64; ++i) {
        mock.steps[i] = (struct network_step){1, 7, SNAPSHOT_BYTES, NO_ACK};
        mock.ack_count[i] = 1;
    }
    memcpy(mock.context.session, "\1\2\3\4\5\6\7\10", 8);
    for (unsigned i = 0; i < records; ++i) observe(&mock.context, i * 100, 7, 1, 0);
}
static void test_strings(void) {
    reset(); char destination[65];
    assert(copy_string(destination, 33, 0x3fc9ff3cu) && !strcmp(destination, "network"));
    inline_string(0x3fc9ff3cu, "12345678901234");
    assert(copy_string(destination, 15, 0x3fc9ff3cu) && strlen(destination) == 14);
    assert(!copy_string(destination, 14, 0x3fc9ff3cu) && destination[13] == 0);
    memset(memory_at(0x3fc9ff3cu), 0, 16);
    assert(!copy_string(destination, 33, 0x3fc9ff3cu));
    put32(memory_at(0x3fc9ff3cu), 0x20000000u);
    for (unsigned length = 32; length <= 65; ++length) {
        memset(mock.heap, 'x', length); mock.heap[length] = 0;
        assert(copy_string(destination, 33, 0x3fc9ff3cu) == (length < 33));
        assert(destination[length < 33 ? length : 32] == 0);
        assert(copy_string(destination, 65, 0x3fc9ff3cu) == (length < 65));
        assert(destination[length < 65 ? length : 64] == 0);
    }
}
static void test_fallback(void) {
    reset(); mock.allocation_ok = 0; run_entry();
    assert(!strcmp(mock.calls, "SALR") && mock.allocations == 1 && !mock.tasks && !mock.releases);
    reset(); mock.task_result = 0; run_entry();
    assert(!strcmp(mock.calls, "SACLR") && mock.tasks == 1 && mock.releases == 0);
    assert(mock.context.ready == 0);
    reset(); inline_string(0x3fc9ff3cu, ""); run_entry();
    assert(!strcmp(mock.calls, "SAFLR") && !mock.tasks && mock.releases == 1);
    reset(); memset(memory_at(0x3fc9ff2cu), 0, 16); run_entry();
    assert(!strcmp(mock.calls, "SAFLR") && !mock.tasks && mock.releases == 1);
    reset(); mock.bind_result = 0; run_entry();
    assert(!strcmp(mock.calls, "SAFLR") && !mock.tasks && mock.releases == 1);
    reset(); mock.bind_result = -1; run_entry();
    assert(!strcmp(mock.calls, "SALR") && !mock.tasks && !mock.releases && !mock.context.ready);
}
static void test_observation(void) {
    for (unsigned wrap = 0; wrap < 2; ++wrap) {
        reset(); mock.loop_limit = 3;
        mock.brewing = 1;
        mock.clock = wrap ? UINT32_MAX - 63 : 0;
        mock.loop_durations[0] = 99; mock.loop_durations[1] = 1; mock.loop_durations[2] = 100;
        *memory_at(0x3fca2e6cu) = 1;
        put32(memory_at(0x3fc9d0c0u), 0x41140000u);
        put32(memory_at(0x3fc9d0c8u), 0x40200000u);
        put32(memory_at(0x3fc9d0bcu), 0x41100000u);
        put32(memory_at(0x3fc9d0c4u), 0x7fc00001u);
        *memory_at(0x3fc9d0a0u) = 0x34; *memory_at(0x3fc9d0a1u) = 0x12;
        *memory_at(0x3fc9cfe1u) = 2; *memory_at(0x3fc9ce52u) = 3;
        *memory_at(0x3fc9ca66u) = 4; *memory_at(0x3fc9ca6au) = 5;
        *memory_at(0x3fc9ff64u) = 0x78; *memory_at(0x3fc9ff65u) = 0x56;
        run_entry();
        assert(!strcmp(mock.calls, "SACWLRWLRWLRW"));
        assert(mock.context.ring.head == 2 && mock.context.ring.sequence == 2);
        const uint8_t *first = mock.context.ring.records[0], *second = mock.context.ring.records[1];
        assert(!memcmp(first, "WPS2\2\1\200\0", 8));
        assert(get32(first + 20) == (wrap ? 36u : 100u));
        assert(get32(second + 20) == (wrap ? 136u : 200u));
        assert(get32(first + 28) == 1 && get32(second + 28) == 100);
        assert(get32(first + 32) == 0x41140000u && get32(first + 36) == 0x40200000u);
        assert(get32(first + 40) == 0x41100000u && get32(first + 44) == 0x7fc00001u);
        assert(!memcmp(first + 48, "\64\22\2\3\4\5\170\126", 8));
        assert(get32(first + 56) == 1 && get32(first + 60) == 0);
    }
}
static void test_begin_once(void) {
    network_fixture(2); mock.begin_result = 4;
    for (unsigned i = 0; i < 8; ++i) mock.steps[i].connected = 0;
    run_network(8);
    assert(!mock.sockets && !mock.sends && mock.context.ring.head == 2 && !mock.context.ring.tail);
    assert(mock.clock == 8 * 260);
}
static void test_reconnect(void) {
    network_fixture(2); mock.steps[1].connected = 0; mock.steps[2].socket_result = -1;
    mock.fail_send_number = 3;
    mock.steps[4].ack = GOOD_ACK; mock.ack_count[4] = 2;
    mock.ack_source[4][0] = 3; mock.ack_source[4][1] = 4;
    run_network(5);
    assert(mock.sockets == 4 && mock.closes == 2 && mock.sends == 5);
    assert(!memcmp(mock.sent[0].bytes, mock.sent[2].bytes, SNAPSHOT_BYTES));
    assert(!memcmp(mock.sent[0].bytes, mock.sent[3].bytes, SNAPSHOT_BYTES));
    assert(get32(mock.sent[4].bytes + 16) == 1 && mock.context.ring.tail == 2);
}
static void test_ack_validation(void) {
    for (enum ack_kind kind = BAD_IP; kind <= BAD_SEQUENCE; ++kind) {
        network_fixture(2); mock.steps[0].ack = kind; run_network(1);
        assert(mock.context.ring.tail == 0 && mock.sends == 2);
    }
    network_fixture(2); mock.steps[0].ack = GOOD_ACK; mock.steps[1].ack = DUPLICATE_ACK;
    mock.steps[2].ack = GOOD_ACK; mock.ack_source[2][0] = 1; run_network(3);
    assert(mock.context.ring.tail == 2 && mock.sends == 2);
    assert(mock.sent[1].time == mock.sent[0].time);
}
static void test_retry(void) {
    for (unsigned wrap = 0; wrap < 2; ++wrap) {
        network_fixture(1); mock.clock = wrap ? UINT32_MAX - 100 : 0;
        run_network(27);
        assert(mock.sends == 2 && mock.context.ring.tail == 0);
        assert((uint32_t)(mock.sent[1].time - mock.sent[0].time) == 250);
        assert(!memcmp(mock.sent[0].bytes, mock.sent[1].bytes, SNAPSHOT_BYTES));
    }
}
static void test_window_delivery(void) {
    network_fixture(16);
    for (unsigned tick = 0; tick < 4; ++tick) {
        if (!(tick & 1)) continue;
        mock.steps[tick].ack = GOOD_ACK; mock.ack_count[tick] = 8;
        for (unsigned n = 0; n < 8; ++n) mock.ack_source[tick][n] = (tick / 2) * 8 + 7 - n;
    }
    run_network(4);
    assert(mock.sends == 16 && mock.context.ring.tail == 16 && mock.clock == 40);
    for (unsigned n = 0; n < 8; ++n) assert(mock.sent[n].time == 10 && mock.sent[n + 8].time == 30);
    network_fixture(4); mock.fail_send_number = 3;
    mock.steps[1].ack = GOOD_ACK; mock.ack_count[1] = 4;
    for (unsigned n = 0; n < 4; ++n) mock.ack_source[1][n] = n + 3;
    run_network(2);
    assert(mock.sends == 7 && mock.closes == 1 && mock.sockets == 2 && mock.context.ring.tail == 4);
    assert(mock.sent[2].time == 10 && mock.sent[3].time == 270);
    assert(!memcmp(mock.sent[0].bytes, mock.sent[3].bytes, SNAPSHOT_BYTES));
}
static void test_lifecycle(void) {
    reset(); mock.bound = 1; mock.context.ready = 1;
    for (unsigned i = 0; i < 28; ++i) *memory_at(0x3fc9ce56u + i) = i + 40;
    *memory_at(0x3fc9ff64u) = 7;
    recorder_after(4, 123, 0x41100000);
    recorder_after(5, 0, 0);
    assert(mock.context.ring.head == 0);
    recorder_after(2, 42, 0);
    recorder_after(4, 123, 0x41100000);
    recorder_after(4, 123, 0x41100000);
    recorder_after(5, 0, 0);
    recorder_after(3, 0, 0);
    recorder_after(2, 43, 0);
    recorder_after(3, 0, 0);
    const unsigned kinds[] = {2, 4, 4, 5, 3, 2, 3};
    assert(mock.context.ring.head == 7);
    for (unsigned i = 0; i < 7; ++i) {
        const uint8_t *p = mock.context.ring.records[i];
        assert(p[5] == kinds[i] && get32(p + 20) == 0 && get32(p + 16) == i);
        assert(get32(p + 60) == (i < 5 ? 1u : 2u));
        assert(get32(p + 80) == (i < 5 ? 42u : 43u));
        assert(p[54] == 7 && get32(p + 56) == 7);
        assert(get32(p + 92) == 28 && !memcmp(p + 96, memory_at(0x3fc9ce56u), 28));
    }
    assert(get32(mock.context.ring.records[1] + 64) == 2);
    assert(get32(mock.context.ring.records[2] + 64) == 3);
    assert(get32(mock.context.ring.records[3] + 72) == 2);
    assert(get32(mock.context.ring.records[2] + 84) == 0x41100000);
    assert(get32(mock.context.ring.records[2] + 88) == 123);
    recorder_after(3, 0, 0);
    assert(get32(mock.context.ring.records[7] + 56) == 15);
    recorder_after(2, 44, 0); recorder_after(2, 45, 0);
    assert(get32(mock.context.ring.records[9] + 56) == 23);
    mock.other_task = 1; recorder_after(2, 0, 0);
    mock.other_task = 0; mock.context.ready = 0; recorder_after(2, 0, 0);
    mock.context.ready = 1; mock.bound = 0; recorder_after(2, 0, 0);
    assert(mock.context.ring.head == 10);
    mock.bound = 1; mock.clock = UINT32_MAX; mock.context.pressure_generation = UINT32_MAX;
    recorder_after(4, 0xfffffff0, 0x7fc01234);
    assert(mock.context.pressure_generation == UINT32_MAX);
    assert(get32(mock.context.ring.records[10] + 56) == 39);
    assert(get32(mock.context.ring.records[10] + 84) == 0x7fc01234);
    mock.clock = 0; recorder_after(5, 0, 0);
    assert(get32(mock.context.ring.records[11] + 76) == 0);
    for (unsigned i = 0; i < SNAPSHOT_CAPACITY; ++i) recorder_after(4, i, i);
    assert(mock.context.ring.head == SNAPSHOT_CAPACITY - BOUNDARY_RESERVE);
    assert(mock.context.ring.dropped == 12 + BOUNDARY_RESERVE);
    uint8_t ack[16]; const uint8_t *first = pending(&mock.context.ring);
    memcpy(ack, "WPA1", 4); memcpy(ack + 4, first + 8, 8); memcpy(ack + 12, first + 16, 4);
    assert(acknowledge(&mock.context.ring, ack, 16));
    recorder_after(3, 0, 0);
    const uint8_t *stop = ring_slot(&mock.context.ring, SNAPSHOT_CAPACITY - BOUNDARY_RESERVE);
    assert(get32(stop + 24) == 12 + BOUNDARY_RESERVE && stop[5] == 3);
}
static void test_idle_retention(void) {
    reset(); mock.loop_limit = 3;
    for (unsigned i = 0; i < 3; ++i) mock.loop_durations[i] = 5000;
    run_entry();
    assert(mock.context.ring.head == 1 && mock.context.ring.sequence == 1);
    assert(!mock.context.ring.dropped && get32(pending(&mock.context.ring) + 20) == 5000);
    assert(get32(pending(&mock.context.ring) + 124) == SNAPSHOT_CAPACITY);
    recorder_after(2, 0, 0);
    for (unsigned i = 0; i < SNAPSHOT_CAPACITY; ++i) recorder_after(4, i, i);
    recorder_after(3, 0, 0);
    assert(mock.context.ring.head == SNAPSHOT_CAPACITY - BOUNDARY_RESERVE + 1);
    assert(ring_slot(&mock.context.ring, mock.context.ring.head - 1)[5] == 3);
    uint8_t ack[16];
    while (pending(&mock.context.ring)) {
        const uint8_t *packet = pending(&mock.context.ring);
        memcpy(ack, "WPA1", 4); memcpy(ack + 4, packet + 8, 12);
        assert(acknowledge(&mock.context.ring, ack, sizeof(ack)));
    }
    uint32_t lost = mock.context.ring.dropped;
    observe(&mock.context, mock.clock, 0, 1, 0);
    assert(get32(pending(&mock.context.ring) + 24) == lost && lost > 0);
}
static void test_retention_lifetime(void) {
    for (int bound = -1; bound <= 1; ++bound) {
        reset(); mock.caps_free = 131072; mock.caps_largest = 65536;
        mock.bind_result = bound;
        run_entry();
        assert(mock.caps_allocations == 1);
        assert(mock.caps_releases == (unsigned)(bound == 0));
        if (bound == 1) {
            assert(ring_capacity(&mock.context.ring) == 256);
            recorder_after(2, 9, 0);
            assert(get32(pending(&mock.context.ring) + 124) == 256);
        }
    }
    reset(); mock.caps_free = 131072; mock.caps_largest = 65536;
    mock.task_result = 0; run_entry();
    assert(mock.caps_allocations == 1 && !mock.caps_releases && !mock.releases);
}
int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "wire")) {
        reset(); mock.bound = 1; mock.context.ready = 1;
        memcpy(mock.context.session, "\1\2\3\4\5\6\7\10", 8);
        for (unsigned i = 0; i < 28; ++i) *memory_at(0x3fc9ce56u + i) = i;
        recorder_after(2, 17, 0);
        mock.clock = 11; recorder_after(4, 456, 0x41180000);
        mock.clock = 12; recorder_after(5, 0, 0);
        mock.clock = 30; recorder_after(3, 0, 0);
        assert(mock.context.ring.head == 4);
        assert(fwrite(mock.context.ring.records, SNAPSHOT_BYTES, 4, stdout) == 4);
        return 0;
    }
    if (!strcmp(argv[1], "strings")) test_strings();
    else if (!strcmp(argv[1], "fallback")) test_fallback();
    else if (!strcmp(argv[1], "observation")) test_observation();
    else if (!strcmp(argv[1], "begin-once")) test_begin_once();
    else if (!strcmp(argv[1], "reconnect")) test_reconnect();
    else if (!strcmp(argv[1], "ack-validation")) test_ack_validation();
    else if (!strcmp(argv[1], "retry")) test_retry();
    else if (!strcmp(argv[1], "window-delivery")) test_window_delivery();
    else if (!strcmp(argv[1], "lifecycle")) test_lifecycle();
    else if (!strcmp(argv[1], "idle-retention")) test_idle_retention();
    else if (!strcmp(argv[1], "retention-lifetime")) test_retention_lifetime();
    else abort();
    printf("%s passed\n", argv[1]);
}
