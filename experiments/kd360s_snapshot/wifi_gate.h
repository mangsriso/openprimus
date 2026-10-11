#ifndef KD360S_WIFI_GATE_H
#define KD360S_WIFI_GATE_H
#include <stdint.h>
#define WIFI_RETRY_MS 10000u
struct wifi_gate {
    void *mutex;
    void *producer_task;
    uint32_t boot_pending;
    uint32_t last_attempt;
    uint32_t attempted;
};
static struct wifi_gate *wifi_current_gate(void);
static int wifi_owner_allows_retry(void);
extern int original_explicit_begin(void *, const char *, const char *, int, const void *, int);
extern int original_saved_begin(void *);
extern int original_disconnect(void *, int, int);
extern int original_mode(uint32_t);

int wrap_wifi_explicit(void *object, const char *ssid, const char *password,
                       int channel, const void *bssid, int connect) {
    struct wifi_gate *gate = wifi_current_gate();
    if (!gate) return original_explicit_begin(object, ssid, password, channel, bssid, connect);
    if (!wifi_gate_take(gate->mutex, UINT32_MAX)) return 4;
    if (wifi_current_task() == gate->producer_task) gate->boot_pending = 0;
    int result = original_explicit_begin(object, ssid, password, channel, bssid, connect);
    wifi_gate_give(gate->mutex);
    return result;
}
int wrap_wifi_saved(void *object) {
    struct wifi_gate *gate = wifi_current_gate();
    if (!gate) return original_saved_begin(object);
    if (!wifi_gate_take(gate->mutex, UINT32_MAX)) return 4;
    if (wifi_current_task() == gate->producer_task) gate->boot_pending = 0;
    int result = original_saved_begin(object);
    wifi_gate_give(gate->mutex);
    return result;
}
int wrap_wifi_disconnect(void *object, int off, int erase) {
    struct wifi_gate *gate = wifi_current_gate();
    if (!gate) return original_disconnect(object, off, erase);
    if (!wifi_gate_take(gate->mutex, UINT32_MAX)) return 0;
    if ((off || erase) && wifi_current_task() == gate->producer_task) gate->boot_pending = 0;
    int result = original_disconnect(object, off, erase);
    wifi_gate_give(gate->mutex);
    return result;
}
int wrap_wifi_mode(uint32_t mode) {
    struct wifi_gate *gate = wifi_current_gate();
    if (!gate) return original_mode(mode);
    if (!wifi_gate_take(gate->mutex, UINT32_MAX)) return 0;
    int result = original_mode(mode);
    wifi_gate_give(gate->mutex);
    return result;
}
static inline int wifi_retry(struct wifi_gate *gate, const char *ssid, const char *password, uint32_t now) {
    if (!gate || !gate->mutex || !wifi_gate_take(gate->mutex, 0)) return 0;
    int result = 0;
    if (wifi_owner_allows_retry()
            && (!gate->attempted || (uint32_t)(now - gate->last_attempt) >= WIFI_RETRY_MS)) {
        gate->attempted = 1;
        gate->last_attempt = now;
        if (gate->boot_pending) {
            original_explicit_begin(WIFI_OBJECT, ssid, password, 0, 0, 1);
            result = 1;
        } else {
            original_saved_begin(WIFI_OBJECT);
            result = 2;
        }
    }
    wifi_gate_give(gate->mutex);
    return result;
}
static inline int wifi_bootstrap_complete(struct wifi_gate *gate) {
    if (!gate || !gate->mutex || !wifi_gate_take(gate->mutex, 0)) return 0;
    gate->boot_pending = 0;
    wifi_gate_give(gate->mutex);
    return 1;
}
#endif
