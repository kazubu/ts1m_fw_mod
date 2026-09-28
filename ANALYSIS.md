> Japanese version: [ANALYSIS.ja.md](ANALYSIS.ja.md)

# MINIWARE TS1M firmware analysis notes

Target: `TS1MAPPV202.zip` (MINIWARE TS1M multifunction mini soldering-iron station)

| Item | Content |
| --- | --- |
| File | `TS1M_Master_APP_V202_EN.hex` / `TS1M_Master_APP_V202_CN.hex` |
| Format | Intel HEX (CRLF) |
| Layout | `0x08010000`–`0x080773C4` (approx. 413KB, contiguous) |
| SHA-256 (EN) | `90d6b724965644997839f23288bf36f9f610d0b714f0203fa8861440fa18881f` |
| SHA-256 (CN) | `52b3dfab7078c302d4a8dce51d902f4d6b206c505708ba9ce4521a9edf78287f` |
| On-screen version | `SWVer: 2.2` (Info screen) |

The difference between EN and CN is **only 1 byte** (`0x0802A682`). It is only whether the initial value of the `Language` setting is `0` (English) or `1` (Chinese); the code is completely identical.

---

## 1. MCU and build environment

**WCH CH32F208WBU6** (Cortex-M3, with built-in BLE 5.3 / 10M Ethernet, QFN68).
**Confirmed by the chip ID on the actual device** (2026-09-27): `0x1FFFF704` = `0x2080043C`, read by a diagnostic patch.
It matches CH32F208WBU = `0x208004xC` in the official SDK's list (x = 3 is the revision).
We cross-referenced the SDK drivers in the code against the WCH official SDK ([openwch/ch32f20x](https://github.com/openwch/ch32f20x)),
and collated them with the pin layout in the datasheet (CH32F208 Datasheet V2.4).

### Series: CH32F20x (not STM32F103)

- USB descriptors contain `wch.cn` / `CH20xUDisk` / `WCH32` (UTF-16LE). WCH's USB mass storage sample is used almost verbatim
- The USB clock prescaler is 2 bits (CFGR0 bit22-23). At `0x0801613C` it looks at whether SystemCoreClock is 144/96/48MHz and
  selects ÷3/÷2/÷1 (same as the official `RCC_USBCLKConfig`). STM32F103 is 1 bit (÷1 / ÷1.5) and has no ÷3
- The clock setup does not set the flash wait cycles (FLASH_ACR). CH32 runs from a zero-wait region, so this is unnecessary
- It uses `0x40023800` (EXTEN_CTR): bit1 = USBD internal pull-up, bit4 = HSI pre-divide before the PLL
- The 256-byte fast erase using flash control register bit16/17 is CH32-specific

### Variant: D8W = CH32F208

The official SDK switches the variant with `CH32F20x_D6` (F203 small capacity) / `D8` (F203 large capacity) / `D8C` (F205/F207) / `D8W` (F208).
The firmware's `RCC_GetClocksFreq` (`0x0801592C`) **matches the D8W branch exactly**:

| Item | Firmware | Official SDK D8W |
| --- | --- | --- |
| Frequency when HSE is used as SYSCLK | 32,000,000 | `HSE_VALUE = 32000000` (D8W only. Others are 8MHz) |
| PLL input (HSE, XTPRE=0) | 8,000,000 | `HSE_VALUE >> 2` |
| PLL input (HSE, XTPRE=1) | 4,000,000 | `(HSE_VALUE >> 2) >> 1` |
| PLL input (CFGR0[23:22]=3) | 16,000,000 | `HSE_VALUE >> 1` (D8W-specific branch) |
| PLL multiplier | value 17 → ×18 | Handling for D6 / D8 / D8W (D8C is a separate table) |

`SystemInit` (`0x08016870`) also matches the official one (`CFGR0 &= 0xF0FF0000`, `INTR = 0x009F0000`, no CFGR2 write for D8C).
`SetSysClock` (`0x080160A0`) has the same instruction sequence as the official `SetSysClockTo96_HSE`, matching down to `HSE_STARTUP_TIMEOUT = 0x1000`.

**The system clock is 96MHz** (HSE 32MHz ÷4 ×12). APB1 = 48MHz, APB2 = 96MHz.
Previously writing "144MHz" was an error; 144/96/48 are merely candidates for the USB prescaler selection.
Note that the emulator's UART output `SystemClk:8000000` is because the RCC registers are filled with `0xFFFFFFFF`; it is not the value on the actual device.

### Package: QFN68 (CH32F208WBU6)

The CH32F208 comes in LQFP64 (RBT6) and QFN68 (WBU6). **PD3 (pin 66) and PD4 (pin 18) exist only on QFN68.**
The firmware actually drives PD2 / PD3 / PD4 as analog multiplexer select lines for tip measurement (EMULATION.md),
so we judged it to be QFN68.

### Consistency with the datasheet

| Item | Datasheet (CH32F208) | Firmware |
| --- | --- | --- |
| Flash | 480KB total (128K zero-wait + non-zero-wait) | Image goes up to `0x080773C4` (within 480KB) |
| SRAM | 64KB (selectable from 128K+64K / 144K+48K / 160K+32K) | Uses beyond initial SP `0x2000CFC8` = exceeds 48KB → 128K+64K configuration |
| External crystal | 32MHz (for BLE) | `HSE_VALUE = 32000000` |
| USB | USBD + USBHD | Uses only USBD (`0x40005C00`, packet memory `0x40006000`) |

BLE and Ethernet (PC6-PC9 as RXP/RXN/TXP/TXN) are not used by the firmware.

**Note on flash capacity**: "Flash 128K" in the datasheet's variant comparison table is **the value for the zero-wait execution region (R0WAIT) only**
(table note 1). The whole program flash is 480KB, the rest being the non-zero-wait region (feature list and memory map at the top of the datasheet).
The firmware actually executes code located beyond the 128K boundary (`0x08020000`)
(heater control `0x080214A0`, patch hook `0x0802849C`. The latter is confirmed working on the actual device).
Since it uses more than 48KB of SRAM the configuration is thought to be 128K+64K, and the code at and beyond `0x08020000` is executed from the non-zero-wait region.

### Confidence and remaining possibilities

| Claim | Confidence | Basis and limits |
| --- | --- | --- |
| WCH CH32F20x (Cortex-M3) | Certain | Thumb-2 code (excludes the RISC-V CH32V), use of CH32-specific registers, FLASH_ACR unset |
| Variant F208 | Confirmed (chip ID) | Chip ID `0x2080043C`. Static analysis had inferred it from "the SDK is built with the D8W setting". Running a D8W build on F203/F205/F207 makes RCC_GetClocksFreq return 1/4 of the real frequency and shifts the UART baud rate by 4×. These also cannot use a 32MHz crystal (PLL out of range too) |
| Package QFN68 | Confirmed (chip ID) | The upper part of the chip ID (`208004`) indicates WBU. In static analysis, without PD3 adc1/adc2 could not be distinguished and the 245 tip could not be determined. Tip determination working on the actual device backs this up. However, the multiplexer interpretation comes from emulation |

Addresses of peripherals that should not exist on the F208 appear in the code, but none are actually used:
- `0x40005000` (the location of UART5) is a base value used to reference USB endpoint registers `0x40005C00 + ep*4` with a `+0xC00` offset
- TIM8/9/10 (`0x40013400` / `0x40014C00` / `0x40015000`) appear only in a library function's "is TIMx one of TIM1–TIM10" comparison

The firmware reads neither the chip ID (`0x1FFFF704`) nor the flash capacity register, so from code alone a different part number on the same die or a compatible clone could not be ruled out.
So we used a diagnostic patch that displays the chip ID on screen
(`python3 patches/build.py -p chipid`, shown as 8 hex digits in the top-left of the home screen)
(official SDK values: CH32F208WBU = `0x208004xC`, CH32F208RBT = `0x208104xC`), read the value on the actual device,
and got `0x2080043C` (CH32F208WBU, revision 3). This agrees with the inference from static analysis above.

Peripheral access uses the WCH SDK, which is STM32F1 StdPeriph compatible. Keil (ARMCC) build.

## 2. Memory map

| Range | Content |
| --- | --- |
| `0x08000000`-`0x0800FFFF` | Bootloader (**not included** in the distributed HEX. Dumped from the actual device. ch. 13) |
| `0x0800FFF0` | Per-device key (for UID matching) |
| `0x08010000`-`0x08027FFF` | Application code |
| `0x08028000`-`0x080773C4` | Fonts (4bpp AA), images, strings |
| `0x08077D00` | Factory calibration record |
| `0x08077E00` | 1 word of 16-bit value (copied to `0x200002F0`). The settings body is TS1M.TXT on the external SPI flash (ch. 12) |

Vector table: SP=`0x2000CFC8`, Reset=`0x0801085D`, SysTick=`0x080165E9`. 54 IRQs. VTOR is set by code (`0x0801520A`).

### Free space within the code region

**Note: the "0x00 padding" listed here previously was not free space.**
`0x0802AFC4` (669 B) was the zero interior of a 1bpp bitmap drawn by `0x0801FA8C`
(760 B = an 80×76 px frame mask from `0x0802AF92`).
Placing code here corrupts part of the screen. `0x0802B958` / `0x0802DBA4` / `0x0802CDA8` are also
runs of zeros within the font/image region and for the same reason cannot be called free space (not individually verified).

Places that can be used with certainty:

| Address | Size | Basis |
| --- | --- | --- |
| `0x0802849C`-`0x080285F7` | 348 B | Unused FatFs test function (`M0.TXT` / `Hello, World!`). No BL, literal, or ADR reference to it in the code. However, the settings object's (`0x200002D0`) function table +0x10 (`0x200002E0`) has a pointer to this function (it is expanded from compressed initialization data, so it is not visible in a literal search on flash). Judged unused because it is not read outside expansion at startup (emulation: read hook, all pages), no instruction accesses it (static analysis), and the patch works on the actual device |

Free RAM:

| Address | Size | Basis |
| --- | --- | --- |
| `0x2000BDC8`-`0x2000BFC7` | 512 B | Heap (`__user_initial_stackheap` = `0x08010878`). No code using malloc is found, and it stays all-zero in emulation. Currently used as the patch's state storage on the actual device |

The stack is `0x2000BFC8`-`0x2000CFC8` (4 KB). RAM is not zero-cleared at power-on, so
if you place state on the heap you need your own initialization check (magic value).

## 3. Key functions / addresses

### Confirmed (verified on the actual device, or with multiple bases)

| Address | Content |
| --- | --- |
| `0x080165E8` | SysTick handler. **Hook confirmed** |
| `0x080214A0` | Heater / PID control. **Confirmed by emulation to be called every cycle from the main loop** |
| `0x0801EAA4` | Input / sleep processing |
| `0x08022098` | Tip determination |
| `0x080199A8` | ADC classification |
| `0x08022944` | Drawing |
| `0x0801D638` | Factory inspection mode |
| `0x0801759C` | UID matching |
| `0x08011EC8` | ADC sample acquisition (r0 = channel) |
| `0x080140B8` | GPIO BRR (drive pin Low) |
| `0x080140BC` | GPIO BSRR (drive pin High) |
| `0x0801E99C` | USART1 putchar (r4 to DR at `0x0801E9AE`) |
| `0x0801EE10` | Temperature calculation (r0 = ironType). Called via function table +4 at `0x200003F4`. See ch. 10 |
| `0x08015494` | PID (r0 = current temperature). Reads target from `[0x200001F4]`. Called via the function pointer at `0x20000234` |
| `0x0801EB58` | Stand / sleep state machine. Rewrites `0x2000023C` (operating mode) |
| `0x08011DC8` | ADC1_2 interrupt (vector 34). Manages the measurement window while the heater is off |
| `0x08012218` | **Beep `beep(pattern)`**. +4 of the buzzer object `0x20000330`. See ch. 10 |
| `0x08013A40` | Authentication result handling (r0: 0=`Demo Mode` stop, 1=record time and return (normal), 2=Demo 3s→3, 3=`Not e-Design Product!` stop) |
| `0x0801424C` | `get_ms()` = `[0x200003BC]` |
| `0x08023644` | Settings load (16-bit copy from flash, no validation) |
| `0x080193B8` | Thermocouple processing (does not read the ADC internally) |
| `0x08016DFC` | `TIMx->CCR3 = r1` (offset `0x3C`) |
| `0x08014B70` | Buzzer playback engine. TIM4 (`0x40000800`) CH3 PWM |
| `0x0802A7DC` | Volume table `0,50,100,150,200,250` (u16). Indexed by `BeepVolume` |
| `0x08014834` | String drawing (core) |
| `0x080108B4` | sprintf equivalent |
| `0x08014DA4` | Rectangle drawing etc. |
| `0x0801FDDC` | The 'P' of the string `POW`. **Confirmed reflected on screen by a 1-byte rewrite** |
| `0x08031394` | Display label `LowVol` (in the multilingual table). **Confirmed a rewrite is reflected** |
| `0x0802A688` | Settings key name `LowVolProtect` (for `TS1M.TXT`, not shown on screen) |
| `0x200003BC` | Millisecond counter |
| `0x2000023C` | Operating mode (u8). 1=working, 2=sleep, 0/3=heater stopped (emulation) |
| `0x2000023E` | **Current temperature** (s16, 0.1℃). See ch. 10 (emulation) |
| `0x200001F4` | **PID target temperature** (s16, 0.1℃). The heater control rewrites it from the setting every cycle (emulation) |
| `0x20000330` | Buzzer object. See ch. 10 (emulation) |
| `0x200015A4` | **Head of the settings struct** (not a pointer. All 99 accesses are offset-based) |
| `0x200002F0` | Destination of the settings load |

### Inferences that were wrong (kept for the record)

| Address | Former inference | Actual |
| --- | --- | --- |
| `0x0802796C` / `0x08027960` | Beep function | Sets the drawing position. Always calls a drawing function immediately before, passing coordinate values |
| `0x20000244` | Target temperature | Refuted on the actual device. Stays 0 even after full boot |
| `0x20000914` | Current temperature | Refuted on the actual device. Actually the calibration-mode (CAL) flag (ch. 12) |
| `0x200002F0` | Drawing struct | Load destination of the settings |
| `0x0802AFC4` | 669 B of free padding | Interior of a 1bpp bitmap (`0x0802AF92`, 760 B) |
| `0x08013A40` | `Demo Mode` display + stop | Authentication result handling that branches on r0. r0=1 is the normal path and can be called every cycle |

### Undetermined

- (ch13 = supply voltage, ch0 = external thermocouple TK, now known. ch. 12)

RAM literal references number 189. Main clusters:
`0x20000000`-`0x200001F4`, `0x200002D0`-`0x20000461`, `0x200007FC`-`0x2000099C`, `0x20000A9C`-`0x200011A4`

## 4. Settings items (33 total)

The `TS1M.TXT` key name table starts at `0x0802A4E6` at 22-byte intervals. Each entry is name (16B) + initial/min/max (u16×3).

The base of the settings struct is `0x200015A4`, and **this is not a pointer but the head of the struct itself**
(all 99 places in the code are offset-based access, zero pointer dereferences).
The field offset is `0x10 + 22*index`, so absolute addresses are obtained by simple addition.
Verified by `BoostTemp`'s `0x68` matching the actual code's `[r2, #0x68]`.

Main addresses: WorkTemp1 `0x200015CA` / WorkTemp2 `0x200015E0` / WorkTemp3 `0x200015F6` /
BoostTemp `0x2000160C` / SleepTemp `0x200016A6` / StepVal `0x200016E8` / BeepVolume `0x2000172A`

The settings body is `TS1M.TXT` on the external SPI flash, parsed at startup into the struct (ch. 12). Until then it is zero.

| # | Key | offset |
| --- | --- | --- |
| 0 | DefaultTemp | `0x10` |
| 1 | WorkTemp1 | `0x26` |
| 2 | WorkTemp2 | `0x3C` |
| 3 | WorkTemp3 | `0x52` |
| 4 | BoostTemp | `0x68` |
| 5-10 | colorEffect / colorMode / colorBright / colorR / colorG / colorB | |
| 11 | SleepTemp | |
| 12 | SleepTime | |
| 13 | IdelTime | |
| 14 | StepVal | |
| 15 | BackLight | |
| 16 | TempType | |
| 17 | BeepVolume | `0x186` |
| 18 | Language | `0x19C` |
| 19 | LowVolProtect | |
| 20 | FlipOver | |
| 21 | user_UI | |
| 22-26 | MaxPow_245 / _210 / _115 / _100 / _80P | |
| 27-31 | CalibraVal_245 / _210 / _115 / _100 / _80P | |
| 32 | TS1M_APP | `0x2D0` |

The temperature range is 100.0–450.0℃ (internal value inferred to be in 0.1℃ units). `MaxPow_245` is 30–200W (initial value 140W). `LowVolProtect` is 6–21V.

## 5. Tip determination logic

### Type code (`ironType` = `0x200003D3`)

| Value | Tip |
| --- | --- |
| 0 | None |
| 1 | 245 |
| 2 | 210 |
| 3 | 115 |
| 4 | TS80P (**unreachable in this version**) |
| 5 | H100 |
| 0x0F / 0xF0 / 0xFF | Intermediate states during determination |

The display name table is at and after `0x0805F568` (`(empty)`, `H100`, `210`, `115`, `245`, `TS80P`).

### Determination flow (function `0x080199A8`)

An analog switch switches paths, and the ADC is measured at 2 points each (average of 10 each).
Classify with `A = adc2-adc1`, `B = adc4-adc3`:

| UART | Code | Condition |
| --- | --- | --- |
| None | `0x01` (245) | A=420–1000, B<50, re-measured ADC ≤ 3800 |
| None | `0xF0` (210/115) | A=40–1000, B=50–260 |
| None | `0xFF` | B ≥ 270 |
| Yes | `0x0F`→`5` (H100) | B=50–440 and UART frame[2]==1 |
| — | `0` | Anything else |

- The same determination **continuing for 1000ms** confirms it
- If UART drops out for 200ms, it immediately reverts to `0` and drops GPIOD Pin4
- **210 and 115 are not distinguished electrically.** Selected manually with the menu `TipTyp` (cursor 0→210, 1→115)
- `0xFF` is not eligible for manual selection = not heated

The emulator (Unicorn) swept A=0–1300 × B=0–4000 exhaustively to confirm the table above. There are 11 places in all that write `ironType`, but **no path writes the value 4**.

### Safety checks after confirmation

- Resistance check (`0x08022490`): if out of range (1000–12000) the determination is canceled and heating stopped. Log `ResValTmp`
- Overheat protection (`hot_ADC`): 245=3700, 210=3000, 115=2900, TS80P=4000, H100=2800
- Power coefficient: 245/210/TS80P=0.30, 115=0.43, H100=0.03

### Sleep detection

- Analog handle: **GPIOC Pin6 only**. Low continuing for 500ms → sleep, High recovery → working state
- H100: uses UART frame[8] (motion detection). No-motion is judged after `SleepTime` seconds
- There is **no input** for a handle-side switch

## 6. Hidden features / unused code

### Factory inspection mode (checkMode)

At startup it reads the factory calibration record at `0x08077D00` (u16×10 + a total checksum), and **if it is corrupt it automatically enters factory mode**. It cannot be entered by button operation.

Inspection items: accelerometer, current/relay, AD zero point and resistance for 210/80/245, thermocouple, stand detection, current/voltage calibration.
A jig handshake on UART2 (`A1 1A` → `A2 2A` → `A5 5A`).

### Unused code

- FatFs test function (writes "Hello, World!" to `M0.TXT`). Only a pointer remains in the settings object's function table (+0x10), but it is not called (see ch. 2)
- Processing-time measurement printf (`1111111111:%dms` etc.), and many PID/ADC debug outputs remain

### Hidden items in the settings menu

The settings menu is a ring of item objects (`0x20000594`, 28 bytes each, +20 = previous, +24 = next),
where the item number matches the settings number. **Items 4–10 are not linked into the ring and do not appear in the menu** (confirmed by emulation).
By relinking the ring and drawing to the virtual panel, they displayed as follows:

| Item | Menu display | Settings key | Displayed initial value |
| --- | --- | --- | --- |
| 4 | `QkTmp` | BoostTemp | 400℃ |
| 5 | `RGB FX` | colorEffect | Star |
| 6 | `RGB Mode` | colorMode | Man. |
| 7 | `RGB Bright` | colorBright | -- |
| 8–10 | `Red` / `Green` / `Blue` | colorR / colorG / colorB | 255 |

- The multilingual table has effect names `1 Star (流星)`, `2 Breath (呼吸)`, and modes `Auto` / `Man.`
- The RGB values are read only for menu display/editing and TS1M.TXT read/write (confirmed by a full code sweep with `skipdata`).
  **No processing that drives an LED is found.** It is thought to be a remnant of another model (or a plan) that has RGB lighting
- BoostTemp (QkTmp) is removed from the menu but is used in the heating-screen key handling. On key event 3, it saves the current target and
  switches to BoostTemp (SET display and operation sound); on event 5 it restores it (`0x08020534` / `0x0802054C`). Thought to be the device's manual boost feature.
  Which button operation corresponds to event 3 / 5 is not verified. The value can be changed via `BoostTemp` in TS1M.TXT

### Authentication / protection

- At startup it reads the chip UID (`0x1FFFF7E8`, 12B) and matches it against the saved value at `0x0800FFF0`. Each 32-bit word is in a form XORed with `0x0800FFF0`. On mismatch it shows `Demo Mode` and stops
- If external device authentication fails it shows `Not e-Design Product!` and stops. The decision material is on the bootloader side (it calls `0x08002001` directly)

## 7. About DFU programming

**Programming method**: put the device into USB mass storage and copy the HEX.

### What was confirmed

- **Writes are reflected in both the code region and the string region**
  - A 1-byte rewrite of `0x0801FDDC` (the string `POW` within the code region) was reflected on screen
  - A 1-byte rewrite of `0x08031394` (the multilingual label `LowVol`) was reflected on screen
- A version with a hook installed on SysTick could reproduce a boot-logo freeze, so **patches to the code region are executed too**
- The file name is irrelevant (behavior is the same whether left as `V202` or renamed to `V203`)

### Undetermined: the effect of HEX line structure

Early in the analysis, there was an observation that a patch that re-serialized the whole HEX in address order (line count 26438 → 26456) was not reflected,
whereas a version that replaced only the relevant lines of the original file (line count preserved) was reflected.
From this we thought "the line structure must be preserved", but **this causal relationship was not confirmed.**

Reason: the two also differed in their change content (the former was a detour + hook, the latter a 1-byte string).
Also, later verification found that many versions judged "not reflected" at the time actually had a working hook, and
**nothing happened simply because the beep function and temperature variable inferences were wrong**.
In other words, the re-serialized version too may have written successfully.

**Resolution (ch. 14)**: the bootloader's HEX inspection pass (`0x08003064`) rejects line-length mismatch of `13 + 2·LL` (err 0x02) and missing CR+LF (err 0x07).
So **the line structure does affect whether writing succeeds**. If re-serialization changes line length, record splitting, or line endings, CHECK rejects it.
The "rewrite only the relevant lines of the original HEX, preserving line count and structure" method is correct.

### Font subset

The font holds only the characters that appear in the UI. Changing `P` of `POW` to `Q` produced no glyph, drew a blank, and displayed
`OW:`. When verifying by string rewrite, **use characters that appear in the original UI**.

## 8. Verified patch methods

The method of replacing the first 4 bytes of the SysTick handler (`0x080165E8`) with `b.w <hook>` and placing the hook body elsewhere
was **confirmed working on the actual device** (an infinite loop inside the hook froze at the boot logo).

However, `0x0802AFC4` where the hook body was placed at the time was not free space but the interior of a bitmap (see ch. 2).
The code runs so it does not affect the determination, but part of the screen must have been corrupted.
Going forward, use `0x0802849C` (the unused FatFs test function).

The heater control function `0x080214A0` was also confirmed by emulation to be called every cycle from the main loop.
The earlier modification that hooked it did not work not because of the hook target but **because the beep function and temperature variable inferences were wrong**.

## 9. Status of open items

### Unresolved (needs verification on the actual device or additional analysis)

1. In Fahrenheit mode CalibraVal is converted as an absolute temperature (ch. 12). Needs verification on the actual device
2. The reason TS80P is disabled (ch. 12. The code remains but is unreachable)
3. Whether the external thermocouple (TK) has cold-junction compensation. The TK value did not change even when ch12 was moved
4. The content of UI page 3 (part of the graph screen could be drawn, but the whole was not verified because emulation is slow)
5. The exact part number of the IDChip (1-Wire EEPROM on PB11) and the tip (iron) update protocol (USART2, only an outline in ch. 14)
6. The purpose for which the bootloader side drives PC4 as an output (the app side is confirmed as ADC1_IN14. The physical net is the app's ADC sense, ch. 15)
7. What UART4 (PC11) was originally meant to receive (9600 RX only but no ISR, unused. Possibly a remnant for another variant/dock, ch. 15)
8. RGB lighting: settings have colorR/G/B, FX, Mode, Bright, yet **no code that drives an RGB LED is found** (ch. 6). Whether an LED is mounted, and which pin/driver, is also unknown (separate from backlight PA8/TIM1)
9. The utilization logic for the accelerometer (LIS3DH family, I2C 0x19). The bus/register settings are known, but **which axis/threshold FlipOver / motion detection uses for sleep wake or flip determination** is not traced
10. Whether the app uses USB. USB (`0x40005C00`) is clocked in the app too, but whether the app exposes a USB drive/communication is not verified (DFU is bootloader only)
11. The intended purpose of PB10 (output) and PB1 / TIM3_CH4 (100kHz PWM but always 0%), and the actual use of PC1 (ADC IN11, current sense?) — all configured only, purpose undetermined (ch. 15)
12. Details of each inspection item and pins used (current/relay sense etc.) in the factory inspection mode (checkMode) (ch. 6)
13. The entire frame of the USART2 link during normal operation of the tip (H100) (the motion-detection byte[8], type byte[2], etc. are known, ch. 5)
14. When and what the write to the IDChip (bootloader mode 7 / `IDChip Save Err`) writes to the 1-Wire EEPROM (ch. 14-15)

### Resolved (record)

- **The HEX line structure affects whether writing succeeds** → it does. The bootloader's inspection pass rejects line length/line endings (ch. 7, 14)
- **Heater drive pin** → **PA1 (TIM5_CH2)**. A single-element scheme that turns ON at end of measurement / OFF at start of measurement (ch. 15)
- **PC4 / PC5** → PC5 = LCD RESET, PC4 = ADC1_IN14 (injected-conversion tip sense) (ch. 15)
- **Input sense PA15 / PC7 / PC9** → all configured only and never read = unused. The only read input is PC6 (button) (ch. 15)
- **IDChip / accelerometer bus** → IDChip = 1-Wire EEPROM on PB11 (DS2431/DS28E07 family), PD5/PD6 = accelerometer I2C (address 0x19, LIS3DH family). Authentication turned out to be a **keyed anti-clone** that collates the ROM/memory against a UID-derived record. A full forgery analysis is not done, since cloning is not the goal (ch. 15, `bl_emu.py`)
- **Settings volume and DFU volume** → **separate FAT regions** on the same SPI flash (settings = base `0x000000`, DFU = base `0x200000`). They do not erase each other and both persist (ch. 12, 14, measured empirically in emulation)

### Means of extracting information

Since SWD is unavailable, the internal state of the actual device cannot be read directly. Current means:

- **Emulation** (`ts1m_emu.py`) — the UART debug output can be captured wholesale. The most information-rich. GPIO/timer/UART state can also be read (`pin_mode` / `heater_on` / `uart_tx` etc., ch. 15), and `sim_measure` can run measurement/heating cycles to observe the heater
- **Bootloader emulation** (`bl_emu.py`) — runs the dumped bootloader function by function. Models the accelerometer I2C and IDChip 1-Wire to observe the authentication bus communication (ch. 15)
- **Freeze determination** — a hook loops infinitely when a condition holds, and whether it freezes gives a 1-bit determination.
  Confirmed to reliably work on the actual device (freeze at the boot logo)
- 1-byte string rewrite — confirmed by on-screen display. Confirmed to be reflected
- **On-screen display patch** — replaces the tip name label (`drawString` at `0x080201CC`) in the top-left of the home screen to display any value (chipid / findmark / dumpboot)
- **Extraction via the DFU drive** — overwrite the actual data of a file placed on the DFU drive from the app, and copy that file on the PC. Data can be extracted in 64KB units (ch. 13)

## 10. Temperature control and buzzer

Content identified by emulation (ramping measurement points one at a time) and static analysis.
Backed up by the patch of ch. 11 using these working on the actual device (2026-09-27).
All temperature units are 0.1℃ (setting WorkTemp1 initial value 3000 = 300.0℃).

### Variables

| Address | Type | Content | Basis |
| --- | --- | --- | --- |
| `0x2000023C` | u8 | Operating mode. 1=working, 2=sleep, 0/3=heater stopped | Heater control branch. 1 → target=WorkTemp, 2 → target=SleepTemp |
| `0x2000023E` | s16 | **Current temperature** | Ramping adc3 alone makes it follow monotonically (606→1485, 3431→6037). ch12 acts in reverse as cold-junction compensation. ch13 / ch0 are irrelevant |
| `0x200001F4` | s16 | **PID target temperature** | Confirmed to become WorkTemp1 (3000) in working mode. PID `0x08015494` reads here |
| `0x200001F6` / `0x200001F8` | s16 | Copy of current temperature written by PID / error (target-current) | PID disassembly |
| `0x200001D0` | u16 | Heater output (PID output, upper limit 290) | PID and ADC interrupt |
| `0x200001D2` | u8 | Heater control state. 0=measure+PID, 1=waiting for ADC interrupt, 2=stopped | Heater control disassembly |
| `0x20000402` | u16 | Tip ADC raw value (ch10, mux 000). The comparison target for overheat protection `hot_ADC` | Same as above |
| `0x200003FC` / `0x20000400` | u16 / s16 | ch12 (NTC) raw value / cold-junction temperature | Follows when ch12 is ramped alone |
| `0x20000930` | u16×5 | Per-tip temperature calibration coefficient (/1000). Saved to flash `0x08077C00` | Calibration processing `0x0801990A` |

### Processing flow

The heater control `0x080214A0` runs each cycle as follows.

1. Depending on the operating mode, **rewrite the target temperature `[0x200001F4]` from the setting**
   (working: WorkTemp1/2/3 selected by `0x200002E8`, sleep: SleepTemp, stopped: 0)
2. If state is 0, call temperature calculation `0x0801EE10` and update the current temperature `[0x2000023E]`
3. `0x08021620`: with `r0 = [0x2000023E]`, call PID (`[0x20000234]` = `0x08015495`)
4. PID decides the output `[0x200001D0]` with `[0x200001F4]` as the target
5. Overheat protection (comparison of `[0x20000402]` and `hot_ADC`) is done independently of PID

Since the target temperature is rewritten every cycle, if you rewrite it only just before the PID call, it affects only that cycle's control.

The display uses a moving average (`0x20009DD4`, `0x0801EE00` = total/count), and shows the target value if within ±1.8℃ of the target.

### Buzzer

Buzzer object `0x20000330` (a function table expanded into RAM at runtime. Content confirmed by emulation):

| Offset | Content |
| --- | --- |
| +0x00 | Init `0x08021E81` |
| +0x04 | **`beep(pattern)` `0x08012219`** |
| +0x08 | tick `0x08014B71` (called every time from the end of SysTick) |
| +0x0C | u8 remaining count |
| +0x10 | Pointer to the pattern being played |
| +0x14–+0x20 | 4 built-in patterns |

`beep()` only registers `count = pattern[0]` and the pattern if not currently playing.
A pattern is a u16 array whose first low byte is the count, followed by (ON ms, OFF ms) pairs.

| Pattern | Content | Use |
| --- | --- | --- |
| `0x0802A7B0` | 1 time 300ms | |
| `0x0802A7B6` | 5 times 200/200ms | |
| `0x0802A7CC` | 2 times 80ms | |
| `0x0802A7D6` | 1 time 20ms | Operation sound. Also sounded on stand / sleep transitions |

How to sound it: `r0 = pattern; ldr r3, =0x20000330; ldr r3, [r3, #4]; blx r3`

Writing directly to CCR3 did not sound it because tick writes `CCR3 = 0` every millisecond when not playing.
The volume indexes the volume table by `BeepVolume`, so with BeepVolume=0 it does not sound.

## 11. Modification patch: target-temperature reached notification and auto boost

Implemented in `patches/`. **Confirmed working on the actual device** (2026-09-27). Also confirmed in emulation.

```
python3 patches/build.py              # -> TS1M_Master_APP_V202_EN_notify_boost.hex / .bin
python3 patches/test_notify_boost.py  # scenario test in the emulator (a few minutes)
python3 patches/screenshot.py         # draw the heating screen to the virtual panel (normal / during boost)
```

### Mechanism

- Replace `ldr r1,[r6,#64]; blx r1` (4 bytes) at `0x08021624` with `bl hook`
- The hook (`0x0802849C`, 332 bytes) performs the original PID call on its behalf
- During boost, it raises the target temperature only during that PID call, and restores the original value on return.
  The target temperature seen by the display and other processing always stays the setting value
- State is placed at the tail of the unused heap `0x2000BFA8` (24 bytes) with a magic value
- For the boost display, two places that decide the text color of the large current-temperature digits are replaced with `bl` (below)
- Does not touch the heater control or safety-device (overheat protection, resistance check) code

### Behavior

| Feature | Condition |
| --- | --- |
| Reached notification | After the set temperature changes or working mode is entered, on first entering ±3.0℃, beep twice (`0x0802A7CC`) |
| Boost start | After reaching once, if a state 10.0℃ or more below continues for 300ms, target +20.0℃ |
| Boost cap | 450.0℃. If the setting is 450.0℃ or more, do nothing (never lowers the target) |
| Boost end | When it recovers to the set temperature. If it continues for 20 seconds, abort, and do not restart until it recovers |
| Disabled | Outside working mode (sleep etc.), calibration mode (CAL), target 0 |
| Boost display | While the target is actually raised, make the large current-temperature digits on the heating screen red (`0xF800`) from white |

Parameters can be changed via the `.equ` at the top of `patches/notify_boost.S` (display color is `BOOST_COLOR`, RGB565).

### Boost display

The heating screen (UI page 1) draws the current temperature with `drawString(str, x, y, font, fg, bg, flag)`
(`0x08014834`, fg/bg passed on the stack as RGB565). The places that decide the text color:

| Replacement site | Original instruction | Display style | After replacement |
| --- | --- | --- | --- |
| `0x0801FA28` | `movw r1, #0xFFFF` (r1 → fg) | Normal (with graph) | `bl temp_color_a` (r1 = white / red) |
| `0x0801B21A` | `str.w r9, [sp]` (r9 = `0xFFFF`) | 7-seg style (`[0x20000455] != 0`) | `bl temp_color_b` ([sp] = white / red) |

Both just look at the "target raised in this pass" flag (state +14) that the hook writes on each PID call.
It does not touch the branch (`0x0801F990`) that temporarily displays the setting value in orange (`0xFC0A`) right after a temperature change.

Results confirmed on the virtual panel (EMULATION.md):
- Normal style: the digits are red only during boost, otherwise stay white
- 7-seg style: the digits blend with the gray background, so they look pinkish rather than red. Normally stays white
- The setting that selects the 7-seg style was not `user_UI` (it stays normal style even with `user_UI=1`).
  We set `0x20000455` directly to draw it. Which menu item switches this is not verified

### Confirmation results in emulation

Scenarios of `patches/test_notify_boost.py` (move the tip ADC as scripted, and record the target passed to PID and beep calls):

- Heating 150→300℃: notified only once at 297.0℃
- The display flag is 1 only on the pass that passed the raised target to PID
- Drop to 285℃ and hold: from 300ms on, the PID target is 3200, and reverts to 3000 after the call. Aborts at 20 seconds, does not restart until recovery
- Drop again after recovery: boosts again
- A drop of less than 300ms: no boost
- Setting 440℃: boost caps at 450.0℃. Setting 450℃: the target does not change
- During sleep: neither notification nor boost. On returning to work, notifies again

Running the same test on the original firmware fails the notification/boost items (confirmation that the test is effective).

### Confirmation on the actual device

On 2026-09-27 the patch's operation was confirmed on the actual device.

Even when re-programming with changed parameters, keep the ability to write the original firmware back.

## 12. Settings file / external flash / calibration mode

Content examined by emulation and static analysis. **Not verified on the actual device** (except the chip ID).

### External SPI flash (W25Q64)

The settings file `TS1M.TXT` is on a FatFs volume, whose actual location is the **external SPI flash**:

| Item | Content |
| --- | --- |
| Bus | SPI2 (`0x40003800`), CS = PB12 (selected Low, `0x08017E7C`) |
| Device | Requires the response `0xEF16` to `0x90` (Manufacturer/Device ID) = Winbond W25Q64 (8MB) |
| Byte transfer | `0x08017F50(byte, 8)`, transmit-only continuous transfer `0x08017FB0(buf, len, 8)` |
| Volume | Label `TS1M`. If mounting fails, recreate with `f_mkfs` |

The files placed on the USB mass storage in DFU mode are also on the same SPI flash, but in a **separate FAT region from the settings volume** (app settings = base `0x000000`, DFU = base `0x200000`). Measured empirically in ch. 14 "Storage layout". So the DFU content and the settings do not erase each other, and both persist.

### Loading / saving settings

Settings object `0x200002D0` (a function table expanded into RAM at runtime):

| Offset | Function | Content |
| --- | --- | --- |
| +0x00 | `0x08022F61` | Load at startup |
| +0x04 | `0x08012855` | Settings struct → text |
| +0x08 | `0x0801267D` | Text → settings struct (parse) |
| +0x0C | `0x08015B45` | Save (overwrite `0:TS1M.TXT`) |
| +0x10 | `0x0802849D` | FatFs test function (unused. The patch's placement site) |
| +0x14 | `0x08028463` | ℃⇔℉ conversion `conv(to_f, value)` |
| +0x18 | (data) | Selected WorkTemp number (`0x200002E8`) |

Startup flow (`0x08022F60`): mount → (if failed, f_mkfs) → set label → read `0:TS1M.TXT` (max 1536 bytes) →
parse. If parsing fails it shows `Data Invalid`, copies the initial-value table `0x0802A4E6` into the settings struct, and rewrites the file. On success, `Data Valid`.

### Format of TS1M.TXT

The file the firmware writes out (generated in emulation):

```
DefaultTemp = 1  #(1~3)
WorkTemp1 = 300  #(C:100~450 F:212~842)
...
TempType  = 0  #(0:C~1:F)
user_UI = 0  #(0:Wave~1:Digit)
CalibraVal_245 = 0  #(C:-60~20  F:-76~68)
```

- There is a comment line (`/*****...`) at the head, and it finds each key with `memmem` and reads the numeric value after `=`
- Temperatures (WorkTemp1-3, BoostTemp, SleepTemp, CalibraVal) and StepVal are **in degree units in the file, ×10 internally**
- If any value is out of range, parsing fails → all settings revert to initial values
- When `TempType=1`, the ranges (min/max) are also converted to ℉ before checking
- `user_UI` is the display style: 0 = Wave (with graph), 1 = Digit (7-seg style, reflected in `0x20000455`)

**A likely firmware bug**: the load side passes the number of bytes read by f_read (a value) as the parse function's 3rd argument, but
the parse function treats it as a pointer and uses `*(u16 *)br` as the search length. That is, the search length is determined by the content of the bootloader region
at the address `br` (the file size, around 1300). If that value is smaller than the key positions (up to about 1300 bytes), even a correct file always becomes
`Data Invalid` and settings revert to initial values. The emulator places `0x0606` there.

**Verified on the actual device (2026-09-27)**: settings were retained even after changing WorkTmp1 and rebooting. On this individual unit and this file size,
the value in the bootloader region is large enough and parsing works normally. However, since `br` is the file size, if the number of digits of a value changes and
the file length changes, the referenced address also changes. The possibility of failure in that case theoretically remains.

### Fahrenheit mode

- In `TempType=1`, the settings/target/current temperatures all become ℉×10 (confirmed WorkTemp1 = 5720 = 572.0℉)
- When switching units in the menu, the values and ranges of SleepTemp / WorkTemp1-3 / BoostTemp are converted. **CalibraVal is not converted**
- The calibration coefficient is `tempCalRatio = (3500 − CalibraVal) / 3500` (`0x08021C28`). In ℉ mode it uses CalibraVal **as an absolute temperature**,
  converting it ℉→℃ first, so CalibraVal = 0 is treated as 0℉ = −17.8℃ and the coefficient becomes 1.0486
- As a result, in ℉ mode with CalibraVal left at 0, **the displayed temperature comes out about 4.9% high (about +15℃ at 300℃), and the actual tip may be lower than the setting**.
  To make the correction 0 in ℉ mode, CalibraVal must be 32 (32℉ → 0℃). **Not verified on the actual device**
- The notification/boost patch compares in internal units, so in ℉ mode it operates as ±3.0℉ / 10.0℉ / +20.0℉.
  The cap `TMAX = 4500` also becomes 450.0℉ (232℃), so at normal set temperatures (450℉ or more) boost does not act (on the safe side)

### Calibration mode (CAL) and external thermocouple (TK)

What was previously called "thermocouple mode", `[0x20000914] != 0`, was **an auto-calibration mode using an external thermocouple**.

| Item | Content |
| --- | --- |
| Entry | Key operation (`0x08020754`) when in Digit display (`0x20000455 != 0`) on the heating screen. The `CAL` / `TK:…℃` on screen |
| Target temperature | Fixed at 300.0℃ (`0x2000090E`) |
| Processing | `0x080193B8`. When the current temperature enters 298.0–302.0℃ and stabilizes, it collects the external thermocouple value every 500ms up to 100 times, and updates the calibration coefficient (`0x20000930`) from the average |
| External thermocouple | **ADC ch0**. `0x200008A4` ≒ ch0 × 1.17 (inferred to be in 0.1℃ units). Accepted range 250.0–380.0 |

### ADC channel usage (summary)

| Channel | Use | Confirmation method |
| --- | --- | --- |
| ch10 (mux 000) | Tip thermocouple | Current temperature follows when ramped alone |
| ch12 | Cold junction (NTC) | Cold-junction temperature follows in reverse when ramped alone |
| ch13 | Supply voltage (×7.77×cal/1000 mV) | `0x2000039C` follows when ramped alone |
| ch0 | External thermocouple (TK) | `0x200008A4` follows when ramped alone |
| ch4 | TS80P temperature (disabled path) | Static analysis |

### TS80P

The code for TS80P (`ironType=4`) remains: it switches PA3's input mode before/after measurement (`0x08028BA8` / `0x08028BD4`),
finds the temperature with ADC ch4 and a dedicated 158-point table (`0x0802AA56`), and defines the overheat threshold 4000 and power coefficient 0.30.
However, tip determination does not return 4 and the manual selection menu is only 210/115, so it is unreachable. The reason for disabling it cannot be told from the firmware.

## 13. DFU drive and dumping the bootloader

Performed on the actual device 2026-09-27.

### Location of files on the DFU drive

We copied `MARK.BIN` (a 16-byte line `TS1M-FINDVOL-MK\n` repeated to fill 64KB) to the USB mass storage in DFU mode,
and with the app-side patch `findmark` searched the 8MB external SPI flash (W25Q64) at every 512-byte boundary.

- The result was `247000:80`. **128 sectors (64KB) were found contiguous starting from `0x247000`.**
- That is, the file on the DFU drive is placed on the external SPI flash and can be read/written from the app too
- Even after subsequently writing a HEX via DFU, the location of MARK.BIN did not change (it was in the same place when findmark → dumpboot were written in succession)

### Dumping the bootloader

With the patch `dumpboot`, we overwrote the internal flash `0x08000000`–`0x0800FFFF` with SPI flash `0x247000`–`0x256FFF` (the actual data of MARK.BIN)
(erase/write and read back to verify only when all 64KB are markers). After `OK` appeared on screen, we copied MARK.BIN in DFU mode and
obtained `bootloader.bin` (64KB). **It contains the per-device key so it is not put in the repository** (`*.bin` is in .gitignore).

Verification of content:

| Item | Content |
| --- | --- |
| Vector table | SP=`0x20004F88`, Reset=`0x08000229`. All handlers are `0x0800xxxx` Thumb addresses |
| Reset handler | Keil boilerplate (`SystemInit` → `__main`). Same toolchain as the app |
| Used range | `0x0000`-`0xAD0C` is code/data, zero beyond. Per-device key at `0xFFF0` (12 bytes + 4 zero bytes) |
| Strings (DFU) | `DFU Mode`, `TS1M_DFU` (thought to be the volume label), `Check the valid of the hex file:`, `Update the app:`, `Update the digital iron:`, `Completed %d%%`, `Check err:0x%02x`, `Update err:0x%02x` |
| Strings (auth) | `Demo Mode`, `Not e-Design Product!`, `IDChip Err`, `IDChip Save Err`, `Init Err`, `Init OK` |
| Strings (FAT) | `FAT32`, `MSDOS5.0`, `NO NAME`, `USB Special Disk` |

Since there is `Update the digital iron:`, the bootloader is thought to handle updating the tip side (external device) too.
From the `IDChip`-family strings, the external device authentication of `Not e-Design Product!` is thought to use an authentication chip (IDChip).
Both bootloader analyses are still to come.

## 14. Bootloader analysis

We examined `bootloader.bin` (internal flash `0x08000000`-`0x0800FFFF`) dumped in ch. 13 by static analysis + Unicorn emulation. All addresses are absolute. "Confirmed" = backed by code reading or emulation, "inferred" = otherwise.

### Boot sequence

`Reset_Handler` (`0x08000228`) → Keil `__main` (`0x08000120`, scatter-load expansion) → `main` (`0x080081F8`).

`main` flow: SysTick setup → read product variant (`0x08008814`) → peripheral init (clock `0x080026C4`, GPIO `0x080018CC`, display `0x08005D60`, IWDG `0x08003632`, SPI2 flash `0x08002918`, USART2 iron `0x08004824`) → boot decision → anti-clone check `0x08003D64(0)` → if normal, stop SysTick (`0x08007010`) then jump to the app; if a DFU condition, into the DFU loop.

### Boot decision (DFU or app)

| Condition | Decision |
| --- | --- |
| **PC8 is Low** (`GPIOC_IDR` bit8, `0x08008260`) | Force DFU mode (thought to be a button/strap. Polarity inferred, bit test confirmed) |
| **The app's initial SP is outside SRAM** (`*(0x08010000) & 0x2FFF0000 != 0x20000000`) | Treat the app as invalid and go to DFU (confirmed) |

App validation is **only the single point of whether SP is in the SRAM range**; there is no range check of the reset vector, nor an image CRC. The jump sets the app SP into `MSP` and does `blx` to `*(0x08010004)` (= `0x0801085D`) (`0x0800388C`→`0x0800651C`→`0x0800021A`). **VTOR is not set by the bootloader**; the app side sets it (as in ch. 2, `0x0801520A`).

### Anti-clone / per-device authentication

Two layers (both confirmed):

- **Store A — per-device key `0x0800FFF0` (16B, 12B used)**: the chip UID (`0x1FFFF7E8`, 96 bits) stored XOR-obfuscated with its own address value `0x0800FFF0` as the key. At startup it is matched against the raw UID (`0x080056A4`). **Copying to different silicon (a different UID) causes a mismatch**, and this is the core of the anti-clone.
- **Store B — signed 38B record `0x08002A00`**: a fixed block common to the model (not a per-device secret). The trailing 4B is the CRC32 of the 34B body. It contains a model/serial, an expected-UID field, and two version strings (both `"1.00"`).

`0x08003D64` accumulates 4 status bits in RAM `0x200000C0`: bit0 = record CRC/range OK, bit1 = derived-structure CRC OK, bit2 = in-record UID == raw UID, bit3 = sub-field match. It passes with all bits + Store A match. On failure it shows `Not e-Design Product!` at `0x08001104` and stops (on clone/tamper).

`IDChip` (`IDChip Err` / `IDChip Save Err`) is an **authentication EEPROM on the 1-Wire bus of PB11** (DS2431 / DS28E07 family). Confirmed with the bootloader emulator (`bl_emu.py`) (ch. 15 "Bootloader emulator and IDChip").

The crypto is not real; it is a combination of single-byte XOR obfuscation (`0x08001226` / `0x0800124A`) and a "largest prime less than n" generator for constant hiding (`0x08001C18`).

### App → bootloader service calls

The app calls the bootloader's `0x08002000` during execution as `service(r0=0..3, out, in, ...)` (the app's `0x0801419C` holds `0x08002001`):

| service | Function | Content |
| --- | --- | --- |
| 0 | `0x08001DE2` | Verify record → return version string A (`"1.00"`) |
| 1 | `0x08001E7A` | Same → version string B (`"1.00"`) |
| 2 | `0x08001B44` | Halfword reference into the settings/calibration table (inferred) |
| 3 | `0x08000B24` | Serial/HEX string processing (inferred) |

The app stopping at `Not e-Design Product!` is the reaction when this verification (service 0/1) does not pass.

### DFU mode

`0x0800380C` handles the volume `TS1M_DFU`. On mount failure it recreates with `f_mkfs` (`0x080071A8`) and sets the label (`0x080089BE`). The state machine at `0x0800331C` (RAM `0x20000098`) loops:

| State | Content |
| --- | --- |
| 0 | Idle. **When USB writing stops and times out, move to the next** (no eject needed, edge-driven) |
| 1 (`0x08003220`) | Search for the update file. `f_readdir` the root and **select a file with extension `.hex`** (file name is arbitrary) |
| 2 (`0x08002ECC`) | CHECK pass. On success, look at the top-level ELA to decide the target (≥ `0x09000000` → tip, otherwise → app). `Check completed.` |
| 3 (`0x0800314C`) | PROGRAM pass. `Update completed.` / on failure `Update err` |
| 5 | Cleanup. Reset the ELA tracker, reconnect USB to make the host reload |

**The update file is neither deleted nor renamed.** It is not reprocessed until the next USB write.

### Storage layout (important)

The disk layer common to FatFs / USB-MSC (`disk_read` `0x08007054`, `disk_write` `0x0800709E`, `disk_ioctl` `0x08007010`):

- **1 FatFs sector = 4096 bytes**, sector count **512** → the volume is the **external SPI flash `0x00200000`-`0x003FFFFF` (2MB)**. `flash_addr = 0x200000 + (sector << 12)`.
- Writing is 4KB sector erase (`0x080028C0`) → page program (`0x08002AE2`).
- **Why `MARK.BIN` was placed at `0x247000`**: `0x247000 - 0x200000 = 0x47000` = sector 71. The file's data cluster just fell there (matches the empirical measurement in ch. 13).
- **A separate region from the app's settings volume** (correction). We initially inferred the "same 2MB region" but that was wrong. Confirmed by measuring both flash accesses empirically in emulation:
  - The app's flash read/write (`read` `0x08014A64` / `write`) **sends the 24-bit address as-is with no offset = base 0x000000**. Starting the app and having it write `TS1M.TXT`, the accesses all fall within **`0x000000`-`0x046000`** (the FAT boot sector signature `55AA` at `0x1FE`, the `TS1M` label at `0x41000`, `DefaultTemp` at `0x450F1`), and it never touches `0x200000`.
  - The DFU volume is base `0x200000`. **Two independent FATs, one for the app (at the head) and one for DFU (`0x200000`), coexist on the 8MB flash**.
  - DFU **does not reformat on successful mount** (`f_mkfs` only on failure). Therefore a file placed via DFU remains even after the app boots and DFU is re-entered, and the settings (`TS1M.TXT`) are not erased either. The persistence observed on the actual device is explained by this.

### HEX inspection and programming

The line validation of the CHECK pass `0x08003064` (confirmed):

1. Does not start with `:` → **err 0x01**
2. **Line length does not match `13 + 2·LL` → err 0x02** (colon + byte count + address + type + data + 2-digit checksum + CRLF)
3. Record type > 5 → **err 0x04** (type 1 = EOF is success, type 4 = ELA)
4. Intel-HEX checksum mismatch (`0x080011B8`) → **err 0x06**
5. **Does not end with CR+LF → err 0x07**
6. Cannot open file → **err 0xFF**

The address range gate of the PROGRAM pass `0x08002B34` / `0x08002D76` (confirmed, backed by literals):

| Range | Target |
| --- | --- |
| `[0x08010000, 0x08077C00)` | App code → write to internal flash |
| `[0x09000000, 0x09020000)` | Tip firmware (virtual address) → transfer to the tip |
| Anything else (including length 0) | **Rejected with err 0x0A** |

→ **Neither the bootloader (`0x08000000`-`0x0800FFFF`, below `0x08010000`) nor the calibration/settings pages (`0x08077C00` and above) can be rewritten from HEX.** There is no whole-image signature, CRC, or crypto; only the per-line Intel-HEX checksum and the address range are the gates.

Internal flash write `0x08003728` uses the CH32 fast mode (`FLASH_CR` bit17 FTER erase / bit16 FTPG program, `0x40022000`), **granularity 256 bytes**.

### "Update the digital iron" = updating the tip

The target is **decided not by file name but by address**: if the ELA is `0x09000000` or higher, it is the tip. The transfer is **USART2 (`0x40004400`), 115200 8N1, DMA1 ch7**, with ACK/timeout (`0x08004794` / `0x08004824`). Response statuses `0x24`-`0x26` correspond to `Update err`'s `0x0C`-`0x0E` (inferred).

### Error codes

| Check err | Meaning | Update err | Meaning |
| --- | --- | --- | --- |
| 0xFF | Cannot open file | 0xFF | Cannot open file |
| 0x01 | No leading `:` | 0x02 | Line exceeds 550 characters |
| 0x02 | Line length ≠ 13+2·LL / line too long | 0x0A | Address out of range / length 0 |
| 0x04 | Record type > 5 | 0x0C-0x0E | Abnormal status of tip transfer (inferred) |
| 0x06 | Checksum mismatch | | |
| 0x07 | No CR+LF | | |

(0x09 = EOF reached = success, 0x08 = internal continue code)

### Resolution of the unresolved item in ch. 7

**The HEX line structure affects whether writing succeeds (confirmed).** The CHECK pass (`0x08003064`) rejects lines whose length does not match `13 + 2·LL` (err 0x02) and lines that do not end with CR+LF (err 0x07). Therefore **if re-serialization changes line length, record splitting, or line endings (LF only, etc.), CHECK rejects them and they never reach programming**. The phenomenon observed in ch. 7 that "the re-serialized version was not reflected" is explained by this line validation (apart from the difference in the patch processing content at the time, the line structure alone can be a rejection cause). The current "rewrite only the relevant lines of the original HEX" method is correct.

### `0x08077E00` (product variant)

`main` calls `0x08008814(0x08077E00)`, reads the low byte of the 16-bit value in the calibration page, clamps it to `{2,3}` (default 2), and holds it in RAM `0x20000000`. It **only reads**; the bootloader does not write it, and it cannot be written by a HEX update (out of range). Thought to be the product-variant selection at startup.

## 15. MCU connection map (pin/peripheral list)

Compiled by analyzing GPIO CRL/CRH init, RCC clock enables, AFIO, DMA, ADC, and each serial's configuration from both the bootloader and the app. CH32F208WBU6 (QFN68). "Confirmed" = backed by code/emulation, "inferred" = otherwise. A peripheral actually clocked by RCC is the basis for "in use".

### Peripherals in use (enabled by RCC)

| Bus | App | Bootloader |
| --- | --- | --- |
| APB2 | GPIOA/B/C/D, ADC1, TIM1, SPI1, USART1 | GPIOA/B/C/D, ADC1, TIM1, SPI1 |
| APB1 | TIM2, TIM3, TIM4, TIM5, SPI2, USART2, UART4, USB | SPI2, USART2 |
| AHB | DMA1 | DMA1 |
| Other | IWDG, (USB pull-up via EXTEN) | IWDG, USB |

- **Neither enables the AFIO clock** = no pin remapping at all. All peripherals are on default pins. EXTI (external interrupts) is also unset.
- GPIOE, ADC2/3, USART3, I2C1/2, DMA2, TIM6/7 are unused. ADC is **ADC1 only** (the "adc1/adc2" in tip determination is not the ADC peripheral but the naming of measurement points).
- RCC enable helpers: app APB2ENR=`0x080158FC` / APB1ENR=`0x080158E4` / AHBENR=`0x080158CC`, bootloader APB2=`0x08004034`, etc.

### Pin assignment (both firmwares combined)

| Pin | Function | Confidence | Notes |
| --- | --- | --- | --- |
| PA0 | ADC1_IN0 = external thermocouple (TK/CAL) | Confirmed | Analog input |
| PA1 | **TIM5_CH2 = heater drive** (AF open drain, 100Hz) | Confirmed | ON at end of measurement / OFF at start (below) |
| PA2 | USART2_TX | Confirmed | Tip serial / jig / iron update |
| PA3 | USART2_RX | Confirmed | Temporarily switched to input on the TS80P path |
| PA4 | ADC1_IN4 = TS80P temperature (disabled path) | Confirmed | |
| PA5 | SPI1_SCK (display) | Confirmed | |
| PA6 | Analog MUX select line (SPI1_MISO unused) | Confirmed | Display is write-only |
| PA7 | SPI1_MOSI (display) | Confirmed | |
| PA8 | TIM1_CH1 PWM = LCD backlight brightness | Confirmed/inferred | |
| PA9 | USART1_TX = debug output | Confirmed | putchar `0x0801E99C` |
| PA10 | USART1_RX | Inferred | |
| PA11 / PA12 | USB_DM / USB_DP | Confirmed | D+ pull-up is EXTEN `0x40023800` bit1 (built-in) |
| PA13 / PA14 | SWDIO / SWCLK | Inferred | |
| PA15 | Input pull-up, **never read by code** (unused) | Confirmed | |
| PB0 | LCD D/C (data/command) | Confirmed (BL) | |
| PB1 | TIM3_CH4 100kHz PWM, duty always 0 (unused spare output) | Confirmed | Output enabled but not modulated |
| PB3 / PB4 | Analog MUX select lines | Confirmed | Range where SWJ must be disabled |
| PB5 / PB6 / PB7 | Analog front-end gain/range select (switched by tip type) | Confirmed | `0x08019DCC`/`0x08019DF0` |
| PB8 | TIM4_CH3 PWM = buzzer | Confirmed | period 999, volume via CCR3 |
| PB10 | Output (purpose unknown, front-end enable?) | Confirmed (config) | |
| PB12 | SPI2 flash CS (active Low) | Confirmed | `0x08017E7C` |
| PB13 / PB14 / PB15 | SPI2_SCK / MISO / MOSI (W25Q64) | Confirmed | |
| PC0 | ADC1_IN10 = tip thermocouple (via MUX) | Confirmed | Injected conversion in the ADC interrupt |
| PC1 | ADC1_IN11 (not regular-converted, purpose undetermined) | Confirmed (config) | SQR unused |
| PC2 | ADC1_IN12 = cold-junction NTC | Confirmed | |
| PC3 | ADC1_IN13 = supply voltage divider | Confirmed | |
| PC4 | **ADC1_IN14 = injected-conversion tip sense** (read between heating) | Confirmed | injected ch14, TIM2 trigger (below) |
| PC5 | **LCD RESET** (output) | Confirmed | Low→High pulse in display init |
| PC6 | Analog handle button (input pull-up) | Confirmed | Low 500ms → sleep |
| PC7 | Input pull-up, **never read** (unused) | Confirmed | |
| PC8 | **DFU boot select strap** (input pull-up, Low → DFU) | Confirmed | BL reads IDR bit8 |
| PC9 | Input pull-up, **never read** (unused) | Confirmed | |
| PC10 / PC11 | (PC10 unset) / UART4_RX (input pull-up) | Confirmed | UART4 is RX-only remnant (below) |
| PC12 | Analog MUX select line | Confirmed | |
| PD2 / PD3 / PD4 | Analog MUX select lines (PD3=245 determination, PD4 QFN68 only) | Confirmed | |
| PD5 / PD6 | **Accelerometer I2C (SDA / SCL, open drain, bit-banged)** | Confirmed (BL) | LIS3DH family, address 0x19. FlipOver/motion detection. Configured by the bootloader |
| PB11 | **IDChip 1-Wire data line** | Confirmed (BL) | Tip/cartridge authentication EEPROM. Driven only by the bootloader |
| GPIOE | Unused | Confirmed | |

### Peripheral → pin

| Peripheral | Pin |
| --- | --- |
| Display (SPI1, write-only) | SCK=PA5, MOSI=PA7, D/C=PB0, backlight=PA8 (TIM1_CH1). CS/RST undetermined (possibly PC4/PC5, below) |
| External flash W25Q64 (SPI2) | SCK=PB13, MISO=PB14, MOSI=PB15, CS=PB12 |
| Tip serial (USART2) | TX=PA2, RX=PA3. H100 frame / jig handshake / iron firmware update. RX interrupt `0x08017600` (the only active USART IRQ). TX is DMA1 ch7 |
| Debug (USART1) | TX=PA9 |
| Second serial (UART4) | RX=PC11. 9600 baud, RX-only but unused (below). TX (PC10) is unset |
| Buzzer (TIM4_CH3) | PB8 |
| Backlight (TIM1_CH1) | PA8 |
| Measurement timer (TIM2) | No pin. Reconfigured to period 400 per measurement and used for ADC synchronization |
| ADC1 inputs | IN0=PA0 (external TC), IN4=PA4 (TS80P), IN10=PC0 (tip TC, MUX, regular), IN12=PC2 (cold junction), IN13=PC3 (supply voltage), **IN14=PC4 (injected conversion = the heating loop's fast tip sense)**, IN11=PC1 (unused) |
| Analog MUX select (7 lines) | PA6, PB3, PB4, PC12, PD2, PD3, PD4 → select which signal is passed to ADC1_IN10 |
| Front-end gain/range select | PB5, PB6, PB7 (switched by tip type) |
| USB (DFU/MSC) | DM=PA11, DP=PA12 |
| Accelerometer (I2C bit-bang) | SDA=PD5, SCL=PD6 (address 0x19, LIS3DH family) |
| IDChip authentication (1-Wire) | PB11 (1-Wire EEPROM, DS2431/DS28E07 family) |
| DFU boot strap | PC8 (Low → DFU) |

### Heater drive = PA1 (TIM5_CH2)

The tip's heating element is a single-element scheme shared with measurement. **Heating and temperature measurement are time-multiplexed**:

- **Measurement window** (TIM2, `0x40000000`, period 400): TIM2 triggers the ADC's injected conversion, and the ADC1_2 interrupt (`0x08011DC8`, vector 34) reads **injected channel ch14 = PC4** (JSQR=`0x70000`). The heater is OFF during this. The current temperature for display and tip determination is obtained separately by regular conversion from ch10 = PC0 (via MUX).
- **Heating** (between measurement windows): enable the output of **PA1 = TIM5_CH2** (`0x40000C00`, 100Hz) to energize the element.

The switching is done by methods in the measurement subsystem's function table (RAM `0x20000368`):

| Method | Address | Action |
| --- | --- | --- |
| Start measurement | `0x08012F3C` | Disable TIM5 + `CCxCmd(TIM5, CH2, 0)` → **PA1 output OFF** (stop heating and measure) |
| End measurement | `0x08013A20` | Enable TIM5 + `CCxCmd(TIM5, CH2, 1)` → **PA1 output ON** (resume heating) |

`0x08016C24` = `TIM_CCxCmd` (CCER operation, `[base+0x0C]`). PID (`0x08015494`) computes the output from error = target (`0x200001F4`) − current temperature, and it is a software PWM whose power is decided by heating time (length of the heating phase). Overheat protection compares the measured ADC raw value (`0x20000402`) against the per-model `hot_ADC` threshold (3700 / 3000 / 2900 / 4000 / 2800 = `0x08021646`–), and if exceeded it sets the output to 0.

PA1 is AF open drain (mode `0x1c`), seen as an external pull-up + MOSFET gate. In the main-loop emulation, the ADC interrupt is stubbed so the measurement cycle does not advance, PA1 does not move, and it was initially misidentified as idle (because it is driven only via the function table).

- Note: none of the timer PWM channels have a variable duty for the heater (`SetCompare2`/`4` are unused, CCR is only TIM1_CH1=PA8 backlight and TIM4_CH3=PB8 buzzer). There is also no write to TIM5_CH2 CCR2 (`0x40000C38`), and power is decided by the length of the heating phase.

### Display (SPI1) CS / RESET and PC4/PC5 (confirmed with the emulator)

We pinned down 3 points with an enhanced emulator (`sim_measure`, `pin_mode`, ADC/JSQR hooks):

- **PC5 = LCD RESET**: the display init `0x08014440` pulses PC5 Low→High, then immediately sends commands like SLPOUT (0x11) / MADCTL / gamma (E0h/E1h). **The LCD's CS is tied Low in hardware** (no GPIO drive), D/C = PB0.
- **PC4 = ADC1_IN14 injected-conversion input**: the regular sequence (SQR1-3) is empty, and the ADC's single conversion (`ADC_READ_FN`) reads ch10/PC0 (via MUX). Meanwhile `ADC_InjectedChannelConfig` (`0x080120C8`) sets the **injected sequence JSQR = `0x70000` → ch14 = PC4**. The injected conversion is triggered by TIM2 (the measurement window) and read by the ADC1_2 interrupt (`0x08011DC8`) — that is, **PC4 is the sense input of the fast loop that measures the tip between heating**. It is not "unused".
- **PB1 = TIM3_CH4 100kHz PWM**: `TIM_OC4Init` (`0x08016D5C`) sets CH4 to PWM1 mode and also enables the output (CCER CC4E=1, PSC=1 / ARR=479 → 100kHz), but **the duty CCR4 is only written 0 at startup and is not modulated at runtime** (there is no call to `SetCompare4`). So it is always Low. It seems a hardware spare output is left at 0%.

Note: the bootloader sets both PC4/PC5 as outputs. PC5 is consistent as RESET, same as the app. PC4 is driven as an output by the bootloader's own display processing, but in the app it is an injected-conversion analog input. The physical net is the app's ADC sense, and only the bootloader's PC4 output purpose (inferred as CS) remains undetermined.

### UART4 and unused input pins (confirmed with the emulator)

- **UART4 (PC11)**: init (`0x08015398`) is **9600 baud, RX-only** (CR1 = `UE|RE|RXNEIE`, TE=0). The TX pin PC10 is not AF-configured. But **UART4's interrupt vector is the shared default handler `0x08010876` (`b .` infinite loop)** with no dedicated ISR. DR (`0x40004C04`) is never read during execution. So **UART4 is functionally unused (a remnant)**. Because RXNEIE=1, there is a **latent bug that would hang in the default handler if a 9600 signal arrives on PC11**, but normally nothing is connected so it does not fire. Thought to be a remnant for another variant (dock etc.).
- **Input senses PA15 / PC7 / PC9**: all set to input pull-up at startup, but **read from nowhere in the code** (the GPIO IDR is read via `GPIO_ReadInputDataBit` in only one place, and the target is only PC6 = the handle button, at `0x801EBA0` inside the stand/sleep function `0x801EB58`). So PA15 / PC7 / PC9 are **unused/spare inputs**. There is no EXTI either, so no button interrupt.

### Bootloader emulator and IDChip (`bl_emu.py`)

We built a harness `bl_emu.py` for the bootloader, modeled the bit-bang buses, and pinned down the IDChip and accelerometer. Because a full boot (USB/FAT) is heavy, we use `call()`, which calls functions directly.

- **Accelerometer (I2C, PD5=SDA / PD6=SCL, address 0x19)**: at startup `0x08002624` (GPIO config) → `0x080025D0` writes `57 00 00 08 00` to registers 0x20–0x24. These are the **LIS3DH-family CTRL_REG1–5** (CTRL_REG1=0x57 = 100Hz, all axes enabled; CTRL_REG4=0x08). For FlipOver / motion detection. I2C primitives: `write_regs`=`0x080035E8`, `read_regs`=`0x08003544` (restart + 0x33=read address). Confirmed by reproducing the write (reg 0x20–0x24) and read-back in the harness's I2C slave.
- **IDChip (1-Wire, PB11)**: `0x080042BE` = reset+presence, `0x0800637C` = byte write, `0x080041E0` = byte read (all time-slot based). `0x080021A0` does **reset → Read ROM (`0x33`) → 8 bytes** (if no presence, `IDChip Err`). Authentication `0x08003D64` further reads a memory page (0x0000 / 0x0040) with **Skip ROM (`0xCC`) + Read Memory (`0xF0`) + address**. So the IDChip is not just an ID chip but a **1-Wire EEPROM (DS2431 / DS28E07 family)**. It authenticates by collating the ROM ID and memory content against an internal record (UID-derived XOR/CRC, ch. 13-14). Confirmed by capturing the command sequences (`CC F0 00 00` / `CC F0 40 00` / `33`) in the harness's 1-Wire slave.

Previously ch. 15 stated "IDChip = I2C on PD5/PD6", which was wrong; PD5/PD6 are the accelerometer, and the IDChip is 1-Wire on PB11.

#### Authentication structure and forgeability (analyzed with the harness)

Authentication `0x08003D64` passes when all 4 bits of the status word `0x200000C0` are set and the UID key match `0x080056A4` (r5≠0) holds (ch. 13-14). We identified each bit's condition by capturing memcmp (`0x08001200`) / CRC (`0x080013E0`) in the harness:

| Bit | Decision | Relation to the IDChip |
| --- | --- | --- |
| bit0 | Record CRC (`0x08003DC6`, 34B) | None (internal record) |
| bit1 | Derived-structure CRC (60B) | Memory content is mixed in |
| bit2 | `memcmp(internal UID-derived value, decrypted memory-derived value)` | **The 12B produced by a keyed transform of the IDChip memory (1-Wire) must match the UID** |
| bit3 | `memcmp(decrypted memory-derived value, ROM)` | **The ROM must match the "memory-derived expected value"** |

- **bit3 could be forged**: it passes if you put the "8B the authentication expects" into the ROM (observed the expected value in the harness and set it into the ROM → bit3 holds).
- **bit2 cannot be forged by simple value substitution**: the 12B to be matched is produced from the 1-Wire memory via **a scramble keyed by the ROM/serial + decryption**, and the key depends on the memory content itself (placing the UID in a known slot of the memory does not change the match value). That is, it is **a keyed, genuine anti-clone**, and a full forgery requires reverse-analysis of the memory descramble/key derivation. `bl_emu.py`'s `run_auth()` and its 1-Wire slave (`OneWire`, ROM/memory swappable) provide the foundation for that work.

## Disclaimer

These notes contain inferences based on static analysis and emulation. Items verified on the actual device are noted as such.
Modifying the firmware is at your own risk. In particular, do not touch the heater control and safety devices (overheat thresholds, resistance check, 450℃ cap).
