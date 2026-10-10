ENTRY(probe_entry)

SECTIONS
{
    . = 0x42106ea0;
    .text : { *(.literal*) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }

    ASSERT(SIZEOF(.rodata) == 0, "Unexpected read-only data")
    ASSERT(SIZEOF(.data) == 0, "The inert probe cannot own static writable data")
    ASSERT(SIZEOF(.bss) == 0, "The inert probe cannot own BSS")
    ASSERT(. <= 0x42110000, "Probe crosses the candidate page boundary")

    /DISCARD/ : {
        *(.comment) *(.note*) *(.eh_frame*)
        *(.xtensa.info) *(.xt.prop*) *(.xt.lit*)
    }
}
