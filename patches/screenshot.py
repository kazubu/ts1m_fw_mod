#!/usr/bin/env python3
"""
Render the heat screen on the emulator's virtual panel, with and without a boost.

    python3 patches/screenshot.py [patched.bin] [out_dir]

Writes heat_normal.png / heat_boost.png (default display style) and
heat7_normal.png / heat7_boost.png (7-segment style). Each takes several
minutes: the display is drawn pixel by pixel through the emulated SPI/DMA path.

The heat page is forced (the key / stand handling that normally selects it is
not emulated), the supply is ~24 V (ch13) and the tip reads 300.0; for the
boost shots it drops to 285.0 after 30 heater passes. The ms counter is slowed
once booted, so the 20 s boost timeout does not expire during the slow redraw.
"""
import os
import struct
import sys
from multiprocessing import Process

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from ts1m_emu import Emu, HEATER_FN  # noqa: E402

CUR_PAGE = 0x200004D4          # UI: pointer to the current page object
HEAT_PAGE = 0x20000524         # page 1: heat screen (entering it sets MODE = 1)
STYLE7 = 0x20000455            # != 0: 7-segment display style


def adc_for(temp):
    return int(round((temp - 508) / 1.612))


class Shot(Emu):
    def __init__(self, image, drop_at, style7):
        super().__init__(image, force_tip=1, skip_res=True, defaults=True, work_mode=True,
                         lcd=True, adc_hook=self.adc)
        self.drop_at = drop_at
        self.style7 = style7

    def adc(self, emu, key):
        if key == (13, 0, 0, 0):
            return 3100                                  # ~24 V supply
        if key == (10, 0, 0, 0):
            low = self.drop_at and self.heater_calls >= self.drop_at
            return adc_for(2850 if low else 3000)
        return 2000

    def _on_code(self, uc, address, size, user):
        if address == HEATER_FN and self.heater_calls >= 10:
            self.tick_every = 5000
            if self.style7:
                uc.mem_write(STYLE7, b'\x01')
            if struct.unpack('<I', uc.mem_read(CUR_PAGE, 4))[0] != HEAT_PAGE:
                uc.mem_write(CUR_PAGE, struct.pack('<I', HEAT_PAGE))
                uc.mem_write(HEAT_PAGE + 16, b'\x01')    # "just entered": full redraw
        return super()._on_code(uc, address, size, user)


def render(image, out, drop_at, style7):
    s = Shot(image, drop_at, style7).run(count=30_000_000)
    s.save_png(out)
    print(f'{out}: boost flag {s.mu.mem_read(0x2000BFA8 + 14, 1)[0]}, err {s.err}')


def main(argv):
    image = argv[1] if len(argv) > 1 else os.path.join(
        ROOT, 'TS1M_Master_APP_V202_EN_notify_boost.bin')
    out = argv[2] if len(argv) > 2 else ROOT
    jobs = [('heat_normal.png', 0, False), ('heat_boost.png', 30, False),
            ('heat7_normal.png', 0, True), ('heat7_boost.png', 30, True)]
    procs = [Process(target=render, args=(image, os.path.join(out, name), drop, s7))
             for name, drop, s7 in jobs]
    for p in procs:
        p.start()
    for p in procs:
        p.join()


if __name__ == '__main__':
    main(sys.argv)
