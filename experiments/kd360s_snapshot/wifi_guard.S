/* Interior scheduler splice: no ENTRY/CALL; a8/a9 are dead in stock CFG. */
.file "wifi_guard.S"
.begin no-transform
.section .text.wifi_guard,"ax"
.align 4
.Lactive: .long 0x3fc9d104
.Lota: .long 0x3fca8564
.Lwifi: .long 0x3fca84cc
.Lpassword: .long 0x3fca849c
.Lflags: .long 0x3fc9ca60
.Lyes: .long 0x4201d3db
.Lno: .long 0x4201d43d
.global wifi_pending_guard
.type wifi_pending_guard,@function
wifi_pending_guard:
 memw
 l8ui a3,a4,0
 extui a3,a3,0,8
 l32r a2,.Lflags
 beqz a3,.Lzero
 l32r a8,.Lactive
 l32i a8,a8,0
 beqz a8,.Lclear
 l32r a9,.Lota
 l32i a9,a9,0
 beq a8,a9,.Lowned
 l32r a9,.Lwifi
 l32i a9,a9,0
 beq a8,a9,.Lowned
 l32r a9,.Lpassword
 l32i a9,a9,0
 beq a8,a9,.Lowned
.Lclear:
 movi a3,0
 s8i a3,a4,0
 memw
.Lzero:
 l32r a8,.Lno
 jx a8
.Lowned:
 l32r a8,.Lyes
 jx a8
.size wifi_pending_guard,.-wifi_pending_guard
.end no-transform
