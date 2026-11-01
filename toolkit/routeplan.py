#!/usr/bin/env python3
"""Plan a route that follows the campus roads visible on the AMap screenshot.

The fence polygon is a coarse quadrilateral: a straight line between two fence
vertices cuts through buildings, lawns and the lake, which is exactly what made
the old track look synthetic.  This script rasterises the map screenshot we
already captured, detects the pale-yellow campus roads, and plans a loop with
A* over a cost grid (road cheap, lawn expensive, water/green forbidden).

The result is a WGS84 polyline plus a curvature-driven speed profile, ready for
the xbudloc module.

Usage:
  python routeplan.py --shot D:\\Work\\xbud_run_logs\\225817_campus16.png \
                      --anchor 33.54829,119.04732 --length 3400 --preview
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import random
from collections import deque

import numpy as np
from PIL import Image

# Screen-space georeference measured with the module's `project x y` command on
# the reference screenshot (zoom 16, 1080x1920).
ANCHOR_A = (200.0, 300.0, 33.554907220979764, 119.04294913011695)
ANCHOR_B = (880.0, 1700.0, 33.5426494331741, 119.05002077406373)

# Campus fence (GCJ02) - used to keep waypoints inside the campus.
FENCE_GCJ = [
    (33.55436426, 119.04117762),
    (33.55382304, 119.05274382),
    (33.54883792, 119.05231005),
    (33.54941376, 119.04079667),
]


def affine():
    x1, y1, lat1, lng1 = ANCHOR_A
    x2, y2, lat2, lng2 = ANCHOR_B
    lng_per_px = (lng2 - lng1) / (x2 - x1)
    lat_per_px = (lat2 - lat1) / (y2 - y1)
    return lng_per_px, lat_per_px, lat1 - y1 * lat_per_px, lng1 - x1 * lng_per_px


LNG_PER_PX, LAT_PER_PX, LAT0, LNG0 = affine()


def px_to_gcj(x: float, y: float) -> tuple[float, float]:
    return LAT0 + y * LAT_PER_PX, LNG0 + x * LNG_PER_PX


def gcj_to_px(lat: float, lng: float) -> tuple[float, float]:
    return (lng - LNG0) / LNG_PER_PX, (lat - LAT0) / LAT_PER_PX


def gcj_to_wgs(lat: float, lng: float) -> tuple[float, float]:
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

    def wgs_to_gcj(wlat, wlng):
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


def inside_mask(scale: int, shape: tuple[int, int]) -> np.ndarray:
    """Boolean grid: True where the cell falls inside the campus fence."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w]
    lats = LAT0 + (ys * scale) * LAT_PER_PX
    lngs = LNG0 + (xs * scale) * LNG_PER_PX
    inside = np.zeros(lats.shape, dtype=bool)
    for i in range(len(FENCE_GCJ)):
        y1, y2 = FENCE_GCJ[i][0], FENCE_GCJ[(i + 1) % len(FENCE_GCJ)][0]
        x1, x2 = FENCE_GCJ[i][1], FENCE_GCJ[(i + 1) % len(FENCE_GCJ)][1]
        cond = (y1 > lats) != (y2 > lats)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = (x2 - x1) * (lats - y1) / (y2 - y1 + 1e-12) + x1
        inside ^= cond & (lngs < xint)
    return inside


# ------------------------------------------------------------------ raster costs
def build_cost(shot: str, scale: int = 2) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = np.asarray(Image.open(shot).convert("RGB"), dtype=np.int16)
    r, g, b = image[:, :, 0], image[:, :, 1], image[:, :, 2]

    # Campus roads render as a pale yellow (#ffffd6-ish) on a beige ground.
    road = (r >= 250) & (g >= 248) & (b >= 200) & (b <= 238)
    water = (b - r > 12) & (b > 150)
    green = (g - r > 20) & (g > 150)

    cost = np.full(image.shape[:2], 6.0, dtype=np.float32)   # campus ground
    cost[road] = 1.0
    cost[green] = 18.0
    cost[water] = 400.0

    small_cost = cost[::scale, ::scale].copy()
    small_road = road[::scale, ::scale]
    inside = inside_mask(scale, small_cost.shape)
    small_cost[~inside] = 90.0

    return small_cost, small_road, inside


def astar(cost: np.ndarray, start: tuple[int, int],
          goal: tuple[int, int]) -> list[tuple[int, int]]:
    """8-connected A* on the downsampled grid."""
    h, w = cost.shape
    sy, sx = start
    gy, gx = goal
    dist = {start: 0.0}
    prev: dict[tuple[int, int], tuple[int, int]] = {}
    heap = [(math.hypot(sx - gx, sy - gy), 0.0, start)]
    seen = set()
    while heap:
        _, gcost, node = heapq.heappop(heap)
        if node in seen:
            continue
        seen.add(node)
        if node == goal:
            path = [node]
            while node in prev:
                node = prev[node]
                path.append(node)
            return path[::-1]
        y, x = node
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = y + dy, x + dx
                if not (0 <= ny < h and 0 <= nx < w):
                    continue
                step = math.hypot(dx, dy)
                ng = gcost + cost[ny, nx] * step
                if ng < dist.get((ny, nx), 1e18):
                    dist[(ny, nx)] = ng
                    prev[(ny, nx)] = node
                    heapq.heappush(heap, (ng + math.hypot(nx - gx, ny - gy), ng, (ny, nx)))
    return []


