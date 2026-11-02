#!/usr/bin/env python3
"""Build a human-looking campus route from the live AMap screenshot.

Why not a rectangle any more: the old 380 m loop was traversed ~21 times, so the
recorded track was one perfectly repeated outline.  Here the route is a single
3.3 km wander:

  * it starts on the assigned fixed point (南门 pin read from `markers`);
  * it refuses to enter water, lawns, buildings' lawns or to leave the fence;
  * it keeps momentum (long straights) but turns randomly, so it follows the
    campus grid roughly the way a person chooses streets;
  * every generation uses a fresh seed, so two days never look alike.

The screenshot and the screen<->GCJ transform come from `mapfit.py`, which
captures both in the same frame.

Usage:
  python routewander.py --seed 12 --length 3300 --preview
  python routewander.py --seed 12 --push
"""

from __future__ import annotations

import argparse
import json
import math
import random

import numpy as np
from PIL import Image

from routeplan import FENCE_GCJ, gcj_to_wgs

MAPFIT = r"D:\Work\xbud_run_logs\mapfit.json"
ROUTE_OUT = r"D:\Work\xbud_route.json"


class Transform:
    def __init__(self, data: dict):
        self.shot = data["shot"]
        self.lat_per_px = data["lat_per_px"]
        self.lat0 = data["lat0"]
        self.lng_per_px = data["lng_per_px"]
        self.lng0 = data["lng0"]

    def px_to_gcj(self, x: float, y: float) -> tuple[float, float]:
        return self.lat0 + y * self.lat_per_px, self.lng0 + x * self.lng_per_px

    def gcj_to_px(self, lat: float, lng: float) -> tuple[float, float]:
        return (lng - self.lng0) / self.lng_per_px, (lat - self.lat0) / self.lat_per_px


def point_in_polygon(x: float, y: float, poly) -> bool:
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def load_mask(transform: Transform) -> np.ndarray:
    """True = walkable (campus ground/road), False = blocked."""
    image = np.asarray(Image.open(transform.shot).convert("RGB"), dtype=np.int16)
    r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]
    water = (b - r > 12) & (b > 150)
    green = (g - r > 20) & (g > 150)
    walkable = ~(water | green)

    h, w = walkable.shape
    ys, xs = np.mgrid[0:h, 0:w]
    lats = transform.lat0 + ys * transform.lat_per_px
    lngs = transform.lng0 + xs * transform.lng_per_px
    inside = np.zeros(lats.shape, dtype=bool)
    for i in range(len(FENCE_GCJ)):
        y1, y2 = FENCE_GCJ[i][0], FENCE_GCJ[(i + 1) % len(FENCE_GCJ)][0]
        x1, x2 = FENCE_GCJ[i][1], FENCE_GCJ[(i + 1) % len(FENCE_GCJ)][1]
        cond = (y1 > lats) != (y2 > lats)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (lats - y1) / (y2 - y1 + 1e-12) + x1
        inside ^= cond & (lngs < xint)

    # Map labels are blue/teal, so they read as "water"; close small holes so a
    # 3x3 free-space test does not get trapped inside a glyph.
    from PIL import ImageFilter
    base = Image.fromarray((walkable & inside).astype(np.uint8) * 255, mode="L")
    closed = base.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(9))
    cleaned = np.asarray(closed) > 127

    # Real water stays blocked (dilated to cover its shoreline).
    water_img = Image.fromarray(water.astype(np.uint8) * 255, mode="L")
    water_big = np.asarray(water_img.filter(ImageFilter.MaxFilter(5))) > 127
    return cleaned & ~water_big & inside


def free(mask: np.ndarray, x: float, y: float, radius: int = 1) -> bool:
    h, w = mask.shape
    xi, yi = int(round(x)), int(round(y))
    if xi < radius or yi < radius or xi >= w - radius or yi >= h - radius:
        return False
    return bool(mask[yi - radius:yi + radius + 1, xi - radius:xi + radius + 1].all())


