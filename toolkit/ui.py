#!/usr/bin/env python3
"""Reliably drive 小步点 from launcher to a running 学分跑 session."""

from __future__ import annotations

import re
import subprocess
import sys
import time

from PIL import Image
import adbcfg

ADB = adbcfg.ADB
SERIAL = adbcfg.serial()


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


def focus() -> str:
    text = adb("shell", "dumpsys window | grep mCurrentFocus")
    return text.strip()


def tap(x: int, y: int) -> None:
    adb("shell", f"input tap {x} {y}")


def screenshot(path: str) -> None:
    adb("shell", "screencap -p /sdcard/_ui.png")
    adb("pull", "/sdcard/_ui.png", path)


def find_run_button(path: str) -> tuple[int, int] | None:
    image = Image.open(path).convert("RGB")
    w, h = image.size
    px = image.load()
    pts = []
    for y in range(int(h * 0.72), h):
        for x in range(w):
            r, g, b = px[x, y]
            if r >= 245 and 180 <= g <= 215 and b <= 40:
                pts.append((x, y))
    if not pts:
        return None
    # largest blob
    pool = set(pts)
    best = []
    while pool:
        s = pool.pop()
        blob = [s]
        stack = [s]
        while stack:
            cx, cy = stack.pop()
            for dx in (-2, 0, 2):
                for dy in (-2, 0, 2):
                    p = (cx + dx, cy + dy)
                    if p in pool:
                        pool.remove(p)
                        blob.append(p)
                        stack.append(p)
        if len(blob) > len(best):
            best = blob
    if len(best) < 30:
        return None
    xs = [p[0] for p in best]
    ys = [p[1] for p in best]
    return sum(xs) // len(xs), sum(ys) // len(ys)


def main() -> int:
    adb("shell", "am force-stop com.yinshibai.location")
    adb("shell", "am force-stop run.xbud.android")
    time.sleep(1)
    adb("shell", "am start -n run.xbud.android/.mvp.ui.other.SplashActivity --activity-clear-top")
    time.sleep(8)

    for attempt in range(4):
        f = focus()
        print("focus:", f)
        if "RunAdvancedActivity" in f:
            print("already running")
            return 0
        if "MainActivity" in f:
            screenshot(r"D:\Work\_ui_home.png")
            point = find_run_button(r"D:\Work\_ui_home.png")
            print("run button:", point)
            if point:
                tap(*point)
                time.sleep(5)
            continue
        if "RunStartActivity" in f:
            tap(695, 150)  # 学分跑
            time.sleep(4)
            tap(540, 1680)  # play
            time.sleep(8)
            continue
        if "AISport" in f or "AI" in f:
            adb("shell", "input keyevent 4")
            time.sleep(2)
            continue
        # unfinished dialog or result page
        tap(320, 1140)
        time.sleep(2)

    print("final:", focus())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
