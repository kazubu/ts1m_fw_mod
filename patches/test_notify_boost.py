#!/usr/bin/env python3
"""
Emulator check of the notify/boost patch.

    python3 patches/build.py
    python3 patches/test_notify_boost.py [patched.bin]

Drives the tip temperature (ADC ch10, mux 000 = "adc3") through a scripted
profile with the heater held in work mode, and records what the PID is given
as its target and when the buzzer's beep() is called. Takes a few minutes.

Time base: ts1m_emu advances the ms counter once per 50 instructions, so
"300 ms" here is emulated time, not wall-clock or heater passes.
"""
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from unicorn.arm_const import UC_ARM_REG_R0  # noqa: E402
from ts1m_emu import (Emu, HEATER_FN, PID_FN, TARGET_TEMP, CUR_TEMP,  # noqa: E402
                      MODE)
SLEEP_TEMP_DEFAULT = 1000

BEEP_FN = 0x08012218          # buzzer beep(pattern); object 0x20000330, +4
HOOK_PATTERN = 0x0802A7CC     # the pattern the hook plays
STATE = 0x2000BFA8            # hook state; +14 = "boost applied" flag read by the display
AFTER_PID = 0x08021628        # first instruction after the patched call site
TIP_ADC = (10, 0, 0, 0)


def adc_for(temp):
    """Inverse of the measured ch10 -> CUR_TEMP curve (245 tip, cold junction 4.4 deg)."""
    return int(round((temp - 508) / 1.612))


# (until heater pass, tip temp, mode, WorkTemp1)
PLAN = [
    (300, ('ramp', 1500, 3000), 1, 3000),   # heat up
    (400, 3000, 1, 3000),                   # hold
    (1300, 2850, 1, 3000),                  # sustained load: boost, then 20 s timeout
    (1400, 3000, 1, 3000),                  # recover
    (1500, 2850, 1, 3000),                  # load again: boost allowed again
    (1600, 3000, 1, 3000),
    (1603, 2850, 1, 3000),                  # blip shorter than 300 ms: no boost
    (1700, 3000, 1, 3000),
    (1800, 4400, 1, 4400),                  # set 440: boost is capped at 450
    (1900, 4250, 1, 4400),
    (2000, 4500, 1, 4500),                  # set 450: target never changes
    (2100, 4350, 1, 4500),
    (2300, 4500, 2, 4500),                  # sleep: no beep, no boost
    (2400, 4500, 1, 4500),                  # back to work: beep again
    (2500, 3000, 1, 3000),                  # set 300 and reach it: beep
    (2600, 1500, 3, 3000),                  # heater off (PID not called), tip cools
    (2700, 1500, 1, 3000),                  # heater on, same set temp, cold tip: no boost
    (2800, 3000, 1, 3000),                  # reached again: beep
]


class Scenario(Emu):
    def __init__(self, image):
        super().__init__(image, force_tip=1, skip_res=True, defaults=True, work_mode=True,
                         adc_hook=lambda e, key: adc_for(e.tip) if key == TIP_ADC else 2000)
        self.tip = 1500
        self.mode = 1
        self.work = 3000
        self.rows = []          # [pass, tip, target at PID, target after PID, cur, base]
        self.beeps = []         # (pass, pattern)

    def step(self):
        n = self.heater_calls + 1           # the pass about to run
        for until, temp, mode, work in PLAN:
            if n < until:
                break
        else:
            return False
        if isinstance(temp, tuple):
            _, lo, hi = temp
            temp = lo + (hi - lo) * n // until
        self.tip, self.mode, self.work = temp, mode, work
        self.set_setting(1, work)
        return True

    def _on_code(self, uc, address, size, user):
        if address == HEATER_FN and self.heater_calls:
            if not self.step():
                uc.emu_stop()
                return
        ret = super()._on_code(uc, address, size, user)
        if address == HEATER_FN and self.heater_calls > 1:
            uc.mem_write(MODE, bytes([self.mode]))      # after work_mode forced 1
        if address == PID_FN:
            base = self.work if self.mode == 1 else SLEEP_TEMP_DEFAULT
            self.rows.append([self.heater_calls, self.tip, self.s16(TARGET_TEMP), None,
                              self.s16(CUR_TEMP), base])
        elif address == AFTER_PID and self.rows:
            self.rows[-1][3] = self.s16(TARGET_TEMP)
            self.rows[-1].append(uc.mem_read(STATE + 14, 1)[0])
        elif address == BEEP_FN:
            self.beeps.append((self.heater_calls, uc.reg_read(UC_ARM_REG_R0)))
        return ret


def main(argv):
    image = argv[1] if len(argv) > 1 else os.path.join(
        ROOT, 'TS1M_Master_APP_V202_EN_notify_boost.bin')
    s = Scenario(image).run(count=100_000_000)
    assert s.err is None and not s.halted, (s.err, s.halted)

    beeps = [p for p, pat in s.beeps if pat == HOOK_PATTERN]
    boosted = [r for r in s.rows if r[2] != r[3]]
    print('beeps at heater pass:', beeps)
    last = None
    for r in s.rows:
        key = (r[2], r[3])
        if key != last:
            print(f'  pass {r[0]:5d} tip {r[1]:5d} cur {r[4]:5d} '
                  f'target@PID {r[2]:5d} after {r[3]:5d}')
            last = key

    def in_(a, b):
        return [r for r in boosted if a <= r[0] < b]

    checks = {
        'one beep per reached target (heat-up, 440, 450, after sleep, after off)':
            len(beeps) == 6 and beeps[0] < 300 and 2700 <= beeps[5] < 2800,
        'no beep while sleeping': not [p for p in beeps if 2100 <= p < 2300],
        'target restored to the set value after every PID call':
            all(r[3] == r[5] for r in s.rows if r[0] > 1),
        'PID target is the set value unless boosting':
            all(r[2] in (r[5], min(r[5] + 200, 4500)) for r in s.rows if r[0] > 1),
        'boost = +20.0 under load': in_(400, 1300) and all(r[2] == 3200 for r in in_(400, 1300)),
        'boost times out and stays off until recovery':
            bool(in_(400, 1300)) and in_(400, 1300)[-1][0] < 1250,
        'boost allowed again after recovery': bool(in_(1400, 1500)),
        'no boost for a short blip': not in_(1600, 1700),
        'boost capped at 450.0': all(r[2] == 4500 for r in in_(1800, 1900)) and in_(1800, 1900),
        'set 450: target untouched': not in_(1900, 2100),
        'no boost while sleeping': not in_(2100, 2300),
        'no boost after heater off/on': not in_(2600, 2800),
        'display flag set exactly when the target was raised':
            all(r[6] == (1 if r[2] != r[3] else 0) for r in s.rows if r[0] > 1 and len(r) > 6),
    }
    ok = True
    for name, passed in checks.items():
        print(('PASS ' if passed else 'FAIL ') + name)
        ok &= bool(passed)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