def nearest_walkable(mask: np.ndarray, x: float, y: float, limit: int = 120) -> tuple[float, float]:
    """BFS over the mask for the closest walkable cell."""
    from collections import deque
    h, w = mask.shape
    start = (int(round(y)), int(round(x)))
    queue = deque([(start, 0)])
    seen = {start}
    while queue:
        (cy, cx), dist = queue.popleft()
        if 0 <= cy < h and 0 <= cx < w and mask[cy, cx]:
            return float(cx), float(cy)
        if dist >= limit:
            continue
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = cy + dy, cx + dx
            if (ny, nx) not in seen and 0 <= ny < h and 0 <= nx < w:
                seen.add((ny, nx))
                queue.append(((ny, nx), dist + 1))
    return x, y


def wander(mask: np.ndarray, start: tuple[float, float], target: float,
           rng: random.Random) -> list[tuple[float, float]]:
    x, y = start
    heading = rng.uniform(0, 6.283)
    pts = [(x, y)]
    travelled = 0.0
    step = 4.0
    straight_left = 0.0
    stuck = 0
    guard = 0
    while travelled < target and guard < 200000:
        guard += 1
        if straight_left <= 0:
            # New intention: mostly straight runs with the occasional bend.
            heading += rng.gauss(0, 0.45)
            straight_left = rng.uniform(80, 320)
        straight_left -= step
        # Gentle curve while running.
        heading += rng.gauss(0, 0.035)

        moved = False
        for attempt in range(16):
            probe = heading + (attempt // 2 + 1) * 0.5 * (1 if attempt % 2 else -1)
            nx = x + math.cos(probe) * step
            ny = y + math.sin(probe) * step
            if free(mask, nx, ny, radius=1):
                x, y = nx, ny
                heading = probe
                travelled += step
                pts.append((x, y))
                moved = True
                stuck = 0
                break
        if not moved:
            # Blocked on all sides: step back the way we came and re-aim.
            stuck += 1
            heading += math.pi * (0.6 + rng.random() * 0.3)
            straight_left = 0
            bx = x - math.cos(heading) * step * 2
            by = y - math.sin(heading) * step * 2
            if free(mask, bx, by, radius=1):
                x, y = bx, by
                travelled += step * 2
                pts.append((x, y))
            if stuck > 40:
                break
    return pts


def waypoint_route(mask: np.ndarray, start: tuple[float, float], target: float,
                   rng: random.Random, count: int = 10) -> list[tuple[float, float]]:
    """Loop through random walkable waypoints, smoothed - like choosing streets."""
    ys, xs = np.nonzero(mask)
    pts = list(zip(xs.tolist(), ys.tolist()))
    if not pts:
        return [start]
    picks = [start]
    for _ in range(4000):
        if len(picks) > count:
            break
        p = pts[rng.randrange(len(pts))]
        if any(math.hypot(p[0] - q[0], p[1] - q[1]) < 70 for q in picks):
            continue
        picks.append((float(p[0]), float(p[1])))

    sx, sy = start
    picks.sort(key=lambda p: math.atan2(p[1] - sy, p[0] - sx))
    loop = [start] + picks + [start]

    # Catmull-Rom through the waypoints, then resample.
    dense: list[tuple[float, float]] = []
    for i in range(len(loop) - 1):
        p0 = loop[max(0, i - 1)]
        p1 = loop[i]
        p2 = loop[i + 1]
        p3 = loop[min(len(loop) - 1, i + 2)]
        seg = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        steps = max(4, int(seg / 3.0))
        for s in range(steps):
            t = s / steps
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                       + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                       + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                       + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                       + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            if not free(mask, x, y, radius=0):
                x, y = p1
            dense.append((x, y))

    out = resample(dense)
    # Pad by walking the loop again if the campus is small, then cut to length.
    guard = 0
    while sum(math.hypot(out[i][0] - out[i - 1][0], out[i][1] - out[i - 1][1])
              for i in range(1, len(out))) < target and len(out) > 3 and guard < 6:
        out = out + out[1:]
        guard += 1
    return trim(out, target)


def trim(pts: list[tuple[float, float]], target: float) -> list[tuple[float, float]]:
    """Cut the polyline at `target` metres."""
    out = [pts[0]]
    acc = 0.0
    for p in pts[1:]:
        step = math.hypot(p[0] - out[-1][0], p[1] - out[-1][1])
        if acc + step >= target:
            break
        out.append(p)
        acc += step
    return out


def resample(pts: list[tuple[float, float]], spacing: float = 3.5) -> list[tuple[float, float]]:
    out = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= spacing:
            out.append(p)
    return out


def speed_profile(pts: list[tuple[float, float]]) -> list[list[float]]:
    profile = []
    acc = 0.0
    for i, (x, y) in enumerate(pts):
        if i:
            acc += math.hypot(x - pts[i - 1][0], y - pts[i - 1][1])
        i0, i1 = max(0, i - 3), min(len(pts) - 1, i + 3)
        ax, ay = x - pts[i0][0], y - pts[i0][1]
        bx, by = pts[i1][0] - x, pts[i1][1] - y
        angle = abs(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
        v = 4.20 - 0.80 * min(angle, 1.3) + 0.12 * math.sin(acc / 95.0)
        if acc < 160:
            v -= 0.35 * (1 - acc / 160.0)
        profile.append([round(acc, 1), round(min(4.45, max(3.15, v)), 3)])
    return profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapfit", default=MAPFIT)
    parser.add_argument("--anchor", default="",
                        help="fixed point as GCJ 'lat,lng'; default: read from the device")
    parser.add_argument("--length", type=float, default=3300.0)
    parser.add_argument("--mode", choices=["waypoints", "wander"], default="waypoints")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=ROUTE_OUT)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()

    rng = random.Random(args.seed or random.randrange(1 << 30))
    transform = Transform(json.load(open(args.mapfit, encoding="utf-8")))
    mask = load_mask(transform)
    print(f"walkable cells: {int(mask.sum())} of {mask.size}")

    if args.anchor:
        anchor_lat, anchor_lng = (float(v) for v in args.anchor.split(","))
    else:
        # 行学楼 fixed point (BD09 33.557596,119.053042 -> GCJ below); stable
        # across assignments and inside the captured frame.
        anchor_lat, anchor_lng = 33.5512633, 119.0466473
    print(f"anchor GCJ {anchor_lat:.7f},{anchor_lng:.7f}")

    sx, sy = transform.gcj_to_px(anchor_lat, anchor_lng)
    if not (0 <= sx < mask.shape[1] and 0 <= sy < mask.shape[0]):
        print("anchor is outside the captured map")
        return 1
    if not free(mask, sx, sy, radius=1):
        print("anchor not walkable, snapping to the nearest walkable cell")
        sx, sy = nearest_walkable(mask, sx, sy, limit=120)

    pts = wander(mask, (sx, sy), args.length, rng) if args.mode == "wander" \
        else waypoint_route(mask, (sx, sy), args.length, rng)
    pts = resample(pts)
    total = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
                for i in range(1, len(pts)))

    wgs = []
    for x, y in pts:
        gcj_lat, gcj_lng = transform.px_to_gcj(x, y)
        wlat, wlng = gcj_to_wgs(gcj_lat, gcj_lng)
        wgs.append([round(wlat, 7), round(wlng, 7)])

    data = {"speed": 3.8, "points": wgs, "profile": speed_profile(pts)}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    speeds = [v for _, v in data["profile"]]
    avg = sum(speeds) / len(speeds)
    print(f"route: {len(wgs)} points, {total/1000:.2f} km, speed {min(speeds):.2f}-{max(speeds):.2f} "
          f"(avg {avg:.2f} m/s, {int(1000/avg//60)}'{int(1000/avg%60):02d}\"/km)")

    if args.preview:
        image = Image.open(transform.shot).convert("RGB")
        pixels = image.load()
        for x, y in pts:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    xi, yi = int(x) + dx, int(y) + dy
                    if 0 <= xi < image.width and 0 <= yi < image.height:
                        pixels[xi, yi] = (220, 20, 30)
        out = args.out.replace(".json", "_preview.png")
        image.save(out)
        print("preview", out)

    if args.push:
        import adbcfg
        dst = "/data/data/run.xbud.android/files/xbud_route.json"
        tmp = "/data/local/tmp/xbud_route.json"
        adbcfg.adb("push", args.out, tmp)
        adbcfg.adb("shell", f"su -c 'cat {tmp} > {dst}; chmod 666 {dst}'")
        print(f"pushed {dst} (serial={adbcfg.serial()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
