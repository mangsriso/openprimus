ENTRY(snapshot_entry)
SECTIONS {
    . = 0x42106ea0;
    .text : { *(.literal*) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) }
    .bss : { *(.bss*) *(COMMON) }
    ASSERT(SIZEOF(.rodata) == 0, "Byte-accessed constants must live in owned RAM")
    ASSERT(SIZEOF(.data) == 0, "No static writable data permitted")
    ASSERT(SIZEOF(.bss) == 0, "No unallocated BSS permitted")
    ASSERT(. <= 0x42110000, "Payload exceeds candidate page")
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.xtensa.info) *(.xt.prop*) *(.xt.lit*) }
}
