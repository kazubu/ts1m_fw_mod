#!/usr/bin/env python3
"""
TS1M bootloader emulator harness (Unicorn / ARM Cortex-M3).

The bootloader (internal flash 0x08000000-0x0800FFFF, dumped separately as
bootloader.bin; see ANALYSIS.md ch.13-14) is not part of the distributed HEX.
This harness loads it, installs a matching chip UID key, and lets individual
functions be called with two bit-banged buses modelled:

  * an I2C accelerometer on SDA=PD5 / SCL=PD6 (7-bit address 0x19, a
    LIS3DH-style part configured via CTRL_REG1..5 at 0x20..0x24), and
  * the 1-Wire "IDChip" authentication device on PB11 (Read ROM 0x33 ->
    an 8-byte ROM id; a missing presence pulse gives "IDChip Err").

Because full boot pulls in USB / FAT, the useful entry point is call():

    from bl_emu import BLEmu
    e = BLEmu()
    for t in e.run_accel():   print(t)   # accelerometer config, over I2C
    print(e.run_idchip_read())           # 1-Wire ROM the bootloader reads

Requires: pip install unicorn capstone
"""
import struct
import collections

from unicorn import *
from unicorn.arm_const import *

BL_PATH = 'bootloader.bin'
APP_PATH = 'TS1M_Master_APP_V202_EN.bin'
BL_BASE = 0x08000000
APP_BASE = 0x08010000

# --- Accelerometer: bit-bang I2C on GPIOD (0x40011400): SDA=PD5 (0x20),
#     SCL=PD6 (0x40). Driven through these generic pin helpers:
GPIOD = 0x40011400
SDA_BIT, SCL_BIT = 0x20, 0x40
PIN_SET_FN = 0x08001FE8       # set(port, mask)   -> BSRR (pin high / released)
PIN_CLR_FN = 0x08001FDC       # clear(port, mask) -> BRR  (pin low)
PIN_RD_FN = 0x08001FCC        # read(port, mask)  -> IDR bit (0/1)
ACCEL_ADDR = 0x19             # write byte 0x32, read byte 0x33
ACCEL_INIT_FN = 0x08002624    # configure PD5/PD6 open-drain, both high
ACCEL_CMD_FN = 0x080025D0     # write CTRL_REG1..5 (0x20..0x24) = 57,00,00,08,00
ACCEL_READ_FN = 0x08003544    # read_regs(reg, buf, len)
ACCEL_WRITE_FN = 0x080035E8   # write_regs(reg, buf, len)

# --- IDChip: 1-Wire on PB11 (GPIOB 0x40010C00, bit11). Byte-level helpers:
OW_RESET_FN = 0x080042BE      # reset + presence; returns >0 if a device answers
OW_WRITE_FN = 0x0800637C      # write one byte (r0)
OW_READ_FN = 0x080041E0       # read one byte -> r0
IDCHIP_READ8_FN = 0x080021A0  # reset, Read ROM (0x33), read 8 bytes -> buf; else "IDChip Err"

AUTH_FN = 0x08003D64          # anti-clone auth; reads the 1-Wire ROM (mode in r0)

RET_TRAP = 0x1FFF0000         # even, mapped: where a called function returns


class OneWire:
    """
    Byte-level 1-Wire slave (the IDChip on PB11). The bootloader talks to it
    through reset/write-byte/read-byte helpers, so we model at that level: a
    reset returns presence, and after a Read ROM (0x33) command the read-byte
    helper returns the 8-byte ROM id. .cmds records the commands seen.
    """

    def __init__(self, rom=None, present=True):
        # default ROM: family 0x01 (DS2401-style), 6-byte serial, CRC placeholder
        self.rom = bytearray(rom) if rom else bytearray(b'\x01\x22\x33\x44\x55\x66\x77\x8f')
        self.present = present
        self.cmds = []
        self.ptr = 0
        self._src = None

    def reset(self):
        self.ptr = 0
        self._src = None
        return 0x40 if self.present else 0     # nonzero presence count

    def write(self, byte):
        self.cmds.append(byte)
        if byte == 0x33 or byte == 0x0F:      # Read ROM
            self._src, self.ptr = self.rom, 0

    def read(self):
        if self._src is not None and self.ptr < len(self._src):
            b = self._src[self.ptr]
            self.ptr += 1
            return b
        return 0xFF


