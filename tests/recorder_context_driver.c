#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define RECORDER_CONTEXT_HOST
#include "../experiments/kd360s_snapshot/recorder_context.h"

struct value { uint32_t key; const void *pointer; struct value *next; };
struct values { struct value *head; };
static pthread_mutex_t registry_lock = PTHREAD_MUTEX_INITIALIZER;
static struct recorder_key_node *registry;
static struct values *tasks[2];
static _Thread_local int current_task, lock_held;
static unsigned allocation, fail_allocation, creates, deletes;
static int fail_enter, fail_clear, fail_delete, wrong_readback, unexpected_value;
static int stored, context_value, other_value;
static void other_marker(void *p) { (void)p; }
static void *allocate(size_t n) {
    if (++allocation == fail_allocation) return NULL;
    return calloc(1, n);
}
static int recorder_rt_enter(void) {
    assert(!lock_held);
    if (fail_enter) return 0;
    assert(pthread_mutex_lock(&registry_lock) == 0);
    lock_held = 1;
    return 1;
}
static void recorder_rt_exit(void) {
    assert(lock_held);
    lock_held = 0;
    assert(pthread_mutex_unlock(&registry_lock) == 0);
}
static struct recorder_key_node *recorder_rt_head(void) {
    assert(lock_held);
    return registry;
}
static int recorder_rt_create(uint32_t *key, void (*destructor)(void *)) {
    assert(!lock_held);
    ++creates;
    struct recorder_key_node *node = allocate(sizeof(*node));
    if (!node) return 12;
    assert(recorder_rt_enter());
    node->key = registry ? registry->key + 1 : 1;
    node->destructor = destructor;
    node->next = registry;
    registry = node;
    *key = node->key;
    recorder_rt_exit();
    return 0;
}
static int recorder_rt_delete(uint32_t key) {
    assert(!lock_held);
    ++deletes;
    if (fail_delete) return 1;
    assert(recorder_rt_enter());
    struct recorder_key_node **node = &registry;
    while (*node && (*node)->key != key) node = &(*node)->next;
    assert(*node);
    struct recorder_key_node *removed = *node;
    *node = removed->next;
    recorder_rt_exit();
    free(removed);
    return 0;
}
static void *recorder_rt_get(uint32_t key) {
    assert(!lock_held);
    if (unexpected_value || (wrong_readback && stored)) return &other_value;
    struct values *values = tasks[current_task];
    for (struct value *p = values ? values->head : NULL; p; p = p->next)
        if (p->key == key) return (void *)p->pointer;
    return NULL;
}
static int recorder_rt_set(uint32_t key, const void *pointer) {
    assert(!lock_held);
    if (!pointer && fail_clear) return 1;
    if (!tasks[current_task]) {
        tasks[current_task] = allocate(sizeof(struct values));
        if (!tasks[current_task]) return 12;
    }
    struct value **p = &tasks[current_task]->head;
    while (*p && (*p)->key != key) p = &(*p)->next;
    if (!*p && pointer) {
        *p = allocate(sizeof(struct value));
        if (!*p) return 12;
        (*p)->key = key;
    }
    if (*p) (*p)->pointer = pointer;
    if (pointer) stored = 1;
    return 0;
}
static void cleanup(void) {
    while (registry) { struct recorder_key_node *p = registry; registry = p->next; free(p); }
    for (unsigned i = 0; i < 2; ++i) {
        if (!tasks[i]) continue;
        while (tasks[i]->head) { struct value *p = tasks[i]->head; tasks[i]->head = p->next; free(p); }
        free(tasks[i]); tasks[i] = NULL;
    }
    allocation = fail_allocation = creates = deletes = 0;
    fail_enter = fail_clear = fail_delete = wrong_readback = unexpected_value = stored = 0;
    current_task = 0;
}
static void isolation(void) {
    assert(recorder_context() == NULL);
    uint32_t other;
    assert(recorder_rt_create(&other, other_marker) == 0);
    assert(recorder_rt_set(other, &other_value) == 0);
    assert(recorder_bind(&context_value) == RECORDER_BIND_OK);
    assert(recorder_context() == &context_value);
    assert(recorder_rt_get(other) == &other_value);
    unsigned before = creates;
    assert(recorder_bind(&context_value) == RECORDER_BIND_RETAIN);
    assert(recorder_bind(&other_value) == RECORDER_BIND_RETAIN);
    assert(creates == before && recorder_context() == &context_value);
    current_task = 1;
    assert(recorder_context() == NULL);
    current_task = 0;
    registry->destructor(&context_value);
    assert(recorder_context() == &context_value);
}
static void failures(void) {
    assert(recorder_bind(NULL) == RECORDER_BIND_FAILED && !creates);
    for (unsigned failure = 1; failure <= 3; ++failure) {
        fail_allocation = failure;
        assert(recorder_bind(&context_value) == RECORDER_BIND_FAILED);
        assert(recorder_context() == NULL && registry == NULL);
        assert(deletes == (failure == 1 ? 0u : 1u));
        cleanup();
    }
    wrong_readback = 1;
    assert(recorder_bind(&context_value) == RECORDER_BIND_FAILED);
    wrong_readback = 0;
    assert(recorder_context() == NULL && !registry);
    cleanup();
    unexpected_value = 1;
    assert(recorder_bind(&context_value) == RECORDER_BIND_FAILED);
    assert(!stored && !registry);
    cleanup();
    fail_enter = 1;
    assert(recorder_bind(&context_value) == RECORDER_BIND_RETAIN);
    assert(recorder_context() == NULL && !creates && !lock_held);
}
static void rollback(void) {
    wrong_readback = fail_clear = 1;
    assert(recorder_bind(&context_value) == RECORDER_BIND_RETAIN);
    assert(!registry && deletes == 1);
    cleanup();
    wrong_readback = fail_delete = 1;
    assert(recorder_bind(&context_value) == RECORDER_BIND_RETAIN);
    assert(registry && deletes == 1);
    wrong_readback = 0;
    assert(recorder_context() == NULL);
    unsigned before = creates;
    assert(recorder_bind(&context_value) == RECORDER_BIND_RETAIN && creates == before);
}
static void registry_cases(void) {
    for (unsigned i = 0; i < 128; ++i) {
        uint32_t key; assert(recorder_rt_create(&key, other_marker) == 0);
    }
    assert(recorder_bind(&context_value) == RECORDER_BIND_OK);
    for (unsigned i = 0; i < 128; ++i) {
        uint32_t key; assert(recorder_rt_create(&key, other_marker) == 0);
    }
    assert(recorder_context() == &context_value);
    uint32_t duplicate;
    assert(recorder_rt_create(&duplicate, recorder_key_marker) == 0);
    assert(recorder_context() == NULL);
    assert(recorder_rt_delete(duplicate) == 0);
    assert(recorder_context() == &context_value);
}
static void *mutate_keys(void *unused) {
    (void)unused;
    current_task = 1;
    for (unsigned i = 0; i < 20000; ++i) {
        uint32_t key;
        assert(recorder_rt_create(&key, other_marker) == 0);
        assert(recorder_context() == NULL);
        assert(recorder_rt_delete(key) == 0);
    }
    return NULL;
}
static void concurrency(void) {
    assert(recorder_bind(&context_value) == RECORDER_BIND_OK);
    pthread_t thread;
    assert(pthread_create(&thread, NULL, mutate_keys, NULL) == 0);
    for (unsigned i = 0; i < 20000; ++i) assert(recorder_context() == &context_value);
    assert(pthread_join(thread, NULL) == 0);
    assert(recorder_context() == &context_value);
}
int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "isolation")) isolation();
    else if (!strcmp(argv[1], "failures")) failures();
    else if (!strcmp(argv[1], "rollback")) rollback();
    else if (!strcmp(argv[1], "registry")) registry_cases();
    else if (!strcmp(argv[1], "concurrency")) concurrency();
    else abort();
    cleanup();
    printf("%s passed\n", argv[1]);
}
