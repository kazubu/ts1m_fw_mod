@ TS1M V202 diagnostic: find MARK.BIN in the external SPI flash
@
@ MARK.BIN (64 KB of the 16-byte line "TS1M-FINDVOL-MK\n") is copied to the
@ DFU mass-storage drive. This patch scans the W25Q64 (SPI2, CS = PB12, 8 MB)
@ for that line at every 512-byte boundary and shows the result in place of
@ the tip-name label at the top left of the home screen:
@
@   AAAAAA:CC   AAAAAA = first matching address (24-bit hex)
@               CC     = number of matching 512 B sectors (saturates at FF;
@                        the whole 64 KB file contiguous = 80)
@   FFFFFF:00   not found
@   --------    SPI2 not enabled yet (nothing was scanned)
@
@ A file's data starts on a sector boundary, so probing only the first 16
@ bytes of each 512 B sector is enough: 16384 probes, 8 bytes on the bus each
@ unless the first 4 match. The label is only drawn on boot / page redraws,
@ so the scan simply runs on every draw (a few hundred ms, heater untouched).
@ The watchdog (IWDG, ~3.2 s) is fed once per probe with the firmware's own
@ reload function, as its SPI busy-wait does.
@
@ Call site: 0x080201CC  bl drawString(str, x=12, y=2, font, [sp] fg, bg, flag)
@ Same site and x = 2 as the chipid patch.
@
@ Placed at 0x0802849C, over the uncalled FatFs test function (as the other
@ patches; build one of them, not several).

    .syntax unified
    .cpu cortex-m3
    .thumb

    .equ DRAWSTRING,  0x08014834
    .equ FLASH_CS,    0x08017E7C    @ cs(0) = select (PB12 low), cs(1) = release
    .equ FLASH_XFER,  0x08017F50    @ xfer(byte, 8) -> received byte
    .equ IWDG_FEED,   0x08014C68
    .equ SPI2_CR1,    0x40003800
    .equ SPE,         0x40
    .equ FLASH_SIZE,  0x800000
    .equ STEP,        512

    .text
    .global findmark_label
    .type findmark_label, %function
findmark_label:                     @ r0 = original string, r1-r3 = x, y, font
    push    {r4-r11, lr}            @ 36 bytes
    sub     sp, #28                 @ [sp+0..11] outgoing stack args, [sp+12..27] text
    ldr     r4, [sp, #64]           @ copy the caller's fg / bg / flag
    str     r4, [sp, #0]
    ldr     r4, [sp, #68]
    str     r4, [sp, #4]
    ldr     r4, [sp, #72]
    str     r4, [sp, #8]
    mov     r9, r3                  @ font
    mov     r10, r2                 @ y

    ldr     r0, =SPI2_CR1
    ldrh    r0, [r0]
    tst     r0, #SPE
    bne     scan
    add     r0, sp, #12             @ "--------"
    movs    r1, #'-'
    movs    r2, #8
1:
    strb    r1, [r0], #1
    subs    r2, r2, #1
    bne     1b
    strb    r2, [r0]
    b       draw

scan:
    ldr     r5, =FLASH_CS + 1
    ldr     r11, =FLASH_XFER + 1
    movs    r6, #0                  @ r6 = address
    mov     r7, #0xFFFFFFFF         @ r7 = first hit
    movs    r8, #0                  @ r8 = hits
probe:
    ldr     r0, =IWDG_FEED + 1
    blx     r0
    movs    r0, #0
    blx     r5                      @ select
    movs    r0, #0x03               @ READ DATA, 24-bit address
    movs    r1, #8
    blx     r11
    ubfx    r0, r6, #16, #8
    movs    r1, #8
    blx     r11
    ubfx    r0, r6, #8, #8
    movs    r1, #8
    blx     r11
    uxtb    r0, r6
    movs    r1, #8
    blx     r11
    adr     r4, mark
2:
    movs    r0, #0xFF
    movs    r1, #8
    blx     r11
    ldrb    r1, [r4], #1
    uxtb    r0, r0
    cmp     r0, r1
    bne     3f                      @ mismatch: stop clocking this sector
    adr     r0, mark_end
    cmp     r4, r0
    bne     2b
    cmp     r8, #0
    it      eq
    moveq   r7, r6
    adds    r8, r8, #1
3:
    movs    r0, #1
    blx     r5                      @ release (ends the read)
    add     r6, r6, #STEP
    cmp     r6, #FLASH_SIZE
    blo     probe

    add     r0, sp, #12             @ "AAAAAA:CC"
    lsls    r1, r7, #8              @ address in the top 24 bits
    movs    r2, #6
    bl      hex
    movs    r1, #':'
    strb    r1, [r0], #1
    cmp     r8, #0xFF
    it      hi
    movhi   r8, #0xFF
    lsl     r1, r8, #24
    movs    r2, #2
    bl      hex
    movs    r1, #0
    strb    r1, [r0]

draw:
    add     r0, sp, #12
    movs    r1, #2                  @ x (was 12)
    mov     r2, r10
    mov     r3, r9
    ldr     r4, =DRAWSTRING + 1
    blx     r4
    add     sp, #28
    pop     {r4-r11, pc}

@ hex(r0 = out, r1 = value left-aligned, r2 = digits) -> r0 = end of output
hex:
    lsrs    r3, r1, #28
    lsls    r1, r1, #4
    cmp     r3, #10
    ite     lo
    addlo   r3, r3, #'0'
    addhs   r3, r3, #('A' - 10)
    strb    r3, [r0], #1
    subs    r2, r2, #1
    bne     hex
    bx      lr

    .align  2
mark:
    .ascii  "TS1M-FINDVOL-MK\n"
mark_end:

    .ltorg
    .size findmark_label, . - findmark_label
