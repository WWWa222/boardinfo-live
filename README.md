# TaishanPi Boardinfo Live

Terminal telemetry tools for the LCKFB TaishanPi 3M board.

This repository installs two Python scripts onto the board:

```sh
/usr/local/bin/boardinfo
/usr/local/bin/boardinfo-live
```

`boardinfo` prints one full system snapshot. `boardinfo-live` opens an
interactive curses dashboard for live telemetry.

## Target Board

- Board: LCKFB TaishanPi 3M RK3576 Board
- SoC: Rockchip RK3576
- CPU: 8-core ARM64, reported through Linux `cpufreq` policies
- Tested OS/kernel: Linux 6.1.99 aarch64 GNU/Linux
- Tested access method: SSH as the board user listed in the parent project's
  `config/ssh.txt`

The scripts read standard Linux interfaces such as `/proc`, `/sys`,
`df`, `ip`, and `ps`, so they should also work on nearby Rockchip Linux
boards with minor differences in device names.

## Features

- CPU usage, load average, current frequency, and governor
- Memory and swap usage
- Thermal zones from `/sys/class/thermal`
- Block storage and filesystem usage
- Network interface state, wifi/wire type, IPv4 address, RX/TX counters
- Live network RX/TX throughput in `KiB/s`
- USB stage device list from `/sys/bus/usb/devices`
- Regulator state and voltage snapshot
- Top process snapshot
- Fixed-scale live charts with warning and critical thresholds

The live dashboard order is:

```text
CPU -> memory -> network RX/TX -> disk -> USB stage -> thermal zones
```

## Install

Run from the parent project directory, not from inside this repository:

```powershell
python .\boardinfo-live\install_boardinfo.py
```

The installer reads SSH connection details from:

```text
config/ssh.txt
```

Expected format:

```text
user@192.168.x.x or user@192.168.x.y
password
```

The installer tries each configured target, uploads the generated scripts to
`/tmp`, then installs them with `sudo` to `/usr/local/bin`.

## Usage

Run a full snapshot immediately after installation:

```powershell
python .\boardinfo-live\install_boardinfo.py --once
```

Run one live-style text snapshot immediately after installation:

```powershell
python .\boardinfo-live\install_boardinfo.py --live-once
```

On the board:

```sh
boardinfo
boardinfo-live
```

`boardinfo-live` controls:

- `Up` / `Down`: select telemetry item
- `q`: quit
- Left panel: current values
- Right panel: selected item's waveform

## USB Notes

USB device names are intentionally read from safe sysfs fields only. The tool
does not call `lsusb` and does not read USB descriptor strings such as
`manufacturer` or `product`, because an unhealthy USB device or bus can block
those reads. The USB stage therefore shows stable bus/device IDs and
vendor:product IDs.

Example:

```text
USB stage 1a40:0201 1-1
```

## Network Notes

Network rows classify interfaces as:

- `wifi`: wireless interface, such as `wlan0`
- `wire`: wired Ethernet interface, such as `eth0` or `end0`
- `net`: other network interfaces

IPv4 addresses are shown when available. Interfaces without an address show
`-`.

## Repository Notes

This repository intentionally does not include SSH credentials, board images,
logs, generated archives, or local project configuration.
