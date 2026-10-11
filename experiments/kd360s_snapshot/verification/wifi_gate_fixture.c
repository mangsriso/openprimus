#include <stdint.h>
#define WIFI_OBJECT ((void *)0x01020304u)
#define NOINLINE __attribute__((noinline, noclone))
static int wifi_gate_take(void *, uint32_t);
static int wifi_gate_give(void *);
static void *wifi_current_task(void);
#include "../wifi_gate.h"
struct fixture_state {
    struct wifi_gate gate;
    volatile uint32_t present, role, fail_take, nested, depth, max_depth;
    volatile uint32_t takes, gives, originals, explicit_calls, mode_calls, errors;
    volatile uint32_t expected[6];
};
__attribute__((section(".fixture_state"))) struct fixture_state state;
static NOINLINE uint32_t stress(uint32_t n, uint32_t seed) {
    volatile uint32_t a = seed, b = seed ^ 0xabcdef01u, c = seed + 0x12345678u;
    uint32_t result = n ? stress(n - 1, seed + 0x31415927u) : seed;
    if (a != seed || b != (seed ^ 0xabcdef01u) || c != seed + 0x12345678u) state.errors |= 128;
    return result ^ seed;
}
static NOINLINE struct wifi_gate *wifi_current_gate(void) { return state.present ? &state.gate : 0; }
static NOINLINE int wifi_owner_allows_retry(void) { return 1; }
static NOINLINE void *wifi_current_task(void) { return (void *)state.role; }
static NOINLINE int wifi_gate_take(void *mutex, uint32_t wait) {
    if (mutex != (void *)0xabcdef00u || (wait != 0 && wait != UINT32_MAX)) state.errors |= 2;
    ++state.takes;
    if (state.fail_take) return 0;
    ++state.depth;
    if (state.depth > state.max_depth) state.max_depth = state.depth;
    stress(20, 0x55667788u);
    return 1;
}
static NOINLINE int wifi_gate_give(void *mutex) {
    if (mutex != (void *)0xabcdef00u || !state.depth) state.errors |= 4;
    --state.depth; ++state.gives; stress(20, 0x12345678u); return 1;
}
NOINLINE int original_explicit_begin(void *object, const char *ssid, const char *password,
                                     int channel, const void *bssid, int connect) {
    uint32_t args[] = {(uint32_t)object, (uint32_t)ssid, (uint32_t)password,
                       (uint32_t)channel, (uint32_t)bssid, (uint32_t)connect};
    ++state.originals; ++state.explicit_calls;
    for (unsigned i = 0; i < 6; ++i) if (args[i] != state.expected[i]) state.errors |= 1;
    stress(20, 0x13572468u);
    if (state.nested) {
        int result = ((int (*)(uint32_t))0x42054f88u)(3);
        if ((uint32_t)result != 0xa1b2c3d4u) state.errors |= 8;
    }
    return (int)0xa1b2c3d4u;
}
NOINLINE int original_saved_begin(void *object) {
    ++state.originals;
    if ((uint32_t)object != state.expected[0]) state.errors |= 1;
    stress(20, 0x10101010u); return (int)0xa1b2c3d4u;
}
NOINLINE int original_disconnect(void *object, int off, int erase) {
    ++state.originals;
    if ((uint32_t)object != state.expected[0] || (uint32_t)off != state.expected[1]
            || (uint32_t)erase != state.expected[2]) state.errors |= 1;
    stress(20, 0x20202020u); return (int)0xa1b2c3d4u;
}
NOINLINE int original_mode(uint32_t mode) {
    ++state.originals; ++state.mode_calls;
    if (mode != 3) state.errors |= 1;
    stress(20, 0x30303030u); return (int)0xa1b2c3d4u;
}
NOINLINE uint32_t run_wifi_case(uint32_t which) {
    volatile unsigned char *bytes = (volatile unsigned char *)&state;
    for (unsigned i = 0; i < sizeof(state); ++i) bytes[i] = 0;
    state.gate.mutex = (void *)0xabcdef00u;
    state.gate.producer_task = (void *)0x123u;
    state.gate.boot_pending = 1;
    state.present = which != 0;
    state.role = which == 1 ? 0x456u : 0x123u;
    state.fail_take = which == 3 || which >= 8;
    state.nested = which == 4;
    for (unsigned i = 0; i < 6; ++i) state.expected[i] = 0x01020304u + i * 0x11223344u;
    state.expected[3] = (uint32_t)-17;
    state.expected[5] = 257;
    uint32_t returned;
    if (which <= 4) {
        returned = ((int (*)(void *, const char *, const char *, int, const void *, int))0x420561c0u)(
            (void *)state.expected[0], (const char *)state.expected[1], (const char *)state.expected[2],
            (int)state.expected[3], (const void *)state.expected[4], (int)state.expected[5]);
    } else if (which == 5 || which == 8) {
        returned = ((int (*)(void *))0x420564f4u)((void *)state.expected[0]);
    } else if (which == 6 || which == 9) {
        returned = ((int (*)(void *, int, int))0x42056688u)((void *)state.expected[0],
                      (int)state.expected[1], (int)state.expected[2]);
    } else {
        returned = ((int (*)(uint32_t))0x42054f88u)(3);
    }
    uint32_t expected_return = state.fail_take ? (which == 3 || which == 8 ? 4 : 0) : 0xa1b2c3d4u;
    if (returned != expected_return) state.errors |= 16;
    if (state.depth || state.originals != (state.fail_take ? 0 : state.nested ? 2 : 1)) state.errors |= 32;
    if (state.nested && (state.max_depth != 2 || state.takes != 2 || state.gives != 2)) state.errors |= 64;
    if (which == 0 && (state.takes || state.gives)) state.errors |= 256;
    uint32_t pending = which == 0 || which == 1 || which == 3 || which >= 7;
    if (state.gate.boot_pending != pending) state.errors |= 512;
    return state.errors;
}
