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
CAL_FN = 0x80193B8            # auto-calibration (CAL) against the external thermocouple on ch0
HALT_SCREEN_FN = 0x8013A40    # auth/status helper, r0 = mode:
                              #   0 "Demo Mode" + spin, 1 record timestamp (normal),
                              #   2 "Demo Mode" 3s -> 3, 3 "Not e-Design Product!" + spin
PUTCHAR_STORE = 0x801E9AE     # USART1 putchar: r4 holds the byte

GPIO_RESET_FN = 0x80140B8     # BRR  (pin -> low)
GPIO_SET_FN = 0x80140BC       # BSRR (pin -> high)

# Timer / heater helpers (StdPeriph-style, see ANALYSIS.md ch.15)
TIM_CMD_FN = 0x8016A9E        # TIM_Cmd(base, en): CR1 bit0
TIM_CCXCMD_FN = 0x8016C24     # TIM_CCxCmd(base, ccmask, en): CCER
TIM_SETCCR_FN = {0x8016DF8: 1, 0x8016DFC: 3, 0x8016E00: 4}  # SetCompareN(base, val)
TIM_BASES = {
    0x40000000: 'TIM2', 0x40000400: 'TIM3', 0x40000800: 'TIM4',
    0x40000C00: 'TIM5', 0x40012C00: 'TIM1',
}
HEATER_TIM = 0x40000C00       # TIM5; CH2 output = PA1 = heater element gate
BUZZER_TIM = 0x40000800       # TIM4; CH3 = PB8
BACKLIGHT_TIM = 0x40012C00    # TIM1; CH1 = PA8

# Measurement/heat subsystem method table (RAM 0x20000368):
#   +0x08 measure-end  0x08013A20  enables TIM5/CH2  -> heater ON  (between windows)
#   +0x0C measure-start 0x08012F3C  disables TIM5/CH2 -> heater OFF (during sensing)
MEAS_END_FN = 0x8013A20
MEAS_START_FN = 0x8012F3C

# USART data registers (DR at +0x04); TX bytes the firmware sends
USART_TX = {0x40004400: 2, 0x40004C00: 4}   # USART2 = tip link/jig, UART4 = 2nd serial

MS_TICK = 0x200003BC          # millisecond counter polled by delay loops
IRONTYPE = 0x200003D3         # current tip type
SETTINGS_BASE = 0x200015A4    # settings struct (NOT a pointer)
SETTINGS_DEFAULTS = 0x0802A4E6  # flash table with the same 22-byte layout

# Temperature control (see ANALYSIS.md, "温度制御")
MODE = 0x2000023C             # u8  1 work, 2 sleep, 0/3 heater off
CUR_TEMP = 0x2000023E         # s16 current tip temperature, 0.1 deg
TARGET_TEMP = 0x200001F4      # s16 PID target, 0.1 deg (rewritten every heater pass)
HEATER_STATE = 0x200001D2     # u8  0 measure+PID, 1 wait for ADC IRQ, 2 heater off
PID_FN = 0x8015494            # PID(r0 = current temp), reads TARGET_TEMP
INPUT_FN = 0x801EB58          # stand / sleep state machine, drives MODE
FACTORY_CAL = 0x08077D00      # factory record, u16 x10 + sum (DataCheckArr)
TEMP_CAL = 0x08077C00         # per-tip temperature calibration, u16 x5 (x/1000) + sum
SPI_SR_FN = 0x8015ADC         # SPI status poll used by the display driver
LCD_WINDOW_FN = 0x8014DA4     # set_window(x0, y0, x1, y1) + RAMWR
LCD_DATA16_FN = 0x8015040     # write one 16-bit word to the panel
LCD_DMA_FN = 0x8016FA4        # dma_send(channel, count), source = channel CMAR
LCD_W, LCD_H = 320, 172
FLIPOVER = 20                 # settings index; != 0 reads the accelerometer via the bootloader

