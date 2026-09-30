#!/usr/bin/env python3
"""
Build a patched TS1M HEX from one of the patch sources in this directory.

    python3 patches/build.py [-p PATCH[,PATCH...]] [TS1M_Master_APP_V202_EN.hex]

    PATCH  notify_boost (default)  target-reached beep, auto boost, boost indicator
           pid_fix                 PID: keep D active while approaching, no P dead band
                                   (in-place byte patch, 2 instructions)
           chipid                  diagnostic: show the chip ID on the home screen
           findmark                diagnostic: find MARK.BIN in the SPI flash, show its address
           dumpboot                diagnostic: copy the bootloader over MARK.BIN (at 0x247000)

Produces next to the input:
    *_<PATCH>.hex   patched HEX, only the affected lines rewritten
                    (same line count and record layout as the original)
    *_<PATCH>.bin   flat image for ts1m_emu.py
(for several patches the suffix is the names joined with '_').

Patches with assembly (notify_boost, chipid, findmark, dumpboot) all use the
same free area, so at most one of them can be selected. Byte patches
(pid_fix) touch nothing else and combine with any of them, e.g.
-p notify_boost,pid_fix.

Needs arm-none-eabi-as / arm-none-eabi-ld / arm-none-eabi-objcopy.
"""
import os
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from hex2bin import parse_hex, to_bin  # noqa: E402

BASE = 0x08010000
HOOK_ADDR = 0x0802849C          # uncalled FatFs test function
HOOK_LIMIT = 0x080285F8         # first byte after it
# Bytes every patch expects: the free area must still hold the dead function.
COMMON_EXPECT = {
    HOOK_ADDR: bytes.fromhex('10b5'),          # push {r4, lr} of the dead function
    0x0802855E: b'M0.TXT',
    0x08028580: b'Hello, World!',
}

# name -> sites [(call site, original 4 bytes, symbol)], extra expected bytes,
#         bytes {addr: (original, replacement)} for in-place byte patches.
# A patch with sites is assembled from <name>.S into the free area.
PATCHES = {
    'pid_fix': {
        # PID 0x08015494, see ANALYSIS.md ch. 10 and pid_sim.py.
        'bytes': {
            # mov.w sl, #0 -> nop.w: D was disabled while err >= 0 and falling
            0x0801559A: (bytes.fromhex('4ff0000a'), bytes.fromhex('aff30080')),
            # bls -> nop: P was 0 while -40 <= err <= 50 (dead band)
            0x080155B4: (bytes.fromhex('01d9'), bytes.fromhex('00bf')),
        },
        'expect': {
            0x08015596: bytes.fromhex('a01b01d5'),                  # subs r0,r4,r6; bpl
            0x080155AE: bytes.fromhex('04f128005a28'),              # add.w r0,r4,#40; cmp r0,#90
            0x080155B6: bytes.fromhex('cdf804b0'),                  # str.w fp, [sp, #4]
        },
    },
    'notify_boost': {
        'sites': [
            (0x08021624, bytes.fromhex('316c8847'), 'hook'),           # ldr r1,[r6,#64]; blx r1
            (0x0801FA28, bytes.fromhex('4ff6ff71'), 'temp_color_a'),   # movw r1, #0xffff
            (0x0801B21A, bytes.fromhex('cdf80090'), 'temp_color_b'),   # str.w r9, [sp]
        ],
        'expect': {
            0x08021620: bytes.fromhex('b9f90200'),                  # ldrsh.w r0, [r9, #2]
            0x0802A7CC: bytes.fromhex('0200500064005000'),          # 2-beep pattern
        },
    },
    'chipid': {
        'sites': [
            (0x080201CC, bytes.fromhex('f4f732fb'), 'chipid_label'),   # bl drawString (tip name)
        ],
        'expect': {
            0x080201C6: bytes.fromhex('50f82100'),                  # ldr.w r0, [r0, r1, lsl #2]
            0x080201CA: bytes.fromhex('0c21'),                      # movs r1, #12
        },
    },
    'findmark': {
        'sites': [
            (0x080201CC, bytes.fromhex('f4f732fb'), 'findmark_label'), # bl drawString (tip name)
        ],
        'expect': {
            0x080201C6: bytes.fromhex('50f82100'),                  # ldr.w r0, [r0, r1, lsl #2]
            0x080201CA: bytes.fromhex('0c21'),                      # movs r1, #12
            0x08017E7C: bytes.fromhex('064a10b5'),                  # flash cs()
            0x08017F50: bytes.fromhex('2de9f041'),                  # flash xfer()
            0x08014C68: bytes.fromhex('02494af6'),                  # IWDG reload
        },
    },
    'dumpboot': {
        'sites': [
            (0x080201CC, bytes.fromhex('f4f732fb'), 'dumpboot_label'), # bl drawString (tip name)
        ],
        'expect': {
            0x080201C6: bytes.fromhex('50f82100'),                  # ldr.w r0, [r0, r1, lsl #2]
            0x080201CA: bytes.fromhex('0c21'),                      # movs r1, #12
            0x08017E7C: bytes.fromhex('064a10b5'),                  # flash cs()
            0x08017F50: bytes.fromhex('2de9f041'),                  # flash xfer()
            0x08017ECA: bytes.fromhex('10b50446'),                  # flash wren()
            0x08017E9C: bytes.fromhex('10b501e0'),                  # flash wait-not-busy
            0x08014C68: bytes.fromhex('02494af6'),                  # IWDG reload
        },
    },
}


