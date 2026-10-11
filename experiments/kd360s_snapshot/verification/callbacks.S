.section .test_callback,"ax"
.align 4
trace_pointer: .long 0x3fce1000
.global recorder_after
recorder_after:
 entry a1,64
 movi a10,20
 call8 deep_spill
 l32r a8,trace_pointer
 s32i a2,a8,0
 s32i a3,a8,4
 s32i a4,a8,8
 movi a10,0x55
 movi a11,0x66
 movi a12,0x77
 movi a13,0x88
 movi a14,0x99
 movi a15,0xaa
 retw
.align 4
deep_spill:
 entry a1,64
 movi a3,0x333
 movi a4,0x444
 movi a5,0x555
 movi a6,0x666
 movi a7,0x777
 beqz a2,1f
 addi a10,a2,-1
 call8 deep_spill
1:
 retw
