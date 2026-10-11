.begin no-transform
.file "hooks.S"
.section .text.hooks,"ax"
.align 4
.Lpressure_data: .long 0x3fc9ff04
.Lpressure_continue: .long 0x42003db3
.Lrx_screen: .long 0x3fc9d104
.Lrx_expected: .long 0x3fca8084
.Lrx_equal: .long 0x42005449
.Lrx_other: .long 0x42005440
.Lstart_file: .long 0x3c11152e
.Lstart_function: .long 0x3c11170f
.Lstart_continue: .long 0x42005928
.Lstop_runtime: .long 0x3fc9cb00
.Lstop_continue: .long 0x420031c0
.Lstop_branch: .long 0x420031cc

.align 4
original_pressure:
 entry a1,32
 sext a2,a2,15
 float.s f1,a2,0
 l32r a2,.Lpressure_data
 lsi f0,a2,4
 l32r a15,.Lpressure_continue
 jx a15
.align 4
original_rx:
 entry a1,32
 l32r a8,.Lrx_screen
 l32r a9,.Lrx_expected
 l32i a8,a8,0
 l32i a9,a9,0
 bne a8,a9,1f
 l32r a15,.Lrx_equal
 jx a15
1:
 l32r a15,.Lrx_other
 jx a15
.align 4
original_start:
 entry a1,64
 l32r a14,.Lstart_file
 movi a12,0x1a1
 movi a10,4
 movi a6,0
 l32r a13,.Lstart_function
 l32r a15,.Lstart_continue
 jx a15
.align 4
original_stop:
 entry a1,32
 l32r a8,.Lstop_runtime
 l16ui a9,a8,14
 bnez a9,1f
 movi a9,1
 s16i a9,a8,14
 l32r a15,.Lstop_continue
 jx a15
1:
 l32r a15,.Lstop_branch
 jx a15

/* The 80-byte frame keeps saved input below the window spill area at 48..60. */
.macro WRAPPER name, event
.align 4
.global wrap_\name
.type wrap_\name,@function
wrap_\name:
 entry a1,80
 s32i a2,a1,16
 mov a10,a2
 mov a11,a3
 mov a12,a4
 mov a13,a5
 mov a14,a6
 mov a15,a7
 call8 original_\name
 mov a2,a10
 mov a3,a11
 mov a4,a12
 mov a5,a13
 movi a10,\event
 l32i a11,a1,16
 mov a12,a2
 call8 recorder_after
 retw
.size wrap_\name,.-wrap_\name
.endm
WRAPPER start,2
WRAPPER stop,3
WRAPPER pressure,4
WRAPPER rx,5
.end no-transform
