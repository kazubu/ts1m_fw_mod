> Japanese version: [EMULATION.ja.md](EMULATION.ja.md)

# Analysis by Emulation

`ts1m_emu.py` is a harness that boots the TS1M application firmware on Unicorn (ARM Cortex-M3)
and drives it as far as the main loop. It can capture UART debug output and
inject ADC values per measurement point.

```
pip install unicorn capstone
python3 hex2bin.py TS1M_Master_APP_V202_EN.hex    # -> *_EN.bin
python3 ts1m_emu.py                                # boot and show the UART log
```

The UART output streams while it runs. Progress is printed to stderr.

**Note on execution time**: Unicorn is not fast, so it takes several minutes. The default is 20 million instructions,
which is enough to reach the main loop (the first UART line is at about 40 thousand instructions, and the `ironType`
PID log is within 10 million instructions). Use `-n` to increase or decrease it.

```
python3 ts1m_emu.py -n 5000000     # shorter
python3 ts1m_emu.py -q             # stop the live echo and print everything at the end
```

## Converting HEX to BIN

`ts1m_emu.py` reads a **flat binary starting at `0x08010000`**.
Either the bundled `hex2bin.py` or objcopy works (the output is confirmed identical).

```
python3 hex2bin.py TS1M_Master_APP_V202_EN.hex TS1M_Master_APP_V202_EN.bin
```

```
objcopy -I ihex -O binary TS1M_Master_APP_V202_EN.hex TS1M_Master_APP_V202_EN.bin
```

The distributed HEX is contiguous from `0x08010000` to `0x080773C4`, so either way it is 422852 bytes.

Note: fill the gaps with 0xFF (erased flash). Early in the analysis a binary that filled the gaps with 0x00 was used,
which caused the unused region around `0x08060000` to be mistaken for "0x00 padding". In reality that region is not
included in the HEX and is in the erased state, 0xFF.

## When it does not run

`emu_selftest.py` does the triage automatically.

```
python3 emu_selftest.py
```

It checks, in order, the unicorn version and basic operation, the validity of the bin (initial SP / reset vector),
how far the boot got, and whether UART is being captured.

Confirmed working environment: unicorn 2.1.4 (Python bindings).
The 1.x series handles hooks differently and has not been verified to work.

Common causes:

- The base address of the `.bin` is wrong (e.g. based at `0x08000000`).
  If the initial SP is not `0x2000cfc8` and the reset vector is not `0x0801085d`, the conversion is wrong
- unicorn is old, or a build where `UC_HOOK_CODE` does not fire
- Not enough instructions (the default for `run(count=...)` is 120 million. The first UART line comes out at
  about 36 thousand instructions, so if it does not appear the problem is before that point)

## What was needed to get the boot through

The firmware has many places where it waits for peripheral responses, and on bare Unicorn it stalls one after another.
Below are all the places where it actually got stuck, and how each was handled.

### 1. UID matching (the first gate)

At boot it reads the chip UID (12 bytes from `0x1FFFF7E8`) and compares it against the value in the bootloader region
`0x0800FFF0`. The stored value is each 32-bit word XORed with `0x0800FFF0`.
On a mismatch it falls into `0x08013A40` (an infinite loop that displays `Demo Mode` / `Not e-Design Product!`
while kicking the watchdog).

The bootloader is not included in the distributed HEX, so write the value back-computed from the UID to `0x0800FFF0`.

### 2. `UC_HOOK_MEM_READ` cannot be used

**Important**: doing a `mem_write` inside a `UC_HOOK_MEM_READ` callback does not change
the value that the read instruction observes.

Until this pitfall was noticed, the analysis took a big detour: while believing it was "returning 0xFF to peripheral
registers", 0 was actually being read.
Seed peripheral registers **directly into real memory**.

### 3. Millisecond counter

`0x200003BC` is the system millisecond counter. `0x08016F88` reads it and returns the elapsed time,
and `0x08012F04` waits until a specified time. Because the SysTick interrupt is not emulated,
the counter does not advance and it cannot get out of the wait loop. Increment it in proportion to the instruction count.

### 4. ADC