def nearest_road(road: np.ndarray, point: tuple[int, int], radius: int = 30) -> tuple[int, int]:
    """BFS outward from a point until a road pixel is found."""
    h, w = road.shape
    start = (int(point[0]), int(point[1]))
    queue = deque([start])
    seen = {start}
    while queue:
        y, x = queue.popleft()
        if 0 <= y < h and 0 <= x < w and road[y, x]:
            return (y, x)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if (ny, nx) in seen or not (0 <= ny < h and 0 <= nx < w):
                continue
            if abs(ny - start[0]) + abs(nx - start[1]) > radius:
                continue
            seen.add((ny, nx))
            queue.append((ny, nx))
    return start


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shot", default=r"D:\Work\xbud_run_logs\225817_campus16.png")
    parser.add_argument("--anchor", default="33.54829,119.04732",
                        help="fixed-point GCJ lat,lng from the `markers` command")
    parser.add_argument("--length", type=float, default=3400.0)
    parser.add_argument("--waypoints", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--scale", type=int, default=2)
    parser.add_argument("--out", default=r"D:\Work\xbud_route.json")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()

    rng = random.Random(args.seed or random.randrange(1 << 30))
    cost, road, inside = build_cost(args.shot, args.scale)
    h, w = cost.shape
    print(f"grid {w}x{h}, road pixels {int(road.sum())}")

    anchor_lat, anchor_lng = (float(v) for v in args.anchor.split(","))
    ax, ay = gcj_to_px(anchor_lat, anchor_lng)
    gx, gy = int(ax / args.scale), int(ay / args.scale)
    gx = min(max(gx, 0), w - 1)
    gy = min(max(gy, 0), h - 1)
    start = nearest_road(road & inside, (gy, gx), radius=40)
    print(f"anchor px {ax:.0f},{ay:.0f} -> grid {start}")

    # Waypoints: road pixels inside the fence, ordered by angle so the path
    # forms a loop rather than a random walk.
    ys, xs = np.nonzero(road & inside)
    keep = np.array([inside[y, x] for y, x in zip(ys, xs)], dtype=bool)
    ys, xs = ys[keep], xs[keep]
    print(f"candidate road pixels inside fence: {len(ys)}")
    candidates = list(zip(ys.tolist(), xs.tolist()))
    picks: list[tuple[int, int]] = []
    cy, cx = h / 2, w / 2
    for _ in range(6000):
        if len(picks) >= args.waypoints:
            break
        candidate = candidates[rng.randrange(len(candidates))]
        if math.hypot(candidate[1] - cx, candidate[0] - cy) < 30:
            continue
        if any(math.hypot(candidate[1] - p[1], candidate[0] - p[0]) < 90 for p in picks):
            continue
        picks.append(candidate)

    picks.sort(key=lambda p: math.atan2(p[0] - start[0], p[1] - start[1]))
    chain = [start] + picks + [start]

    path: list[tuple[int, int]] = []
    for i in range(len(chain) - 1):
        leg = astar(cost, chain[i], chain[i + 1])
        if not leg:
            print(f"leg {i} unreachable, skipping")
            continue
        path.extend(leg if not path else leg[1:])

    # Resample in pixel space (scale*1 m per grid cell).
    metres_per_cell = 1.0 * args.scale
    pts = [(x * metres_per_cell, y * metres_per_cell) for y, x in path]
    resampled = [pts[0]]
    for p in pts[1:]:
        if math.hypot(p[0] - resampled[-1][0], p[1] - resampled[-1][1]) >= 3.5:
            resampled.append(p)
    total = sum(math.hypot(resampled[i][0] - resampled[i - 1][0],
                           resampled[i][1] - resampled[i - 1][1])
                for i in range(1, len(resampled)))
    print(f"route cells {len(resampled)} length {total:.0f} m")

    # Back to WGS84.
    lats, lngs = [], []
    for x, y in resampled:
        gcj_lat, gcj_lng = px_to_gcj(x / metres_per_cell, y / metres_per_cell)
        wlat, wlng = gcj_to_wgs(gcj_lat, gcj_lng)
        lats.append(round(wlat, 7))
        lngs.append(round(wlng, 7))

    # Speed profile from the pixel-space curvature.
    profile = []
    acc = 0.0
    for i, (x, y) in enumerate(resampled):
        if i:
            acc += math.hypot(x - resampled[i - 1][0], y - resampled[i - 1][1])
        i0, i1 = max(0, i - 3), min(len(resampled) - 1, i + 3)
        axv = x - resampled[i0][0]
        ayv = y - resampled[i0][1]
        bxv = resampled[i1][0] - x
        byv = resampled[i1][1] - y
        angle = abs(math.atan2(axv * byv - ayv * bxv, axv * bxv + ayv * byv))
        v = 4.25 - 0.85 * min(angle, 1.3) + 0.10 * math.sin(acc / 90.0)
        if acc < 180:
            v -= 0.40 * (1 - acc / 180.0)
        profile.append([round(acc, 1), round(min(4.45, max(3.15, v)), 3)])

    data = {"speed": 3.75, "points": [[a, b] for a, b in zip(lats, lngs)], "profile": profile}
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    print(f"wrote {args.out}: {len(lats)} points, {total/1000:.2f} km")

    if args.preview:
        image = Image.open(args.shot).convert("RGB")
        pixels = image.load()
        for x, y in resampled:
            sx, sy = int(x / metres_per_cell), int(y / metres_per_cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if 0 <= sx + dx < image.width and 0 <= sy + dy < image.height:
                        pixels[sx + dx, sy + dy] = (220, 30, 30)
        out = args.out.replace(".json", "_preview.png")
        image.save(out)
        print("preview", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
