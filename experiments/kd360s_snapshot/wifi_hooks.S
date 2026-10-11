/* Original CALL8 functions: preserve stock frames and resume after each detour. */
.file "wifi_hooks.S"
.begin no-transform
.section .text.wifi_hooks,"ax"
.align 4
.Lcanary: .long 0x3fca31a0
.Lwifi: .long 0x3fca2128
.Lexplicit_cont: .long 0x420561d0
.Lsaved_cont: .long 0x42056504
.Ldisconnect_cont: .long 0x42056697
.Lget_mode: .long 0x42054f64
.Lmode_different: .long 0x42054f9a

.align 4
.global original_explicit_begin
.type original_explicit_begin,@function
original_explicit_begin:
 entry a1,352
 l32r a9,.Lcanary
 movi.n a11,1
 mov.n a2,a5
 extui a5,a7,0,8
 memw
 l32r a15,.Lexplicit_cont
 jx a15
.size original_explicit_begin,.-original_explicit_begin

.align 4
.global original_saved_begin
.type original_saved_begin,@function
original_saved_begin:
 entry a1,224
 l32r a3,.Lcanary
 movi.n a11,1
 memw
 l32i.n a2,a3,0
 l32r a10,.Lwifi
 l32r a15,.Lsaved_cont
 jx a15
.size original_saved_begin,.-original_saved_begin

.align 4
.global original_disconnect
.type original_disconnect,@function
original_disconnect:
 entry a1,176
 extui a6,a4,0,8
 l32r a4,.Lcanary
 movi.n a5,0
 movi.n a8,0
 movi.n a7,1
 l32r a15,.Ldisconnect_cont
 jx a15
.size original_disconnect,.-original_disconnect

.align 4
.global original_mode
.type original_mode,@function
original_mode:
 entry a1,48
 mov.n a4,a2
 l32r a8,.Lget_mode
 callx8 a8
 bne a10,a2,.Lmode_diff
 movi.n a2,1
 retw.n
.Lmode_diff:
 l32r a15,.Lmode_different
 jx a15
.size original_mode,.-original_mode
.end no-transform
