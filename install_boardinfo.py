#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
from pathlib import Path

import paramiko


ROOT = Path(__file__).resolve().parents[1]
SSH_TXT = ROOT / "config" / "ssh.txt"
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

REMOTE_SNAPSHOT_SCRIPT = r"""#!/usr/bin/env python3
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return default


def run(command: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def kv_cpuinfo() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in read_text("/proc/cpuinfo").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            data.setdefault(key.strip(), value.strip())
    return data


def uptime() -> str:
    raw = read_text("/proc/uptime")
    try:
        seconds = int(float(raw.split()[0]))
    except (IndexError, ValueError):
        return ""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


def meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def fmt_kib(kib: int) -> str:
    value = float(kib)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{kib} KiB"


def cpu_times() -> tuple[int, int]:
    parts = read_text("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(v) for v in parts]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_percent(interval: float = 0.25) -> float | None:
    try:
        total1, idle1 = cpu_times()
        time.sleep(interval)
        total2, idle2 = cpu_times()
    except (IndexError, ValueError):
        return None
    total_delta = total2 - total1
    if total_delta <= 0:
        return None
    return 100.0 * (1.0 - ((idle2 - idle1) / total_delta))


def cpu_freqs() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for policy in sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*"), key=lambda p: p.name):
        rows.append({
            "policy": policy.name,
            "governor": read_text(policy / "scaling_governor", "NA"),
            "cur_mhz": khz_to_mhz(read_text(policy / "scaling_cur_freq")),
            "min_mhz": khz_to_mhz(read_text(policy / "scaling_min_freq")),
            "max_mhz": khz_to_mhz(read_text(policy / "scaling_max_freq")),
        })
    return rows


def khz_to_mhz(raw: str) -> str:
    try:
        return f"{int(raw) / 1000:.0f}"
    except ValueError:
        return "NA"


def thermal_zones() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*"), key=lambda p: int(p.name.replace("thermal_zone", ""))):
        raw = read_text(zone / "temp", "NA")
        try:
            temp = f"{int(raw) / 1000:.3f}"
        except ValueError:
            temp = raw
        rows.append({
            "zone": zone.name.replace("thermal_zone", ""),
            "type": read_text(zone / "type", "unknown"),
            "temp_c": temp,
        })
    return rows


def block_devices() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for device in sorted(Path("/sys/block").iterdir(), key=lambda p: p.name):
        if not device.is_dir() or device.name.startswith(("loop", "ram", "zram")):
            continue
        sectors = read_text(device / "size")
        try:
            size = f"{int(sectors) * 512 / (1024 ** 3):.2f} GiB"
        except ValueError:
            size = "NA"
        rows.append({
            "name": device.name,
            "model": read_text(device / "device" / "model", read_text(device / "device" / "name", "")),
            "type": read_text(device / "queue" / "rotational", ""),
            "size": size,
        })
    return rows


def filesystems() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    out = run(["df", "-hT", "-x", "tmpfs", "-x", "devtmpfs"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 7:
            rows.append({
                "fs": parts[0],
                "type": parts[1],
                "size": parts[2],
                "used": parts[3],
                "avail": parts[4],
                "use": parts[5],
                "mount": " ".join(parts[6:]),
            })
    return rows


def net_addrs() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    out = run(["ip", "-o", "addr", "show", "scope", "global"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            rows.append({"if": parts[1], "family": parts[2], "addr": parts[3]})
    return rows


def net_stats() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for iface in sorted(Path("/sys/class/net").iterdir(), key=lambda p: p.name):
        if iface.name == "lo":
            continue
        rows.append({
            "if": iface.name,
            "state": read_text(iface / "operstate", "unknown"),
            "rx_mb": bytes_to_mb(read_text(iface / "statistics" / "rx_bytes")),
            "tx_mb": bytes_to_mb(read_text(iface / "statistics" / "tx_bytes")),
        })
    return rows


def bytes_to_mb(raw: str) -> str:
    try:
        return f"{int(raw) / (1024 ** 2):.1f}"
    except ValueError:
        return "NA"


def top_processes(limit: int = 8) -> list[dict[str, str]]:
    out = run(["ps", "-eo", "pid,comm,%cpu,%mem,rss", "--sort=-%cpu"])
    rows: list[dict[str, str]] = []
    for line in out.splitlines()[1 : limit + 1]:
        parts = line.split(None, 4)
        if len(parts) == 5:
            rows.append({"pid": parts[0], "comm": parts[1], "cpu": parts[2], "mem": parts[3], "rss": parts[4]})
    return rows


def regulators() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for reg in sorted(glob.glob("/sys/class/regulator/regulator.*")):
        base = Path(reg)
        uv = read_text(base / "microvolts")
        try:
            mv = f"{int(uv) / 1000:.0f}"
        except ValueError:
            mv = "NA"
        rows.append({
            "name": read_text(base / "name", base.name),
            "state": read_text(base / "state", ""),
            "mv": mv,
        })
    return rows[:16]


def collect() -> dict[str, object]:
    cpu = kv_cpuinfo()
    mem = meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    return {
        "time": time.strftime("%F %T %Z"),
        "hostname": socket.gethostname(),
        "kernel": run(["uname", "-srmo"]),
        "os": read_text("/etc/os-release"),
        "model": read_text("/proc/device-tree/model").replace("\x00", ""),
        "serial": read_text("/proc/device-tree/serial-number").replace("\x00", ""),
        "uptime": uptime(),
        "loadavg": read_text("/proc/loadavg"),
        "cpu": {
            "hardware": cpu.get("Hardware", ""),
            "model_name": cpu.get("model name", cpu.get("Processor", "")),
            "cores": str(os.cpu_count() or ""),
            "usage_percent": cpu_percent(),
            "freqs": cpu_freqs(),
        },
        "memory": {
            "total": fmt_kib(mem_total),
            "available": fmt_kib(mem_avail),
            "used_percent": None if not mem_total else round(100 * (mem_total - mem_avail) / mem_total, 1),
            "swap_total": fmt_kib(swap_total),
            "swap_used": fmt_kib(swap_total - swap_free),
        },
        "thermal": thermal_zones(),
        "block": block_devices(),
        "filesystems": filesystems(),
        "network": {"addresses": net_addrs(), "stats": net_stats()},
        "regulators": regulators(),
        "top": top_processes(),
    }


def print_section(title: str) -> None:
    print()
    print(f"== {title} ==")


def main() -> int:
    data = collect()

    print(f"{data['time']}  {data['hostname']}")
    print(f"Model : {data['model'] or 'unknown'}")
    print(f"Kernel: {data['kernel']}")
    print(f"Uptime: {data['uptime']}  Load: {data['loadavg']}")

    cpu = data["cpu"]
    print_section("CPU")
    usage = cpu["usage_percent"]
    usage_text = "NA" if usage is None else f"{usage:.1f}%"
    print(f"Cores: {cpu['cores']}  Usage: {usage_text}  Hardware: {cpu['hardware'] or cpu['model_name'] or 'unknown'}")
    for row in cpu["freqs"]:
        print(f"{row['policy']:<8} {row['cur_mhz']:>5} MHz  {row['min_mhz']:>5}-{row['max_mhz']:<5} MHz  {row['governor']}")

    mem = data["memory"]
    print_section("Memory")
    print(f"Total: {mem['total']}  Available: {mem['available']}  Used: {mem['used_percent']}%")
    print(f"Swap : {mem['swap_used']} / {mem['swap_total']}")

    print_section("Thermal")
    print(f"{'ZONE':<4} {'TYPE':<22} {'TEMP(C)':>10}")
    for row in data["thermal"]:
        print(f"{row['zone']:<4} {row['type']:<22.22} {row['temp_c']:>10}")

    print_section("Storage")
    for row in data["block"]:
        model = f" {row['model']}" if row["model"] else ""
        print(f"{row['name']:<10} {row['size']:>10}{model}")
    print()
    print(f"{'MOUNT':<24} {'TYPE':<8} {'SIZE':>7} {'USED':>7} {'AVAIL':>7} {'USE':>5}")
    for row in data["filesystems"]:
        print(f"{row['mount']:<24.24} {row['type']:<8} {row['size']:>7} {row['used']:>7} {row['avail']:>7} {row['use']:>5}")

    print_section("Network")
    for row in data["network"]["addresses"]:
        print(f"{row['if']:<10} {row['family']:<5} {row['addr']}")
    for row in data["network"]["stats"]:
        print(f"{row['if']:<10} {row['state']:<8} rx={row['rx_mb']}MiB tx={row['tx_mb']}MiB")

    if data["regulators"]:
        print_section("Regulators")
        for row in data["regulators"]:
            print(f"{row['name']:<24.24} {row['state']:<9} {row['mv']:>6} mV")

    print_section("Top Processes")
    print(f"{'PID':>6} {'COMMAND':<22} {'CPU%':>6} {'MEM%':>6} {'RSS(KiB)':>9}")
    for row in data["top"]:
        print(f"{row['pid']:>6} {row['comm']:<22.22} {row['cpu']:>6} {row['mem']:>6} {row['rss']:>9}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

REMOTE_LIVE_SCRIPT = r"""#!/usr/bin/env python3
from __future__ import annotations

