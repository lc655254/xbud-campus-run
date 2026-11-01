#!/usr/bin/env python3
"""One command for the daily credit run.

    python daily_run.py                # track laps, 2.5 km, verify counters
    python daily_run.py --mode campus  # random campus loop instead
    python daily_run.py --check        # environment + counters only

This is the same flow as daily.ps1 but implemented in Python: PowerShell 5.1
keeps hanging on adb's daemonised output and mangles UTF-8 scripts, so the
.ps1 wrapper only forwards to this file.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import autorun as a
import trackgen

ADB = trackgen.ADB
SERIAL = trackgen.SERIAL
MUMU = r"D:\MuMuPlayer\nx_main\MuMuManager.exe"
TOOLKIT = r"D:\Work\xbud_toolkit"
MIN_KM = 2.5


def run(cmd: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (proc.stdout or "") + (proc.stderr or "")


def device_online() -> bool:
    return f"{SERIAL}\tdevice" in run([ADB, "devices"])


def connect(verbose: bool = True) -> bool:
    run([ADB, "start-server"], timeout=60)
    run([ADB, "connect", SERIAL], timeout=60)
    for _ in range(3):
        if device_online():
            return True
        time.sleep(2)
    return False


def ensure_device() -> bool:
    if connect():
        return True
    info = run([MUMU, "info", "-v", "0"], timeout=60)
    if '"is_android_started": true' not in info.replace('":true', '": true'):
        a.log("MuMu is not started, launching")
        run([MUMU, "control", "-v", "0", "launch"], timeout=120)
    else:
        a.log("MuMu is running but adb is not connected yet")
    for i in range(18):
        time.sleep(10)
        if connect(verbose=False):
            return True
        a.log(f"waiting for device ({i})")
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["track", "campus"], default="track")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--start-only", action="store_true",
                        help="只负责开跑（建路线 -> reset/hold -> 播放 -> go），不跑完不收尾")
    parser.add_argument("--laps", type=int, default=8)
    parser.add_argument("--min-km", type=float, default=MIN_KM)
    args = parser.parse_args()

    a.log(f"=== daily run start (mode={args.mode}) ===")
    if not ensure_device():
        a.log("device offline, abort")
        return 1
    a.log("device online")

    # Record the counter before touching anything, so every day has a clean
    # before/after pair for the 计入次数 investigation.
    a.goto_home()
    before = a.home_count()
    a.log(f"home counts before: {before}")

    if args.check:
        a.log("focus: " + a.focus())
        a.goto_home()
        a.log(f"home counts: {a.home_count()}")
        return 0

    # Route must be pushed before the app process starts.
    if args.mode == "track":
        a.log("build track route (田径场 laps, north-south)")
        out = run([sys.executable, TOOLKIT + r"\track_route.py", "--laps", str(args.laps)],
                  timeout=300)
    else:
        a.log("build campus route (random loop)")
        out = run([sys.executable, TOOLKIT + r"\routegen.py",
                   "--out", r"D:\Work\xbud_route.json", "--push"], timeout=300)
    print(out.strip())
    if "pushed" not in out:
        a.log("route push failed, abort")
        return 1

    a.adb("shell", "am force-stop run.xbud.android")
    time.sleep(2)

    a.log(f"run: min-km={args.min_km}")
    ok = a.one_run(900, args.min_km)
    a.log(f"run finished: {ok}")

    a.goto_home()
    counts = a.home_count()
    a.log(f"home counts: {counts}")
    a.log("=== daily run done ===")
    return 0 if counts else 1


if __name__ == "__main__":
    raise SystemExit(main())

