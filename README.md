# ts1m_fw_mod

> Japanese version: [README.ja.md](README.ja.md)

Reverse-engineering notes and firmware mods for the MINIWARE TS1M soldering-iron station.

Target firmware: `TS1MAPPV202` (`TS1M_Master_APP_V202_EN.hex` / `_CN.hex`; the on-screen version reads `SWVer: 2.2`).

## Status

Analysis is ongoing. The firmware structure, the tip-detection logic, the settings, and the constraints on DFU flashing are understood. A technique for patching in and running arbitrary code has been verified on the device.

The temperature-related RAM variables (current temperature, target temperature, work mode) and how to sound the beeper were identified through emulation, and a patch (`patches/`) that adds a target-reached beep, auto boost, and a boost indicator was built. **Verified on the device** (2026-09-27). It is also confirmed in emulation (including on-screen checks via the virtual panel).

By finding that files on the DFU drive live in the external SPI flash, the **bootloader was dumped from the device through that path and analyzed** (ANALYSIS.md ch. 13-14). The boot decision, the anti-clone check (UID + external IDChip), the DFU HEX check/program path, and the soldering-iron firmware update were all worked out.

- [ANALYSIS.md](ANALYSIS.md) — analysis of the firmware itself
- [EMULATION.md](EMULATION.md) — Unicorn emulation procedure and findings
- [ts1m_emu.py](ts1m_emu.py) — the application emulator harness
- [bl_emu.py](bl_emu.py) — the bootloader emulator harness (models the IDChip 1-Wire and the accelerometer I2C)
- [hex2bin.py](hex2bin.py) — Intel HEX → raw binary converter
- [patches/](patches/) — the target-reached beep + auto boost + boost indicator patch (ANALYSIS.md ch. 11), and diagnostic patches (chip ID, DFU file search, bootloader dump)

## What is known (summary)

- The MCU is a **WCH CH32F208WBU6** (Cortex-M3, QFN68, 96 MHz). Inferred from the SDK drivers and pinout, then confirmed by the device's chip ID `0x2080043C`. Keil build.
- The app spans `0x08010000`-`0x080773C4`. The bootloader (`0x08000000`-`0x0800FFFF`) is not part of the distribution but was dumped from the device.
- Tips supported: 245 / 210 / 115 / H100. **210 and 115 are not distinguished electrically and are selected manually.**
- A `TS80P` type code exists but is **unreachable**.
- At boot the chip UID is checked; on a mismatch it stops with `Demo Mode`.
- The factory test mode starts automatically only when the factory calibration record is corrupted.
- **The emulator boots to the main loop and captures the UART debug output.**
- Current temperature `0x2000023E`, PID target temperature `0x200001F4`, work mode `0x2000023C` (units of 0.1 °C; identified in emulation and backed by the patch running on the device).
- The beep is sounded via the buzzer object `0x20000330`'s `beep(pattern)` (`0x08012219`).
- The `0x0802AFC4` area previously used as a hook site was not free — it was part of a bitmap. Use the unused FatFs test function `0x0802849C` (348 B) instead.
- String drawing is `drawString(str, x, y, font, fg, bg, flag)` (`0x08014834`; colors are RGB565 passed on the stack).
- The UI is page-structured. Entering the heat screen (page 1) puts the work mode into working state.
- The settings file `TS1M.TXT` lives on a FatFs volume in the external SPI flash (W25Q64, SPI2). The emulator has a model of it.
- The DFU drive and the settings file are **separate FAT regions** on the same SPI flash (settings = base `0x000000`, DFU = base `0x200000`). They do not clobber each other, so both persist (MARK.BIN was 64 KB contiguous from `0x247000`; measured in emulation).
- ADC: ch10 = tip thermocouple, ch12 = cold junction, ch13 = supply voltage, ch0 = external thermocouple (used in calibration mode CAL).
- A full pin/peripheral map of the MCU was built from both firmwares (ANALYSIS.md ch. 15). Heater = **PA1 (TIM5_CH2)**, display = SPI1 (SCK PA5 / MOSI PA7 / DC PB0 / BL PA8), flash = SPI2 (PB12-15), tip link = USART2 (PA2/PA3), buzzer = TIM4/PB8, ADC1 inputs = PA0/PA4/PC0-4, 7 analog-mux select lines, IDChip auth = 1-Wire on PB11, accelerometer = I2C on PD5/PD6. No remaps, no EXTI.
- The settings menu has hidden items: QkTmp (BoostTemp) and RGB lighting (RGB FX / Mode / Bright / Red / Green / Blue). There is no code that drives an LED from the RGB values.
- In Fahrenheit mode CalibraVal is converted as an absolute temperature; left at 0, the reading may come out about 4.9 % high (not verified on the device).

