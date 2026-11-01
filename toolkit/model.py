#!/usr/bin/env python3
"""Apply a coherent device profile with KernelSU's resetprop.

Only user-visible properties are touched.  `ro.hardware` and
`ro.boot.hardware` are deliberately left alone: they select the HAL modules
loaded at boot, and changing them after boot breaks camera/audio.

Examples:
    python model.py --list
    python model.py --profile oneplus12
    python model.py --profile xiaomi14 --persist
    python model.py --profile revert
"""

from __future__ import annotations

import argparse
import subprocess
import sys

ADB = r"D:\MuMuPlayer\nx_device\15.0\shell\adb.exe"
SERIAL = "127.0.0.1:16384"
MODULE_DIR = "/data/adb/modules/xbud_profile"

PROFILES = {
    "oneplus12": {
        "ro.product.brand": "OnePlus",
        "ro.product.manufacturer": "OnePlus",
        "ro.product.model": "PJD110",
        "ro.product.name": "PJD110",
        "ro.product.device": "PJD110",
        "ro.build.product": "PJD110",
        "ro.product.board": "pineapple",
        "ro.build.fingerprint": "OnePlus/PJD110/PJD110:15/V417IR/1137:user/release-keys",
    },
    "xiaomi14": {
        "ro.product.brand": "Xiaomi",
        "ro.product.manufacturer": "Xiaomi",
        "ro.product.model": "23127PN0CC",
        "ro.product.name": "houji",
        "ro.product.device": "houji",
        "ro.build.product": "houji",
        "ro.product.board": "pineapple",
        "ro.build.fingerprint": "Xiaomi/houji/houji:15/AQ3A.240912.001/OS2.0.9.0:user/release-keys",
    },
    "pixel9": {
        "ro.product.brand": "google",
        "ro.product.manufacturer": "Google",
        "ro.product.model": "Pixel 9",
        "ro.product.name": "tokay",
        "ro.product.device": "tokay",
        "ro.build.product": "tokay",
        "ro.product.board": "tokay",
        "ro.build.fingerprint": "google/tokay/tokay:15/AP3A.241105.008/12345678:user/release-keys",
    },
    "revert": {
        "ro.product.brand": "OnePlus",
        "ro.product.manufacturer": "OnePlus",
        "ro.product.model": "PHB110",
        "ro.product.name": "PHB110",
        "ro.product.device": "PHB110",
        "ro.build.product": "PHB110",
        "ro.product.board": "kona",
        "ro.build.fingerprint": "OnePlus/PHB110/PHB110:15/V417IR/1137:user/release-keys",
    },
}


def adb(args: list[str], data: bytes | None = None) -> bytes:
    proc = subprocess.run(
        [ADB, "-s", SERIAL, *args],
        input=data,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout


def shell(cmd: str) -> str:
    return adb(["shell", cmd]).decode("utf-8", "replace")


def apply_profile(name: str) -> None:
    props = PROFILES[name]
    script = ["#!/system/bin/sh"]
    for key, value in props.items():
        script.append(f"resetprop {key} '{value}'")
    body = "\n".join(script) + "\n"

    adb(["push", "-"], b"") if False else None
    # Write through su so the script runs with root; base64 avoids quoting.
    import base64
    b64 = base64.b64encode(body.encode()).decode()
    shell(f"su -c 'echo {b64} | base64 -d > /data/local/tmp/xbud_profile.sh'")
    shell("su -c 'sh /data/local/tmp/xbud_profile.sh'")


def persist(name: str) -> None:
    props = PROFILES[name]
    lines = ["#!/system/bin/sh", "RESETPROP=/data/adb/ksu/bin/resetprop", ""]
    for key, value in props.items():
        lines.append(f"$RESETPROP {key} '{value}'")
    script = "\n".join(lines) + "\n"

    module_prop = (
        "id=xbud_profile\n"
        "name=Device profile\n"
        f"description=Applies the {name} property profile at boot\n"
        "version=1.0\n"
        "versionCode=1\n"
        "author=xbud-toolkit\n"
    )

    import base64
    for path, content in (
        (f"{MODULE_DIR}/module.prop", module_prop),
        (f"{MODULE_DIR}/post-fs-data.sh", script),
    ):
        b64 = base64.b64encode(content.encode()).decode()
        shell(f"su -c 'mkdir -p {MODULE_DIR}'")
        shell(f"su -c 'echo {b64} | base64 -d > {path}'")
    shell(f"su -c 'chmod 755 {MODULE_DIR}/post-fs-data.sh'")
    print(f"installed KernelSU module at {MODULE_DIR} (active after reboot)")


def verify() -> None:
    keys = ["ro.product.brand", "ro.product.manufacturer", "ro.product.model",
            "ro.product.name", "ro.product.device", "ro.build.product",
            "ro.product.board", "ro.build.fingerprint"]
    for key in keys:
        print(f"{key}={shell(f'getprop {key}').strip()}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", choices=sorted(PROFILES))
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name, props in PROFILES.items():
            print(f"{name}: {props['ro.product.model']} ({props['ro.product.device']})")
        return 0

    if not args.profile:
        ap.error("--profile is required")

    apply_profile(args.profile)
    if args.persist:
        persist(args.profile)
    print(f"applied profile: {args.profile}")
    verify()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
