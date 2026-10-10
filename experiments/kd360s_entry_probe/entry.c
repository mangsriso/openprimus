#if !defined(__XTENSA_WINDOWED_ABI__) || !defined(__XTENSA_EL__)
#error "This probe requires the little-endian Xtensa windowed ABI"
#endif

_Static_assert(sizeof(void *) == 4, "The stock task argument is a 32-bit pointer");

typedef void (*task_entry)(void *);

void probe_entry(void *argument)
{
    ((task_entry)0x42067014u)(argument);
}
