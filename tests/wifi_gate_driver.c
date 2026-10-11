#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <pthread.h>
#include <sched.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define WIFI_OBJECT ((void *)(uintptr_t)0x12345678)
static int wifi_gate_take(void *, uint32_t);
static int wifi_gate_give(void *);
static void *wifi_current_task(void);
#include "../experiments/kd360s_snapshot/wifi_gate.h"
enum { PRODUCER = 1, WORKER = 2, NATIVE = 3 };
static _Thread_local unsigned role = PRODUCER, depth;
static pthread_mutex_t mutex, events = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static struct wifi_gate gate;
static atomic_uint takes, gives, originals, owner_checks, producer_waiting, ui_done;
static atomic_int owner_allowed, fail_take;
static int nested_mode, stall_worker, entered, release_worker;
static int explicit_result = 4, saved_result = 7, disconnect_result = 11, mode_result = 13;
static void *last_object;
static const char *last_ssid, *last_password;
static const void *last_bssid;
static int last_channel, last_connect, last_off, last_erase;
static uint32_t last_mode;
static unsigned explicit_calls, saved_calls;
static struct wifi_gate *wifi_current_gate(void) { return role == NATIVE ? NULL : &gate; }
static void *wifi_current_task(void) { return (void *)(uintptr_t)role; }
static int wifi_owner_allows_retry(void) {
    assert(depth > 0); atomic_fetch_add(&owner_checks, 1); return atomic_load(&owner_allowed);
}
static int wifi_gate_take(void *handle, uint32_t timeout) {
    assert(handle == &mutex && (timeout == 0 || timeout == UINT32_MAX));
    atomic_fetch_add(&takes, 1);
    if (atomic_load(&fail_take)) return 0;
    if (role == PRODUCER && timeout == UINT32_MAX) atomic_store(&producer_waiting, 1);
    int result = timeout ? pthread_mutex_lock(&mutex) : pthread_mutex_trylock(&mutex);
    if (result) return 0;
    ++depth; return 1;
}
static int wifi_gate_give(void *handle) {
    assert(handle == &mutex && depth); --depth; atomic_fetch_add(&gives, 1);
    assert(!pthread_mutex_unlock(&mutex)); return 1;
}
int original_explicit_begin(void *object, const char *ssid, const char *password,
                            int channel, const void *bssid, int connect) {
    atomic_fetch_add(&originals, 1);
    assert(!pthread_mutex_lock(&events));
    ++explicit_calls; last_object = object; last_ssid = ssid; last_password = password;
    last_channel = channel; last_bssid = bssid; last_connect = connect;
    if (stall_worker && role == WORKER) {
        entered = 1; assert(!pthread_cond_broadcast(&changed));
        while (!release_worker) assert(!pthread_cond_wait(&changed, &events));
    }
    assert(!pthread_mutex_unlock(&events));
    if (nested_mode) assert(wrap_wifi_mode(1) == mode_result);
    return explicit_result;
}
int original_saved_begin(void *object) {
    atomic_fetch_add(&originals, 1); ++saved_calls; last_object = object;
    if (nested_mode) assert(wrap_wifi_mode(1) == mode_result);
    return saved_result;
}
int original_disconnect(void *object, int off, int erase) {
    atomic_fetch_add(&originals, 1); last_object = object; last_off = off; last_erase = erase;
    if (nested_mode) assert(wrap_wifi_mode(0) == mode_result);
    return disconnect_result;
}
int original_mode(uint32_t mode) { atomic_fetch_add(&originals, 1); last_mode = mode; return mode_result; }
static void reset(void) {
    gate = (struct wifi_gate){&mutex, (void *)(uintptr_t)PRODUCER, 1, 0, 0};
    atomic_store(&takes, 0); atomic_store(&gives, 0); atomic_store(&originals, 0);
    atomic_store(&owner_checks, 0); atomic_store(&owner_allowed, 1); atomic_store(&fail_take, 0);
    atomic_store(&producer_waiting, 0); atomic_store(&ui_done, 0);
    nested_mode = stall_worker = entered = release_worker = 0; explicit_calls = saved_calls = 0;
    role = PRODUCER; assert(depth == 0);
}
static void wrapper_contract(void) {
    reset(); const char ssid[] = "new", password[] = "new-secret"; uint8_t bssid[6] = {1,2,3,4,5,6};
    nested_mode = 1;
    assert(wrap_wifi_explicit(WIFI_OBJECT, ssid, password, -17, bssid, 257) == explicit_result);
    assert(last_object == WIFI_OBJECT && last_ssid == ssid && last_password == password);
    assert(last_channel == -17 && last_bssid == bssid && last_connect == 257);
    assert(!gate.boot_pending && !atomic_load(&owner_checks));
    assert(atomic_load(&takes) == 2 && atomic_load(&gives) == 2 && depth == 0);
    gate.boot_pending = 1; assert(wrap_wifi_saved(WIFI_OBJECT) == saved_result && !gate.boot_pending);
    gate.boot_pending = 1; assert(wrap_wifi_disconnect(WIFI_OBJECT, 0, 0) == disconnect_result && gate.boot_pending);
    assert(last_off == 0 && last_erase == 0);
    assert(wrap_wifi_disconnect(WIFI_OBJECT, 0, 257) == disconnect_result && !gate.boot_pending);
    gate.boot_pending = 1; assert(wrap_wifi_disconnect(WIFI_OBJECT, -1, 0) == disconnect_result && !gate.boot_pending);
    gate.boot_pending = 1; assert(wrap_wifi_mode(UINT32_MAX) == mode_result && last_mode == UINT32_MAX && gate.boot_pending);
    unsigned before = atomic_load(&originals); atomic_store(&fail_take, 1);
    assert(wrap_wifi_explicit(WIFI_OBJECT, ssid, password, 1, bssid, 1) == 4);
    assert(wrap_wifi_saved(WIFI_OBJECT) == 4 && wrap_wifi_disconnect(WIFI_OBJECT, 1, 1) == 0);
    assert(wrap_wifi_mode(2) == 0 && atomic_load(&originals) == before);
    assert(!wifi_bootstrap_complete(&gate));
    role = NATIVE;
    assert(wrap_wifi_mode(3) == mode_result && last_mode == 3);
    assert(wrap_wifi_saved(WIFI_OBJECT) == saved_result);
    assert(wrap_wifi_explicit(WIFI_OBJECT, ssid, password, 1, bssid, 1) == explicit_result);
    assert(wrap_wifi_disconnect(WIFI_OBJECT, 1, 1) == disconnect_result);
    assert(gate.boot_pending);
}
static void retry_contract(void) {
    reset(); role = WORKER; nested_mode = 1;
    atomic_store(&owner_allowed, 0);
    assert(!wifi_retry(&gate, "boot", "secret", 0) && !gate.attempted && !explicit_calls);
    atomic_store(&owner_allowed, 1);
    assert(wifi_retry(&gate, "boot", "secret", UINT32_MAX - 99) == 1);
    assert(gate.boot_pending && gate.attempted && explicit_calls == 1 && !saved_calls);
    assert(last_object == WIFI_OBJECT && !strcmp(last_ssid, "boot") && !strcmp(last_password, "secret"));
    assert(last_channel == 0 && last_bssid == NULL && last_connect == 1);
    assert(!wifi_retry(&gate, "boot", "secret", 9899));
    assert(wifi_retry(&gate, "boot", "secret", 9900) == 1 && explicit_calls == 2);
    assert(wifi_bootstrap_complete(&gate) && !gate.boot_pending);
    assert(wifi_retry(&gate, "boot", "secret", 19900) == 2 && saved_calls == 1);
    atomic_store(&fail_take, 1);
    unsigned before = atomic_load(&originals), checks = atomic_load(&owner_checks);
    assert(!wifi_retry(&gate, "boot", "secret", 29900));
    assert(atomic_load(&originals) == before && atomic_load(&owner_checks) == checks);
    reset();
    assert(wrap_wifi_explicit(WIFI_OBJECT, "UI", "changed", 0, NULL, 1) == explicit_result);
    role = WORKER; assert(wifi_retry(&gate, "old", "stale", 0) == 2);
    assert(explicit_calls == 1 && saved_calls == 1);
}
static void *stalled_retry(void *unused) {
    (void)unused; role = WORKER;
    assert(wifi_retry(&gate, "boot", "secret", 0) == 1); return NULL;
}
static void *waiting_ui(void *unused) {
    (void)unused; role = PRODUCER;
    assert(wrap_wifi_explicit(WIFI_OBJECT, "UI", "changed", 6, NULL, 1) == explicit_result);
    atomic_store(&ui_done, 1); return NULL;
}
static void contention(void) {
    reset(); stall_worker = 1; nested_mode = 1;
    pthread_t worker, ui; assert(!pthread_create(&worker, NULL, stalled_retry, NULL));
    assert(!pthread_mutex_lock(&events));
    while (!entered) assert(!pthread_cond_wait(&changed, &events));
    assert(!pthread_mutex_unlock(&events));
    assert(!wifi_retry(&gate, "boot", "secret", 10000));
    assert(!wifi_bootstrap_complete(&gate));
    volatile unsigned lifecycle = 0;
    for (unsigned i = 0; i < 10000; ++i) ++lifecycle;
    assert(lifecycle == 10000);
    role = NATIVE; unsigned before = atomic_load(&takes);
    assert(wrap_wifi_mode(2) == mode_result && atomic_load(&takes) == before);
    assert(!pthread_create(&ui, NULL, waiting_ui, NULL));
    while (!atomic_load(&producer_waiting)) sched_yield();
    assert(!atomic_load(&ui_done));
    assert(!pthread_mutex_lock(&events)); release_worker = 1;
    assert(!pthread_cond_broadcast(&changed)); assert(!pthread_mutex_unlock(&events));
    assert(!pthread_join(worker, NULL) && !pthread_join(ui, NULL));
    assert(atomic_load(&ui_done) && !gate.boot_pending && explicit_calls == 2);
    assert(!strcmp(last_ssid, "UI") && !strcmp(last_password, "changed"));
}
int main(void) {
    pthread_mutexattr_t attr; assert(!pthread_mutexattr_init(&attr));
    assert(!pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE));
    assert(!pthread_mutex_init(&mutex, &attr)); assert(!pthread_mutexattr_destroy(&attr));
    wrapper_contract(); retry_contract(); contention();
    assert(!pthread_mutex_destroy(&mutex));
    puts("wifi gate arguments, recursion, ownership, backoff, native bypass and UI contention passed");
}