import argparse
import curses
import os
import shutil
import sys
import time
from collections import deque
from pathlib import Path


WHITE_PAIR = 1
GREEN_PAIR = 2
YELLOW_PAIR = 3
RED_PAIR = 4


def read_text(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return default


def cpu_times() -> tuple[int, int]:
    vals = [int(v) for v in read_text("/proc/stat").splitlines()[0].split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_usage(prev: tuple[int, int] | None) -> tuple[float | None, tuple[int, int]]:
    now = cpu_times()
    if prev is None:
        return None, now
    total_delta = now[0] - prev[0]
    idle_delta = now[1] - prev[1]
    if total_delta <= 0:
        return None, now
    return 100.0 * (1.0 - (idle_delta / total_delta)), now


def mem_usage() -> tuple[float | None, str]:
    values: dict[str, int] = {}
    for line in read_text("/proc/meminfo").splitlines():
        parts = line.replace(":", "").split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0]] = int(parts[1])
    total = values.get("MemTotal", 0)
    avail = values.get("MemAvailable", 0)
    if not total:
        return None, "NA"
    used = total - avail
    return 100.0 * used / total, f"{used / 1024 / 1024:.2f}/{total / 1024 / 1024:.2f} GiB"


def disk_usage(path: str = "/") -> tuple[float | None, str]:
    usage = shutil.disk_usage(path)
    if not usage.total:
        return None, "NA"
    return 100.0 * usage.used / usage.total, f"{usage.used / 1024 ** 3:.2f}/{usage.total / 1024 ** 3:.2f} GiB"


