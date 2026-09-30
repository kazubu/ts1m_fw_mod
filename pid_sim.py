#!/usr/bin/env python3
"""
Thermal-model simulation of the TS1M PID (0x08015494).

    python3 pid_sim.py              # C245: reproduce the overshoot, compare fixes
    python3 pid_sim.py --tips       # all tip types, sensor lag sweep
    python3 pid_sim.py --check      # PyPID vs the firmware PID in Unicorn (slow)

The controller is either the firmware's own PID function run in Unicorn
(FirmwarePID) or PyPID, a Python transcription used for fast sweeps. --check
feeds both the same random inputs and requires identical outputs, for every
tip type and for the D-on-approach / no-dead-band byte patch.

Plant (assumed; no parameter is fitted to a real tip):
    heater node  Ch: element, receives the heater power
    tip node     Ct: tip body, loses heat to ambient
    reading      first-order lag tau_s of the heater node (what the PID sees)
    Ch dTh/dt = P - Ght (Th - Tt)
    Ct dTt/dt = Ght (Th - Tt) - Gta (Tt - Ta)
P = Pmax * out / 290, held for one PID period.
"""
import argparse
import os
import random
import struct
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OUT_MAX = 290
TIP_NAMES = {1: '245', 2: '210', 3: '115', 5: 'H100'}

# Byte patch: keep D active while approaching from below, and drop the P dead band.
PATCH_D_APPROACH = (0x0801559A, 'aff30080')   # mov.w sl, #0 -> nop.w
PATCH_NO_DEADBAND = (0x080155B4, '00bf')      # bls          -> nop


class PyPID:
    """Transcription of 0x08015494. Temperatures in 0.1 deg, err = target - cur.

    fix: 'd_approach' (D not disabled while approaching), 'no_skip' (integrate
    every pass while above target), 'clamp_all' (int_max also on the err > 300
    path). deadband=(1, 0) removes the P dead band.
    """
    # (Kp, Ki, Kd) read from RAM 0x20000204 after tip detection, per ironType
    GAINS = {1: (1.0, 0.2, 4.0), 2: (0.4, 0.1, 0.03), 3: (0.15, 0.1, 0.0),
             5: (0.4, 0.3, 2.0)}
    # integral factor while err > 300, per ironType (0x080154F0)
    FAR_K = {1: 0.001, 2: 0.01, 5: 0.01}

    def __init__(self, iron_type=1, gains=None, int_max=3000, reset_over=60,
                 fix=(), deadband=(-40, 50)):
        self.kp, self.ki, self.kd = gains or self.GAINS[iron_type]
        self.iron_type = iron_type
        self.int_max = int_max
        self.reset_over = reset_over
        self.fix = set(fix)
        self.deadband = deadband
        self.integ = 0
        self.prev = 0
        self.cnt = 0
        self.terms = (0.0, 0.0, 0.0)

    def step(self, cur, tgt):
        err = tgt - cur
        if cur < tgt:
            self.cnt = 0
        elif cur > tgt:
            self.cnt += 1
        if self.cnt > 5 or cur > tgt + 40:
            self.cnt = 0
        if err > 300:
            self.integ = int(err * self.FAR_K.get(self.iron_type, 0.1) + self.integ)
            if 'clamp_all' in self.fix:
                self.integ = min(self.integ, self.int_max)
        else:
            if self.cnt == 0 or 'no_skip' in self.fix:
                self.integ = int(err * 0.1 + self.integ)
            self.integ = min(self.integ, self.int_max)
        if cur >= tgt and (cur - tgt > self.reset_over or self.integ < 0):
            self.integ = 0
        d_on = 1
        if err >= 0 and err - self.prev < 0 and 'd_approach' not in self.fix:
            d_on = 0
        if err < -2 and err - self.prev > 0:
            d_on = 0
        lo, hi = self.deadband
        p = err * self.kp if not lo <= err <= hi else 0.0
        i = self.integ * self.ki
        d = min((err - self.prev) * self.kd * d_on, 20.0)
        self.prev = err
        self.terms = (p, i, d)
        return max(0, min(OUT_MAX, int(p + i + d)))


