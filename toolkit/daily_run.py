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
import os
import subprocess
import sys
import time

import adbcfg
import autorun as a
import trackgen

ADB = adbcfg.ADB
MUMU = adbcfg.MUMU
TOOLKIT = r"D:\Work\xbud_toolkit"
MIN_KM = 2.5


def run(cmd: list[str], timeout: int = 120) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (proc.stdout or "") + (proc.stderr or "")


def device_online() -> bool:
    return adbcfg.is_online(adbcfg.serial(refresh=True))


def connect(verbose: bool = True) -> bool:
    adbcfg.serial(refresh=True)
    if adbcfg.connect(adbcfg.serial()):
        if verbose:
            a.log(f"device: {adbcfg.describe()}")
        return True
    return False


def ensure_device() -> bool:
    """Connect to whichever MuMu instance is up; launch one only if none is."""
    if connect():
        return True

    running = adbcfg.mumu_running()
    if running:
        a.log(f"MuMu running but adb not reachable yet: {adbcfg.describe()}")
    else:
        index = os.environ.get("XBUD_MUMU_INDEX", "0").strip() or "0"
        a.log(f"no MuMu instance running, launching instance {index}")
        run([MUMU, "control", "-v", index, "launch"], timeout=180)
        time.sleep(5)

    for i in range(18):
        if connect(verbose=False):
            a.log(f"device ready: {adbcfg.describe()}")
            return True
        time.sleep(10)
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

