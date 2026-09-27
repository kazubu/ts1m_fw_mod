@ TS1M V202 diagnostic: show the MCU chip ID on the home screen
@
@ The tip-name label at the top left of the home screen (normally "245",
@ "H100", ... or empty) is replaced by the 32-bit chip ID read from
@ 0x1FFFF704, as 8 hex digits. Nothing else changes; the heater is not touched.
@
@ Expected values (WCH SDK, DBGMCU_GetCHIPID):
@   CH32F208WBU  0x208004xC     CH32F208RBT  0x208104xC
@   CH32F203VCT  0x203004x4     CH32F207VCT  0x207004x8   (x = revision)
@
@ Call site: 0x080201CC  bl drawString(str, x=12, y=2, font, [sp] fg, bg, flag)
@ The hook keeps y / font / colours, swaps the string and moves x from 12 to 2:
@ the label is drawn into a 132 px wide buffer and 8 wide hex digits (A-F)
@ can otherwise run past its right edge and wrap into the next row.
@
@ Placed at 0x0802849C, over the uncalled FatFs test function (as notify_boost;
@ build one or the other, not both).

    .syntax unified
    .cpu cortex-m3
    .thumb

    .equ CHIPID,      0x1FFFF704
    .equ DRAWSTRING,  0x08014834

    .text
    .global chipid_label
    .type chipid_label, %function
chipid_label:                       @ r0 = original string, r1-r3 = x, y, font
    push    {r4-r6, lr}             @ 16 bytes
    sub     sp, #24                 @ [sp+0..11] outgoing stack args, [sp+12..23] text
    ldr     r4, [sp, #40]           @ copy the caller's fg / bg / flag
    str     r4, [sp, #0]
    ldr     r4, [sp, #44]
    str     r4, [sp, #4]
    ldr     r4, [sp, #48]
    str     r4, [sp, #8]

    ldr     r4, =CHIPID
    ldr     r4, [r4]
    add     r0, sp, #12
    movs    r5, #8
1:
    lsrs    r6, r4, #28
    lsls    r4, r4, #4
    cmp     r6, #10
    ite     lo
    addlo   r6, r6, #'0'
    addhs   r6, r6, #('A' - 10)
    strb    r6, [r0], #1
    subs    r5, r5, #1
    bne     1b
    strb    r5, [r0]                @ r5 == 0: terminator

    add     r0, sp, #12
    movs    r1, #2                  @ x (was 12)
    ldr     r4, =DRAWSTRING + 1
    blx     r4                      @ r2-r3 untouched: same y, font
    add     sp, #24
    pop     {r4-r6, pc}

    .ltorg
    .size chipid_label, . - chipid_label
