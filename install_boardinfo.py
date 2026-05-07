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


def fan_status() -> dict[str, str] | None:
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*"), key=lambda p: p.name):
        if read_text(hwmon / "name") != "pwmfan":
            continue
        pwm_raw = read_text(hwmon / "pwm1", "NA")
        pwm_enable = read_text(hwmon / "pwm1_enable", "NA")
        rpm = read_text(hwmon / "fan1_input", "")
        try:
            pwm_pct = f"{(int(pwm_raw) / 255.0) * 100.0:.1f}"
        except ValueError:
            pwm_pct = "NA"
        return {
            "name": "pwmfan",
            "pwm_raw": pwm_raw,
            "pwm_pct": pwm_pct,
            "enable": pwm_enable,
            "rpm": rpm or "NA",
        }
    return None


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
            "kind": iface_kind(iface),
            "ip": iface_ip(iface.name),
            "state": read_text(iface / "operstate", "unknown"),
            "rx_mb": bytes_to_mb(read_text(iface / "statistics" / "rx_bytes")),
            "tx_mb": bytes_to_mb(read_text(iface / "statistics" / "tx_bytes")),
        })
    return rows


def iface_kind(iface: Path) -> str:
    if (iface / "wireless").exists() or iface.name.startswith(("wl", "wlan")):
        return "wifi"
    if iface.name.startswith(("en", "eth")):
        return "wire"
    return "net"


def iface_ip(name: str) -> str:
    out = run(["ip", "-o", "-4", "addr", "show", "dev", name, "scope", "global"])
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            idx = parts.index("inet")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return "-"


def bytes_to_mb(raw: str) -> str:
    try:
        return f"{int(raw) / (1024 ** 2):.1f}"
    except ValueError:
        return "NA"


def usb_devices() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for dev in sorted(Path("/sys/bus/usb/devices").glob("[0-9]*-*"), key=lambda p: p.name):
        vendor = read_text(dev / "idVendor")
        product = read_text(dev / "idProduct")
        if not vendor or not product:
            continue
        busnum = read_text(dev / "busnum", "-")
        devnum = read_text(dev / "devnum", dev.name)
        rows.append({"bus": busnum, "dev": devnum, "id": f"{vendor}:{product}", "name": dev.name})
    return rows


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
        "fan": fan_status(),
        "thermal": thermal_zones(),
        "block": block_devices(),
        "filesystems": filesystems(),
        "network": {"addresses": net_addrs(), "stats": net_stats()},
        "usb": usb_devices(),
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

    print_section("Fan")
    fan = data["fan"]
    if fan:
        speed = f"{fan['rpm']} RPM" if fan["rpm"] != "NA" else f"{fan['pwm_pct']}% PWM"
        print(
            f"Device: {fan['name']}  Speed: {speed}  "
            f"PWM raw: {fan['pwm_raw']}  Enable: {fan['enable']}"
        )
    else:
        print("No pwmfan hwmon device detected")

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
        print(f"{row['if']:<10} {row['kind']:<5} {row['state']:<8} ip={row['ip']:<18} rx={row['rx_mb']}MiB tx={row['tx_mb']}MiB")

    print_section("USB Stage")
    if data["usb"]:
        for row in data["usb"]:
            print(f"Bus {row['bus']:<3} Dev {row['dev']:<3} ID {row['id']:<9} {row['name']}")
    else:
        print("No USB devices detected")

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
import re
import shutil
import subprocess
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


def run(command: list[str], timeout: float = 2.0) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


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