## Patches

```
python3 patches/build.py              # -> TS1M_Master_APP_V202_EN_notify_boost.hex / .bin
python3 patches/test_notify_boost.py  # scenario test on the emulator (a few minutes)
python3 patches/screenshot.py         # render the heat screen on the virtual panel (normal / boosting, a few minutes)
```

| Feature | Behavior |
| --- | --- |
| Target-reached beep | Beeps twice the first time the temperature enters ±3.0 °C of the set point. Re-armed on a setting change or wake from sleep. |
| Auto boost | After the target has been reached once, if it stays 10.0 °C or more below for 300 ms, raise the target by +20.0 °C (capped at 450 °C). Ends on recovery, gives up after 20 s. |
| Boost indicator | While boosting, the current-temperature digits on the heat screen turn from white to red (pinkish in the 7-segment style). |

### Screen (boost indicator)

The heat screen rendered on the emulator's virtual panel (regenerate with `python3 patches/screenshot.py docs/images`). The current-temperature digits stay `000` because they are not updated in emulation.

| | Normal | Boosting |
| --- | --- | --- |
| Default style | ![default/normal](docs/images/heat_normal.png) | ![default/boosting](docs/images/heat_boost.png) |
| 7-segment style | ![7-seg/normal](docs/images/heat7_normal.png) | ![7-seg/boosting](docs/images/heat7_boost.png) |

- The only changes are three 4-byte instructions (replaced with `bl`) plus the hook body placed in the area of an unused function.
- The boosted target is used only during the PID call and restored immediately. The heater control and safety code are not touched.
- The parameters (thresholds, boost amount, indicator color) can be changed via the `.equ` values at the top of `patches/notify_boost.S`.
- Only the affected lines of the original HEX are rewritten, so the line count and structure match the original.

### Diagnostic patch: chip ID display

```
python3 patches/build.py -p chipid    # -> TS1M_Master_APP_V202_EN_chipid.hex
```

Instead of the tip name at the top left of the home screen ("245", etc.), it shows the chip ID (`0x1FFFF704`) as 8 hex digits. It does not touch the heater control. After checking, flash back the original firmware (or the notify_boost version). It uses the same free area as notify_boost, so the two cannot be installed at once.

| Display | Chip (from the WCH SDK `DBGMCU_GetCHIPID` list) |
| --- | --- |
| `208004xC` | CH32F208WBU (QFN68). **The value on the TS1M device is `2080043C`.** |
| `208104xC` | CH32F208RBT (LQFP64) |
| `203...` / `205...` / `207...` | CH32F203 / F205 / F207 |

(x is the revision)

### Diagnostic patch: file location on the DFU drive (MARK.BIN)

```
python3 patches/build.py -p findmark  # -> TS1M_Master_APP_V202_EN_findmark.hex
```

Finds where a file written to the DFU mass storage lands in the external SPI flash (W25Q64). Copy `MARK.BIN` (the 16-byte line `TS1M-FINDVOL-MK\n` repeated to fill 64 KB) onto the DFU drive, then flash this patch. MARK.BIN can be made with `python3 -c "open('MARK.BIN','wb').write(b'TS1M-FINDVOL-MK\n'*4096)"`. At boot it scans the 8 MB flash at every 512-byte boundary and displays the result where the tip name would be at the top left of the home screen.

