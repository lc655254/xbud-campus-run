#!/usr/bin/env python3
"""One-shot: read the 田径场 marker from the app, build a track route, push it.

Order matters: the module only knows a route when the app process starts, so the
flow is

  1. launch the app and open 学分跑 (this loads the map + the 固定点 marker)
  2. read the marker GCJ, generate the oval route, push it
  3. force-stop the app so the next launch picks the new route up

Usage:
  python track_route.py --laps 8
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time

import autorun as a
import trackgen

ADB = trackgen.ADB
SERIAL = trackgen.SERIAL
DEVICE_ROUTE = trackgen.DEVICE_ROUTE
TMP_ROUTE = trackgen.TMP_ROUTE


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--laps", type=int, default=8)
    parser.add_argument("--orientation", choices=["ew", "ns"], default="ns")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=r"D:\Work\xbud_route.json")
    args = parser.parse_args()

    a.goto_home()
    if not a.open_start():
        print("could not open the run start page")
        return 1
    time.sleep(4)                       # let the map draw the 固定点 marker

    center = trackgen.read_track_center()
    print(f"track centre GCJ {center[0]:.7f},{center[1]:.7f}")

    data = trackgen.build(center, args.laps, args.orientation, args.seed or int(time.time()) % 100000)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"route: {len(data['points'])} points, {data['profile'][-1][0]:.0f} m")

    subprocess.run([ADB, "-s", SERIAL, "push", args.out, TMP_ROUTE], check=True, capture_output=True)
    subprocess.run([ADB, "-s", SERIAL, "shell",
                    f"su -c 'cat {TMP_ROUTE} > {DEVICE_ROUTE}; chmod 666 {DEVICE_ROUTE}'"],
                   check=True, capture_output=True)
    print("pushed", DEVICE_ROUTE)

    # The module loads the route at process start.
    a.adb("shell", "am force-stop run.xbud.android")
    print("app stopped; next launch uses the new route")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

