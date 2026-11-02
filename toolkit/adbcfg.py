#!/usr/bin/env python3
"""ADB / MuMu endpoint resolution.

MuMu Player can host several instances and each one gets its own adb port, so a
hard-coded ``127.0.0.1:16384`` breaks the moment the user runs instance 1 (which
listens on 16416) or switches instances.

Resolution order:

1. ``XBUD_SERIAL``           explicit override, e.g. ``127.0.0.1:16416``
2. ``XBUD_MUMU_INDEX``       index of the instance to use (0, 1, ...)
3. MuMuManager instance table -- the first instance with ``is_android_started``
4. ``adb devices``           the only entry in state ``device``
5. ``127.0.0.1:16384``       historical default

Typical use::

    import adbcfg
    adbcfg.adb("shell", "dumpsys window | grep mCurrentFocus")
    adbcfg.serial()          # '127.0.0.1:16416'
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

ADB = os.environ.get("XBUD_ADB") or r"D:\MuMuPlayer\nx_device\15.0\shell\adb.exe"
MUMU = os.environ.get("XBUD_MUMU") or r"D:\MuMuPlayer\nx_main\MuMuManager.exe"
PKG = "run.xbud.android"
DEFAULT_SERIAL = "127.0.0.1:16384"

_serial_cache: str | None = None
_verbose = os.environ.get("XBUD_VERBOSE", "") not in ("", "0", "false")


def _note(message: str) -> None:
    if _verbose:
        print(f"[adbcfg] {message}", file=sys.stderr, flush=True)


def run(cmd: list[str], timeout: int = 60) -> str:
    """Run a command and return stdout+stderr (never raises on non-zero)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<error: {exc}>"


def mumu_instances() -> dict[str, dict]:
    """MuMuManager info table: {'0': {...}, '1': {...}}."""
    raw = run([MUMU, "info", "-v", "all"], timeout=30)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        _note(f"cannot parse MuMuManager output: {raw[:120]!r}")
        return {}
    if not isinstance(data, dict):
        return {}
    # A single-instance reply comes back as one flat object.
    if "index" in data and "adb_port" in data:
        return {str(data.get("index", "0")): data}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def mumu_running() -> list[tuple[int, str, dict]]:
    """(index, 'host:port', info) for every started instance, ascending."""
    found = []
    for key, info in mumu_instances().items():
        started = bool(info.get("is_android_started")) or \
            info.get("player_state") == "start_finished"
        host = info.get("adb_host_ip")
        port = info.get("adb_port")
        if not started or not host or not port:
            continue
        try:
            index = int(key)
        except ValueError:
            index = 0
        found.append((index, f"{host}:{port}", info))
    return sorted(found, key=lambda item: item[0])


def adb_devices() -> list[tuple[str, str]]:
    """[(serial, state)] from `adb devices`."""
    out = run([ADB, "devices"], timeout=30)
    devices = []
    for line in out.splitlines():
        match = re.match(r"^(\S+)\t(\S+)$", line.strip())
        if match:
            devices.append((match.group(1), match.group(2)))
    return devices


def is_online(target: str) -> bool:
    return any(serial_ == target and state == "device" for serial_, state in adb_devices())


def connect(target: str, timeout: float = 8.0) -> bool:
    """adb connect + wait until the device shows up as `device`."""
    if is_online(target):
        return True
    run([ADB, "start-server"], timeout=30)
    run([ADB, "connect", target], timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_online(target):
            return True
        time.sleep(1.0)
    return is_online(target)


def detect_serial() -> str:
    """Resolve the adb endpoint of the MuMu instance that is actually running."""
    env_serial = os.environ.get("XBUD_SERIAL", "").strip()
    if env_serial:
        _note(f"serial from XBUD_SERIAL: {env_serial}")
        return env_serial

    running = mumu_running()
    if running:
        want = os.environ.get("XBUD_MUMU_INDEX", "").strip()
        if want:
            for index, target, info in running:
                if str(index) == want:
                    _note(f"serial from XBUD_MUMU_INDEX={want}: {target}")
                    return target
            _note(f"instance {want} is not running, falling back to the first one")
        index, target, info = running[0]
        if len(running) > 1:
            names = ", ".join(f"{i}:{t}({d.get('name', '')})" for i, t, d in running)
            _note(f"several MuMu instances running ({names}); using {target}")
        else:
            _note(f"MuMu instance {index} ({info.get('name', '')}) -> {target}")
        return target

    devices = [d for d in adb_devices() if d[1] == "device"]
    if len(devices) == 1:
        _note(f"serial from `adb devices`: {devices[0][0]}")
        return devices[0][0]
    if len(devices) > 1:
        preferred = [d[0] for d in devices if d[0].startswith("127.0.0.1:")]
        if preferred:
            _note(f"several adb devices, using {preferred[0]}")
            return preferred[0]

    _note(f"nothing running, defaulting to {DEFAULT_SERIAL}")
    return DEFAULT_SERIAL


def serial(refresh: bool = False) -> str:
    """Cached adb endpoint."""
    global _serial_cache
    if refresh or _serial_cache is None:
        _serial_cache = detect_serial()
    return _serial_cache


def adb(*args: str, timeout: int = 60) -> str:
    """Run an adb command against the resolved instance."""
    target = serial()
    if not is_online(target):
        connect(target)
    return run([ADB, "-s", target, *args], timeout=timeout)


def describe() -> str:
    """Human readable one-liner for logs."""
    running = mumu_running()
    if not running:
        return f"serial={serial()} (no running MuMu instance detected)"
    parts = [f"{i}:{t}" + (f"({d.get('name', '')})" if d.get("name") else "")
             for i, t, d in running]
    return f"serial={serial()}  instances=[{', '.join(parts)}]"


if __name__ == "__main__":
    print(describe())
    if not is_online(serial()):
        print("not connected; trying adb connect...")
        print("connected" if connect(serial()) else "still offline")
