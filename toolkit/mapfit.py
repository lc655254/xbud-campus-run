#!/usr/bin/env python3
"""Capture the AMap view and fit a screen<->GCJ affine transform.

The map moves between calls (auto-centering, zoom animation), so anchors and
the screenshot must be captured together.  Four projected points are fitted
with least squares; the residual is printed so a bad capture is obvious.

Usage:
  python mapfit.py --zoom 16
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time

import numpy as np

import autorun as a

PROJECTIONS = [(120, 260), (960, 260), (120, 1660), (960, 1660)]
OUT = r"D:\Work\xbud_run_logs\mapfit.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zoom", type=float, default=16.0)
    parser.add_argument("--settle", type=float, default=4.0)
    args = parser.parse_args()

    a.send(f"zoom {args.zoom}")
    time.sleep(args.settle)
    shot = a.shot(f"mapfit_z{args.zoom:g}")

    # Project after the screenshot so both refer to the same static frame.
    for x, y in PROJECTIONS:
        a.send(f"project {x} {y}")
        time.sleep(0.7)
    raw = a.adb("logcat", "-d", "-t", "400")

    samples = {}
    for line in raw.splitlines():
        m = re.search(r"PROJFROM (\d+\.?\d*),(\d+\.?\d*) -> ([0-9.]+),([0-9.]+)", line)
        if m:
            samples[(float(m.group(1)), float(m.group(2)))] = (float(m.group(3)), float(m.group(4)))
    print("projections:", samples)
    if len(samples) < 3:
        print("not enough projections")
        return 1

    xs = np.array([p[0] for p in samples])
    ys = np.array([p[1] for p in samples])
    lats = np.array([v[0] for v in samples.values()])
    lngs = np.array([v[1] for v in samples.values()])

    # lat = a0 + a1*y ; lng = b0 + b1*x  (north-up WebMercator, locally linear)
    ay, by = np.polyfit(ys, lats, 1)
    bx, cx = np.polyfit(xs, lngs, 1)
    lat_res = np.max(np.abs(np.polyval([ay, by], ys) - lats))
    lng_res = np.max(np.abs(np.polyval([bx, cx], xs) - lngs))
    print(f"scale: {abs(bx):.3e} deg/px lng, {abs(ay):.3e} deg/px lat")
    print(f"residual: {lat_res*111320:.1f} m lat, {lng_res*93000:.1f} m lng")

    data = {
        "shot": shot,
        "zoom": args.zoom,
        "lat_per_px": float(ay),
        "lat0": float(by),
        "lng_per_px": float(bx),
        "lng0": float(cx),
        "samples": {f"{k[0]},{k[1]}": v for k, v in samples.items()},
    }
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