- Waiting for conversion complete: set EOC/JEOC in SR (`+0x00`)
- Waiting for calibration complete: keep the relevant bit in CR2 (`+0x08`) cleared
- The firmware has a place where it waits for the `0x90000000` bits to be set in CR2

The CR2 of ADC1 (`0x40012408`) was at one point polled 1.8 million times.

### 5. USART

The output destination of printf. Set TXE/TC (`0xC0`) in SR.
`0x0801E99C` is putchar, and `0x0801E9AE` writes the byte in r4 to DR.
Hooking here captures the entire debug output.

### 6. DMA

The ISR at `0x40020000`. Set all flags.

### 7. Stretching `UC_HOOK_MEM_WRITE` over a wide range breaks it

If a write hook is placed over the entire RAM (`0x20000000`-`0x2000FFFF`),
it stops every time at exactly 524318 instructions with `UC_ERR_WRITE_UNMAPPED`.
Without the hook it runs to completion over 120 million instructions, so this seems to be a problem on the Unicorn side.

When tracking changes in RAM, **take periodic snapshots and look at the diffs**.

### 8. SPI (display)

The display driver polls the TXE/RXNE of the SR (`+0x08`) of SPI1 (`0x40013000`) up to 6000 times
(`0x08015ADC`). If this is not filled in, every draw waits for a timeout, and once settings are loaded
the main loop hardly advances. Write `0x0003` to SR just before `0x08015ADC`.

### 9. `0x08013A40` is not the halt screen itself

Previously, entering `0x08013A40` was treated as "halt in Demo Mode", but this function branches on r0.
r0=0 / 2 / 3 are halt screens (`Demo Mode` / `Not e-Design Product!`), but **r0=1 is a normal path that just records
the time and returns**.
Once settings are loaded, the input processing calls this with r0=1, so it was being wrongly treated as a halt.
Now only r0≠1 is treated as a halt.

## The state that can be reached

With the above in place, UART output like the following is obtained.

```
SystemClk:8000000     <- the value because RCC is filled with 0xFFFFFFFF. On real hardware it is 96MHz
DataCheckArr[0]:0
...
DataCheckArr[10]:0
tempCalRatio:1.000000   Param:0
ironType:0x1 P:1.00  I:0.20   D:4.00  FF:0.15
 28V
```

It is also confirmed that each function of the main loop is cycling (about 10 thousand times each at 120 million instructions).

| Function | Role |
| --- | --- |
| `0x0801EAA4` | Input / sleep processing |
| `0x08022098` | Tip detection |
| `0x080199A8` | ADC classification |
| `0x080214A0` | **Heater / PID control** |
| `0x08022944` | Drawing |

That `0x080214A0` is called on every cycle from the main loop was confirmed by this measurement.

## Analog multiplexer and measurement points

The tip measurement switches the path with an analog switch and reads ADC ch10.
The select lines are **PB3 / PC12 / PD2 / PA6 / PD3 / PB4**.

GPIO operation functions:

| Address | Action |
| --- | --- |
| `0x080140B8` | BRR (`+0x14`) — pin Low |
| `0x080140BC` | BSRR (`+0x10`) — pin High |

There are four measurement points during the tip detection loop. They correspond to the firmware's own debug output
(`1_5 adc1:... adc2:...` / `1_2 adc3:... adc4:...`).

| PD2 | PD3 | PB4 | ch | Name in the log |
| --- | --- | --- | --- | --- |
| 1 | 0 | 0 | 10 | adc1 |
| 1 | 1 | 0 | 10 | adc2 |
| 0 | 0 | 0 | 10 | adc3 |
| 0 | 0 | 1 | 10 | adc4 |

The correspondence was confirmed by injecting a different value per measurement point and matching against the debug output.

### Note: the path changes with the operating mode

If the tip detection function is skipped with `force_tip`, the mux switching no longer happens and
a different measurement pattern occurs (reading ch0 / ch12 / ch13 with the mux all 0).
**Which measurement point is read depends on the operating mode**, so
when injecting ADC values, confirm the key that was actually read with `adc_log`.

