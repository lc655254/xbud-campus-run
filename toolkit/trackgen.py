#!/usr/bin/env python3
"""Generate a track-style route: warm-up leg + laps on a 400 m oval.

The app's own assignment often points at 「田径场」, and the school asks for the
run to happen on the track.  A track run is: walk onto the ring, then N laps in
one lane, with occasional lane changes when overtaking.

Geometry is a standard 400 m oval (two 84.39 m straights + two 36.5 m radius
semicircles); each lap gets its own lane offset so consecutive laps draw as
concentric rings instead of one identical line.

Usage:
  python trackgen.py --center 33.55057,119.05091 --laps 8 --push
  python trackgen.py --center 33.55057,119.05091 --orientation ns --preview
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess

from routegen import gcj_to_wgs

ADB = r"D:\MuMuPlayer\nx_device\15.0\shell\adb.exe"
SERIAL = "127.0.0.1:16384"
DEVICE_ROUTE = "/data/data/run.xbud.android/files/xbud_route.json"
TMP_ROUTE = "/data/local/tmp/xbud_route.json"

STRAIGHT = 84.39      # metres, lane 1
RADIUS = 36.5         # metres, lane 1


class Frame:
    """Local metre frame around the track centre (GCJ input, WGS output)."""

    def __init__(self, lat: float, lng: float):
        self.lat = lat
        self.lng = lng
        self.m_lat = 111320.0
        self.m_lng = 111320.0 * math.cos(math.radians(lat))

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        """Local metres -> WGS84 (the module feeds WGS to GNSS)."""
        gcj_lat = self.lat + y / self.m_lat
        gcj_lng = self.lng + x / self.m_lng
        return gcj_to_wgs(gcj_lat, gcj_lng)


def oval_points(lane: float, orientation: str, rng: random.Random,
                spacing: float = 3.5) -> list[tuple[float, float]]:
    """One lap of the oval at `lane` metres outside lane 1, counter-clockwise."""
    r = RADIUS + lane
    straight = STRAIGHT
    pts: list[tuple[float, float]] = []

    def push(x: float, y: float) -> None:
        if orientation == "ns":          # long axis north-south
            x, y = y, -x
        # Slight inside/outside drift, like a runner not holding a perfect line.
        pts.append((x + rng.uniform(-0.25, 0.25), y + rng.uniform(-0.25, 0.25)))

    # bottom straight, left -> right
    steps = max(4, int(straight / 4.0))
    for i in range(steps):
        push(-straight / 2 + straight * i / steps, -r)
    # right semicircle
    steps = max(6, int(math.pi * r / 4.0))
    for i in range(steps):
        a = -math.pi / 2 + math.pi * i / steps
        push(straight / 2 + r * math.cos(a), r * math.sin(a))
    # top straight, right -> left
    for i in range(steps):
        push(straight / 2 - straight * i / steps, r)
    # left semicircle
    for i in range(steps):
        a = math.pi / 2 + math.pi * i / steps
        push(-straight / 2 + r * math.cos(a), r * math.sin(a))

    # Resample at a constant spacing so the module's linear interpolation is smooth.
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= spacing:
            out.append(p)
    return out


def build(center_gcj: tuple[float, float], laps: int, orientation: str,
          seed: int) -> dict:
    rng = random.Random(seed)
    frame = Frame(*center_gcj)

    # Warm-up: start at the track centre (where the固定点 pin usually sits) and
    # walk out to the ring, then start lapping.
    warm: list[tuple[float, float]] = []
    for i in range(0, 60, 3):
        warm.append((0.0, -i))

    lanes = [0.0, 0.0, 1.2, 1.2, 2.2, 0.6, 0.0, 3.0, 1.5, 0.0]
    pts = list(warm)
    for lap in range(laps):
        lane = lanes[lap % len(lanes)] + rng.uniform(-0.2, 0.2)
        ring = oval_points(lane, orientation, rng)
        # Join the ring at the point closest to where we are.
        start = min(range(len(ring)),
                    key=lambda i: math.hypot(ring[i][0] - pts[-1][0],
                                             ring[i][1] - pts[-1][1]))
        pts.extend(ring[start:] + ring[:start])

    wgs = [list(frame.to_latlon(x, y)) for x, y in pts]
    wgs = [[round(a, 7), round(b, 7)] for a, b in wgs]

    # Pace: steady track rhythm with a small per-lap drift.
    profile = []
    acc = 0.0
    for i in range(len(pts)):
        if i:
            acc += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        v = 4.15 + 0.12 * math.sin(acc / 210.0) + 0.05 * math.sin(acc / 23.0)
        if acc < 80:
            v -= 0.35 * (1 - acc / 80.0)
        profile.append([round(acc, 1), round(min(4.4, max(3.6, v)), 3)])

    return {"speed": 4.1, "points": wgs, "profile": profile}


def read_track_center() -> tuple[float, float]:
    """Read the 田径场 marker (or the first marker) from the running app."""
    import time

    import autorun as a

    a.send("markers")
    time.sleep(1.2)
    raw = a.adb("logcat", "-d", "-t", "600")
    matches = []
    fallback = None
    for line in raw.splitlines():
        if "XbudLoc: MARKER" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            lat, lng = parts[-1].strip().split(",")
            point = (float(lat), float(lng))
        except ValueError:
            continue
        title = parts[0]
        if "田径场" in title or "操场" in title:
            matches.append(point)
        fallback = fallback or point
    if matches:
        # The assignment rotates between 田径场1/2/3 inside the same sports area;
        # their centroid is the field centre.
        lats = [p[0] for p in matches]
        lngs = [p[1] for p in matches]
        return sum(lats) / len(lats), sum(lngs) / len(lngs)
    if fallback:
        return fallback
    raise SystemExit("no marker found; pass --center lat,lng")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--center", required=True,
                        help="track centre as GCJ 'lat,lng', or 'auto' to read the "
                             "田径场 marker from the running app")
    parser.add_argument("--laps", type=int, default=8)
    parser.add_argument("--orientation", choices=["ew", "ns"], default="ns")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=r"D:\Work\xbud_route.json")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    if args.center == "auto":
        center = read_track_center()
    else:
        center = tuple(float(v) for v in args.center.split(","))
    data = build(center, args.laps, args.orientation, args.seed or random.randrange(1 << 20))
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))

    total = data["profile"][-1][0]
    print(f"points={len(data['points'])} distance={total:.0f} m, laps={args.laps}, "
          f"orientation={args.orientation}")

    if args.push:
        subprocess.run([ADB, "-s", SERIAL, "push", args.out, TMP_ROUTE], check=True,
                       capture_output=True)
        subprocess.run([ADB, "-s", SERIAL, "shell",
                        f"su -c 'cat {TMP_ROUTE} > {DEVICE_ROUTE}; chmod 666 {DEVICE_ROUTE}'"],
                       check=True, capture_output=True)
        print("pushed", DEVICE_ROUTE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