# Status-register helpers the firmware spins on
STATUS_HELPERS = {0x8012058, 0x8012020, 0x801200C, 0x8017728, 0x8012B7C}

# Peripherals whose status registers we force to "ready"
PERI_STATUS = [
    0x40012400, 0x40012800, 0x40013C00,          # ADC1/2/3
    0x40013800, 0x40004400, 0x40004800,          # USART1/2/3
    0x40004C00, 0x40005000,                      # UART4/5
    0x40020000, 0x40020400,                      # DMA1/2
    0x40021000,                                  # RCC
    0x40003800, 0x40013000,
    # NOTE: GPIO bases are deliberately NOT seeded. Their offset 0 is CRL (pin
    # config), not a status register, so writing 0xFFFFFFFF there corrupted the
    # config that pin_mode() reads back. No GPIO status wait depends on it.
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


# ---------------------------------------------------------------- SPI flash

SPI2_BASE = 0x40003800
SPI_SEND_FN = 0x8015AEE       # SPI_I2S_SendData(SPIx, data)
SPI_RECV_FN = 0x8015AEA       # SPI_I2S_ReceiveData(SPIx)
FLASH_CS = ('B', 12)          # PB12, active low


class W25Q64:
    """
    Minimal Winbond W25Q64 (8 MB SPI NOR) model: the FatFs volume holding
    TS1M.TXT lives here. The firmware's disk_initialize checks the 0x90
    manufacturer/device ID for 0xEF16.
    """
    SIZE = 8 * 1024 * 1024

    def __init__(self, image=None):
        self.mem = bytearray(image) if image else bytearray(b'\xff') * self.SIZE
        assert len(self.mem) == self.SIZE
        self.wel = False
        self.selected = False
        self.cmd = None
        self.buf = []
        self.addr = 0
        self.log = collections.Counter()

    def select(self, on):
        if on and not self.selected:
            self.cmd, self.buf = None, []
        if not on and self.selected and self.cmd is not None:
            self._finish()
        self.selected = on

    def _finish(self):
        erase = {0x20: 0x1000, 0x52: 0x8000, 0xD8: 0x10000}
        if self.cmd in erase and len(self.buf) >= 3 and self.wel:
            size = erase[self.cmd]
            a = self._addr() & ~(size - 1)
            self.mem[a:a + size] = b'\xff' * size
            self.wel = False
        elif self.cmd in (0xC7, 0x60) and self.wel:
            self.mem[:] = b'\xff' * self.SIZE
            self.wel = False
        elif self.cmd == 0x02:
            self.wel = False

    def _addr(self):
        return (self.buf[0] << 16 | self.buf[1] << 8 | self.buf[2]) % self.SIZE

    def xfer(self, byte):
        if not self.selected:
            return 0xFF
        if self.cmd is None:
            self.cmd = byte
            self.log[hex(byte)] += 1
            if byte == 0x06:
                self.wel = True
            elif byte == 0x04:
                self.wel = False
            return 0xFF
        c, b = self.cmd, self.buf
        b.append(byte)
        n = len(b)
        if c == 0x05:                               # status 1: never busy
            return 0x02 if self.wel else 0x00
        if c in (0x35, 0x15):
            return 0x00
        if c == 0x9F:                               # JEDEC ID
            return (0xEF, 0x40, 0x17)[(n - 1) % 3]
        if c == 0x90:                               # manufacturer / device ID
            return 0xFF if n <= 3 else (0xEF, 0x16)[(n - 4) % 2]
        if c == 0xAB:
            return 0xFF if n <= 3 else 0x16
        if c == 0x4B:                               # unique ID
            return 0xFF if n <= 4 else 0x10 + n
        if c in (0x03, 0x0B):                       # read / fast read
            skip = 3 if c == 0x03 else 4
            if n <= skip:
                return 0xFF
            v = self.mem[(self._addr() + n - skip - 1) % self.SIZE]
            return v
        if c == 0x02:                               # page program (wraps in page)
            if n > 3 and self.wel:
                a = self._addr()
                page = a & ~0xFF
                i = page | ((a + n - 4) & 0xFF)
                self.mem[i] &= byte
            return 0xFF
        return 0xFF


def edit_ts1m_txt(image, **values):
    """
    Return a copy of a W25Q64 image with values in TS1M.TXT replaced in place,
    e.g. edit_ts1m_txt(img, FlipOver=0, TempType=1). The new value must have
    the same number of characters as the old one, because the FAT directory
    entry (file size) is not rewritten.
    """
    import re
    mem = bytearray(image)
    for key, value in values.items():
        m = re.search(rb'(\n' + key.encode() + rb' *= *)(-?\d+)', mem)
        if not m:
            raise KeyError(key)
        new = str(value).encode()
        if len(new) != len(m.group(2)):
            raise ValueError(f'{key}: {m.group(2)!r} -> {new!r} changes the length')
        mem[m.start(2):m.end(2)] = new
    return bytes(mem)


def make_flash_image(image_path='TS1M_Master_APP_V202_EN.bin', count=15_000_000):
    """
    Boot once with a blank W25Q64 so the firmware formats it and writes the
    default TS1M.TXT, then set FlipOver=0 (FlipOver=1 calls the accelerometer
    through the missing bootloader). Returns the 8 MB image.
    """
    e = Emu(image_path, force_tip=1, skip_res=True, spi_flash=True).run(count=count)
    if b'DefaultTemp' not in e.flash.mem:
        raise RuntimeError('TS1M.TXT was not written')
    return edit_ts1m_txt(e.flash.mem, FlipOver=0)


# ---------------------------------------------------------------- the harness

class Emu:
    """
    Boots the firmware image and runs the main loop.

    adc_values   dict mapping a MUX_POINTS key -> sample value (default 2000)
    adc_hook     optional callable(emu, key) -> value, overrides adc_values
    force_tip    if set, the tip classifier is stubbed to return this type
    skip_res     bypass the resistance sanity check (keeps a forced tip alive)
    defaults     load the default settings table (settings are otherwise all 0
                 because TS1M.TXT does not exist here), a neutral temperature
                 calibration and a supply-voltage calibration, so CUR_TEMP /
                 TARGET_TEMP and the supply voltage get real values.
                 FlipOver is forced to 0 (it calls into the missing bootloader).
    spi_flash    model the W25Q64 SPI flash (FatFs volume with TS1M.TXT):
                 True for a blank (erased) chip, or bytes / a file path for an
                 existing 8 MB image. See self.flash.mem after the run.
    lcd          keep a 320x172 RGB565 frame buffer of what is sent to the
                 panel (window + per-pixel writes + DMA); see save_png()
    work_mode    keep the heater in "work" (MODE=1): stubs the stand / sleep
                 state machine and stands in for the ADC interrupt that normally
                 releases HEATER_STATE 1 -> 0, so every heater pass measures and
                 runs the PID. Needs defaults=True to be meaningful.
    sim_measure  drive the measure/heat cycle the ADC-injected interrupt normally
                 runs (it is not modelled by Unicorn): between run() slices, call
                 the measure-start (heater off) and measure-end (heater on)
                 methods so TIM5/CH2 (PA1, the heater gate) actually toggles and
                 the cycle can be observed. See heater_on / heater_log.

    After a run, inspect the modelled hardware state:
        e.pin('PA1')        driven level (0/1) of a GPIO from the ODR shadow
        e.pins()            all pins currently driven high, e.g. {'PB3', ...}
        e.pin_mode('PC4')   configured mode from CRL/CRH ('AIN', 'OUT_PP', ...)
        e.heater_on         True while TIM5/CH2 (PA1) drives the element
        e.buzzer_duty       TIM4_CH3 (PB8) compare value; backlight_duty likewise
        e.uart_tx[2]        bytes the firmware sent on USART2 (tip link); [4]=UART4
    """

    def __init__(self, image_path='TS1M_Master_APP_V202_EN.bin',
                 adc_values=None, adc_hook=None,
                 force_tip=None, skip_res=False,
                 defaults=False, work_mode=False, lcd=False, spi_flash=None,
                 sim_measure=False, echo=False, progress=False):
        self.image = open(image_path, 'rb').read()
        self.adc_values = adc_values or {}
        self.adc_hook = adc_hook
        self.force_tip = force_tip
        self.skip_res = skip_res
        self.defaults = defaults
        self.work_mode = work_mode
        self.sim_measure = sim_measure
        self.heater_calls = 0
        self.tick_every = 50        # instructions per emulated millisecond

        # Modelled hardware state (populated by the write / helper hooks).
        self.gpio_odr = {n: 0 for n in GPIO_PORTS.values()}      # driven levels
        self.timers = {}                                        # base -> state
        self.uart_tx = {2: bytearray(), 4: bytearray()}         # captured TX
        self.heater_log = []                                    # (n, on) toggles
        self.lcd = lcd
        self.flash = None
        if spi_flash is not None and spi_flash is not False:
            image = None
            if isinstance(spi_flash, (bytes, bytearray)):
                image = spi_flash
            elif isinstance(spi_flash, str):
                image = open(spi_flash, 'rb').read()
            self.flash = W25Q64(image)
        self._spi2_rx = 0xFF
        self.fb = [0] * (LCD_W * LCD_H) if lcd else None
        self._win = (0, 0, 0, 0)
        self._cur = 0
        self.echo = echo            # print UART bytes as they arrive
        self.progress = progress    # print an instruction counter while running

        self.n = 0                       # instructions executed
        self.out = bytearray()           # captured UART bytes
        self.halted = False              # hit the Demo Mode / auth halt screen
        self.err = None
        self.mux = {p: 0 for p in MUX_PINS}
        self.adc_log = collections.Counter()

        mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        # 0x00000000 is the boot alias (bootloader flash, not distributed: zeros)
        for base, size in ((0x00000000, 0x10000),
                           (0x08000000, 0x80000), (0x20000000, 0x20000),
                           (0x40000000, 0x100000), (0xE0000000, 0x100000),
                           (0x1FFF0000, 0x10000)):
            mu.mem_map(base, size)
        mu.mem_write(FLASH_BASE, self.image)
        # The settings loader passes the f_read byte count where the parser
        # expects a pointer to it, so the parser reads its search length from
        # the boot alias at address ~0x600. That area is the (missing)
        # bootloader; 0x0606 (>= the 1536-byte read buffer, at any alignment)
        # stands in for it.
        mu.mem_write(0x00000000, b'\x06' * 0x10000)
        self.mu = mu
        # Calibration records live in internal flash outside the HEX: seed
        # neutral ones whenever settings are meant to be realistic.
        if defaults or spi_flash:
            mu.mem_write(TEMP_CAL, struct.pack('<6H', 1000, 1000, 1000, 1000, 1000, 5000))
            # Factory record (DataCheckArr, u16 x10 + sum). [9] scales the supply
            # voltage (ch13 * 7.77 * [9] / 1000 mV); 0 means "LowVol" on the heat screen.
            fac = [0] * 10
            fac[9] = 1000
            mu.mem_write(FACTORY_CAL, struct.pack('<11H', *fac, sum(fac)))

        self._install_uid()
        self._seed_peripherals()

        mu.hook_add(UC_HOOK_CODE, self._on_code)
        # Registering an unmapped-access hook keeps Unicorn from aborting the
        # run on stray accesses; returning False leaves them unhandled.
        mu.hook_add(UC_HOOK_MEM_UNMAPPED, lambda *a: False)
        # Shadow GPIO output/config and capture serial TX. These hooks are
        # scoped to the peripheral windows (a global write hook makes Unicorn
        # abort on the first stray write); keep them narrow.
        mu.hook_add(UC_HOOK_MEM_WRITE, self._gpio_write,
                    begin=0x40010800, end=0x40011BFF)
        mu.hook_add(UC_HOOK_MEM_WRITE, self._uart_write,
                    begin=0x40004400, end=0x40004FFF)

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

    # -- modelled peripherals ---------------------------------------------

    def _gpio_write(self, uc, typ, addr, size, val, user):
        """Shadow a GPIO port's output (ODR/BSRR/BRR) and config (CRL/CRH)."""
        name = GPIO_PORTS.get(addr & ~0x3FF)
        if not name:
            return
        off = addr & 0x3FF
        if off == 0x0C:                                  # ODR
            self.gpio_odr[name] = val & 0xFFFF
        elif off == 0x10:                                # BSRR: set low, reset high
            self.gpio_odr[name] = (self.gpio_odr[name] | (val & 0xFFFF)) \
                & ~((val >> 16) & 0xFFFF) & 0xFFFF
        elif off == 0x14:                                # BRR: reset
            self.gpio_odr[name] &= ~(val & 0xFFFF) & 0xFFFF

    def _uart_write(self, uc, typ, addr, size, val, user):
        """Capture bytes the firmware sends on USART2 / UART4 (DR at +0x04)."""
        n = USART_TX.get(addr & ~0x3FF)
        if n and (addr & 0x3FF) == 0x04:
            self.uart_tx[n].append(val & 0xFF)

    def _timer(self, base):
        return self.timers.setdefault(base, {'en': 0, 'ccer': 0, 'ccr': {}})

    def _note_heater(self):
        on = self.heater_on
        if not self.heater_log or self.heater_log[-1][1] != on:
            self.heater_log.append((self.n, on))

    # -- per-instruction hook ---------------------------------------------

    def _on_code(self, uc, address, size, user):
        self.n += 1

        # Advance the millisecond counter the delay loops poll.
        if self.n % self.tick_every == 0:
            t = struct.unpack('<I', uc.mem_read(MS_TICK, 4))[0]
            uc.mem_write(MS_TICK, struct.pack('<I', (t + 1) & 0xFFFFFFFF))

        # Refresh status registers right before any spin-wait helper runs.
        if address in STATUS_HELPERS:
            self._seed_peripherals()

        # SPI SR (+8): TXE|RXNE, otherwise every display transfer waits out
        # a 6000-iteration timeout.
        if address == SPI_SR_FN:
            base = uc.reg_read(UC_ARM_REG_R0)
            if base in (0x40013000, 0x40003800, 0x40003C00):
                uc.mem_write(base + 8, b'\x03\x00')

        if self.lcd:
            self._lcd(uc, address)

        if address == HEATER_FN:
            self.heater_calls += 1
            if self.defaults and self.heater_calls == 1:
                o = SETTINGS_DEFAULTS - FLASH_BASE
                uc.mem_write(SETTINGS_BASE, self.image[o:o + 33 * 22])
                self.set_setting(FLIPOVER, 0)
            if self.work_mode:
                uc.mem_write(MODE, b'\x01')
                if uc.mem_read(HEATER_STATE, 1)[0] == 1:
                    uc.mem_write(HEATER_STATE, b'\x00')

        if self.work_mode and address == INPUT_FN and self.heater_calls:
            uc.reg_write(UC_ARM_REG_R0, 0)
            uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
            return

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
                        if self.flash and (name, bit) == FLASH_CS:
                            self.flash.select(not high)

        # Track timer enable / channel-output / compare so heater_on and the
        # buzzer / backlight duties can be read back (ANALYSIS.md ch.15).
        if address == TIM_CMD_FN:
            base = uc.reg_read(UC_ARM_REG_R0)
            if base in TIM_BASES:
                self._timer(base)['en'] = 1 if uc.reg_read(UC_ARM_REG_R1) else 0
                if base == HEATER_TIM:
                    self._note_heater()
        elif address == TIM_CCXCMD_FN:
            base = uc.reg_read(UC_ARM_REG_R0)
            if base in TIM_BASES:
                mask = uc.reg_read(UC_ARM_REG_R1)
                t = self._timer(base)
                if uc.reg_read(UC_ARM_REG_R2):
                    t['ccer'] |= mask
                else:
                    t['ccer'] &= ~mask
                if base == HEATER_TIM:
                    self._note_heater()
        elif address in TIM_SETCCR_FN:
            base = uc.reg_read(UC_ARM_REG_R0)
            if base in TIM_BASES:
                self._timer(base)['ccr'][TIM_SETCCR_FN[address]] = \
                    uc.reg_read(UC_ARM_REG_R1) & 0xFFFF

        # SPI2 = W25Q64: answer each byte as it is sent.
        if self.flash and address in (SPI_SEND_FN, SPI_RECV_FN) \
                and uc.reg_read(UC_ARM_REG_R0) == SPI2_BASE:
            if address == SPI_SEND_FN:
                self._spi2_rx = self.flash.xfer(uc.reg_read(UC_ARM_REG_R1) & 0xFF)
            else:
                uc.reg_write(UC_ARM_REG_R0, self._spi2_rx)
                uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))
                return

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

        # Only modes 0/2/3 are the halt screens; mode 1 is a routine call.
        if address == HALT_SCREEN_FN and uc.reg_read(UC_ARM_REG_R0) != 1:
            self.halted = True
            uc.emu_stop()

    # -- driving ----------------------------------------------------------

    def run(self, count=120_000_000):
        if not self.sim_measure:
            try:
                self.mu.emu_start(APP_ENTRY | 1, 0, count=count)
            except UcError as exc:
                self.err = exc
            return self
        # Drive the measure/heat cycle the ADC-injected interrupt normally runs
        # (Unicorn does not model interrupts). Run the main loop in slices and,
        # between slices, call the measure-start (heater off) and measure-end
        # (heater on) methods so TIM5/CH2 (PA1) toggles like on hardware.
        pc = APP_ENTRY | 1
        slice_n = 1_000_000
        try:
            while count > 0:
                step = min(slice_n, count)
                start = self.n
                self.mu.emu_start(pc, 0, count=step)
                count -= self.n - start
                if self.halted:
                    break
                pc = self.mu.reg_read(UC_ARM_REG_PC) | 1
                if self.heater_calls:                 # main loop reached
                    self._call_guest(MEAS_START_FN)   # heater off (sensing)
                    self._call_guest(MEAS_END_FN)     # heater on (heating)
        except UcError as exc:
            self.err = exc
        return self

    _CORE_REGS = (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                  UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7,
                  UC_ARM_REG_R8, UC_ARM_REG_R9, UC_ARM_REG_R10, UC_ARM_REG_R11,
                  UC_ARM_REG_R12, UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC,
                  UC_ARM_REG_CPSR)
    _RET_TRAP = 0x1FFF0000        # mapped, even; where a called function returns to

    def _call_guest(self, fn):
        """Invoke a small self-contained guest function between run() slices,
        preserving the suspended context. Used by sim_measure."""
        saved = [self.mu.reg_read(r) for r in self._CORE_REGS]
        self.mu.reg_write(UC_ARM_REG_SP, 0x2000F000)   # scratch stack
        self.mu.reg_write(UC_ARM_REG_LR, self._RET_TRAP | 1)
        try:
            self.mu.emu_start(fn | 1, self._RET_TRAP, count=200_000)
        except UcError:
            pass
        for r, v in zip(self._CORE_REGS, saved):
            self.mu.reg_write(r, v)

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

    # -- virtual panel ----------------------------------------------------

    def _lcd_put(self, value):
        x0, y0, x1, y1 = self._win
        w = x1 - x0 + 1
        n = w * (y1 - y0 + 1)
        if w <= 0 or n <= 0:
            return
        i = self._cur % n
        x, y = x0 + i % w, y0 + i // w
        if 0 <= x < LCD_W and 0 <= y < LCD_H:
            self.fb[y * LCD_W + x] = value
        self._cur += 1

    def _lcd(self, uc, address):
        if address == LCD_WINDOW_FN:
            r = [uc.reg_read(x) for x in (UC_ARM_REG_R0, UC_ARM_REG_R1,
                                          UC_ARM_REG_R2, UC_ARM_REG_R3)]
            self._win = tuple(v & 0xFFFF for v in r)
            self._cur = 0
        elif address == LCD_DATA16_FN:
            lr = uc.reg_read(UC_ARM_REG_LR) & ~1
            if not LCD_WINDOW_FN <= lr < LCD_WINDOW_FN + 0x74:   # not a CASET/RASET argument
                self._lcd_put(uc.reg_read(UC_ARM_REG_R0) & 0xFFFF)
        elif address == LCD_DMA_FN:
            ch = uc.reg_read(UC_ARM_REG_R0)
            count = uc.reg_read(UC_ARM_REG_R1) & 0xFFFF
            src = struct.unpack('<I', uc.mem_read(ch + 0x0C, 4))[0]
            try:
                data = uc.mem_read(src, count * 2)
            except UcError:
                return
            for (v,) in struct.iter_unpack('<H', bytes(data)):
                self._lcd_put(v)

    def save_png(self, path, scale=2):
        """Write the virtual panel as a PNG (no PIL needed)."""
        import zlib
        rows = []
        for y in range(LCD_H):
            row = bytearray(b'\x00')
            for x in range(LCD_W):
                v = self.fb[y * LCD_W + x]
                px = bytes(((v >> 11) * 255 // 31, (v >> 5 & 63) * 255 // 63, (v & 31) * 255 // 31))
                row += px * scale
            rows.extend([bytes(row)] * scale)

        def chunk(tag, data):
            return (struct.pack('>I', len(data)) + tag + data
                    + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF))
        ihdr = struct.pack('>IIBBBBB', LCD_W * scale, LCD_H * scale, 8, 2, 0, 0, 0)
        with open(path, 'wb') as fh:
            fh.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
                     + chunk(b'IDAT', zlib.compress(b''.join(rows), 6)) + chunk(b'IEND', b''))

    def s16(self, addr):
        return struct.unpack('<h', self.mu.mem_read(addr, 2))[0]

    # -- modelled hardware read-back --------------------------------------

    @staticmethod
    def _split_pin(name):
        return name[1].upper(), int(name[2:])

    def pin(self, name):
        """Driven level (0/1) of a GPIO pin, e.g. pin('PA1'), from the ODR shadow."""
        port, bit = self._split_pin(name)
        return (self.gpio_odr.get(port, 0) >> bit) & 1

    def pins(self):
        """Set of pins currently driven high (output ports only)."""
        out = set()
        for port, odr in self.gpio_odr.items():
            for bit in range(16):
                if odr >> bit & 1:
                    out.add(f'P{port}{bit}')
        return out

    # STM32F1/CH32 CRL/CRH nibble -> mode string (CNF<<2 | MODE)
    _CR_MODE = {
        0x0: 'AIN', 0x4: 'IN_FLOAT', 0x8: 'IN_PUPD',
        0x1: 'OUT_PP_10', 0x2: 'OUT_PP_2', 0x3: 'OUT_PP_50',
        0x5: 'OUT_OD_10', 0x6: 'OUT_OD_2', 0x7: 'OUT_OD_50',
        0x9: 'AF_PP_10', 0xA: 'AF_PP_2', 0xB: 'AF_PP_50',
        0xC: 'AF_OD', 0xD: 'AF_OD_10', 0xE: 'AF_OD_2', 0xF: 'AF_OD_50',
    }

    def pin_mode(self, name):
        """Configured mode of a GPIO pin ('AIN', 'AF_PP_50', ...), read from the
        live CRL/CRH in Unicorn memory (authoritative; the firmware writes these
        directly, unlike BSRR/BRR whose ODR side effect must be shadowed)."""
        port, bit = self._split_pin(name)
        base = next(b for b, n in GPIO_PORTS.items() if n == port)
        off = 0x00 if bit < 8 else 0x04
        reg = struct.unpack('<I', self.mu.mem_read(base + off, 4))[0]
        return self._CR_MODE.get((reg >> (4 * (bit & 7))) & 0xF, '?')

    @property
    def heater_on(self):
        """True while TIM5 (CH2 = PA1) is enabled to drive the heater element."""
        t = self.timers.get(HEATER_TIM)
        return bool(t and t['en'])

    def _ccr(self, base, ch):
        t = self.timers.get(base)
        return t['ccr'].get(ch) if t else None

    @property
    def buzzer_duty(self):
        """TIM4_CH3 (PB8) compare value, or None if never set."""
        return self._ccr(BUZZER_TIM, 3)

    @property
    def backlight_duty(self):
        """TIM1_CH1 (PA8) compare value, or None if never set."""
        return self._ccr(BACKLIGHT_TIM, 1)


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
    ap.add_argument('--make-flash', metavar='OUT',
                    help='create a formatted W25Q64 image with the default TS1M.TXT '
                         '(FlipOver=0) and exit')
    ap.add_argument('--flash', metavar='IMG',
                    help='boot with this W25Q64 image (settings come from its TS1M.TXT)')
    ap.add_argument('--sim', action='store_true',
                    help='drive the measure/heat cycle (work mode) and report the '
                         'modelled pin / timer / heater state')
    args = ap.parse_args()

    if args.make_flash:
        img = make_flash_image(args.image)
        open(args.make_flash, 'wb').write(img)
        print(f'{args.make_flash}: {len(img)} bytes')
        sys.exit(0)

    print(f'running {args.image} for up to {args.count // 1_000_000}M instructions...',
          file=sys.stderr)
    emu = Emu(args.image, force_tip=1, skip_res=True, spi_flash=args.flash,
              defaults=args.sim, work_mode=args.sim, sim_measure=args.sim,
              echo=not args.quiet, progress=True).run(count=args.count)
    print()
    print(f'instructions: {emu.n}  halted: {emu.halted}')
    print('measurement points used:')
    for key, hits in emu.adc_log.most_common():
        print(f'  {MUX_POINTS.get(key, key)}: {hits}')

    if args.sim:
        print('\n--- modelled hardware state (ANALYSIS.md ch.15) ---')
        print(f'heater (PA1/TIM5_CH2) on: {emu.heater_on}   '
              f'toggles: {len(emu.heater_log)}   '
              f'backlight duty: {emu.backlight_duty}')
        labelled = {
            'PA1': 'heater', 'PA2': 'USART2_TX', 'PA5': 'SPI1_SCK',
            'PA8': 'backlight', 'PB0': 'LCD_DC', 'PB8': 'buzzer',
            'PB12': 'flash_CS', 'PB13': 'SPI2_SCK', 'PC0': 'tip_TC(IN10)',
            'PC3': 'Vsupply(IN13)', 'PC4': 'IN14', 'PC6': 'button', 'PC8': 'DFU_strap',
        }
        for pin, what in labelled.items():
            print(f'  {pin:4} {emu.pin_mode(pin):9} {what}')
        print(f'USART2 TX bytes: {len(emu.uart_tx[2])}   '
              f'UART4 TX bytes: {len(emu.uart_tx[4])}')

    if args.quiet:
        print('--- UART ---')
        print(emu.text()[:1200])
