@ TS1M V202: target-reached beep + auto boost
@
@ Hook site: 0x08021624 inside the heater function (0x080214A0).
@   original:  0x08021620  ldrsh.w r0, [r9, #2]   @ r0 = current temp
@              0x08021624  ldr     r1, [r6, #64]  @ PID fn ptr (0x20000234)
@              0x08021626  blx     r1             @ PID(r0) -> reads target [0x200001F4]
@   patched:   0x08021624  bl      hook           @ 4 bytes, same size
@
@ The hook runs the original PID call itself. When a boost is active it raises
@ the target for that one PID call, then restores the base value, so the
@ display and the rest of the firmware only ever see the user's set temperature.
@ The heater function rewrites the target from the settings on every pass, so
@ removing the hook (or any fault in its state) cannot leave the target raised.
@
@ Boost indicator: the big current-temperature digits are drawn in BOOST_COLOR
@ instead of white while a boost is actually raising the target. Two more
@ 4-byte call sites are replaced (see temp_color_a / temp_color_b below).
@
@ Placed at 0x0802849C, over the uncalled FatFs test function ("M0.TXT" /
@ "Hello, World!"), 0x0802849C-0x080285F7 (348 bytes, no references to it).
@
@ Units are the firmware's internal 0.1 degree.

    .syntax unified
    .cpu cortex-m3
    .thumb

    .equ STATE,        0x2000BFA8   @ top of the unused 512 B heap (0x2000BDC8-0x2000BFC8)
    .equ MAGIC,        0x4D315354   @ "TS1M"; RAM is not zeroed at reset, so self-init
    .equ TARGET,       0x200001F4   @ u16 target temp (PID struct +0)
    .equ MODE,         0x2000023C   @ u8  1 = working, 2 = sleep, 0/3 = heater off
    .equ TCMODE,       0x20000914   @ u16 != 0: thermocouple measuring mode
    .equ MSTICK,       0x200003BC   @ u32 millisecond counter
    .equ BUZZER,       0x20000330   @ buzzer object, +4 = beep(pattern)
    .equ PATTERN,      0x0802A7CC   @ built-in pattern: 2 x 80 ms beeps

    .equ BAND,         30           @ "reached" = within +-3.0
    .equ DROOP,        100          @ boost when 10.0 below target ...
    .equ HOLD_MS,      300          @ ... for this long
    .equ BOOST,        200          @ +20.0
    .equ BOOST_MAX_MS, 20000        @ give up boosting after 20 s
    .equ TMAX,         4500         @ never ask for more than 450.0
    .equ WHITE,        0xFFFF       @ normal colour of the big temperature digits
    .equ BOOST_COLOR,  0xF800       @ RGB565 red while boosting

    @ state layout
    .equ S_MAGIC,   0
    .equ S_LAST,    4               @ u16 base target seen last pass (0 = not working)
    .equ S_ARMED,   6               @ u8  beep pending for this target
    .equ S_REACHED, 7               @ u8  target has been reached since it was set
    .equ S_DROOP,   8               @ u32 ms when the droop started (0 = none)
    .equ S_BOOST,   12              @ u8  boost active
    .equ S_LOCK,    13              @ u8  boost timed out: wait for recovery before the next one
    .equ S_ACTIVE,  14              @ u8  the last PID call ran with a raised target (for the display)
    .equ S_BSTART,  16              @ u32 ms when the boost started

    .text
    .global hook
    .type hook, %function
hook:                               @ r0 = current temp, r6 = 0x200001F4
    push    {r3-r7, lr}
    mov     r7, r0                  @ r7 = current temp
    ldr     r4, =STATE
    ldr     r1, [r4, #S_MAGIC]
    ldr     r2, =MAGIC
    cmp     r1, r2
    beq     1f
    str     r2, [r4, #S_MAGIC]
    movs    r1, #0
    str     r1, [r4, #4]
    str     r1, [r4, #8]
    str     r1, [r4, #12]
    str     r1, [r4, #16]
1:
    ldr     r5, =TARGET
    ldrh    r1, [r5]                @ r1 = base target, written by the firmware this pass
    ldr     r2, =MODE
    ldrb    r2, [r2]
    cmp     r2, #1
    bne     idle
    ldr     r2, =TCMODE
    ldrh    r2, [r2]
    cmp     r2, #0
    bne     idle
    cmp     r1, #0
    beq     idle

    ldrh    r2, [r4, #S_LAST]
    cmp     r1, r2
    beq     2f
    strh    r1, [r4, #S_LAST]       @ new set temperature: re-arm
    movs    r2, #1
    strb    r2, [r4, #S_ARMED]
    movs    r2, #0
    strb    r2, [r4, #S_REACHED]
    str     r2, [r4, #S_DROOP]
    strh    r2, [r4, #S_BOOST]      @ clears S_BOOST and S_LOCK
2:
    subs    r2, r1, r7              @ r2 = error = base - current

    @ --- target reached notification
    ldrb    r3, [r4, #S_ARMED]
    cbz     r3, 3f
    cmn     r2, #BAND
    blt     3f
    cmp     r2, #BAND
    bgt     3f
    movs    r3, #0
    strb    r3, [r4, #S_ARMED]
    movs    r3, #1
    strb    r3, [r4, #S_REACHED]
    push    {r1, r2}
    ldr     r0, =PATTERN
    ldr     r3, =BUZZER
    ldr     r3, [r3, #4]
    blx     r3
    pop     {r1, r2}
3:
    @ --- auto boost (only once the target has been reached)
    ldrb    r3, [r4, #S_REACHED]
    cmp     r3, #0
    beq     call
    ldr     r0, =MSTICK
    ldr     r0, [r0]                @ r0 = now (ms)
    ldrb    r3, [r4, #S_BOOST]
    cbnz    r3, boosting
    ldrb    r3, [r4, #S_LOCK]
    cbz     r3, 5f
    cmp     r2, #0
    bgt     nodroop                 @ still below target after a timed-out boost
    movs    r3, #0
    strb    r3, [r4, #S_LOCK]
5:
    cmp     r2, #DROOP
    blt     nodroop
    ldr     r3, [r4, #S_DROOP]
    cbnz    r3, 4f
    orr     r3, r0, #1              @ never store 0 as a start time
    str     r3, [r4, #S_DROOP]
    b       call
4:
    subs    r3, r0, r3
    cmp     r3, #HOLD_MS
    blo     call
    movs    r3, #1
    strb    r3, [r4, #S_BOOST]
    str     r0, [r4, #S_BSTART]
    b       apply
nodroop:
    movs    r3, #0
    str     r3, [r4, #S_DROOP]
    b       call

boosting:
    cmp     r2, #0
    ble     endboost                @ recovered to the base target
    ldr     r3, [r4, #S_BSTART]
    subs    r3, r0, r3
    movw    ip, #BOOST_MAX_MS
    cmp     r3, ip
    bhs     timeout
apply:
    movw    r3, #TMAX
    cmp     r1, r3
    bhs     call                    @ at or above the cap: never touch the target
    add     r2, r1, #BOOST
    cmp     r2, r3
    it      hi
    movhi   r2, r3
    strh    r2, [r5]                @ boosted target for this PID call only
    b       call
timeout:
    movs    r3, #1
    strb    r3, [r4, #S_LOCK]
endboost:
    movs    r3, #0
    strb    r3, [r4, #S_BOOST]
    str     r3, [r4, #S_DROOP]
    b       call

idle:                               @ not working: re-arm for the next time
    movs    r2, #0
    strh    r2, [r4, #S_LAST]
    strb    r2, [r4, #S_REACHED]
    str     r2, [r4, #S_DROOP]
    strh    r2, [r4, #S_BOOST]      @ clears S_BOOST and S_LOCK

call:                               @ r1 = base target on every path
    ldrh    r2, [r5]
    subs    r2, r2, r1
    it      ne
    movne   r2, #1
    strb    r2, [r4, #S_ACTIVE]
    mov     r0, r7
    mov     r7, r1
    ldr     r3, [r6, #64]           @ original: ldr r1, [r6, #64]
    blx     r3                      @ original: blx r1
    strh    r7, [r5]                @ restore the base target
    pop     {r3-r7, pc}

    .ltorg
    .size hook, . - hook

@ ---------------------------------------------------------------------------
@ Colour of the big current-temperature digits.
@ Returns BOOST_COLOR while boosting, WHITE otherwise. Uses only r1, r2, ip.

boost_colour:                       @ -> r1
    ldr     r1, =STATE
    ldr     r2, [r1, #S_MAGIC]
    ldr     ip, =MAGIC
    cmp     r2, ip
    bne     1f
    ldrb    r2, [r1, #S_ACTIVE]
    cbz     r2, 1f
    movw    r1, #BOOST_COLOR
    bx      lr
1:
    movw    r1, #WHITE
    bx      lr

@ Site A: 0x0801FA28  movw r1, #0xFFFF  (default UI; r1 = fg colour for drawString)
@ r2 is overwritten by the caller right after, r0/r3 must survive.
    .global temp_color_a
    .type temp_color_a, %function
temp_color_a:
    b       boost_colour
    .size temp_color_a, . - temp_color_a

@ Site B: 0x0801B21A  str.w r9, [sp]  (other UI; [sp] = fg colour, r9 = 0xFFFF)
@ No push, so [sp] here is the caller's argument slot. r0, r1, r3 must survive.
    .global temp_color_b
    .type temp_color_b, %function
temp_color_b:
    push    {r1, lr}
    bl      boost_colour
    str     r1, [sp, #8]            @ caller's [sp] (we pushed 8 bytes)
    pop     {r1, pc}
    .size temp_color_b, . - temp_color_b

    .ltorg