## Virtual panel (`lcd=True`)

With `Emu(lcd=True)`, the pixels sent to the panel are reproduced into a 320×172 RGB565 frame buffer, and
`save_png(path)` can turn it into an image (no PIL required).

| Function | Handling |
| --- | --- |
| `0x08014DA4(x0, y0, x1, y1)` | Set the drawing window (CASET/RASET/RAMWR; the offset 34 of the 172-row panel is added inside the function) |
| `0x08015040(v)` | One word of 16-bit data. Calls from inside the window-setting function (coordinates) are excluded |
| `0x08016FA4(ch, count)` | DMA transfer. The transfer source is the CMAR of that channel (`ch+0x0C`) |

The UI has a page structure (`0x200004CC`; +8 is the current page, and from +0x3C there is a page table of 28 bytes each).
Each page has functions (+8 draw, +12 update), and the update function calls the draw function only when there is a change.

| Page | Draw | Content |
| --- | --- | --- |
| 0 `0x20000508` | `0x08020169` | Home (settings / heat / thermometer icons) |
| 1 `0x20000524` | `0x0801F4FD` | Heat screen. **Entering it sets the operating mode to 1 (work)** |
| 2 `0x20000540` | `0x080203ED` | Settings menu (DefWork / WorkTmp1-3 / SlpTmp …) |
| 3 `0x2000055C` | `0x0801FF81` | Graph screen |

Key input is not simulated, so write the page directly to `0x200004D4` and write 1 to +16 of the page to force a full redraw.

Notes:
- Drawing is heavy. The first draw of the heat screen alone takes several million instructions, and the heater control
  cycling becomes extremely slow.
  Since the millisecond counter is advanced by instruction count, when observing time-dependent processing (such as the
  20-second cutoff of boost), increase `tick_every` (default 50 instructions/ms) after boot
- The current-temperature display uses a moving average (`0x20009DD4`) updated by SysTick. SysTick is not simulated, so
  the display stays `000`
- The supply voltage is ch13 × 7.77 × factory calibration [9] / 1000 (mV). If the factory calibration is 0, the heat screen
  gives a `LowVol` warning
  (with `defaults=True`, [9]=1000 is set. At ch13=3100 it is about 24V)

## Getting temperature control running (`defaults` / `work_mode`)

Bare, both the current temperature and the target temperature stay at 0. Causes and remedies:

| Cause | Remedy (`Emu(defaults=True)`) |
| --- | --- |
| The settings struct `0x200015A4` is all 0 (`TS1M.TXT` does not exist so it is not read. Setting `0x08077E00` to 0xFF is the same) | On the first heater call, copy the flash default table `0x0802A4E6` (same 22-byte layout) |
| The temperature calibration coefficient `0x20000930` is 0, so current temperature = polynomial × coefficient/1000 becomes 0 | Place `1000×5 + total` in flash `0x08077C00` |
| If the setting `FlipOver` is 1, the accelerometer is read via the bootloader and it jumps to code that does not exist | `FlipOver = 0` |

Work state (`Emu(work_mode=True)`):

| Cause | Remedy |
| --- | --- |
| The operating mode `0x2000023C` stays 0 (idle) | Force it to 1 every time |
| The stand / sleep state machine `0x0801EB58` writes the mode back | Make the call a no-op |
| The heater control state `0x200001D2` becomes 1 after PID, and does not measure until the ADC interrupt (vector 34) returns it. The interrupt is not simulated | If it is 1, return it to 0 (measurement+PID runs every time) |

In this state the target temperature `0x200001F4` = WorkTemp1 (3000), and the PID output `0x200001D0` changes.

## Modeled hardware state (pins, timers, UART)

`Emu` tracks the state of GPIO, timers, and UART during execution, and it can be read out afterward (ANALYSIS.md ch. 15).

