#!/usr/bin/env python3
"""
TS1M firmware emulator harness (Unicorn / ARM Cortex-M3).

Boots TS1M_Master_APP_V202_EN.hex far enough to reach the main loop,
captures the UART debug output, and lets you inject ADC values per
measurement point.

Requires: pip install unicorn capstone

Usage:
    from ts1m_emu import Emu, MUX_POINTS

    e = Emu().run()
    print(e.text())          # UART debug log

    # inject per-measurement-point ADC values
    e = Emu(adc_values={('ch10', 1, 0, 0): 3333}).run()

See EMULATION.md for what each workaround is for.
"""
import struct
import sys
import collections

from unicorn import *
from unicorn.arm_const import *

# ---------------------------------------------------------------- constants

FLASH_BASE = 0x08010000
APP_ENTRY = 0x08010119

# Functions of interest (see ANALYSIS.md)
ADC_READ_FN = 0x8011EC8       # ADC sample helper; r0 = channel, returns sample
TIP_DETECT_FN = 0x80199A8     # tip classification
RES_CHECK_FN = 0x8022490      # resistance sanity check
HEATER_FN = 0x80214A0         # heater / PID control, called every main loop
THERMOCOUPLE_FN = 0x80193B8   # thermocouple processing (no ADC reads inside)
HALT_SCREEN_FN = 0x8013A40    # "Demo Mode" / "Not e-Design Product!" + spin
PUTCHAR_STORE = 0x801E9AE     # USART1 putchar: r4 holds the byte

GPIO_RESET_FN = 0x80140B8     # BRR  (pin -> low)
GPIO_SET_FN = 0x80140BC       # BSRR (pin -> high)

MS_TICK = 0x200003BC          # millisecond counter polled by delay loops
IRONTYPE = 0x200003D3         # current tip type
SETTINGS_BASE = 0x200015A4    # settings struct (NOT a pointer)

# Status-register helpers the firmware spins on
STATUS_HELPERS = {0x8012058, 0x8012020, 0x801200C, 0x8017728, 0x8012B7C}

# Peripherals whose status registers we force to "ready"
PERI_STATUS = [
    0x40012400, 0x40012800, 0x40013C00,          # ADC1/2/3
    0x40013800, 0x40004400, 0x40004800,          # USART1/2/3
    0x40004C00, 0x40005000,                      # UART4/5
    0x40020000, 0x40020400,                      # DMA1/2
    0x40021000,                                  # RCC
    0x40011000, 0x40010800, 0x40010C00,          # GPIOC/A/B
    0x40003800, 0x40013000,
]
ADC_BASES = (0x40012400, 0x40012800, 0x40013C00)

GPIO_PORTS = {
    0x40010800: 'A', 0x40010C00: 'B', 0x40011000: 'C',
    0x40011400: 'D', 0x40011800: 'E',
}

# Analog-mux select lines that pick which signal reaches ADC channel 10.
MUX_PINS = ('PD2', 'PD3', 'PB4', 'PB3', 'PC12', 'PA6')

# Measurement points, as they appear in the firmware's own debug output.
#   key = (channel, PD2, PD3, PB4)
MUX_POINTS = {
    (10, 1, 0, 0): 'adc1',
    (10, 1, 1, 0): 'adc2',
    (10, 0, 0, 0): 'adc3',
    (10, 0, 0, 1): 'adc4',
    (13, 0, 0, 1): 'ch13',   # separate input, purpose unconfirmed
    (0,  0, 0, 1): 'ch0',    # separate input, purpose unconfirmed
}


def settings_offset(index):
    """Byte offset of settings entry `index` within the settings struct."""
    return 0x10 + 22 * index