def cpu_core_times() -> dict[str, tuple[int, int]]:
    rows: dict[str, tuple[int, int]] = {}
    for line in read_text("/proc/stat").splitlines():
        if not line.startswith("cpu") or not line[3:4].isdigit():
            continue
        name, *parts = line.split()
        vals = [int(v) for v in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        rows[name] = (sum(vals), idle)
    return rows


def cpu_core_usage(prev: dict[str, tuple[int, int]] | None) -> tuple[list[dict[str, object]], dict[str, tuple[int, int]]]:
    now = cpu_core_times()
    rows: list[dict[str, object]] = []
    prev = prev or {}
    for name in sorted(now, key=lambda item: int(item[3:])):
        total, idle = now[name]
        last = prev.get(name)
        usage: float | None = None
        if last is not None:
            total_delta = total - last[0]
            idle_delta = idle - last[1]
            if total_delta > 0:
                usage = 100.0 * (1.0 - (idle_delta / total_delta))
        rows.append({"name": name, "usage": usage})
    return rows, now


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


def iface_kind(iface: Path) -> str:
    if (iface / "wireless").exists() or iface.name.startswith(("wl", "wlan")):
        return "wifi"
    if iface.name.startswith(("en", "eth")):
        return "wire"
    return "net"


def iface_ip(name: str) -> str:
    out = run(["ip", "-o", "-4", "addr", "show", "dev", name, "scope", "global"])
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            idx = parts.index("inet")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    return "-"


def net_counters() -> dict[str, tuple[int, int, str, str, str]]:
    counters: dict[str, tuple[int, int, str, str, str]] = {}
    for iface in sorted(Path("/sys/class/net").iterdir(), key=lambda p: p.name):
        if iface.name == "lo":
            continue
        try:
            rx = int(read_text(iface / "statistics" / "rx_bytes", "0"))
            tx = int(read_text(iface / "statistics" / "tx_bytes", "0"))
        except ValueError:
            continue
        counters[iface.name] = (
            rx,
            tx,
            read_text(iface / "operstate", "unknown"),
            iface_kind(iface),
            iface_ip(iface.name),
        )
    return counters


def usb_devices() -> list[str]:
    devices: list[str] = []
    for dev in sorted(Path("/sys/bus/usb/devices").glob("[0-9]*-*"), key=lambda p: p.name):
        vendor = read_text(dev / "idVendor")
        product = read_text(dev / "idProduct")
        if not vendor or not product:
            continue
        devices.append(f"{vendor}:{product} {dev.name}")
    return devices


def thermal() -> list[tuple[str, str, float | None]]:
    rows: list[tuple[str, str, float | None]] = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*"), key=lambda p: int(p.name.replace("thermal_zone", ""))):
        try:
            temp = int(read_text(zone / "temp")) / 1000.0
        except ValueError:
            temp = None
        rows.append((zone.name.replace("thermal_zone", ""), read_text(zone / "type", "unknown"), temp))
    return rows


def fan_status() -> dict[str, object] | None:
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*"), key=lambda p: p.name):
        if read_text(hwmon / "name") != "pwmfan":
            continue
        pwm_raw = read_text(hwmon / "pwm1", "NA")
        pwm_enable = read_text(hwmon / "pwm1_enable", "NA")
        rpm = read_text(hwmon / "fan1_input", "")
        try:
            pwm_pct = (int(pwm_raw) / 255.0) * 100.0
        except ValueError:
            pwm_pct = None
        try:
            rpm_value = float(rpm)
        except ValueError:
            rpm_value = None
        return {
            "pwm_raw": pwm_raw,
            "enable": pwm_enable,
            "rpm": rpm or "NA",
            "mode": "rpm" if rpm_value is not None else "pwm",
            "label_value": rpm if rpm_value is not None else ("NA" if pwm_pct is None else f"{pwm_pct:.1f}%"),
            "chart_value": rpm_value if rpm_value is not None else pwm_pct,
        }
    return None


def task_rows(limit: int = 12) -> list[dict[str, str]]:
    out = run(["ps", "-eo", "pid,ppid,psr,%cpu,%mem,rss,stat,etimes,comm,args", "--sort=-%cpu"])
    rows: list[dict[str, str]] = []
    for line in out.splitlines()[1 : limit + 1]:
        parts = line.split(None, 9)
        if len(parts) == 10:
            rows.append({
                "pid": parts[0],
                "ppid": parts[1],
                "psr": parts[2],
                "cpu": parts[3],
                "mem": parts[4],
                "rss": parts[5],
                "stat": parts[6],
                "etime": parts[7],
                "comm": parts[8],
                "args": parts[9],
            })
    return rows


def task_detail(pid: str) -> dict[str, str]:
    out = run(["ps", "-p", pid, "-o", "pid=,ppid=,psr=,ni=,pri=,pcpu=,pmem=,rss=,stat=,etimes=,comm=,args="])
    if not out:
        return {}
    parts = out.split(None, 11)
    if len(parts) < 12:
        return {}
    return {
        "pid": parts[0],
        "ppid": parts[1],
        "psr": parts[2],
        "ni": parts[3],
        "pri": parts[4],
        "cpu": parts[5],
        "mem": parts[6],
        "rss": parts[7],
        "stat": parts[8],
        "etime": parts[9],
        "comm": parts[10],
        "args": parts[11],
    }


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
    if key == "fan:speed":
        if value >= 70:
            return GREEN_PAIR
        if value >= 30:
            return YELLOW_PAIR
        return WHITE_PAIR
    if key in ("cpu", "memory", "disk:/"):
        if value >= 85:
            return RED_PAIR
        if value >= 70:
            return YELLOW_PAIR
        return GREEN_PAIR
    if key == "usb:devices":
        return GREEN_PAIR if value and value > 0 else YELLOW_PAIR
    return WHITE_PAIR


def axis_range(key: str, values: list[float]) -> tuple[float, float]:
    if key in ("cpu", "memory", "disk:/"):
        return 0.0, 100.0
    if key == "fan:speed":
        hi = max(values) if values else 100.0
        return 0.0, max(100.0, hi * 1.1)
    if key.startswith("net:"):
        hi = max(values) if values else 1.0
        return 0.0, max(1.0, hi * 1.2)
    if key == "usb:devices":
        hi = max(values) if values else 1.0
        return 0.0, max(1.0, hi)
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
    if key == "fan:speed":
        return "NA" if value is None else f"{value:7.1f}"
    if key.startswith("net:"):
        return "NA" if value is None else f"{value:7.1f} KiB/s"
    if key == "usb:devices":
        return "NA" if value is None else f"{int(value)} dev"
    return fmt_pct(value)


def collect_points(
    prev_cpu: tuple[int, int],
    prev_net: dict[str, tuple[int, int, str, str, str]],
    elapsed: float,
    labels: dict[str, str],
) -> tuple[list[tuple[str, float | None]], tuple[int, int], dict[str, tuple[int, int, str, str, str]]]:
    cpu, next_cpu = cpu_usage(prev_cpu)
    mem, mem_label = mem_usage()
    disk, disk_label = disk_usage("/")
    labels["memory"] = f"memory {mem_label}"
    labels["disk:/"] = f"disk / {disk_label}"
    points: list[tuple[str, float | None]] = [("cpu", cpu), ("memory", mem)]

    next_net = net_counters()
    elapsed = max(elapsed, 0.001)
    for name, (rx, tx, state, kind, ip_addr) in next_net.items():
        prev = prev_net.get(name)
        rx_rate: float | None = None
        tx_rate: float | None = None
        if prev:
            rx_rate = max(0, rx - prev[0]) / elapsed / 1024.0
            tx_rate = max(0, tx - prev[1]) / elapsed / 1024.0
        labels[f"net:{name}:rx"] = f"{kind} {name} RX {ip_addr} {state}"
        labels[f"net:{name}:tx"] = f"{kind} {name} TX {ip_addr} {state}"
        points.append((f"net:{name}:rx", rx_rate))
        points.append((f"net:{name}:tx", tx_rate))

    points.append(("disk:/", disk))
    usb = usb_devices()
    labels["usb:devices"] = "USB stage " + (" | ".join(usb[:3]) if usb else "no devices")
    points.append(("usb:devices", float(len(usb))))

    fan = fan_status()
    if fan:
        labels["fan:speed"] = f"fan {fan['mode']} {fan['label_value']} enable={fan['enable']} raw={fan['pwm_raw']}"
        points.append(("fan:speed", fan["chart_value"]))

    for zone, ztype, temp in thermal():
        key = f"temp:{zone}"
        labels[key] = f"temp {zone} {ztype}"
        points.append((key, temp))
    return points, next_cpu, next_net


def summary_line(labels: dict[str, str], prefix: str) -> str:
    parts = [label for key, label in labels.items() if key.startswith(prefix)]
    return " | ".join(parts) if parts else "none"


def clip(text: object, width: int) -> str:
    raw = str(text)
    if width <= 0:
        return ""
    if len(raw) <= width:
        return raw
    if width <= 3:
        return raw[:width]
    return raw[: width - 3] + "..."


def draw_box(stdscr: curses.window, y: int, x: int, h: int, w: int, title: str, pair: int = WHITE_PAIR) -> None:
    if h < 2 or w < 2:
        return
    attr = curses.color_pair(pair)
    stdscr.addch(y, x, curses.ACS_ULCORNER, attr)
    stdscr.addch(y, x + w - 1, curses.ACS_URCORNER, attr)
    stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER, attr)
    stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER, attr)
    for col in range(x + 1, x + w - 1):
        stdscr.addch(y, col, curses.ACS_HLINE, attr)
        stdscr.addch(y + h - 1, col, curses.ACS_HLINE, attr)
    for row in range(y + 1, y + h - 1):
        stdscr.addch(row, x, curses.ACS_VLINE, attr)
        stdscr.addch(row, x + w - 1, curses.ACS_VLINE, attr)
    if title:
        stdscr.addnstr(y, x + 2, f" {title} ", max(0, w - 4), attr | curses.A_BOLD)