class FirmwarePID:
    """Boots the firmware once (about 30 s), then calls PID(r0 = cur) directly."""

    def __init__(self, iron_type=1, patches=()):
        from unicorn.arm_const import UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR
        from ts1m_emu import Emu, IRONTYPE
        self._regs = (UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR)
        image = os.path.join(HERE, 'TS1M_Master_APP_V202_EN.bin')
        tmp = None
        if patches:
            # Patch the image before boot: a mem_write to code that has already
            # run is not seen by Unicorn's translated-block cache.
            data = bytearray(open(image, 'rb').read())
            for addr, hexbytes in patches:
                off = addr - 0x08010000
                data[off:off + len(hexbytes) // 2] = bytes.fromhex(hexbytes)
            fd, tmp = tempfile.mkstemp(suffix='.bin')
            os.write(fd, data)
            os.close(fd)
            image = tmp
        try:
            self.e = Emu(image, force_tip=iron_type, skip_res=True, defaults=True,
                         work_mode=True).run(count=30_000_000)
        finally:
            if tmp:
                os.unlink(tmp)
        mu = self.e.mu
        mu.mem_write(IRONTYPE, bytes([iron_type]))
        # clear the PID state (counter/terms, target..err, output, integral); keep the gains
        mu.mem_write(0x200001E0, bytes(0x14))
        mu.mem_write(0x200001F4, bytes(8))
        mu.mem_write(0x200001FC, bytes(8))

    def gains(self):
        return struct.unpack('<3f', self.e.mu.mem_read(0x20000204, 12))

    def step(self, cur, tgt):
        from ts1m_emu import PID_FN, TARGET_TEMP
        r0, sp, lr = self._regs
        mu = self.e.mu
        mu.mem_write(TARGET_TEMP, struct.pack('<h', tgt))
        mu.reg_write(r0, cur & 0xFFFFFFFF)
        mu.reg_write(sp, 0x2000F000)
        mu.reg_write(lr, self.e._RET_TRAP | 1)
        mu.emu_start(PID_FN | 1, self.e._RET_TRAP, count=100_000)
        return struct.unpack('<H', mu.mem_read(0x200001D0, 2))[0]


class Plant:
    def __init__(self, pmax=100.0, ch=0.5, ct=1.2, ght=0.8, gta=0.03,
                 ta=25.0, tau_s=0.0):
        self.pmax, self.ch, self.ct, self.ght, self.gta, self.ta = \
            pmax, ch, ct, ght, gta, ta
        self.tau_s = tau_s
        self.th = self.tt = self.ts = ta

    def advance(self, power, dt, sub=20):
        h = dt / sub
        for _ in range(sub):
            q = self.ght * (self.th - self.tt)
            self.th += h * (power - q) / self.ch
            self.tt += h * (q - self.gta * (self.tt - self.ta)) / self.ct
            if self.tau_s:
                self.ts += h * (self.th - self.ts) / self.tau_s
            else:
                self.ts = self.th


def simulate(pid, plant, period=0.05, t_end=40.0, target=3000):
    """Rows of (t, reading, tip, out, integral)."""
    rows = []
    t = 0.0
    while t < t_end:
        out = pid.step(int(round(plant.ts * 10)), target)
        plant.advance(plant.pmax * out / OUT_MAX, period)
        rows.append((t, plant.ts, plant.tt, out, getattr(pid, 'integ', None)))
        t += period
    return rows


def summary(rows, target=300.0):
    """peak, time to target-1, mean and peak-to-peak of the last 3 s."""
    peak = max(r[1] for r in rows)
    reach = next((r[0] for r in rows if r[1] >= target - 1), float('nan'))
    tail = [r[1] for r in rows if r[0] > rows[-1][0] - 3]
    return dict(peak=peak, reach=reach, final=sum(tail) / len(tail),
                ripple=max(tail) - min(tail))


# ------------------------------------------------------------------ reports

C245_CASES = [   # plant settings that reproduce 312-319 deg at a 300 deg setting
    ('60W lag2.0s T50ms', dict(pmax=60, tau_s=2.0), 0.05),
    ('80W lag2.0s T100ms', dict(pmax=80, tau_s=2.0), 0.1),
    ('130W lag1.5s T50ms', dict(pmax=130, tau_s=1.5), 0.05),
]
C245_VARIANTS = [
    ('original', {}),
    ('reset at +0', dict(reset_over=0)),
    ('D on approach', dict(fix={'d_approach'})),
    ('D appr + no dead band', dict(fix={'d_approach'}, deadband=(1, 0))),
    ('D appr + Kd 8 + reset 0', dict(fix={'d_approach'}, gains=(1.0, 0.2, 8.0),
                                     reset_over=0)),
]


def report_c245():
    print('C245 (ironType 1), target 300.0')
    for name, plant_kw, period in C245_CASES:
        print(name)
        for vname, kw in C245_VARIANTS:
            s = summary(simulate(PyPID(1, **kw), Plant(**plant_kw), period))
            print(f"  {vname:24} peak {s['peak']:6.1f}  reach {s['reach']:4.1f}s"
                  f"  final {s['final']:6.1f}  ripple {s['ripple']:4.1f}")


def report_tips():
    print('tip   pmax  lag | reach   peak  final ripple  I@300 (I term)')
    for it, name in TIP_NAMES.items():
        for pmax in (60, 100):
            for tau in (0.3, 1.0, 2.0):
                pid = PyPID(it)
                rows = simulate(pid, Plant(pmax=pmax, tau_s=tau))
                s = summary(rows)
                at = next((r for r in rows if r[1] >= 300), rows[-1])
                print(f"{name:5} {pmax:4} {tau:4} | {s['reach']:5.1f} {s['peak']:6.1f}"
                      f" {s['final']:6.1f} {s['ripple']:5.1f}  {at[4]:5} ({at[4] * pid.ki:4.0f})")


def check(n=3000):
    """Random-input comparison, PyPID vs the firmware. Returns True if identical."""
    ok = True
    runs = [(it, (), {}) for it in TIP_NAMES]
    runs.append((1, (PATCH_D_APPROACH, PATCH_NO_DEADBAND),
                 dict(fix={'d_approach'}, deadband=(1, 0))))
    for it, patches, kw in runs:
        fw = FirmwarePID(it, patches)
        py = PyPID(it, **kw)
        assert all(abs(a - b) < 1e-6 for a, b in zip(fw.gains(), (py.kp, py.ki, py.kd)))
        rnd = random.Random(it)
        cur, bad = 250, 0
        for k in range(n):
            cur += rnd.choice([-80, -20, -5, -1, 0, 1, 3, 5, 20, 60])
            cur = max(0, min(4800, cur))
            if k % 500 == 0:
                cur = rnd.randint(0, 4500)
            bad += fw.step(cur, 3000) != py.step(cur, 3000)
        label = TIP_NAMES[it] + (' +patch' if patches else '')
        print(f'{label:12} gains {fw.gains()}  mismatches {bad}/{n}')
        ok &= bad == 0
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--tips', action='store_true', help='sweep all tip types')
    ap.add_argument('--check', action='store_true', help='compare with the firmware')
    a = ap.parse_args()
    if a.check:
        sys.exit(0 if check() else 1)
    if a.tips:
        report_tips()
    else:
        report_c245()


if __name__ == '__main__':
    main()