def assemble(name):
    src = os.path.join(HERE, name + '.S')
    with tempfile.TemporaryDirectory() as tmp:
        obj, elf, raw = (os.path.join(tmp, n) for n in ('h.o', 'h.elf', 'h.bin'))
        subprocess.run(['arm-none-eabi-as', '-mcpu=cortex-m3', '-mthumb', src, '-o', obj],
                       check=True)
        subprocess.run(['arm-none-eabi-ld', f'-Ttext=0x{HOOK_ADDR:08x}', '-e', '0',
                        obj, '-o', elf], check=True)
        subprocess.run(['arm-none-eabi-objcopy', '-O', 'binary', '-j', '.text', elf, raw],
                       check=True)
        nm = subprocess.run(['arm-none-eabi-nm', elf], check=True, capture_output=True,
                            text=True).stdout
        syms = {name: int(addr, 16) for addr, _, name in
                (line.split() for line in nm.splitlines() if len(line.split()) == 3)}
        return open(raw, 'rb').read(), syms


def thumb_bl(src, dst):
    """Encode a Thumb-2 BL at `src` branching to `dst`."""
    off = dst - (src + 4)
    assert -(1 << 24) <= off < (1 << 24) and off % 2 == 0
    s = (off >> 24) & 1
    i1 = (off >> 23) & 1
    i2 = (off >> 22) & 1
    imm10 = (off >> 12) & 0x3FF
    imm11 = (off >> 1) & 0x7FF
    j1 = (~i1 ^ s) & 1
    j2 = (~i2 ^ s) & 1
    hi = 0xF000 | (s << 10) | imm10
    lo = 0xD000 | (j1 << 13) | (j2 << 11) | imm11
    return struct.pack('<HH', hi, lo)