| API | Content |
| --- | --- |
| `e.pin('PA1')` | The drive level of the GPIO pin (0/1). A shadow of ODR/BSRR/BRR (essential because Unicorn does not reproduce the BSRR→ODR side effect). **AF / timer output pins are not reflected** |
| `e.pins()` | The set of pins currently driven High |
| `e.pin_mode('PC4')` | The pin mode read from CRL/CRH (`'AIN'` / `'AF_PP_50'` / `'OUT_PP_50'` …). Accurate because it reads Unicorn memory directly |
| `e.heater_on` | Whether TIM5 (CH2 = **PA1** heater gate) is enabled |
| `e.heater_log` | The history of heater ON/OFF transitions `(instruction count, on)` |
| `e.buzzer_duty` / `e.backlight_duty` | The compare values of TIM4_CH3 (PB8) / TIM1_CH1 (PA8) |
| `e.uart_tx[2]` / `e.uart_tx[4]` | The byte stream the firmware sent to USART2 (tip link/jig) / UART4 |

Tracking is done with a write hook **limited** to the GPIO/UART register ranges (the whole-range hook in ch. 7 breaks it, so the range is narrowed) and code hooks on the timer helpers (`TIM_Cmd` `0x08016A9E` / `TIM_CCxCmd` `0x08016C24` / `SetCompareN`).

> Note: `_seed_peripherals` previously wrote `0xFFFFFFFF` to the GPIO base (offset 0 = CRL), corrupting the pin configuration. GPIO has no status register at offset 0, so this seed was removed (`pin_mode` can now be read correctly).

### Running the measurement/heat cycle (`sim_measure=True`)

The heater (PA1/TIM5_CH2) turns ON/OFF within a measurement/heat cycle driven by the ADC injected-conversion interrupt (vector 34).
Because Unicorn does not simulate interrupts, this cycle normally does not advance, and in the main-loop emulation PA1 does not move
(the cause of previously mistaking PA1 for "idle").

Specifying `Emu(defaults=True, work_mode=True, sim_measure=True)` runs `run()` in slices, and in between calls the
measurement-start method (`0x08012F3C`, heater OFF) and the measurement-end method (`0x08013A20`, heater ON) in a
nested execution with context save/restore. This makes TIM5/CH2 (PA1) toggle just like real hardware, observable via `heater_log`.

```
python3 ts1m_emu.py --sim -q        # display the modeled pin/timer/heater state
```

## Identifying the temperature variables (isolating measurement points)

With `Emu(force_tip=1, skip_res=True, defaults=True)`, the four measurement points were ramped **one at a time** (the others fixed at 2000).

| Ramped measurement point | `0x2000023E` (current temperature) | Others |
| --- | --- | --- |
| adc3 (ch10, mux 000) 606→3431 | follows monotonically 1485→6037 | `0x20000402` (raw value) and `0x20000404` (result of the polynomial) also follow |
| ch12 605→3442 | inversely 4253→3313 | `0x200003FC` (raw value) follows, and `0x20000400` (cold junction) goes 90→8 (NTC) |
| ch13 | no change | |
| ch0 | no change | |

- **Current temperature = `0x2000023E`**, target temperature = `0x200001F4`, operating mode = `0x2000023C`
  (details in ANALYSIS.md ch. 10)
- `0x200003FC` / `0x20000402` / `0x20000404`, previously deemed "wrong", do follow when the correct measurement point is ramped alone.
  However, they are all raw or intermediate values, not the current temperature used for control
- The earlier inference of `0x20000244` (target temperature) / `0x20000914` (current temperature) is wrong. `0x20000914` is the calibration-mode (CAL) flag

## External SPI flash (`spi_flash`) and actual settings loading

Settings are read from `TS1M.TXT` on a FatFs volume on the external SPI flash (Winbond W25Q64, SPI2, CS=PB12)
(ANALYSIS.md ch. 12). On the bare emulator, the flash does not respond, the ID check (`0x90` → `0xEF16`) → `f_mkfs` fails, and
the settings stay 0.

Attaching a W25Q64 model with `Emu(spi_flash=...)` lets the firmware itself format it, write the initial TS1M.TXT, and
read it from then on (`Data Valid`). The substitute processing of `defaults=True` is no longer needed, and the settings path itself can be confirmed.

```
python3 ts1m_emu.py --make-flash flash.bin     # make a formatted image (already rewritten with FlipOver=0)
python3 ts1m_emu.py --flash flash.bin -q       # boot with that image
```

