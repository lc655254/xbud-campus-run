#!/usr/bin/env python3
"""Generate a realistic, non-repeating campus route for the xbudloc module.

The old route was a 380 m rectangle traversed ~21 times, so every lap drew the
same line and the uploaded/displayed track looked synthetic.  This generator
builds one long closed loop instead:

  * the campus fence (AMap polygon, GCJ02) is inset by a random margin and the
    edges are subdivided with perpendicular noise, so the outline is wavy
    rather than a perfect quadrilateral;
  * two interior detours are spliced in (out and back along different lanes);
  * the whole polyline is Catmull-Rom smoothed and resampled every ~4 m;
  * a per-distance speed profile is derived from path curvature plus a warm-up
    and a finishing kick, so pace drifts the way a real runner's does.

The run only needs ~3.1 km, while the loop is ~3.8 km, so the module never
repeats a lap during a single session.

Usage:
  python routegen.py --out D:\\Work\\xbud_route.json
  python routegen.py --seed 7 --length 4000 --push
"""

from __future__ import annotations

import argparse
import json
import math
import random
import adbcfg

ADB = adbcfg.ADB
DEVICE_ROUTE = "/data/data/run.xbud.android/files/xbud_route.json"
TMP_ROUTE = "/data/local/tmp/xbud_route.json"

# 琛屽妤?fixed point, WGS84 (what the module feeds to GNSS)
START_WGS = (33.5526112, 119.0411099)

# Campus fence as reported by AMap (GCJ02), dumped with the `polygon` command.
FENCE_GCJ = [
    (33.55436426, 119.04117762),
    (33.55382304, 119.05274382),
    (33.54883792, 119.05231005),
    (33.54941376, 119.04079667),
]


# ------------------------------------------------------------------ coordinates
def gcj_to_wgs(lat: float, lng: float) -> tuple[float, float]:
    """Inverse of the GCJ-02 obfuscation (iterative, converges in ~4 rounds)."""
    a = 6378245.0
    ee = 0.00669342162296594323

    def tlat(x: float, y: float) -> float:
        r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return r

    def tlng(x: float, y: float) -> float:
        r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    def wgs_to_gcj(wlat: float, wlng: float) -> tuple[float, float]:
        dlat = tlat(wlng - 105.0, wlat - 35.0)
        dlng = tlng(wlng - 105.0, wlat - 35.0)
        rad = wlat / 180.0 * math.pi
        magic = 1 - ee * math.sin(rad) ** 2
        sm = math.sqrt(magic)
        dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sm) * math.pi)
        dlng = (dlng * 180.0) / (a / sm * math.cos(rad) * math.pi)
        return wlat + dlat, wlng + dlng

    wlat, wlng = lat, lng
    for _ in range(20):
        glat, glng = wgs_to_gcj(wlat, wlng)
        dlat, dlng = glat - lat, glng - lng
        wlat -= dlat
        wlng -= dlng
        if abs(dlat) < 1e-11 and abs(dlng) < 1e-11:
            break
    return wlat, wlng


class LocalFrame:
    """Equirectangular projection around the start point (metres)."""

    def __init__(self, lat0: float, lng0: float):
        self.lat0 = lat0
        self.lng0 = lng0
        self.m_lat = 111320.0
        self.m_lng = 111320.0 * math.cos(math.radians(lat0))

    def to_xy(self, lat: float, lng: float) -> tuple[float, float]:
        return (lng - self.lng0) * self.m_lng, (lat - self.lat0) * self.m_lat

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y / self.m_lat, self.lng0 + x / self.m_lng