class I2CDevice:
    """
    Bit-level I2C slave on the SDA/SCL lines the bootloader bit-bangs (models
    the accelerometer at 0x19). Decodes START / STOP / bytes / ACK and serves a
    256-byte register file so reads return programmable values. Records each
    transaction in .log.

    A transaction is a dict: {'addr', 'rw' ('W'/'R'), 'reg', 'data' [bytes]}.
    """

    def __init__(self, addr=0x19, regs=None):
        self.addr = addr
        self.regs = bytearray(regs) if regs else bytearray(256)
        self.log = []
        self.msda = 1            # master-driven SDA level (open-drain: 1 = released)
        self.ssda = 1            # slave-driven SDA level
        self.scl = 1
        self._reset_txn(idle=True)

    # -- bus value the master reads (wired-AND of both drivers) --------------
    def sda(self):
        return self.msda & self.ssda

    def _reset_txn(self, idle=False):
        self.phase = 'idle' if idle else 'addr'
        self.k = 0               # clock within the current byte (0..8; 8 = ACK)
        self.shift = 0           # byte being assembled (master->slave)
        self.out = 0             # byte being shifted out (slave->master)
        self.matched = False
        self.ack = True
        self._drive_ack = False  # slave should pull SDA low on the coming ACK clock
        self._nack = 0
        self._read_byte_done = False
        self.reg = 0
        self.cur = []            # data bytes of the current transaction

    # -- line drivers, called from the Unicorn hooks ------------------------
    def set_scl(self, level):
        if level == self.scl:
            return
        self.scl = level
        if level:
            self._scl_rise()
        else:
            self._scl_fall()

    def set_sda(self, level):
        # START / STOP are SDA transitions while SCL is high.
        if level == self.msda:
            return
        if self.scl:
            if level == 0:                    # SDA falling, SCL high -> START
                self._start()
            else:                             # SDA rising, SCL high -> STOP
                self._stop()
        self.msda = level

    # -- protocol ----------------------------------------------------------
    def _start(self):
        # (repeated) START: keep the reg pointer for read-after-write.
        self.phase = 'addr'
        self.k = 0
        self.shift = 0
        self.ack = True                       # drive ACK once the byte is in
        self.ssda = 1

    def _stop(self):
        if self.phase != 'idle':
            self.log.append({
                'addr': self.addr if self.matched else None,
                'rw': 'R' if self.phase in ('read', 'done') else 'W',
                'reg': getattr(self, 'reg_start', None),
                'data': bytes(self.cur),
            })
        self._reset_txn(idle=True)
        self.ssda = 1

    def _scl_rise(self):
        if self.phase == 'read':
            if self.k < 8:                    # master samples our bit (served by read hook)
                self.k += 1
                if self.k == 8:               # a full read byte has been shifted out
                    self.cur.append(self.out)
                    self._read_byte_done = True
            else:                             # 9th clock = master ACK(0)/NACK(1)
                self._nack = self.sda()
                self.k += 1
            return
        if self.k < 8:                        # sample a master-transmitted bit
            self.shift = (self.shift << 1) | self.sda()
            self.k += 1
            if self.k == 8:
                self._byte_in()               # decide ACK / next phase now
        else:                                 # ACK clock (master reads our ACK)
            self.k += 1

    def _scl_fall(self):
        if self.k >= 9:                       # byte + ACK finished
            self.k = 0
            self.shift = 0
            self._drive_ack = False
            if self.phase == 'read' and self._read_byte_done:
                self._read_byte_done = False
                if self._nack:                # master won't read more
                    self.phase = 'done'
                else:
                    self.reg = (self.reg + 1) & 0xFF
                    self.out = self.regs[self.reg & 0xFF]
        # Drive the line for the upcoming bit while SCL is low.
        if self.k == 8 and self._drive_ack:
            self.ssda = 0                     # pull SDA low = ACK a received byte
        elif self.phase == 'read' and self.k < 8:
            self.ssda = (self.out >> (7 - self.k)) & 1     # next outgoing bit (MSB first)
        else:
            self.ssda = 1

    def _byte_in(self):
        """The 8 data bits of a master->slave byte are in; set up the ACK."""
        if self.phase == 'addr':
            self.matched = (self.shift >> 1) == self.addr
            self.ack = self.matched
            if not self.matched:
                self.phase = 'idle'
            elif self.shift & 1:              # read transaction
                self.phase = 'read'
                self.out = self.regs[self.reg & 0xFF]
                self.reg_start = self.reg
            else:                             # write transaction
                self.phase = 'reg'
        elif self.phase == 'reg':
            self.reg = self.shift & 0xFF
            self.reg_start = self.reg
            self.phase = 'write'
            self.ack = True
        elif self.phase == 'write':
            self.regs[self.reg & 0xFF] = self.shift & 0xFF
            self.cur.append(self.shift & 0xFF)
            self.reg = (self.reg + 1) & 0xFF
            self.ack = True
        self._drive_ack = self.ack