```python
from ts1m_emu import Emu, edit_ts1m_txt
img = open('flash.bin', 'rb').read()
img = edit_ts1m_txt(img, TempType=1, WorkTemp1=572)   # the number of digits of a value cannot be changed
e = Emu(spi_flash=img, force_tip=1, skip_res=True, work_mode=True).run(count=10_000_000)
```

Contents of the model: `0x90` / `0x9F` / `0xAB` / `0x4B` (ID), `0x05` (always non-busy), `0x06` / `0x04`, `0x03` / `0x0B` (read),
`0x02` (page write), `0x20` / `0x52` / `0xD8` / `0xC7` (erase). It intercepts the SPI data register functions
(`0x08015AEE` transmit / `0x08015AEA` receive) only when it is SPI2.

Related remedies put in place:

| Symptom | Remedy |
| --- | --- |
| The parsing function treats the number of read bytes as a pointer and reads the search length from the bootloader region (address ~0x500) | Map `0x00000000`-`0x0000FFFF` (an alias of the boot region) and fill it with `0x06` (search length 0x0606) |
| With the initial value `FlipOver = 1`, the accelerometer is read via the bootloader and it jumps to code that does not exist | `make_flash_image()` rewrites the value in the file to 0 |
| The temperature/voltage calibration records are in internal flash (`0x08077C00` / `0x08077D00`) and are not included in the HEX | When `spi_flash` is specified too, place neutral values just like `defaults=True` |

What could be confirmed by rewriting TS1M.TXT:
- `TempType=1`: settings, target, and current temperature become ℉×10. If CalibraVal stays 0, the current temperature comes out about 4.9% high (ANALYSIS.md ch. 12)
- `user_UI=1`: at boot `0x20000455` = 1, and the heat screen is drawn in a 7-segment style (Digit)

## Uses of the measurement points

| Measurement point | Use | Cell that follows |
| --- | --- | --- |
| ch10 (mux 000, adc3) | tip thermocouple | `0x20000402` (raw value) → `0x2000023E` (current temperature) |
| ch12 | cold junction NTC | `0x200003FC` (raw value), `0x20000400` (cold junction temperature) |
| ch13 | supply voltage | `0x2000039C` (mV) |
| ch0 | external thermocouple (TK) | `0x200008A4` (≒ ch0 × 1.17) |

## Calling the PID directly (`pid_sim.py`)

`pid_sim.py` boots once with `defaults=True, work_mode=True, force_tip=N` (about 30 s), clears the PID state
(`0x200001E0`–`0x20000203`, keeping the gains at `0x20000204`), and then calls `0x08015494` directly for each step:
write the target to `0x200001F4`, set r0 = current temperature, SP to a scratch stack and LR to `_RET_TRAP`, and read the output from `0x200001D0`.
A thermal model supplies the next temperature (ANALYSIS.md ch. 10).

```
python3 pid_sim.py            # C245: reproduce the overshoot and compare fixes
python3 pid_sim.py --tips     # all tip types
python3 pid_sim.py --check    # Python transcription vs the firmware (a few minutes)
```

Note: **patching code with `mem_write` after it has run has no effect** — Unicorn keeps executing the block it already translated.
Apply code patches to the image before boot (`FirmwarePID(patches=...)` does this).

## Unresolved

- The effect of the HEX line structure is bootloader processing, so it cannot be investigated
- Cold junction compensation of the external thermocouple: moving ch12 did not change `0x200008A4`
- UI page 3 could be drawn up to part of the graph screen, but drawing is slow and the whole thing has not been confirmed

### Supplement on settings loading

`0x08023644` (from main's `0x080223BE` with `r0=0x08077E00, r1=0x200002F0, r2=1`) is a process that
copies just one word of a 16-bit value, not the settings body. The settings body is read via TS1M.TXT as above.

Note that `0x200002F0` is not a drawing struct but the destination of this one word.
The `+6` / `+8` touched by `0x0802796C` / `0x08027960`, previously mistaken for beep functions, are also fields of this struct.
