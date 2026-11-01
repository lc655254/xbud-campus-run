#!/usr/bin/env python3
"""Drive MuMu's virtual accelerometer (or toolbar shake) at a target cadence.

MuMuManager is a one-shot CLI: every call costs ~220-240 ms, which is the
real reason the old shake loop could not exceed ~4 Hz.  `tool gyro` writes a
gravity vector straight into the goldfish sensor HAL, so it has no toolbar
animation/cooldown; at ~4.2 Hz it is enough for 200-250 spm.

Examples:
    python cadence.py --spm 180 --duration 60
    python cadence.py --spm 220 --backend gyro --amplitude 12
    python cadence.py --backend shake --spm 160
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MANAGER = r"D:\MuMuPlayer\nx_main\MuMuManager.exe"


def manager_call(manager: str, args: list[str], timeout: float = 10.0) -> str:
    proc = subprocess.run(
        [manager, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(out or f"MuMuManager exit {proc.returncode}")
    return out


def gyro(manager: str, index: str, z: float, x: float = 0.0, y: float = 0.0) -> None:
    manager_call(
        manager,
        ["control", "-v", str(index), "tool", "gyro",
         "-gx", f"{x:.4f}", "-gy", f"{y:.4f}", "-gz", f"{z:.4f}"],
    )


def shake(manager: str, index: str) -> None:
    manager_call(manager, ["control", "-v", str(index), "tool", "func", "-n", "shake"])


def probe_rate(manager: str, index: str, backend: str, samples: int = 10) -> float:
    start = time.perf_counter()
    for i in range(samples):
        if backend == "gyro":
            gyro(manager, index, 9.8 + (9.8 if i % 2 else 0.0))
        else:
            shake(manager, index)
    elapsed = time.perf_counter() - start
    return samples / elapsed if elapsed > 0 else 0.0


def run(args: argparse.Namespace) -> int:
    interval = 60.0 / args.spm
    # A gyro sample is a step impulse: alternate rest / peak once per step.
    low, high = 9.80665, 9.80665 + args.amplitude

    if args.dry_run:
        print(f"backend={args.backend} spm={args.spm:g} interval={interval * 1000:.1f} ms")
        print(f"low={low:.3f} high={high:.3f} amplitude={args.amplitude:g}")
        return 0

    if args.probe:
        hz = probe_rate(args.manager, args.index, args.backend, args.probe)
        print(f"measured call rate: {hz:.2f} Hz -> max spm {hz * 60:.0f}")
        if hz * 60 < args.spm:
            print(f"warning: target {args.spm:g} spm exceeds the {hz:.2f} Hz "
                  f"ceiling; cadence will be clamped", file=sys.stderr)

    deadline = time.perf_counter()
    steps = 0
    started = deadline
    try:
        while True:
            if args.duration and time.perf_counter() - started >= args.duration:
                break
            delay = deadline - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

            if args.backend == "gyro":
                gyro(args.manager, args.index, high if steps % 2 == 0 else low)
            else:
                shake(args.manager, args.index)

            steps += 1
            if steps % 10 == 0:
                elapsed = time.perf_counter() - started
                print(f"\rsteps={steps} effective={steps / elapsed * 60:.1f} spm",
                      end="", flush=True)

            deadline += interval
            now = time.perf_counter()
            if deadline <= now:
                missed = math.floor((now - deadline) / interval) + 1
                deadline += missed * interval
                steps += missed - 1
    except KeyboardInterrupt:
        pass

    elapsed = max(time.perf_counter() - started, 1e-6)
    print(f"\ndone: {steps} impulses in {elapsed:.1f}s "
          f"({steps / elapsed * 60:.1f} spm effective)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager", default=DEFAULT_MANAGER)
    parser.add_argument("--index", default="0", help="MuMu player index")
    parser.add_argument("--spm", type=float, default=180.0, help="steps per minute")
    parser.add_argument("--backend", choices=["gyro", "shake"], default="gyro")
    parser.add_argument("--amplitude", type=float, default=9.80665,
                        help="gyro delta added to 1g on Z")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="seconds to run; 0 = until Ctrl+C")
    parser.add_argument("--probe", type=int, default=0,
                        help="measure N calls first and report the Hz ceiling")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not Path(args.manager).is_file():
        parser.error(f"MuMuManager not found: {args.manager}")
    if not 10 <= args.spm <= 400:
        parser.error("--spm must be within 10..400")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
