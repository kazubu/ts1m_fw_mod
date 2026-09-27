#!/usr/bin/env python3
"""
Intel HEX -> raw binary, based at 0x08010000 (the TS1M app load address).

ts1m_emu.py expects a flat image whose first byte is the content of
0x08010000, which is what this produces.

    python3 hex2bin.py TS1M_Master_APP_V202_EN.hex TS1M_Master_APP_V202_EN.bin

Gaps in the HEX are filled with 0xFF (erased flash). Note that the distributed
image is contiguous from 0x08010000 to 0x080773C4, so there are none in
practice, but padding matters if you convert a patched file that added data
past the app body.
"""
import struct
import sys


def parse_hex(path):
    """Return {absolute_address: byte} for every data record in the file."""
    mem = {}
    upper = 0
    for lineno, line in enumerate(open(path), 1):
        line = line.strip()
        if not line.startswith(':'):
            continue
        raw = bytes.fromhex(line[1:])
        if (sum(raw) & 0xFF) != 0:
            raise ValueError(f'{path}:{lineno}: bad checksum')
        count, rectype = raw[0], raw[3]
        offset = (raw[1] << 8) | raw[2]
        data = raw[4:4 + count]
        if rectype == 0x00:                      # data
            base = (upper << 16) | offset
            for i, byte in enumerate(data):
                mem[base + i] = byte
        elif rectype == 0x01:                    # EOF
            break
        elif rectype == 0x02:                    # extended segment address
            upper = struct.unpack('>H', data)[0] >> 12
        elif rectype == 0x04:                    # extended linear address
            upper = struct.unpack('>H', data)[0]
        elif rectype == 0x05:                    # start linear address
            pass
        else:
            raise ValueError(f'{path}:{lineno}: unhandled record type {rectype:#04x}')
    if not mem:
        raise ValueError(f'{path}: no data records')
    return mem


def to_bin(mem, base=None, fill=0xFF):
    lo = base if base is not None else min(mem)
    hi = max(mem)
    out = bytearray([fill]) * (hi - lo + 1)
    for addr, byte in mem.items():
        if addr < lo:
            raise ValueError(f'address {addr:#x} is below base {lo:#x}')
        out[addr - lo] = byte
    return lo, bytes(out)


def main(argv):
    if not 2 <= len(argv) <= 3:
        print(__doc__.strip())
        return 1
    src = argv[1]
    dst = argv[2] if len(argv) == 3 else src.rsplit('.', 1)[0] + '.bin'

    mem = parse_hex(src)
    lo, blob = to_bin(mem, base=0x08010000)

    with open(dst, 'wb') as fh:
        fh.write(blob)

    gaps = (hi_lo := len(blob)) - len(mem)
    print(f'{dst}: {len(blob)} bytes, {lo:#010x}..{lo + len(blob):#010x}'
          f'{f" ({gaps} padding bytes)" if gaps else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