def thermal() -> list[tuple[str, str, float | None]]:
    rows: list[tuple[str, str, float | None]] = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*"), key=lambda p: int(p.name.replace("thermal_zone", ""))):
        try:
            temp = int(read_text(zone / "temp")) / 1000.0
        except ValueError:
            temp = None
        rows.append((zone.name.replace("thermal_zone", ""), read_text(zone / "type", "unknown"), temp))
    return rows


def loadavg() -> str:
    parts = read_text("/proc/loadavg").split()
    return " ".join(parts[:3])


def cpu_freq_summary() -> str:
    freqs: list[str] = []
    for policy in sorted(Path("/sys/devices/system/cpu/cpufreq").glob("policy*"), key=lambda p: p.name):
        raw = read_text(policy / "scaling_cur_freq")
        try:
            freqs.append(f"{policy.name}:{int(raw) / 1000:.0f}MHz")
        except ValueError:
            pass
    return " ".join(freqs) or "NA"


def color_pair_for(key: str, value: float | None = None) -> int:
    if value is None:
        return WHITE_PAIR
    if key.startswith("temp:"):
        if value >= 80:
            return RED_PAIR
        if value >= 65:
            return YELLOW_PAIR
        return GREEN_PAIR
    if key in ("cpu", "memory", "disk:/"):
        if value >= 85:
            return RED_PAIR
        if value >= 70:
            return YELLOW_PAIR
        return GREEN_PAIR
    return WHITE_PAIR


def axis_range(key: str, values: list[float]) -> tuple[float, float]:
    if key in ("cpu", "memory", "disk:/"):
        return 0.0, 100.0
    if key.startswith("temp:"):
        return 20.0, 90.0
    if not values:
        return 0.0, 1.0
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def chart_thresholds(key: str) -> tuple[float | None, float | None]:
    if key in ("cpu", "memory", "disk:/"):
        return 70.0, 85.0
    if key.startswith("temp:"):
        return 65.0, 80.0
    return None, None