| Display | Meaning |
| --- | --- |
| `AAAAAA:CC` | First matching address (6 hex digits) and the number of matching 512 B sectors (max `FF`). Contiguous 64 KB = `80`. |
| `FFFFFF:00` | Not found |
| `--------` | SPI2 not initialized, so nothing was scanned |

The scan runs each time the label is drawn (only at boot and on page redraws) and takes a few hundred ms. It does not touch the heater control. During the scan the watchdog (~3.2 s) is fed by the firmware's own reload function.

On the device it displayed `247000:80` (64 KB contiguous from 0x247000).

### Diagnostic patch: dumping the bootloader (overwrite MARK.BIN)

```
python3 patches/build.py -p dumpboot  # -> TS1M_Master_APP_V202_EN_dumpboot.hex
```

Overwrites the MARK.BIN payload found by findmark (SPI flash `0x247000`-`0x256FFF`) with the internal-flash bootloader (`0x08000000`-`0x0800FFFF`, 64 KB). Then copying MARK.BIN off the DFU drive yields the bootloader image.

Only when those 64 KB are **all the marker** does it erase and program the 16 × 4 KB sectors, then read back and verify. Anything else, it writes nothing. On later boots it only re-verifies.

| Display | Meaning |
| --- | --- |
| `OK` | The 64 KB at `0x247000` matches the bootloader (written and verified) |
| `NG:0` | SPI2 not initialized, nothing done |
| `NG:1` | Content was neither the marker nor the bootloader, so nothing was written |
| `NG:2` | Written, but the read-back does not match |

The bootloader also contains the per-device UID key (`0x0800FFF0`). On the device, `OK` was confirmed and MARK.BIN was copied off the DFU drive to obtain the 64 KB bootloader (not included in the repo).

## DFU flashing

The device becomes a USB mass-storage device and the HEX is copied onto it. Writes to both the code area and the string area take effect.

There was a suspicion that re-serializing the whole HEX might not take effect, but that is **now resolved**: the bootloader's check pass rejects lines whose length or line endings differ (see ANALYSIS.md "7. About DFU flashing" and ch. 14).

## Emulation

`ts1m_emu.py` boots it under Unicorn. Since the whole UART debug log can be captured, this is the most informative method when SWD is unavailable.

```
pip install unicorn capstone
python3 hex2bin.py TS1M_Master_APP_V202_EN.hex
python3 ts1m_emu.py
```

Bypassing the UID check, modeling the peripheral registers, and the Unicorn pitfalls (read hooks do not work, write hooks abort abnormally) are summarized in EMULATION.md.

Main options:

| Option | Description |
| --- | --- |
| `force_tip` / `skip_res` | Fix the tip type and skip the resistance check |
| `defaults=True` | Load the default settings, temperature calibration, and supply-voltage calibration (without them, both temperature and voltage read 0) |
| `work_mode=True` | Keep the heater in working state so every pass measures and runs the PID |
| `lcd=True` | Reproduce what is sent to the panel in a 320×172 frame buffer; export with `save_png()` |
| `spi_flash` | Model the external SPI flash (W25Q64). Read settings via the actual TS1M.TXT (`--make-flash` / `--flash`) |
| `adc_hook` | Inject an ADC value per measurement point |
| `sim_measure=True` | Drive the measure/heat cycle so the heater (PA1/TIM5_CH2) toggles as on the device |

It also tracks GPIO / timer / UART state: `e.pin('PA1')` / `e.pin_mode('PC4')` / `e.pins()`, `e.heater_on` / `e.heater_log`, `e.buzzer_duty` / `e.backlight_duty`, `e.uart_tx[2]` (USART2) / `e.uart_tx[4]` (UART4). `python3 ts1m_emu.py --sim -q` prints a summary (ANALYSIS.md ch. 15, EMULATION.md).

## Caution

Modifying firmware is at your own risk. Do not touch the heater control or the safety mechanisms (overheat threshold, resistance check, 450 °C cap). Before trying anything on the device, always make sure you can flash the original firmware back.