def patch_hex_lines(src_path, dst_path, patches):
    """
    Rewrite only the data records that overlap `patches` ({addr: byte}),
    keeping every line, its length and its record layout.
    """
    out = []
    upper = 0
    touched = 0
    remaining = dict(patches)
    with open(src_path, newline='') as fh:
        lines = fh.readlines()
    for line in lines:
        body = line.rstrip('\r\n')
        eol = line[len(body):]
        if not body.startswith(':'):
            out.append(line)
            continue
        raw = bytearray.fromhex(body[1:])
        count, rectype = raw[0], raw[3]
        offset = (raw[1] << 8) | raw[2]
        if rectype == 0x04:
            upper = struct.unpack('>H', raw[4:6])[0]
        elif rectype == 0x00:
            base = (upper << 16) | offset
            changed = False
            for i in range(count):
                a = base + i
                if a in remaining:
                    raw[4 + i] = remaining.pop(a)
                    changed = True
            if changed:
                raw[-1] = (-sum(raw[:-1])) & 0xFF
                body = ':' + raw.hex().upper()
                touched += 1
        out.append(body + eol)
    if remaining:
        raise ValueError(f'{len(remaining)} patch bytes not covered by the HEX, '
                         f'first at {min(remaining):#x}')
    with open(dst_path, 'w', newline='') as fh:
        fh.writelines(out)
    return len(lines), touched


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='Build a patched TS1M HEX.')
    ap.add_argument('-p', '--patch', default='notify_boost',
                    help='comma-separated: ' + ', '.join(sorted(PATCHES)))
    ap.add_argument('hex', nargs='?', default=os.path.join(os.path.dirname(HERE),
                                                           'TS1M_Master_APP_V202_EN.hex'))
    args = ap.parse_args(argv[1:])
    names = args.patch.split(',')
    for name in names:
        if name not in PATCHES:
            ap.error(f'unknown patch {name!r} (choose from {", ".join(sorted(PATCHES))})')
    if len(set(names)) != len(names):
        ap.error('patch given twice')
    hooked = [n for n in names if PATCHES[n].get('sites')]
    if len(hooked) > 1:
        ap.error(f'{" and ".join(hooked)} both use the free area at {HOOK_ADDR:#x}')
    src = args.hex
    stem = src.rsplit('.', 1)[0] + '_' + '_'.join(names)

    expect = dict(COMMON_EXPECT) if hooked else {}
    for name in names:
        p = PATCHES[name]
        expect.update(p['expect'])
        expect.update({addr: orig for addr, orig, _ in p.get('sites', ())})
        expect.update({addr: orig for addr, (orig, _) in p.get('bytes', {}).items()})

    mem = parse_hex(src)
    for addr, want in expect.items():
        have = bytes(mem.get(addr + i, 0xFF) for i in range(len(want)))
        if have != want:
            raise SystemExit(f'unexpected bytes at {addr:#x}: {have.hex()} != {want.hex()}')

    patches = {}
    code = b''
    sites = []
    if hooked:
        sites = PATCHES[hooked[0]]['sites']
        code, syms = assemble(hooked[0])
        if HOOK_ADDR + len(code) > HOOK_LIMIT:
            raise SystemExit(f'hook is {len(code)} bytes, only {HOOK_LIMIT - HOOK_ADDR} available')
        for i, b in enumerate(code):
            patches[HOOK_ADDR + i] = b
        for addr, _, sym in sites:
            for i, b in enumerate(thumb_bl(addr, syms[sym])):
                patches[addr + i] = b
    byte_sites = [(addr, orig, new) for n in names
                  for addr, (orig, new) in PATCHES[n].get('bytes', {}).items()]
    for addr, orig, new in byte_sites:
        assert len(orig) == len(new)
        for i, b in enumerate(new):
            assert addr + i not in patches, f'patches overlap at {addr + i:#x}'
            patches[addr + i] = b

    nlines, touched = patch_hex_lines(src, stem + '.hex', patches)

    # Sanity: the patched HEX must differ from the original in exactly `patches`.
    new = parse_hex(stem + '.hex')
    diff = {a for a in set(mem) | set(new) if mem.get(a) != new.get(a)}
    expected = {a for a, b in patches.items() if mem.get(a) != b}
    assert diff == expected, 'patched HEX differs outside the intended bytes'

    _, blob = to_bin(new, base=BASE)
    with open(stem + '.bin', 'wb') as fh:
        fh.write(blob)

    if hooked:
        print(f'hook: {len(code)} bytes at {HOOK_ADDR:#010x} '
              f'({HOOK_LIMIT - HOOK_ADDR - len(code)} spare)')
    for addr, orig, sym in sites:
        print(f'site: {addr:#010x} {orig.hex()} -> {thumb_bl(addr, syms[sym]).hex()} ({sym})')
    for addr, orig, new in byte_sites:
        print(f'byte: {addr:#010x} {orig.hex()} -> {new.hex()}')
    print(f'{stem}.hex: {nlines} lines, {touched} rewritten, {len(diff)} bytes changed')
    print(f'{stem}.bin: {len(blob)} bytes')


if __name__ == '__main__':
    main(sys.argv)