def point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def seg_distance(p: tuple[float, float], a: tuple[float, float],
                 b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy or 1e-9
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def clearance(p: tuple[float, float], poly: list[tuple[float, float]]) -> float:
    if not point_in_polygon(p[0], p[1], poly):
        return -1.0
    return min(seg_distance(p, poly[i], poly[(i + 1) % len(poly)])
               for i in range(len(poly)))


def ensure_inside(p: tuple[float, float], poly: list[tuple[float, float]],
                  margin: float) -> tuple[float, float]:
    """Walk a point toward the fence centroid until it clears `margin` metres."""
    cx = sum(v[0] for v in poly) / len(poly)
    cy = sum(v[1] for v in poly) / len(poly)
    for _ in range(80):
        if clearance(p, poly) >= margin:
            return p
        dx, dy = cx - p[0], cy - p[1]
        length = math.hypot(dx, dy) or 1.0
        p = (p[0] + dx / length * 3.0, p[1] + dy / length * 3.0)
    return p


# ---------------------------------------------------------------- path building
def wavy_ring(poly: list[tuple[float, float]], rng: random.Random,
              inset: float) -> list[tuple[float, float]]:
    """Inset the fence by `inset` metres, then subdivide with lateral noise."""
    cx = sum(p[0] for p in poly) / len(poly)
    cy = sum(p[1] for p in poly) / len(poly)

    ring = []
    for i, (x, y) in enumerate(poly):
        # Pull each corner toward the centroid, with a per-corner random margin.
        margin = inset + rng.uniform(-8.0, 14.0)
        dx, dy = cx - x, cy - y
        length = math.hypot(dx, dy) or 1.0
        scale = max(0.0, 1.0 - margin / length)
        ring.append((x + dx * (1 - scale), y + dy * (1 - scale)))

    # Subdivide every edge and push the middle samples sideways a little.
    dense: list[tuple[float, float]] = []
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        edge = math.hypot(x1 - x0, y1 - y0)
        steps = max(3, int(edge / 45.0))
        nx, ny = -(y1 - y0) / edge, (x1 - x0) / edge
        for s in range(steps):
            f = s / steps
            wobble = math.sin(f * math.pi) * rng.uniform(-11.0, 11.0)
            dense.append((x0 + (x1 - x0) * f + nx * wobble,
                          y0 + (y1 - y0) * f + ny * wobble))
    return dense


def detour(start: tuple[float, float], end: tuple[float, float],
           poly: list[tuple[float, float]], rng: random.Random) -> list[tuple[float, float]]:
    """Loop out to a random interior point and come back on a nearby lane."""
    for _ in range(40):
        target = (rng.uniform(-260, 260), rng.uniform(-180, 180))
        if point_in_polygon(*target, poly):
            break
    else:
        return [start, end]
    target = ensure_inside(target, poly, 22.0)
    bend = rng.uniform(0.25, 0.45)
    mid1 = (start[0] + (target[0] - start[0]) * bend + rng.uniform(-12, 12),
            start[1] + (target[1] - start[1]) * bend + rng.uniform(-12, 12))
    mid2 = (start[0] + (target[0] - start[0]) * 0.7 + rng.uniform(-12, 12),
            start[1] + (target[1] - start[1]) * 0.7 + rng.uniform(-12, 12))
    back1 = (end[0] + (target[0] - end[0]) * 0.35 + rng.uniform(-9, 9),
             end[1] + (target[1] - end[1]) * 0.35 + rng.uniform(-9, 9))
    return [start, mid1, mid2, target, back1, end]


def catmull_rom(points: list[tuple[float, float]], closed: bool,
                spacing: float = 4.0) -> list[tuple[float, float]]:
    """Smooth a control polygon and resample it at a fixed spacing."""
    pts = list(points)
    if closed and pts[0] != pts[-1]:
        pts.append(pts[0])
    if len(pts) < 3:
        return pts

    # Duplicate the ends so the spline covers the first and last segment.
    p = [pts[0]] + pts + [pts[-1]]
    out: list[tuple[float, float]] = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        seg = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        steps = max(2, int(seg / 3.0))
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            out.append((x, y))

    # Resample at a constant spacing so the module's linear interpolation is smooth.
    dense = [out[0]]
    for point in out[1:]:
        if math.hypot(point[0] - dense[-1][0], point[1] - dense[-1][1]) >= spacing:
            dense.append(point)
    if dense[-1] != out[-1]:
        dense.append(out[-1])
    return dense


def lengths(points: list[tuple[float, float]]) -> list[float]:
    acc = [0.0]
    for i in range(1, len(points)):
        acc.append(acc[-1] + math.hypot(points[i][0] - points[i - 1][0],
                                        points[i][1] - points[i - 1][1]))
    return acc


def speed_profile(points: list[tuple[float, float]], rng: random.Random,
                  base: float) -> list[list[float]]:
    """Curvature-driven pace with a warm-up and a finishing kick."""
    acc = lengths(points)
    total = acc[-1]
    profile: list[list[float]] = []
    for i, d in enumerate(acc):
        i0 = max(0, i - 3)
        i1 = min(len(points) - 1, i + 3)
        ax = points[i][0] - points[i0][0]
        ay = points[i][1] - points[i0][1]
        bx = points[i1][0] - points[i][0]
        by = points[i1][1] - points[i][1]
        angle = abs(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
        # Sharper turns are slower; straights run at the base speed.
        v = base + 0.55 - 0.85 * min(angle, 1.3)
        v += 0.10 * math.sin(d / 90.0 + rng.random())
        if d < 180:                       # warm-up
            v -= 0.45 * (1 - d / 180.0)
        if total - d < 260:               # finishing kick
            v += 0.30 * (1 - (total - d) / 260.0)
        profile.append([round(d, 1), round(min(4.45, max(3.15, v)), 3)])
    return profile


# ------------------------------------------------------------------------ main
def build(seed: int, target: float, avoid_water: bool = False) -> dict:
    rng = random.Random(seed)
    frame = LocalFrame(*START_WGS)
    fence = [frame.to_xy(*gcj_to_wgs(lat, lng)) for lat, lng in FENCE_GCJ]

    ring = wavy_ring(fence, rng, inset=34.0)
    ring = [ensure_inside(p, fence, 14.0) for p in ring]
    # Start the lap at the fixed point: rotate the closed ring so the vertex
    # closest to the start comes first, then add the start point itself.
    best = min(range(len(ring)), key=lambda i: math.hypot(ring[i][0], ring[i][1]))
    ring = ring[best:] + ring[:best]
    control = [(0.0, 0.0)] + ring + [(0.0, 0.0)]

    smooth: list[tuple[float, float]] = []
    for attempt in range(4):
        trial = list(control)
        for _ in range(2 + attempt):
            idx = rng.randrange(3, max(5, len(trial) - 4))
            trial[idx:idx + 1] = detour(trial[idx], trial[idx + 1], fence, rng)
        smooth = catmull_rom(trial, closed=True, spacing=3.5)
        if lengths(smooth)[-1] >= target * 0.82:
            break

    total = lengths(smooth)[-1]
    wgs_points = [[round(lat, 7), round(lng, 7)] for lat, lng in
                  (frame.to_latlon(x, y) for x, y in smooth)]
    if avoid_water:
        wgs_points = push_out_of_water(wgs_points, frame)
    profile = speed_profile(smooth, rng, base=3.75)
    return {
        "speed": 3.75,
        "points": wgs_points,
        "profile": profile,
        "_meta": {"seed": seed, "length_m": round(total, 1), "points": len(wgs_points)},
    }


def push_out_of_water(points: list[list[float]], frame: "LocalFrame") -> list[list[float]]:
    """Nudge route points off lakes/lawns using the mapfit screenshot mask."""
    try:
        import json as _json

        import numpy as np
        from PIL import Image

        data = _json.load(open(r"D:\Work\xbud_run_logs\mapfit.json", encoding="utf-8"))
        image = np.asarray(Image.open(data["shot"]).convert("RGB"), dtype=np.int16)
        r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]
        blocked = ((b - r > 12) & (b > 150)) | ((g - r > 20) & (g > 150))
        h, w = blocked.shape

        def to_px(lat: float, lng: float) -> tuple[int, int]:
            gcj_lat, gcj_lng = wgs_to_gcj_simple(lat, lng)
            x = (gcj_lng - data["lng0"]) / data["lng_per_px"]
            y = (gcj_lat - data["lat0"]) / data["lat_per_px"]
            return int(x), int(y)

        out = []
        moved = 0
        for lat, lng in points:
            for _ in range(10):
                x, y = to_px(lat, lng)
                if not (0 <= x < w and 0 <= y < h) or not blocked[y, x]:
                    break
                # Step toward the campus centre (the start point) until dry.
                lat += (START_WGS[0] - lat) * 0.02
                lng += (START_WGS[1] - lng) * 0.02
                moved += 1
            out.append([round(lat, 7), round(lng, 7)])
        if moved:
            print(f"water avoidance: nudged {moved} samples")
        return out
    except Exception as exc:  # keep the route usable even without a map capture
        print("water avoidance skipped:", exc)
        return points


def wgs_to_gcj_simple(lat: float, lng: float) -> tuple[float, float]:
    """Forward GCJ-02 transform (WGS84 -> GCJ02)."""
    a = 6378245.0
    ee = 0.00669342162296594323

    def tlat(x, y):
        r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return r

    def tlng(x, y):
        r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return r

    dlat = tlat(lng - 105.0, lat - 35.0)
    dlng = tlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - ee * math.sin(rad) ** 2
    sm = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sm) * math.pi)
    dlng = (dlng * 180.0) / (a / sm * math.cos(rad) * math.pi)
    return lat + dlat, lng + dlng


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=r"D:\Work\xbud_route.json")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--length", type=float, default=4200.0)
    parser.add_argument("--push", action="store_true"); parser.add_argument("--no-water", action="store_true", help="skip lake/lawn avoidance")
    args = parser.parse_args()

    seed = args.seed or random.randrange(1, 10**6)
    data = build(seed, args.length, avoid_water=not args.no_water)
    meta = data.pop("_meta")
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    profile = data["profile"]
    speeds = [v for _, v in profile]
    print(f"seed={meta['seed']} points={meta['points']} length={meta['length_m']} m")
    print(f"speed min/avg/max = {min(speeds):.2f}/{sum(speeds)/len(speeds):.2f}/{max(speeds):.2f} m/s")
    if meta["length_m"] > 0:
        pace = 1000.0 / (sum(speeds) / len(speeds))
        print(f"avg pace ~{int(pace // 60)}'{int(pace % 60):02d}\"/km, "
              f"3.05 km takes ~{3050 / (sum(speeds)/len(speeds)):.0f}s")

    if args.push:
        adbcfg.adb("push", args.out, TMP_ROUTE)
        adbcfg.adb("shell",
                   f"su -c 'cat {TMP_ROUTE} > {DEVICE_ROUTE}; chmod 666 {DEVICE_ROUTE}'")
        print(f"pushed to {DEVICE_ROUTE} (serial={adbcfg.serial()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

