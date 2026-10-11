#ifndef KD360S_RECORDER_CONTEXT_H
#define KD360S_RECORDER_CONTEXT_H
#include <stdint.h>
#include <stddef.h>

struct recorder_key_node {
    uint32_t key;
    void (*destructor)(void *);
    struct recorder_key_node *next;
};

#ifdef RECORDER_CONTEXT_HOST
static int recorder_rt_create(uint32_t *, void (*)(void *));
static int recorder_rt_delete(uint32_t);
static int recorder_rt_set(uint32_t, const void *);
static void *recorder_rt_get(uint32_t);
static int recorder_rt_enter(void);
static void recorder_rt_exit(void);
static struct recorder_key_node *recorder_rt_head(void);
#else
#if !defined(__XTENSA_WINDOWED_ABI__) || !defined(__XTENSA_EL__)
#error "Requires Xtensa little-endian windowed ABI"
#endif
_Static_assert(sizeof(struct recorder_key_node) == 12, "Stock pthread key layout");
_Static_assert(offsetof(struct recorder_key_node, destructor) == 4, "Stock destructor offset");
_Static_assert(offsetof(struct recorder_key_node, next) == 8, "Stock key link offset");
#define RECORDER_IMPORT(address, result, ...) ((result (*)(__VA_ARGS__))(address))
#define recorder_rt_create RECORDER_IMPORT(0x42073374u, int, uint32_t *, void (*)(void *))
#define recorder_rt_delete RECORDER_IMPORT(0x420733bcu, int, uint32_t)
#define recorder_rt_set RECORDER_IMPORT(0x42073428u, int, uint32_t, const void *)
#define recorder_rt_get RECORDER_IMPORT(0x42073400u, void *, uint32_t)
static int recorder_rt_enter(void) {
    return RECORDER_IMPORT(0x403811b0u, int, void *, int)((void *)0x3fc973dcu, -1);
}
static void recorder_rt_exit(void) {
    RECORDER_IMPORT(0x403812f4u, void, void *)((void *)0x3fc973dcu);
}
static struct recorder_key_node *recorder_rt_head(void) {
    return *(struct recorder_key_node *volatile *)0x3fca3184u;
}
#endif

enum recorder_bind_result {
    RECORDER_BIND_RETAIN = -1,
    RECORDER_BIND_FAILED = 0,
    RECORDER_BIND_OK = 1
};
enum recorder_key_result {
    RECORDER_KEY_UNUSABLE = -1,
    RECORDER_KEY_ABSENT = 0,
    RECORDER_KEY_FOUND = 1
};

/* This unique callback identifies our registered key. Context is boot-long:
 * sender may outlive loopTask, so pthread deletion must not free it. Preserve
 * distinct callback addresses in the build (-fno-ipa-icf) and retained ELF.
 */
__attribute__((noinline, used))
static void recorder_key_marker(void *context) {
    (void)context;
    __asm__ volatile ("" ::: "memory");
}

static enum recorder_key_result recorder_find_key(uint32_t *key) {
    if (!recorder_rt_enter()) return RECORDER_KEY_UNUSABLE;
    enum recorder_key_result result = RECORDER_KEY_ABSENT;
    uint32_t found = 0;
    struct recorder_key_node *node = recorder_rt_head();
    /* Registry belongs to stock pthread; traverse under its lock exactly as
     * stock find_key does. A legitimate large registry must remain usable.
     */
    while (node) {
        if (node->destructor == recorder_key_marker) {
            if (result == RECORDER_KEY_FOUND || node->key == 0) {
                result = RECORDER_KEY_UNUSABLE;
                break;
            }
            result = RECORDER_KEY_FOUND;
            found = node->key;
        }
        node = node->next;
    }
    recorder_rt_exit();
    if (result == RECORDER_KEY_FOUND) *key = found;
    return result;
}

/* One designated loop startup owner calls this before sender creation. No
 * concurrent bind or deletion of our key is allowed. Context must already be
 * initialized. OK publishes it until reboot; FAILED allows caller to release
 * it; RETAIN means existing/uncertain binding or failed rollback; caller must retain
 * it until reboot.
 * A second bind never replaces an existing binding or creates another key.
 */
static int recorder_bind(void *context) {
    uint32_t key = 0;
    if (!context) return RECORDER_BIND_FAILED;
    enum recorder_key_result existing = recorder_find_key(&key);
    if (existing != RECORDER_KEY_ABSENT)
        return RECORDER_BIND_RETAIN;
    if (recorder_rt_create(&key, recorder_key_marker) != 0)
        return RECORDER_BIND_FAILED;
    uint32_t discovered = 0;
    if (key == 0 || recorder_find_key(&discovered) != RECORDER_KEY_FOUND || discovered != key)
        return recorder_rt_delete(key) == 0 ? RECORDER_BIND_FAILED : RECORDER_BIND_RETAIN;
    if (recorder_rt_get(key) != NULL) {
        return recorder_rt_delete(key) == 0 ? RECORDER_BIND_FAILED : RECORDER_BIND_RETAIN;
    }
    int set_result = recorder_rt_set(key, context);
    if (set_result == 0 && recorder_rt_get(key) == context)
        return RECORDER_BIND_OK;
    int cleared = recorder_rt_set(key, NULL) == 0;
    int removed = recorder_rt_delete(key) == 0;
    return cleared && removed ? RECORDER_BIND_FAILED : RECORDER_BIND_RETAIN;
}

static void *recorder_context(void) {
    uint32_t key = 0;
    if (recorder_find_key(&key) != RECORDER_KEY_FOUND) return NULL;
    /* Registry lock is released. Our key cannot be deleted after publication;
     * stock mutations of other keys do not change this key's identity.
     */
    return recorder_rt_get(key);
}
#endif