SETTINGS_NAMES = [
    'DefaultTemp', 'WorkTemp1', 'WorkTemp2', 'WorkTemp3', 'BoostTemp',
    'colorEffect', 'colorMode', 'colorBright', 'colorR', 'colorG', 'colorB',
    'SleepTemp', 'SleepTime', 'IdelTime', 'StepVal', 'BackLight', 'TempType',
    'BeepVolume', 'Language', 'LowVolProtect', 'FlipOver', 'user_UI',
    'MaxPow_245', 'MaxPow_210', 'MaxPow_115', 'MaxPow_100', 'MaxPow_80P',
    'CalibraVal_245', 'CalibraVal_210', 'CalibraVal_115', 'CalibraVal_100',
    'CalibraVal_80P', 'TS1M_APP',
]


# ---------------------------------------------------------------- the harness

class Emu:
    """
    Boots the firmware image and runs the main loop.

    adc_values   dict mapping a MUX_POINTS key -> sample value (default 2000)
    adc_hook     optional callable(emu, key) -> value, overrides adc_values
    force_tip    if set, the tip classifier is stubbed to return this type
    skip_res     bypass the resistance sanity check (keeps a forced tip alive)
    """

    def __init__(self, image_path='TS1M_Master_APP_V202_EN.bin',
                 adc_values=None, adc_hook=None,
                 force_tip=None, skip_res=False,
                 echo=False, progress=False):
        self.image = open(image_path, 'rb').read()
        self.adc_values = adc_values or {}
        self.adc_hook = adc_hook
        self.force_tip = force_tip
        self.skip_res = skip_res
        self.echo = echo            # print UART bytes as they arrive
        self.progress = progress    # print an instruction counter while running

        self.n = 0                       # instructions executed
        self.out = bytearray()           # captured UART bytes
        self.halted = False              # hit the Demo Mode / auth halt screen
        self.err = None
        self.mux = {p: 0 for p in MUX_PINS}
        self.adc_log = collections.Counter()

        mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        for base, size in ((0x08000000, 0x80000), (0x20000000, 0x20000),
                           (0x40000000, 0x100000), (0xE0000000, 0x100000),
                           (0x1FFF0000, 0x10000)):
            mu.mem_map(base, size)
        mu.mem_write(FLASH_BASE, self.image)
        self.mu = mu

        self._install_uid()
        self._seed_peripherals()

        mu.hook_add(UC_HOOK_CODE, self._on_code)
        # Registering an unmapped-access hook keeps Unicorn from aborting the
        # run on stray accesses; returning False leaves them unhandled.
        mu.hook_add(UC_HOOK_MEM_UNMAPPED, lambda *a: False)

    # -- setup ------------------------------------------------------------

    def _install_uid(self):
        """
        The app compares the chip UID against a key stored in the bootloader
        at 0x0800FFF0 (each 32-bit word XORed with 0x0800FFF0). The bootloader
        is not part of the distributed HEX, so we synthesise a matching key -
        otherwise boot ends in the "Demo Mode" halt screen.
        """
        uid = bytes(range(0x11, 0x11 + 12))
        self.mu.mem_write(0x1FFFF7E8, uid)
        key = b''.join(
            struct.pack('<I', struct.unpack_from('<I', uid, i * 4)[0] ^ 0x0800FFF0)
            for i in range(3))
        self.mu.mem_write(0x0800FFF0, key)

    def _seed_peripherals(self):
        """
        Write 'ready' values straight into peripheral memory.

        NOTE: a UC_HOOK_MEM_READ callback that calls mem_write does NOT change
        the value the faulting instruction observes, so read hooks cannot be
        used to model registers here. Seeding real memory does work.
        """
        mu = self.mu
        for addr in PERI_STATUS:
            mu.mem_write(addr, struct.pack('<I', 0xFFFFFFFF))
        for base in ADC_BASES:
            mu.mem_write(base + 0x08, struct.pack('<I', 0x90000001))  # CR2
            mu.mem_write(base + 0x4C, struct.pack('<I', 2000))        # DR

    # -- per-instruction hook ---------------------------------------------

    def _on_code(self, uc, address, size, user):
        self.n += 1

        # Advance the millisecond counter the delay loops poll.
        if self.n % 50 == 0:
            t = struct.unpack('<I', uc.mem_read(MS_TICK, 4))[0]
            uc.mem_write(MS_TICK, struct.pack('<I', (t + 1) & 0xFFFFFFFF))

        # Refresh status registers right before any spin-wait helper runs.
        if address in STATUS_HELPERS:
            self._seed_peripherals()

        # Track analog-mux select lines.
        if address in (GPIO_RESET_FN, GPIO_SET_FN):
            port = uc.reg_read(UC_ARM_REG_R0)
            mask = uc.reg_read(UC_ARM_REG_R1)
            high = (address == GPIO_SET_FN)
            name = GPIO_PORTS.get(port)
            if name:
                for bit in range(16):
                    if mask >> bit & 1:
                        pin = f'P{name}{bit}'
                        if pin in self.mux:
                            self.mux[pin] = 1 if high else 0

        # Serve ADC samples per measurement point.
        if address == ADC_READ_FN:
            channel = uc.reg_read(UC_ARM_REG_R0)
            key = (channel, self.mux['PD2'], self.mux['PD3'], self.mux['PB4'])
            self.adc_log.update([key])
            if self.adc_hook:
                value = self.adc_hook(self, key)
            else:
                value = self.adc_values.get(key, 2000)
            uc.reg_write(UC_ARM_REG_R0, value)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return

        # Optionally force a tip type instead of driving the classifier.
        if self.force_tip is not None and address == TIP_DETECT_FN:
            uc.mem_write(IRONTYPE, bytes([self.force_tip]))
            uc.reg_write(UC_ARM_REG_R0, self.force_tip)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return

        if self.skip_res and address == RES_CHECK_FN:
            uc.reg_write(UC_ARM_REG_R0, 0)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return

        # Capture UART debug output.
        if address == PUTCHAR_STORE:
            byte = uc.reg_read(UC_ARM_REG_R4) & 0xFF
            self.out.append(byte)
            if self.echo:
                sys.stdout.write(chr(byte) if 9 <= byte < 127 else '.')
                sys.stdout.flush()

        if self.progress and self.n % 10_000_000 == 0:
            sys.stderr.write(f'  ... {self.n // 1_000_000}M instructions, '
                             f'{len(self.out)} UART bytes\n')
            sys.stderr.flush()

        if address == HALT_SCREEN_FN:
            self.halted = True
            uc.emu_stop()

    # -- driving ----------------------------------------------------------

    def run(self, count=120_000_000):
        try:
            self.mu.emu_start(APP_ENTRY | 1, 0, count=count)
        except UcError as exc:
            self.err = exc
        return self

    def ram(self, size=0x10000):
        return bytes(self.mu.mem_read(0x20000000, size))

    def text(self):
        return bytes(self.out).decode('utf-8', 'replace')

    def setting(self, index):
        addr = SETTINGS_BASE + settings_offset(index)
        return struct.unpack('<h', self.mu.mem_read(addr, 2))[0]

    def set_setting(self, index, value):
        addr = SETTINGS_BASE + settings_offset(index)
        self.mu.mem_write(addr, struct.pack('<h', value))


# ---------------------------------------------------------------- demo

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='Boot the TS1M firmware under Unicorn.')
    ap.add_argument('image', nargs='?', default='TS1M_Master_APP_V202_EN.bin')
    ap.add_argument('-n', '--count', type=int, default=20_000_000,
                    help='instruction budget (default 20M, which is enough to reach '
                         'the main loop; the first UART line appears after ~40k)')
    ap.add_argument('-q', '--quiet', action='store_true',
                    help='do not stream UART output while running')
    args = ap.parse_args()

    print(f'running {args.image} for up to {args.count // 1_000_000}M instructions...',
          file=sys.stderr)
    emu = Emu(args.image, force_tip=1, skip_res=True,
              echo=not args.quiet, progress=True).run(count=args.count)
    print()
    print(f'instructions: {emu.n}  halted: {emu.halted}')
    print('measurement points used:')
    for key, hits in emu.adc_log.most_common():
        print(f'  {MUX_POINTS.get(key, key)}: {hits}')
    if args.quiet:
        print('--- UART ---')
        print(emu.text()[:1200])
