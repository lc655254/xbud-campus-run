#!/usr/bin/env python3
"""Detect the yellow checkpoint check-mark on a 小步点 run screenshot."""

from __future__ import annotations

import sys

from PIL import Image


def find_pin(path: str) -> tuple[int, int] | None:
    image = Image.open(path).convert("RGB")
    width, height = image.size
    pixels = image.load()

    pool: set[tuple[int, int]] = set()
    for y in range(250, min(1450, height)):
        for x in range(width):
            r, g, b = pixels[x, y]
            if r >= 248 and 210 <= g <= 238 and b <= 48:
                pool.add((x, y))

    best: tuple[int, int, int, int, int] | None = None
    while pool:
        seed = pool.pop()
        blob = [seed]
        stack = [seed]
        while stack:
            cx, cy = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (cx + dx, cy + dy)
                    if nxt in pool:
                        pool.remove(nxt)
                        blob.append(nxt)
                        stack.append(nxt)
        xs = [p[0] for p in blob]
        ys = [p[1] for p in blob]
        w = max(xs) - min(xs) + 1
        h = max(ys) - min(ys) + 1
        area = len(blob)
        # The pin check is a compact ~35x30 blob; long yellow roads are wide.
        if 15 <= w <= 90 and 12 <= h <= 70 and 80 <= area <= 900:
            score = area
            if best is None or score > best[0]:
                best = (score, sum(xs) // area, sum(ys) // area, w, h)

    if best is None:
        return None
    _, cx, cy, _, _ = best
    return cx, cy + 14


if __name__ == "__main__":
    print(find_pin(sys.argv[1]))