def history_stats(history: deque[float | None]) -> tuple[float | None, float | None, float | None]:
    vals = [v for v in history if v is not None]
    if not vals:
        return None, None, None
    return vals[-1], sum(vals) / len(vals), max(vals)


def area_chart(key: str, history: deque[float | None], width: int, height: int) -> tuple[list[str], str]:
    vals = [v for v in history if v is not None]
    if not vals:
        return [" " * width for _ in range(height)], "no data"

    lo, hi = axis_range(key, vals)
    warn, crit = chart_thresholds(key)
    grid = [[" " for _ in range(width)] for _ in range(height)]
    points = list(history)[-width:]
    start = width - len(points)

    for offset, value in enumerate(points):
        if value is None:
            continue
        x = start + offset
        filled = int(round((value - lo) * (height - 1) / (hi - lo))) + 1
        filled = max(1, min(height, filled))
        for y in range(height - filled, height):
            grid[y][x] = "#"

    for threshold, mark in ((warn, "-"), (crit, "=")):
        if threshold is None or not lo <= threshold <= hi:
            continue
        y = height - 1 - int(round((threshold - lo) * (height - 1) / (hi - lo)))
        for x in range(width):
            if grid[y][x] == " ":
                grid[y][x] = mark

    scale = f"{lo:.1f}->{hi:.1f}"
    return ["".join(row) for row in grid], scale


def fmt_pct(value: float | None) -> str:
    return "NA" if value is None else f"{value:5.1f}%"


def fmt_temp(value: float | None) -> str:
    return "NA" if value is None else f"{value:6.2f} C"


def value_text(key: str, value: float | None) -> str:
        if key.startswith("temp:"):
            return fmt_temp(value)
        return fmt_pct(value)


def collect_points(prev_cpu: tuple[int, int], labels: dict[str, str]) -> tuple[list[tuple[str, float | None]], tuple[int, int]]:
    cpu, next_cpu = cpu_usage(prev_cpu)
    mem, mem_label = mem_usage()
    disk, disk_label = disk_usage("/")
    labels["memory"] = f"memory {mem_label}"
    labels["disk:/"] = f"disk / {disk_label}"
    points: list[tuple[str, float | None]] = [("cpu", cpu), ("memory", mem), ("disk:/", disk)]
    for zone, ztype, temp in thermal():
        key = f"temp:{zone}"
        labels[key] = f"temp {zone} {ztype}"
        points.append((key, temp))
    return points, next_cpu


def draw_text_once(items: list[str], history: dict[str, deque[float | None]], labels: dict[str, str]) -> None:
    print(f"{time.strftime('%F %T')} host={os.uname().nodename} load={loadavg()} freq={cpu_freq_summary()}")
    print(f"{'ITEM':<26} {'NOW':>12} {'STATUS':>8}")
    for idx, key in enumerate(items):
        value = history[key][-1] if history.get(key) else None
        status = {WHITE_PAIR: "white", GREEN_PAIR: "green", YELLOW_PAIR: "yellow", RED_PAIR: "red"}[color_pair_for(key, value)]
        print(f"{labels.get(key, key):<26.26} {value_text(key, value):>12} {status:>8}")


