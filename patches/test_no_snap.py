#!/usr/bin/env python3
"""
Emulator check of the no_snap patch.

    python3 patches/build.py -p no_snap
    python3 patches/test_no_snap.py [patched.bin]

Holds the tip at 299.0 (inside the -1.8..+1.7 snap window of the 300.0 target)
and at 305.0 (outside it) on the heat screen, in both display styles, and in
sleep (target SleepTemp 100.0, tip 99.0 / 105.0), and records the value the heat
screen is about to draw right after each snap site:
    0x0801F9E4 working, default style   0x0801B1D8 working, 7-segment style
    0x0801F6A0 sleeping (either style)
The original firmware must show 300 inside the window; the patched one must
show the measured average there. Takes a few minutes.

The display average (0x20009DD4) is fed by the measurement cycle, which is not
emulated, so the average's return value is replaced by the tip temperature.
"""
import os
import struct
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_R2, UC_ARM_REG_R6,  # noqa: E402
                               UC_ARM_REG_R7)
from ts1m_emu import Emu, HEATER_FN, MODE, TARGET_TEMP  # noqa: E402

CUR_PAGE = 0x200004D4          # UI: pointer to the current page object
HEAT_PAGE = 0x20000524         # page 1: heat screen
STYLE7 = 0x20000455            # != 0: 7-segment display style
TIP_ADC = (10, 0, 0, 0)
ORIGINAL = os.path.join(ROOT, 'TS1M_Master_APP_V202_EN.bin')

# right after each `bl average` (0x0801EE00) that feeds a snap site
AFTER_AVERAGE = (0x0801B1B8, 0x0801F680, 0x0801F9C0)
# first instruction after each snap store: (address, base register, offset of the shown value)
AFTER_SNAP = {
    0x0801B1DC: (UC_ARM_REG_R7, 122),
    0x0801F6A4: (UC_ARM_REG_R6, 118),
    0x0801F9E8: (UC_ARM_REG_R2, 118),
}


def adc_for(temp):
    """Inverse of the measured ch10 -> CUR_TEMP curve (245 tip, cold junction 4.4 deg)."""
    return int(round((temp - 508) / 1.612))


class Screen(Emu):
    def __init__(self, image, tip, style7, sleep):
        super().__init__(image, force_tip=1, skip_res=True, defaults=True, work_mode=True,
                         adc_hook=lambda e, key: adc_for(tip) if key == TIP_ADC else
                         (3100 if key == (13, 0, 0, 0) else 2000))
        self.style7 = style7
        self.tip = tip
        self.sleep = sleep
        self.shown = []         # (site, shown value, target)

    def _on_code(self, uc, address, size, user):
        if address == HEATER_FN and self.heater_calls >= 10:
            uc.mem_write(STYLE7, b'\x01' if self.style7 else b'\x00')
            if struct.unpack('<I', uc.mem_read(CUR_PAGE, 4))[0] != HEAT_PAGE:
                uc.mem_write(CUR_PAGE, struct.pack('<I', HEAT_PAGE))
                uc.mem_write(HEAT_PAGE + 16, b'\x01')    # "just entered": full redraw
        if address in AFTER_AVERAGE:
            uc.reg_write(UC_ARM_REG_R0, self.tip)
        site = AFTER_SNAP.get(address)
        if site and self.heater_calls >= 10:
            reg, off = site
            base = uc.reg_read(reg)
            shown = struct.unpack('<h', uc.mem_read(base + off, 2))[0]
            self.shown.append((address, shown, self.s16(TARGET_TEMP)))
        ret = super()._on_code(uc, address, size, user)
        if address == HEATER_FN and self.sleep and self.heater_calls > 1:
            uc.mem_write(MODE, b'\x02')                 # after work_mode forced 1
        return ret


def run(args):
    image, tip, style7, sleep = args
    s = Screen(image, tip, style7, sleep).run(count=40_000_000)
    return args, s.shown[-20:], s.err


def main(argv):
    patched = argv[1] if len(argv) > 1 else os.path.join(
        ROOT, 'TS1M_Master_APP_V202_EN_no_snap.bin')
    jobs = [(img, tip, s7, False) for img in (ORIGINAL, patched)
            for tip in (2990, 3050) for s7 in (False, True)]
    jobs += [(img, tip, False, True) for img in (ORIGINAL, patched) for tip in (990, 1050)]
    with Pool(len(jobs)) as pool:
        results = pool.map(run, jobs)

    ok = True
    for (img, tip, s7, sleep), shown, err in results:
        label = f'{"patched " if img == patched else "original"} tip {tip / 10:5.1f} ' \
                f'{"sleep  " if sleep else "7-seg  " if s7 else "default"}'
        if err or not shown:
            print(f'FAIL {label}: no heat-screen draw recorded (err {err})')
            ok = False
            continue
        sites = sorted({f'{a:#010x}' for a, _, _ in shown})
        values = sorted({v for _, v, _ in shown})
        target = shown[-1][2]
        snapped = all(v == target for _, v, _ in shown)
        if img == patched or tip in (3050, 1050):
            passed = not snapped and all(abs(v - tip) <= 10 for _, v, _ in shown)
            want = 'measured'
        else:
            passed = snapped
            want = 'target'
        print(f'{"PASS" if passed else "FAIL"} {label}: shown {values} target {target} '
              f'(want {want}) at {", ".join(sites)}')
        ok &= passed
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
