#!/usr/bin/env python3
"""Build a route and load it into FakeLocation's route simulator.

FakeLocation keeps its state in
  /data/data/com.yinshibai.location/shared_prefs/fake_location.xml
with `path_points` as a JSON [[lat, lng], ...] array.  Writing that file and
restarting the app is enough to make it drive the route at `path_speed` m/s;
the app's LSPosed module hides the mock flag from 小步点, so no hook is needed
on either side.

Examples:
    python route.py --point 33.551549,119.046629
    python route.py --center 33.551549,119.046629 --perimeter 400 --laps 5
    python route.py --from-gpx track.gpx --speed 3.0
    python route.py --inspect
    python route.py --stop
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET

ADB = r"D:\MuMuPlayer\nx_device\15.0\shell\adb.exe"
SERIAL = "127.0.0.1:16384"
PREFS = "/data/data/com.yinshibai.location/shared_prefs/fake_location.xml"
PKG = "com.yinshibai.location"


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


def root_cat() -> str:
    return adb(["shell", f"su -c 'cat {PREFS}'"]).decode("utf-8")


def root_write(xml_text: str) -> None:
    # `cat > file` keeps the inode and SELinux label of the original file.
    adb(["shell", f"su -c 'cat > {PREFS}'"], xml_text.encode("utf-8"))


def set_value(root: ET.Element, kind: str, name: str, value: str) -> None:
    node = root.find(f"{kind}[@name='{name}']")
    if node is None:
        node = ET.SubElement(root, kind, {"name": name})
    if kind == "string":
        # SharedPreferences stores strings as element text, not as an attribute.
        node.text = value
    else:
        node.set("value", value)


def get_value(root: ET.Element, name: str) -> str | None:
    for node in root:
        if node.get("name") == name:
            return node.get("value") if node.get("value") is not None else node.text
    return None


def restart_app() -> None:
    adb(["shell", f"am force-stop {PKG}"])
    adb(["shell", f"am start -n {PKG}/.MainActivity"])


def meters_to_deg(lat: float) -> tuple[float, float]:
    return 1.0 / 111_320.0, 1.0 / (111_320.0 * math.cos(math.radians(lat)))


def stadium(center: tuple[float, float], perimeter: float, width: float,
            laps: int, step: float = 8.0) -> list[list[float]]:
    """Closed stadium loop: two straights plus two semicircles."""
    lat0, lon0 = center
    radius = width / 2.0
    straight = (perimeter - 2.0 * math.pi * radius) / 2.0
    if straight <= 0:
        raise ValueError("perimeter too small for the requested width")

    # Local metres: x east, y north.  Start at the south-west corner.
    pts: list[tuple[float, float]] = []
    x0, y0 = -straight / 2.0, -radius
    n_straight = max(2, int(round(straight / step)))
    for i in range(n_straight):
        pts.append((x0 + straight * i / n_straight, y0))

    cx, cy = straight / 2.0, 0.0
    n_arc = max(4, int(round(math.pi * radius / step)))
    for i in range(n_arc):
        a = -math.pi / 2.0 + math.pi * i / n_arc
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))

    x1, y1 = straight / 2.0, radius
    for i in range(n_straight):
        pts.append((x1 - straight * i / n_straight, y1))

    cx, cy = -straight / 2.0, 0.0
    for i in range(n_arc):
        a = math.pi / 2.0 + math.pi * i / n_arc
        pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))

    dlat, dlon = meters_to_deg(lat0)
    loop = [[lat0 + y * dlat, lon0 + x * dlon] for x, y in pts]
    loop.append(loop[0])
    return loop * max(1, laps)


def parse_gpx(path: str) -> list[list[float]]:
    tree = ET.parse(path)
    pts: list[list[float]] = []
    for node in tree.iter():
        if node.tag.endswith("trkpt") or node.tag.endswith("rtept"):
            pts.append([float(node.get("lat")), float(node.get("lon"))])
    if len(pts) < 2:
        raise ValueError("GPX contains fewer than two track points")
    return pts


def haversine(a: list[float], b: list[float]) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def route_length(points: list[list[float]]) -> float:
    return sum(haversine(points[i], points[i + 1]) for i in range(len(points) - 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--point", help="static point: LAT,LNG")
    ap.add_argument("--center", help="route centre: LAT,LNG")
    ap.add_argument("--perimeter", type=float, default=400.0, help="metres per lap")
    ap.add_argument("--width", type=float, default=60.0, help="track width in metres")
    ap.add_argument("--laps", type=int, default=1)
    ap.add_argument("--speed", type=float, default=3.0, help="m/s (3.0 ~= 5'33\"/km)")
    ap.add_argument("--from-gpx")
    ap.add_argument("--accuracy", type=float, default=2.0)
    ap.add_argument("--inspect", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.inspect:
        # Stop first: a running FakeLocation flushes its own copy on exit and
        # would overwrite the file we are about to edit.
        adb(["shell", f"am force-stop {PKG}"])
    xml_text = root_cat()
    root = ET.fromstring(xml_text)

    if args.inspect:
        pts = json.loads(get_value(root, "path_points") or "[]")
        print(f"enabled={get_value(root, 'enabled')} "
              f"lat={get_value(root, 'lat')} lng={get_value(root, 'lng')} "
              f"speed={get_value(root, 'path_speed')} points={len(pts)}")
        if len(pts) > 1:
            print(f"route length ~{route_length(pts):.0f} m")
        return 0

    if args.stop:
        set_value(root, "boolean", "enabled", "false")
        root_write(ET.tostring(root, encoding="unicode"))
        restart_app()
        print("FakeLocation mock stopped")
        return 0

    if args.point:
        lat, lng = (float(v) for v in args.point.split(","))
        set_value(root, "string", "lat", f"{lat:.6f}")
        set_value(root, "string", "lng", f"{lng:.6f}")
        # Clear any saved path so the app comes back in point mode.
        set_value(root, "string", "path_points", "[]")
        set_value(root, "boolean", "enabled", "true")
        if args.dry_run:
            print(f"would set point {lat:.6f},{lng:.6f}")
            return 0
        root_write(ET.tostring(root, encoding="unicode"))
        restart_app()
        print(f"point mock -> {lat:.6f},{lng:.6f}")
        return 0

    if args.from_gpx:
        points = parse_gpx(args.from_gpx)
    elif args.center:
        lat, lng = (float(v) for v in args.center.split(","))
        points = stadium((lat, lng), args.perimeter, args.width, args.laps)
    else:
        ap.error("one of --point, --center, --from-gpx, --inspect, --stop is required")

    length = route_length(points)
    if args.dry_run:
        print(f"points={len(points)} length={length:.0f} m speed={args.speed} m/s "
              f"eta={length / max(args.speed, 0.1) / 60:.1f} min")
        print(json.dumps([[round(a, 6), round(b, 6)] for a, b in points[:3]]))
        return 0

    set_value(root, "string", "path_points", json.dumps(points, separators=(",", ":")))
    set_value(root, "string", "path_geometry", "CONTROL_POINTS")
    set_value(root, "float", "path_speed", f"{args.speed:g}")
    set_value(root, "float", "accuracy", f"{args.accuracy:g}")
    set_value(root, "string", "lat", f"{points[0][0]:.6f}")
    set_value(root, "string", "lng", f"{points[0][1]:.6f}")
    set_value(root, "boolean", "enabled", "true")
    root_write(ET.tostring(root, encoding="unicode"))
    restart_app()
    print(f"route loaded: {len(points)} points, {length:.0f} m, "
          f"{args.speed:g} m/s, eta {length / max(args.speed, 0.1) / 60:.1f} min")
    print("open FakeLocation -> 路线模拟; it starts with the saved path")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