def draw_dashboard(stdscr: curses.window, items: list[str], selected: int, history: dict[str, deque[float | None]], labels: dict[str, str]) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    left_w = min(44, max(32, cols // 2))
    right_x = left_w + 1
    right_w = max(20, cols - right_x - 1)

    stdscr.addnstr(0, 0, f"{time.strftime('%F %T')}  host={os.uname().nodename}", cols - 1, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    stdscr.addnstr(1, 0, f"load={loadavg()}  freq={cpu_freq_summary()}", cols - 1, curses.color_pair(WHITE_PAIR))
    stdscr.addnstr(2, 0, "q quit | Up/Down choose item | right panel shows selected waveform", cols - 1, curses.color_pair(WHITE_PAIR))

    if rows < 12 or cols < 70:
        stdscr.addnstr(4, 0, "terminal too small; enlarge it for waveform view", cols - 1, curses.color_pair(YELLOW_PAIR))
        stdscr.refresh()
        return

    stdscr.addnstr(4, 0, f"{'ITEM':<26} {'NOW':>12}", left_w - 1, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    visible_h = rows - 6
    start = max(0, min(selected - visible_h // 2, max(len(items) - visible_h, 0)))
    for screen_row, idx in enumerate(range(start, min(start + visible_h, len(items))), start=5):
        key = items[idx]
        value = history[key][-1] if history.get(key) else None
        label_pair = WHITE_PAIR if idx % 2 == 0 else GREEN_PAIR
        value_pair = color_pair_for(key, value)
        attr = curses.color_pair(label_pair)
        if idx == selected:
            attr |= curses.A_REVERSE
        marker = ">" if idx == selected else " "
        stdscr.addnstr(screen_row, 0, f"{marker} {labels.get(key, key):<25.25}", 28, attr)
        stdscr.addnstr(screen_row, 29, f"{value_text(key, value):>12}", 12, curses.color_pair(value_pair) | (curses.A_REVERSE if idx == selected else 0))

    for y in range(4, rows):
        stdscr.addch(y, left_w, curses.ACS_VLINE, curses.color_pair(WHITE_PAIR))

    selected_key = items[selected]
    selected_value = history[selected_key][-1] if history.get(selected_key) else None
    wave_h = max(6, rows - 10)
    wave_w = max(12, right_w - 15)
    wave, scale = area_chart(selected_key, history[selected_key], wave_w, wave_h)
    pair = color_pair_for(selected_key, selected_value)
    now_value, avg_value, max_value = history_stats(history[selected_key])
    title = (
        f"{labels.get(selected_key, selected_key)}  "
        f"now={value_text(selected_key, now_value)}  "
        f"avg={value_text(selected_key, avg_value)}  "
        f"max={value_text(selected_key, max_value)}"
    )
    stdscr.addnstr(4, right_x, title, right_w, curses.color_pair(pair) | curses.A_BOLD)
    warn, crit = chart_thresholds(selected_key)
    threshold_text = ""
    if warn is not None and crit is not None:
        threshold_text = f" range={scale}  '-' warn {warn:.0f}  '=' critical {crit:.0f}"
    else:
        threshold_text = f" range={scale}"
    stdscr.addnstr(5, right_x, "older".ljust(max(0, wave_w - 3)) + "now" + threshold_text, right_w, curses.color_pair(WHITE_PAIR))

    vals = [v for v in history[selected_key] if v is not None]
    lo, hi = axis_range(selected_key, vals)
    mid = lo + (hi - lo) / 2.0
    ylabels = {0: hi, wave_h // 2: mid, wave_h - 1: lo}
    for i, line in enumerate(wave):
        y = 6 + i
        if y >= rows:
            break
        ylab = f"{ylabels[i]:>7.1f} " if i in ylabels else " " * 8
        stdscr.addnstr(y, right_x, ylab, 8, curses.color_pair(WHITE_PAIR))
        stdscr.addnstr(y, right_x + 8, line, wave_w, curses.color_pair(pair))
    stdscr.refresh()


def run_curses(args: argparse.Namespace) -> int:
    labels: dict[str, str] = {"cpu": "cpu usage", "memory": "memory used", "disk:/": "disk / used"}
    prev_cpu = cpu_times()
    time.sleep(min(args.interval, 0.25))
    points, prev_cpu = collect_points(prev_cpu, labels)
    items = [key for key, _ in points]
    history: dict[str, deque[float | None]] = {key: deque(maxlen=args.history) for key in items}
    for key, value in points:
        history[key].append(value)

    if args.once or not sys.stdin.isatty() or not sys.stdout.isatty():
        draw_text_once(items, history, labels)
        return 0

    selected = 0

    def loop(stdscr: curses.window) -> None:
        nonlocal selected, prev_cpu, items, history
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(WHITE_PAIR, curses.COLOR_WHITE, -1)
        curses.init_pair(GREEN_PAIR, curses.COLOR_GREEN, -1)
        curses.init_pair(YELLOW_PAIR, curses.COLOR_YELLOW, -1)
        curses.init_pair(RED_PAIR, curses.COLOR_RED, -1)
        stdscr.keypad(True)
        stdscr.nodelay(True)
        next_update = 0.0

        while True:
            now = time.time()
            if now >= next_update:
                points, prev_cpu = collect_points(prev_cpu, labels)
                new_items = [key for key, _ in points]
                for key in new_items:
                    history.setdefault(key, deque(maxlen=args.history))
                for key, value in points:
                    history[key].append(value)
                items = new_items
                selected = max(0, min(selected, len(items) - 1))
                draw_dashboard(stdscr, items, selected, history, labels)
                next_update = now + args.interval

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (curses.KEY_UP, ord("k"), ord("K")):
                selected = max(0, selected - 1)
                draw_dashboard(stdscr, items, selected, history, labels)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                selected = min(len(items) - 1, selected + 1)
                draw_dashboard(stdscr, items, selected, history, labels)
            time.sleep(0.03)

    curses.wrapper(loop)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live TaishanPi board telemetry dashboard.")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--history", type=int, default=120, help="history points per item")
    parser.add_argument("--once", action="store_true", help="print one live-style snapshot and exit")
    args = parser.parse_args()
    return run_curses(args)


if __name__ == "__main__":
    raise SystemExit(main())
"""


def parse_ssh_config(path: Path) -> tuple[list[tuple[str, str]], str]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"{path} must contain at least 2 non-empty lines")

    password = lines[1]
    targets: list[tuple[str, str]] = []
    for item in re.split(r"\s+or\s+|\s*,\s*|\s+", lines[0]):
        item = item.strip()
        if not item:
            continue
        if "@" not in item:
            raise ValueError(f"invalid ssh target: {item}")
        username, host = item.split("@", 1)
        host = re.sub(r"\.+", ".", host).strip(".")
        socket.inet_aton(host)
        targets.append((username, host))
    if not targets:
        raise ValueError(f"no ssh targets found in {path}")
    return targets, password


def connect(targets: list[tuple[str, str]], password: str, timeout: int) -> tuple[paramiko.SSHClient, str, str]:
    errors: list[str] = []
    for username, host in targets:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh.connect(hostname=host, username=username, password=password, timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
            return ssh, username, host
        except Exception as exc:
            ssh.close()
            errors.append(f"{username}@{host}: {exc}")
    raise RuntimeError("could not connect to any configured board target:\n" + "\n".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description="Install TaishanPi board telemetry scripts over SSH.")
    parser.add_argument("--once", action="store_true", help="run boardinfo once after installation")
    parser.add_argument("--live-once", action="store_true", help="run boardinfo-live --once after installation")
    parser.add_argument("--timeout", type=int, default=8, help="SSH connection timeout in seconds")
    args = parser.parse_args()

    targets, password = parse_ssh_config(SSH_TXT)
    ssh, username, host = connect(targets, password, args.timeout)
    try:
        print(f"Connected: {username}@{host}")
        sftp = ssh.open_sftp()
        try:
            with sftp.file("/tmp/boardinfo", "w") as fp:
                fp.write(REMOTE_SNAPSHOT_SCRIPT)
            with sftp.file("/tmp/boardinfo-live", "w") as fp:
                fp.write(REMOTE_LIVE_SCRIPT)
        finally:
            sftp.close()

        install_cmd = (
            f"printf '%s\\n' {password!r} | sudo -S -p '' sh -lc "
            "'install -m 0755 /tmp/boardinfo /usr/local/bin/boardinfo && "
            "install -m 0755 /tmp/boardinfo-live /usr/local/bin/boardinfo-live'"
        )
        _, stdout, stderr = ssh.exec_command(install_cmd, timeout=30)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        code = stdout.channel.recv_exit_status()
        if out:
            print(out, end="")
        if err:
            print(err, end="", file=sys.stderr)
        if code != 0:
            return code

        if args.once or args.live_once:
            command = "/usr/local/bin/boardinfo-live --once" if args.live_once else "/usr/local/bin/boardinfo"
            _, stdout, stderr = ssh.exec_command(command, timeout=30)
            out = stdout.read().decode("utf-8", "ignore")
            err = stderr.read().decode("utf-8", "ignore")
            if out:
                print(out, end="")
            if err:
                print(err, end="", file=sys.stderr)
            return stdout.channel.recv_exit_status()

        print("Installed: /usr/local/bin/boardinfo and /usr/local/bin/boardinfo-live")
        return 0
    finally:
        ssh.close()


if __name__ == "__main__":
    raise SystemExit(main())
