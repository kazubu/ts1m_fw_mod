@ TS1M V202 diagnostic: dump the bootloader into MARK.BIN on the DFU drive
@
@ The findmark patch located MARK.BIN (64 KB of "TS1M-FINDVOL-MK\n") on the
@ W25Q64 at 0x247000, as 128 sectors. This patch overwrites exactly that 64 KB
@ with the internal flash 0x08000000-0x0800FFFF (the bootloader, 64 KB), so
@ copying MARK.BIN off the DFU drive yields the bootloader image.
@
@ Only if all 64 KB at 0x247000 still hold the marker (contiguous file, nothing
@ moved) are the 16 x 4 KB sectors erased and programmed. The data is then read
@ back and compared. Anything else is left untouched. Once dumped, later boots
@ just re-verify.
@
@ Shown in place of the tip-name label at the top left of the home screen:
@   OK        the 64 KB at 0x247000 equals the bootloader (read back and compared)
@   NG:0      SPI2 not enabled yet (nothing done)
@   NG:1      neither the marker nor the bootloader there (nothing written)
@   NG:2      written, but the read-back differs
@
@ The watchdog (IWDG, ~3.2 s) is fed by the firmware's busy-wait after every
@ erase / program; one read pass of 64 KB takes well under a second.
@
@ The text is built in the unused heap (as notify_boost's state) and the hook
@ tail-calls drawString with the caller's stack arguments untouched.
@
@ Call site: 0x080201CC  bl drawString(str, x=12, y=2, font, [sp] fg, bg, flag)
@ Same site and x = 2 as the chipid / findmark patches.
@
@ Placed at 0x0802849C, over the uncalled FatFs test function (as the other
@ patches; build one of them, not several).

    .syntax unified
    .cpu cortex-m3
    .thumb

    .equ DRAWSTRING,  0x08014834
    .equ FLASH_CS,    0x08017E7C    @ cs(0) = select (PB12 low), cs(1) = release
    .equ FLASH_XFER,  0x08017F50    @ xfer(byte, 8) -> received byte
    .equ FLASH_WREN,  0x08017ECA    @ wren(1): WRITE ENABLE (0x06)
    .equ FLASH_WAIT,  0x08017E9C    @ poll status (0x05) until not busy, feeds IWDG
    .equ SPI2_CR1,    0x40003800
    .equ SPE,         0x40
    .equ DST,         0x247000      @ MARK.BIN on the W25Q64 (found by findmark)
    .equ BOOT,        0x08000000    @ source: bootloader, 64 KB
    .equ TXT,         0x2000BFA8    @ label text, top of the unused 512 B heap
    .equ NG0,         0x303A474E    @ "NG:0"
    .equ OK_,         0x00004B4F    @ "OK"

    .text
    .global dumpboot_label
    .type dumpboot_label, %function
dumpboot_label:                     @ r0 = original string, r1-r3 = x, y, font
    push    {r2-r7, r11, lr}        @ keep y / font for the tail call
    ldr     r5, =FLASH_CS + 1
    ldr     r11, =FLASH_XFER + 1
    ldr     r7, =NG0

    ldr     r0, =SPI2_CR1
    ldrh    r0, [r0]
    tst     r0, #SPE
    beq     ng                      @ NG:0
    bl      classify
    tst     r0, #2
    bne     ok
    add     r7, r7, #0x1000000      @ NG:1
    tst     r0, #1
    beq     ng
    bl      dump
    bl      classify
    add     r7, r7, #0x1000000      @ NG:2
    tst     r0, #2
    beq     ng

ok:
    ldr     r7, =OK_                @ "OK"
ng:
    ldr     r0, =TXT
    str     r7, [r0]
    movs    r2, #0
    strb    r2, [r0, #4]            @ terminator
    movs    r1, #2                  @ x (was 12)
    pop     {r2-r7, r11, lr}
    ldr     r12, =DRAWSTRING + 1
    bx      r12

@ classify() -> r0: bit0 = all 64 KB at DST are the marker,
@                   bit1 = all 64 KB at DST equal the bootloader
classify:
    push    {r4, r6, r7, lr}
    ldr     r6, =DST
    movs    r0, #0
    blx     r5                      @ select
    movs    r0, #0x03               @ READ DATA
    bl      tx
    bl      addr3
    ldr     r4, =BOOT
    movs    r6, #3
    adr     r7, mark
3:
    movs    r0, #0xFF
    bl      tx
    uxtb    r0, r0
    ldrb    r1, [r4]
    cmp     r0, r1
    it      ne
    bicne   r6, r6, #2
    and     r2, r4, #15
    ldrb    r1, [r7, r2]
    cmp     r0, r1
    it      ne
    bicne   r6, r6, #1
    adds    r4, r4, #1
    lsls    r1, r4, #15             @ until r4 reaches BOOT + 0x10000
    bpl     3b
    movs    r0, #1
    blx     r5                      @ release
    mov     r0, r6
    pop     {r4, r6, r7, pc}

@ dump(): erase the 16 x 4 KB sectors at DST, then program them from BOOT
dump:
    push    {r4, r6, r7, lr}
    ldr     r6, =DST
    add     r7, r6, #0x10000        @ DST_END
4:
    movs    r0, #0x20               @ SECTOR ERASE (4 KB)
    bl      begin
    bl      done
    add     r6, r6, #0x1000
    cmp     r6, r7
    blo     4b

    ldr     r6, =DST
    ldr     r4, =BOOT
5:
    movs    r0, #0x02               @ PAGE PROGRAM (256 B)
    bl      begin
6:
    ldrb    r0, [r4], #1
    bl      tx
    tst     r4, #0xFF
    bne     6b
    bl      done
    add     r6, r6, #0x100
    cmp     r6, r7
    blo     5b
    pop     {r4, r6, r7, pc}

begin:                              @ write enable, select, command r0, address r6
    push    {r0, lr}
    movs    r0, #1
    ldr     r1, =FLASH_WREN + 1
    blx     r1
    movs    r0, #0
    blx     r5
    pop     {r0}
    bl      tx
    pop     {lr}
addr3:                              @ send the 24-bit address in r6
    push    {lr}
    ubfx    r0, r6, #16, #8
    bl      tx
    ubfx    r0, r6, #8, #8
    bl      tx
    uxtb    r0, r6
    pop     {lr}
tx:                                 @ xfer(r0, 8), returns to the caller
    movs    r1, #8
    bx      r11

done:                               @ release, then wait for the write to finish
    push    {lr}
    movs    r0, #1
    blx     r5
    ldr     r0, =FLASH_WAIT + 1
    blx     r0
    pop     {pc}

    .align  2
mark:
    .ascii  "TS1M-FINDVOL-MK\n"

    .ltorg
    .size dumpboot_label, . - dumpboot_label