def draw_tabs(stdscr: curses.window, view: str, cols: int) -> None:
    tabs = [("overview", "Overview"), ("tasks", "Tasks")]
    x = 2
    for key, label in tabs:
        active = key == view
        text = f" {label} "
        attr = curses.color_pair(GREEN_PAIR if active else WHITE_PAIR)
        if active:
            attr |= curses.A_BOLD | curses.A_REVERSE
        stdscr.addnstr(3, x, text, max(0, cols - x - 1), attr)
        x += len(text) + 1


def draw_status_line(stdscr: curses.window, view: str, labels: dict[str, str], cols: int) -> None:
    cpu_line = cpu_freq_summary()
    fan_line = labels.get("fan:speed", "fan none")
    usb_line = labels.get("usb:devices", "none")
    net_line = summary_line(labels, "net:")
    status = f"{time.strftime('%F %T')}  host={os.uname().nodename}  load={loadavg()}  freq={cpu_line}"
    stdscr.addnstr(0, 0, clip(status, cols - 1), cols - 1, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    meta = f"{fan_line}  |  usb {usb_line}  |  net {net_line}"
    stdscr.addnstr(1, 0, clip(meta, cols - 1), cols - 1, curses.color_pair(WHITE_PAIR))
    stdscr.addnstr(2, 0, "q quit | t toggle view | Up/Down move | Enter details", cols - 1, curses.color_pair(WHITE_PAIR))
    draw_tabs(stdscr, view, cols)


def draw_overview_once(items: list[str], history: dict[str, deque[float | None]], labels: dict[str, str]) -> None:
    print(f"{time.strftime('%F %T')} host={os.uname().nodename} load={loadavg()} freq={cpu_freq_summary()}")
    print(f"Network: {summary_line(labels, 'net:')}")
    print(f"USB Stage: {labels.get('usb:devices', 'none')}")
    print(f"Fan: {labels.get('fan:speed', 'none')}")
    print(f"{'ITEM':<26} {'NOW':>12} {'STATUS':>8}")
    for idx, key in enumerate(items):
        value = history[key][-1] if history.get(key) else None
        status = {WHITE_PAIR: "white", GREEN_PAIR: "green", YELLOW_PAIR: "yellow", RED_PAIR: "red"}[color_pair_for(key, value)]
        print(f"{labels.get(key, key):<26.26} {value_text(key, value):>12} {status:>8}")


def draw_task_once(tasks: list[dict[str, str]], cores: list[dict[str, object]], labels: dict[str, str]) -> None:
    print(f"{time.strftime('%F %T')} host={os.uname().nodename} view=tasks load={loadavg()}")
    core_text: list[str] = []
    for row in cores:
        usage = row["usage"]
        core_text.append(f"{row['name']}={'NA' if usage is None else f'{usage:.1f}%'}")
    print("CPU cores: " + " | ".join(core_text))
    print(f"{'PID':>6} {'CPU%':>6} {'MEM%':>6} {'STAT':<5} COMMAND")
    for row in tasks:
        print(f"{row['pid']:>6} {row['cpu']:>6} {row['mem']:>6} {row['stat']:<5} {row['comm']}")


def draw_overview_view(
    stdscr: curses.window,
    items: list[str],
    selected: int,
    history: dict[str, deque[float | None]],
    labels: dict[str, str],
    tasks: list[dict[str, str]],
    cores: list[dict[str, object]],
    fan: dict[str, object] | None,
) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    draw_status_line(stdscr, "overview", labels, cols)
    if rows < 16 or cols < 88:
        stdscr.addnstr(5, 2, "terminal too small; enlarge it for the split dashboard", cols - 4, curses.color_pair(YELLOW_PAIR))
        stdscr.refresh()
        return

    pane_y = 5
    pane_h = rows - 7
    left_w = min(45, max(34, cols // 2))
    right_x = left_w + 1
    right_w = max(24, cols - right_x - 2)
    draw_box(stdscr, pane_y, 1, pane_h, left_w - 1, "Telemetry")
    draw_box(stdscr, pane_y, right_x, pane_h, right_w, "Detail")

    visible_h = pane_h - 2
    start = max(0, min(selected - visible_h // 2, max(len(items) - visible_h, 0)))
    stdscr.addnstr(pane_y + 1, 3, f"{'ITEM':<24} {'NOW':>11}", left_w - 5, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    for screen_row, idx in enumerate(range(start, min(start + visible_h, len(items))), start=pane_y + 2):
        key = items[idx]
        value = history[key][-1] if history.get(key) else None
        attr = curses.color_pair(WHITE_PAIR if idx % 2 == 0 else GREEN_PAIR)
        if idx == selected:
            attr |= curses.A_REVERSE | curses.A_BOLD
        stdscr.addnstr(screen_row, 3, f"{clip(labels.get(key, key), 24):<24}", 24, attr)
        stdscr.addnstr(screen_row, 28, f"{value_text(key, value):>11}", 11, curses.color_pair(color_pair_for(key, value)) | (curses.A_REVERSE if idx == selected else 0))

    selected_key = items[selected]
    selected_value = history[selected_key][-1] if history.get(selected_key) else None
    now_value, avg_value, max_value = history_stats(history[selected_key])
    pair = color_pair_for(selected_key, selected_value)
    inner_x = right_x + 1
    inner_y = pane_y + 1
    inner_w = right_w - 2
    inner_h = pane_h - 2
    stdscr.addnstr(inner_y, inner_x, clip(f"{labels.get(selected_key, selected_key)}", inner_w), inner_w, curses.color_pair(pair) | curses.A_BOLD)
    stdscr.addnstr(inner_y + 1, inner_x, clip(f"now {value_text(selected_key, now_value)}  avg {value_text(selected_key, avg_value)}  max {value_text(selected_key, max_value)}", inner_w), inner_w, curses.color_pair(WHITE_PAIR))

    if selected_key == "cpu":
        core_pairs = [row for row in cores if row["usage"] is not None]
        stdscr.addnstr(inner_y + 3, inner_x, "CPU cores", inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
        for i, row in enumerate(core_pairs[: max(0, inner_h - 10)]):
            pct = row["usage"]
            bar_w = max(8, inner_w - 12)
            filled = int(round((pct or 0.0) * bar_w / 100.0))
            bar = "#" * filled + "." * max(0, bar_w - filled)
            stdscr.addnstr(inner_y + 4 + i, inner_x, f"{row['name']:<4} [{bar}] {pct:5.1f}%", inner_w, curses.color_pair(GREEN_PAIR if (pct or 0) < 70 else YELLOW_PAIR if (pct or 0) < 90 else RED_PAIR))
        task_start = inner_y + 4 + min(len(core_pairs), max(0, inner_h - 10))
        stdscr.addnstr(task_start, inner_x, "Top tasks", inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
        for i, row in enumerate(tasks[: max(0, inner_h - (task_start - inner_y) - 2)], start=1):
            stdscr.addnstr(task_start + i, inner_x, clip(f"{row['pid']:>6} {row['cpu']:>5}% {row['comm']}", inner_w), inner_w, curses.color_pair(WHITE_PAIR))
    elif selected_key == "fan:speed" and fan:
        mode = fan.get("mode", "pwm")
        stdscr.addnstr(inner_y + 3, inner_x, f"mode {mode}", inner_w, curses.color_pair(WHITE_PAIR))
        stdscr.addnstr(inner_y + 4, inner_x, f"speed {fan.get('label_value', 'NA')}  pwm {fan.get('pwm_raw', 'NA')}  enable {fan.get('enable', 'NA')}", inner_w, curses.color_pair(WHITE_PAIR))
        stdscr.addnstr(inner_y + 6, inner_x, "trend", inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
        wave_h = max(4, inner_h - 8)
        wave_w = max(8, inner_w - 10)
        wave, scale = area_chart(selected_key, history[selected_key], wave_w, wave_h)
        stdscr.addnstr(inner_y + 7, inner_x, f"range {scale}", inner_w, curses.color_pair(WHITE_PAIR))
        for i, line in enumerate(wave[: wave_h]):
            stdscr.addnstr(inner_y + 8 + i, inner_x, clip(line, inner_w), inner_w, curses.color_pair(pair))
    else:
        warn, crit = chart_thresholds(selected_key)
        wave_h = max(4, inner_h - 6)
        wave_w = max(8, inner_w - 10)
        wave, scale = area_chart(selected_key, history[selected_key], wave_w, wave_h)
        stdscr.addnstr(inner_y + 3, inner_x, f"range {scale}", inner_w, curses.color_pair(WHITE_PAIR))
        if warn is not None and crit is not None:
            stdscr.addnstr(inner_y + 4, inner_x, f"warn {warn:.0f}  critical {crit:.0f}", inner_w, curses.color_pair(WHITE_PAIR))
        for i, line in enumerate(wave[: wave_h]):
            stdscr.addnstr(inner_y + 5 + i, inner_x, clip(line, inner_w), inner_w, curses.color_pair(pair))

    stdscr.refresh()


def draw_task_view(
    stdscr: curses.window,
    tasks: list[dict[str, str]],
    selected: int,
    cores: list[dict[str, object]],
    labels: dict[str, str],
) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()
    draw_status_line(stdscr, "tasks", labels, cols)
    if rows < 16 or cols < 88:
        stdscr.addnstr(5, 2, "terminal too small; enlarge it for the task dashboard", cols - 4, curses.color_pair(YELLOW_PAIR))
        stdscr.refresh()
        return

    pane_y = 5
    pane_h = rows - 7
    left_w = min(52, max(40, cols // 2 + 4))
    right_x = left_w + 1
    right_w = max(24, cols - right_x - 2)
    draw_box(stdscr, pane_y, 1, pane_h, left_w - 1, "Tasks")
    draw_box(stdscr, pane_y, right_x, pane_h, right_w, "CPU Detail")

    visible_h = pane_h - 2
    selected = max(0, min(selected, max(len(tasks) - 1, 0)))
    stdscr.addnstr(pane_y + 1, 3, f"{'PID':>6} {'CPU%':>6} {'MEM%':>6} {'PSR':>4} {'CMD':<20}", left_w - 5, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    start = max(0, min(selected - visible_h // 2, max(len(tasks) - visible_h, 0)))
    for screen_row, idx in enumerate(range(start, min(start + visible_h, len(tasks))), start=pane_y + 2):
        row = tasks[idx]
        attr = curses.color_pair(WHITE_PAIR if idx % 2 == 0 else GREEN_PAIR)
        if idx == selected:
            attr |= curses.A_REVERSE | curses.A_BOLD
        stdscr.addnstr(screen_row, 3, f"{row['pid']:>6} {row['cpu']:>6} {row['mem']:>6} {row['psr']:>4} {clip(row['comm'], 20):<20}", left_w - 5, attr)

    selected_row = tasks[selected] if tasks else None
    if not selected_row:
        stdscr.refresh()
        return

    detail = task_detail(selected_row["pid"])
    inner_x = right_x + 1
    inner_y = pane_y + 1
    inner_w = right_w - 2
    inner_h = pane_h - 2
    title = clip(f"{selected_row['pid']} {selected_row['comm']}", inner_w)
    stdscr.addnstr(inner_y, inner_x, title, inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    base_line = detail or selected_row
    stdscr.addnstr(inner_y + 1, inner_x, clip(f"cpu {base_line.get('cpu', selected_row['cpu'])}%  mem {base_line.get('mem', selected_row['mem'])}%  rss {base_line.get('rss', selected_row['rss'])} KiB", inner_w), inner_w, curses.color_pair(WHITE_PAIR))
    stdscr.addnstr(inner_y + 2, inner_x, clip(f"pid {base_line.get('pid', selected_row['pid'])}  ppid {base_line.get('ppid', selected_row['ppid'])}  psr {base_line.get('psr', selected_row['psr'])}  pri {base_line.get('pri', 'NA')}  ni {base_line.get('ni', 'NA')}  stat {base_line.get('stat', selected_row['stat'])}", inner_w), inner_w, curses.color_pair(WHITE_PAIR))

    stdscr.addnstr(inner_y + 4, inner_x, "Per-core load", inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    core_rows = [row for row in cores if row["usage"] is not None]
    for i, row in enumerate(core_rows[: max(0, inner_h - 10)]):
        pct = float(row["usage"] or 0.0)
        bar_w = max(8, inner_w - 12)
        filled = int(round(pct * bar_w / 100.0))
        bar = "#" * filled + "." * max(0, bar_w - filled)
        pair = GREEN_PAIR if pct < 70 else YELLOW_PAIR if pct < 90 else RED_PAIR
        stdscr.addnstr(inner_y + 5 + i, inner_x, f"{row['name']:<4} [{bar}] {pct:5.1f}%", inner_w, curses.color_pair(pair))

    summary_y = inner_y + 5 + min(len(core_rows), max(0, inner_h - 10))
    stdscr.addnstr(summary_y, inner_x, "Command", inner_w, curses.color_pair(WHITE_PAIR) | curses.A_BOLD)
    stdscr.addnstr(summary_y + 1, inner_x, clip(base_line.get("args", selected_row["comm"]), inner_w), inner_w, curses.color_pair(WHITE_PAIR))
    stdscr.refresh()


def run_curses(args: argparse.Namespace) -> int:
    labels: dict[str, str] = {"cpu": "cpu usage", "memory": "memory used", "disk:/": "disk / used"}
    prev_cpu = cpu_times()
    prev_net = net_counters()
    prev_core = cpu_core_times()
    last_sample = time.time()
    view = "tasks" if args.task_view else "overview"
    time.sleep(min(args.interval, 0.25))
    now = time.time()
    points, prev_cpu, prev_net = collect_points(prev_cpu, prev_net, now - last_sample, labels)
    core_rows, prev_core = cpu_core_usage(prev_core)
    tasks = task_rows(args.task_limit)
    fan = fan_status()
    last_sample = now
    items = [key for key, _ in points]
    history: dict[str, deque[float | None]] = {key: deque(maxlen=args.history) for key in items}
    for key, value in points:
        history[key].append(value)

    if args.once or not sys.stdin.isatty() or not sys.stdout.isatty():
        if view == "tasks":
            draw_task_once(tasks, core_rows, labels)
        else:
            draw_overview_once(items, history, labels)
        return 0

    selected = 0

    def loop(stdscr: curses.window) -> None:
        nonlocal selected, prev_cpu, prev_net, prev_core, last_sample, items, history, view, tasks, core_rows, fan
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
                points, prev_cpu, prev_net = collect_points(prev_cpu, prev_net, now - last_sample, labels)
                core_rows, prev_core = cpu_core_usage(prev_core)
                tasks = task_rows(args.task_limit)
                fan = fan_status()
                last_sample = now
                new_items = [key for key, _ in points]
                for key in new_items:
                    history.setdefault(key, deque(maxlen=args.history))
                for key, value in points:
                    history[key].append(value)
                items = new_items
                selected = max(0, min(selected, len(items) - 1))
                if view == "tasks":
                    selected = max(0, min(selected, len(tasks) - 1))
                    draw_task_view(stdscr, tasks, selected, core_rows, labels)
                else:
                    draw_overview_view(stdscr, items, selected, history, labels, tasks, core_rows, fan)
                next_update = now + args.interval

            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("t"), ord("T")):
                view = "tasks" if view == "overview" else "overview"
                selected = 0
                next_update = 0.0
                if view == "tasks":
                    draw_task_view(stdscr, tasks, selected, core_rows, labels)
                else:
                    draw_overview_view(stdscr, items, selected, history, labels, tasks, core_rows, fan)
                continue
            if key in (curses.KEY_UP, ord("k"), ord("K")):
                if view == "tasks":
                    selected = max(0, selected - 1)
                    draw_task_view(stdscr, tasks, selected, core_rows, labels)
                else:
                    selected = max(0, selected - 1)
                    draw_overview_view(stdscr, items, selected, history, labels, tasks, core_rows, fan)
            elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
                if view == "tasks":
                    selected = min(len(tasks) - 1, selected + 1)
                    draw_task_view(stdscr, tasks, selected, core_rows, labels)
                else:
                    selected = min(len(items) - 1, selected + 1)
                    draw_overview_view(stdscr, items, selected, history, labels, tasks, core_rows, fan)
            elif key in (curses.KEY_ENTER, 10, 13):
                next_update = 0.0
            time.sleep(0.03)

    curses.wrapper(loop)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live TaishanPi board telemetry dashboard.")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds")
    parser.add_argument("--history", type=int, default=120, help="history points per item")
    parser.add_argument("--task-limit", type=int, default=12, help="number of tasks to show in the task view")
    parser.add_argument("--task-view", action="store_true", help="start in the task view")
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
