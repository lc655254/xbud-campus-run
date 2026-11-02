#!/usr/bin/env python3
"""Drive 小步点 through its assigned checkpoints using the LSPosed module.

Requires:
  - com.dsh.xbudloc installed and enabled for run.xbud.android
  - module command channel /data/local/tmp/xbud_cmd
  - a running 学分跑 session with the map visible

The module exposes `project x y` (screen -> GCJ02) and `target lat,lng`
(WGS84 waypoint).  This script turns the yellow pin into a target, waits for
arrival, and repeats while the pin keeps moving.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
import time
import adbcfg

ADB = adbcfg.ADB
SERIAL = adbcfg.serial()
CMD = "/data/local/tmp/xbud_cmd"


def adb(*args: str) -> str:
    proc = subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return (proc.stdout or "") + (proc.stderr or "")


def screenshot(path: str) -> None:
    adb("shell", "screencap -p /sdcard/_cp.png")
    adb("pull", "/sdcard/_cp.png", path)


def send(command: str) -> None:
    adb("shell", f"echo '{command}' > {CMD}")


def logcat_tail() -> str:
    return adb("logcat", "-d", "-t", "400")


def project(x: int, y: int) -> tuple[float, float] | None:
    send(f"project {x} {y}")
    deadline = time.time() + 5
    while time.time() < deadline:
        text = logcat_tail()
        matches = re.findall(r"PROJFROM \d+,\d+ -> ([0-9.]+),([0-9.]+)", text)
        if matches:
            lat, lng = matches[-1]
            return float(lat), float(lng)
        time.sleep(0.5)
    return None


def gcj_to_wgs(lat: float, lng: float) -> tuple[float, float]:
    a = 6378245.0
    ee = 0.00669342162296594323

    def transform_lat(x: float, y: float) -> float:
        r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
        r += 0.2 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return r

    def transform_lng(x: float, y: float) -> float:
        r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y
        r += 0.1 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    def wgs_to_gcj(wlat: float, wlng: float) -> tuple[float, float]:
        dlat = transform_lat(wlng - 105.0, wlat - 35.0)
        dlng = transform_lng(wlng - 105.0, wlat - 35.0)
        rad = wlat / 180.0 * math.pi
        magic = 1 - ee * math.sin(rad) ** 2
        sqrt_magic = math.sqrt(magic)
        dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
        dlng = (dlng * 180.0) / (a / sqrt_magic * math.cos(rad) * math.pi)
        return wlat + dlat, wlng + dlng

    wlat, wlng = lat, lng
    for _ in range(30):
        glat, glng = wgs_to_gcj(wlat, wlng)
        dlat, dlng = glat - lat, glng - lng
        wlat -= dlat
        wlng -= dlng
        if abs(dlat) < 1e-10 and abs(dlng) < 1e-10:
            break
    return wlat, wlng


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def detect_pin(path: str) -> tuple[int, int] | None:
    from pin import find_pin

    return find_pin(path)


def main() -> int:
    max_points = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    last_target: tuple[float, float] | None = None
    for index in range(1, max_points + 1):
        screenshot(r"D:\Work\_cp.png")
        pin = detect_pin(r"D:\Work\_cp.png")
        if pin is None:
            print(f"[{index}] no yellow pin visible; checkpoints done or map moved")
            break
        projected = project(*pin)
        if projected is None:
            print(f"[{index}] projection failed at {pin}")
            break
        target = gcj_to_wgs(*projected)
        if last_target and distance(last_target, target) < 25:
            print(f"[{index}] pin still at {target[0]:.6f},{target[1]:.6f}; waiting")
            time.sleep(10)
            continue
        last_target = target
        print(f"[{index}] pin={pin} gcj={projected} wgs={target[0]:.6f},{target[1]:.6f}")
        send(f"target {target[0]:.6f},{target[1]:.6f}")
        # Wait for arrival, then give the app time to register the checkpoint.
        deadline = time.time() + 600
        arrived = False
        while time.time() < deadline:
            text = logcat_tail()
            if f"ARRIVED {target[0]:.6f},{target[1]:.6f}" in text:
                arrived = True
                break
            time.sleep(3)
        print(f"[{index}] arrived={arrived}")
        time.sleep(8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