class BLEmu:
    def __init__(self, bl_path=BL_PATH, app_path=APP_PATH, accel=None, idchip=None):
        self.bl = open(bl_path, 'rb').read()
        self.accel = accel if accel is not None else I2CDevice()
        self.idchip = idchip if idchip is not None else OneWire()

        mu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        for base, size in ((0x00000000, 0x10000), (0x08000000, 0x80000),
                           (0x20000000, 0x20000), (0x40000000, 0x100000),
                           (0xE0000000, 0x100000), (0x1FFF0000, 0x10000)):
            mu.mem_map(base, size)
        mu.mem_write(BL_BASE, self.bl)
        try:
            mu.mem_write(APP_BASE, open(app_path, 'rb').read())
        except OSError:
            pass
        self.mu = mu

        self._install_uid()
        # Run the scatter-load init so RAM .data (function tables etc.) exists.
        self._raw_call(0x08000154, 0x0800AC48, 0x20000000, 0x1EC)

        mu.hook_add(UC_HOOK_CODE, self._on_code)
        mu.hook_add(UC_HOOK_MEM_UNMAPPED, lambda *a: False)

    def _install_uid(self):
        uid = bytes(range(0x11, 0x11 + 12))
        self.mu.mem_write(0x1FFFF7E8, uid)
        key = b''.join(struct.pack('<I', struct.unpack_from('<I', uid, i * 4)[0] ^ 0x0800FFF0)
                       for i in range(3))
        self.mu.mem_write(0x0800FFF0, key)

    # -- calling guest functions ------------------------------------------
    def _raw_call(self, fn, *args, count=5_000_000):
        mu = self.mu
        for r, v in zip((UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3), args):
            mu.reg_write(r, v)
        mu.reg_write(UC_ARM_REG_SP, 0x2001F000)
        mu.reg_write(UC_ARM_REG_LR, RET_TRAP | 1)
        try:
            mu.emu_start(fn | 1, RET_TRAP, count=count)
        except UcError:
            pass
        return mu.reg_read(UC_ARM_REG_R0)

    def call(self, fn, *args, **kw):
        return self._raw_call(fn, *args, **kw)

    def _skip(self, uc, value=0):
        uc.reg_write(UC_ARM_REG_R0, value)
        uc.reg_write(UC_ARM_REG_PC, uc.reg_read(UC_ARM_REG_LR))

    # -- the bus-slave hooks ----------------------------------------------
    def _on_code(self, uc, address, size, user):
        # Accelerometer: I2C bit-bang on PD5/PD6.
        if address in (PIN_SET_FN, PIN_CLR_FN, PIN_RD_FN):
            port = uc.reg_read(UC_ARM_REG_R0)
            mask = uc.reg_read(UC_ARM_REG_R1)
            if port == GPIOD and mask in (SDA_BIT, SCL_BIT):
                chip = self.accel
                if address == PIN_SET_FN:
                    (chip.set_sda if mask == SDA_BIT else chip.set_scl)(1)
                elif address == PIN_CLR_FN:
                    (chip.set_sda if mask == SDA_BIT else chip.set_scl)(0)
                else:                                  # read a bit
                    self._skip(uc, chip.sda() if mask == SDA_BIT else chip.scl)
                return
        # IDChip: 1-Wire on PB11, modelled at the byte level.
        elif address == OW_RESET_FN:
            self._skip(uc, self.idchip.reset())
        elif address == OW_WRITE_FN:
            self.idchip.write(uc.reg_read(UC_ARM_REG_R0) & 0xFF)
            self._skip(uc, 0)
        elif address == OW_READ_FN:
            self._skip(uc, self.idchip.read())

    # -- convenience -------------------------------------------------------
    def run_accel(self):
        """Init the I2C bus and run the boot-time accelerometer config,
        returning the captured I2C transactions."""
        self.accel.log.clear()
        self.call(ACCEL_INIT_FN)
        self.call(ACCEL_CMD_FN)
        return list(self.accel.log)

    def run_idchip_read(self):
        """Call the bootloader's IDChip reader (reset + Read ROM) and return
        (rom_bytes, commands_seen)."""
        buf = 0x20010000
        self.idchip.cmds.clear()
        self.call(IDCHIP_READ8_FN, buf)
        return bytes(self.mu.mem_read(buf, 8)), list(self.idchip.cmds)

    def run_auth(self, mode=0):
        """Run the anti-clone auth and report what it did with the IDChip."""
        self.idchip.cmds.clear()
        ret = self.call(AUTH_FN, mode)
        status = struct.unpack('<H', self.mu.mem_read(0x200000C0, 2))[0]
        return {'ret': ret, 'status': status, 'idchip_cmds': list(self.idchip.cmds)}


if __name__ == '__main__':
    e = BLEmu()
    print('=== accelerometer config over I2C (init + 0x080025D0) ===')
    for t in e.run_accel():
        reg = f"{t['reg']:#04x}" if t['reg'] is not None else '-'
        print(f"  addr={t['addr']:#04x} {t['rw']} CTRL_REG@{reg} data={t['data'].hex()}")
    print('=== IDChip 1-Wire Read ROM (0x080021A0) ===')
    e.idchip.rom = bytearray(b'\x01\x2a\x3b\x4c\x5d\x6e\x7f\x90')
    rom, cmds = e.run_idchip_read()
    print('  commands:', [hex(c) for c in cmds], ' ROM read:', rom.hex())
    print('=== auth (0x08003D64) IDChip use ===')
    print(' ', e.run_auth(0))
