#!/usr/bin/env python3
"""Small-step (小步点) 学分跑 auto-runner.

Drives one complete qualifying run without touching the screen:

  home -> 学分跑 -> reset/hold -> play -> go -> <duration> -> pause ->
  long-press finish -> 结束运动 -> record page -> home

The location/cadence stream comes from the LSPosed module (com.dsh.xbudloc);
this script only presses buttons and reads state back through the module's
`views` command, so it keeps working when the app's UI is re-rendered.

Usage:
  python autorun.py status                 # where are we, what does the app show
  python autorun.py start                  # open a session, reset+hold, play, go
  python autorun.py progress               # distance / time / pace / cadence
  python autorun.py finish                 # pause, long-press finish, confirm
  python autorun.py run --seconds 760      # one full run, then verify the counter
  python autorun.py loop --runs 3          # repeat until the semester target

Everything that matters is logged to xbud_run_logs/.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

import adbcfg

ADB = adbcfg.ADB
PKG = adbcfg.PKG
# The adb endpoint is resolved at runtime from MuMuManager's instance table
# (see adbcfg.py): XBUD_SERIAL > XBUD_MUMU_INDEX > first running instance.
SERIAL = None  # kept for backwards compatibility, use adbcfg.serial()

CMD_FILE = "/data/data/run.xbud.android/files/xbud_cmd"
TMP_CMD = "/data/local/tmp/xbud_cmd.txt"

LOG_DIR = r"D:\Work\xbud_run_logs"

HOME = "MainActivity"
START = "RunStartActivity"
RUN = "RunAdvancedActivity"
RECORD = "RunRecordActivity"

# Fallback coordinates (1080x1920), only used when the view tree is unavailable.
TAP_RUN_FAB = (540, 1846)
TAP_SCORE_TAB = (714, 138)
TAP_PLAY = (540, 1715)
TAP_PAUSE = (540, 1659)
TAP_FINISH = (354, 1659)
TAP_AD_CLOSE = (935, 480)

VIEW_RE = re.compile(
    r"^V (?P<depth>\d+) (?P<cls>\S+) id=(?P<id>\S+) "
    r"(?P<x>-?\d+),(?P<y>-?\d+) (?P<w>\d+)x(?P<h>\d+) vis=(?P<vis>\d+)"
    r"(?: t=(?P<text>.*))?$"
)


# --------------------------------------------------------------------------- io
def adb(*args: str, timeout: int = 60) -> str:
    """adb against whichever MuMu instance is running (resolved by adbcfg)."""
    return adbcfg.adb(*args, timeout=timeout)


def log(message: str) -> None:
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "autorun.log"), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def shot(tag: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    name = f"{time.strftime('%H%M%S')}_{tag}.png"
    path = os.path.join(LOG_DIR, name)
    adb("shell", "screencap -p /sdcard/_auto.png")
    adb("pull", "/sdcard/_auto.png", path)
    return path


def tap(x: int, y: int) -> None:
    adb("shell", f"input tap {x} {y}")


def long_press(x: int, y: int, ms: int = 5000) -> None:
    adb("shell", f"input swipe {x} {y} {x} {y} {ms}")


def back() -> None:
    adb("shell", "input keyevent 4")


# ------------------------------------------------------------------- activities
def focus() -> str:
    text = adb("shell", "dumpsys window | grep -E 'mCurrentFocus'")
    match = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([\w.]+)/([\w.$]+)\}", text)
    if not match:
        return ""
    return match.group(2).rsplit(".", 1)[-1]


def wait_activity(name: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if name in focus():
            return True
        time.sleep(0.7)
    return False


# ----------------------------------------------------------------- module state
_cmd_counter = 0


def send(command: str) -> None:
    """Write a command for the module; the token defeats its de-duplication.

    The write is read back: a dropped `adb push` used to leave the previous
    command in place (which silently kept the session frozen).
    """
    global _cmd_counter
    _cmd_counter += 1
    token = f"{int(time.time() * 1000) % 100000}-{_cmd_counter}"
    payload = f"{command} {token}"
    tmp = os.path.join(LOG_DIR, "_cmd.txt")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(tmp, "w", encoding="ascii", errors="replace") as handle:
        handle.write(payload + "\n")
    for attempt in range(3):
        adb("push", tmp, TMP_CMD)
        adb(
            "shell",
            "su -c 'cat {tmp} > {dst}; chmod 666 {dst}'".format(tmp=TMP_CMD, dst=CMD_FILE),
        )
        time.sleep(0.25)
        current = adb("shell", f"su -c 'cat {CMD_FILE}'").strip()
        if current == payload:
            return
        log(f"send: write mismatch (attempt {attempt + 1}) got '{current}'")
    log(f"send: giving up on '{command}'")


@dataclass
class View:
    vid: str
    cls: str
    x: int
    y: int
    w: int
    h: int
    vis: int
    text: str

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def views(wait: float = 1.4) -> dict[str, View]:
    """Snapshot the current activity's view tree via the module.

    logcat is never cleared (the module also logs the run intent there); the
    newest contiguous `V ...` block that ends with `VIEWS done` is parsed.
    """
    send("views")
    time.sleep(wait)
    raw = adb("logcat", "-d", "-t", "1500")
    lines = []
    for line in raw.splitlines():
        if "XbudLoc: V " in line or "XbudLoc: VIEWS done" in line:
            lines.append(line.split("XbudLoc: ", 1)[1].strip())
    result: dict[str, View] = {}
    if not lines or not lines[-1].endswith("VIEWS done"):
        # A partially flushed block: fall back to the last complete one.
        while lines and not lines[-1].endswith("VIEWS done"):
            lines.pop()
        if not lines:
            return result
    block = []
    for line in reversed(lines[:-1]):
        if not line.startswith("V "):
            break
        block.append(line)
    for body in reversed(block):
        match = VIEW_RE.match(body)
        if not match:
            continue
        view = View(
            vid=match.group("id"),
            cls=match.group("cls"),
            x=int(match.group("x")),
            y=int(match.group("y")),
            w=int(match.group("w")),
            h=int(match.group("h")),
            vis=int(match.group("vis")),
            text=(match.group("text") or "").strip(),
        )
        result[view.vid] = view
    return result


def tap_view(tree: dict[str, View], vid: str, fallback: tuple[int, int]) -> bool:
    view = tree.get(vid)
    if view and view.vis == 0 and view.w > 0 and view.h > 0:
        tap(*view.center)
        return True
    tap(*fallback)
    return False


def pace_seconds(text: str) -> float | None:
    match = re.match(r"^(\d+)'(\d+)\"$", text.strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def clock_seconds(text: str) -> float | None:
    parts = text.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None


@dataclass
class Progress:
    km: float
    seconds: float
    pace: float | None
    cadence: int | None


def progress() -> Progress | None:
    tree = views()
    dist = tree.get("tvDistance")
    clock = tree.get("tvTime")
    if not dist or not clock:
        return None
    try:
        km = float(dist.text)
    except ValueError:
        return None
    seconds = clock_seconds(clock.text) or 0.0
    speed = tree.get("tvSpeed")
    step = tree.get("tvStep")
    return Progress(
        km=km,
        seconds=seconds,
        pace=pace_seconds(speed.text) if speed else None,
        cadence=int(step.text) if step and step.text.isdigit() else None,
    )


# ----------------------------------------------------------------------- counts
def ui_texts() -> list[tuple[str, tuple[int, int, int, int]]]:
    """uiautomator dump -> (text, bounds). Works on Flutter pages, not on maps."""
    adb("shell", "rm -f /sdcard/_auto_ui.xml")
    for _ in range(3):
        out = adb("shell", "uiautomator dump /sdcard/_auto_ui.xml", timeout=40)
        if "dumped" in out:
            break
        time.sleep(1.5)
    local = os.path.join(LOG_DIR, "_auto_ui.xml")
    os.makedirs(LOG_DIR, exist_ok=True)
    adb("pull", "/sdcard/_auto_ui.xml", local)
    try:
        with open(local, "r", encoding="utf-8", errors="replace") as handle:
            xml = handle.read()
    except OSError:
        return []
    nodes = []
    for match in re.finditer(r'<node[^>]*text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        text = match.group(1).strip()
        if not text:
            continue
        nodes.append((text, tuple(int(match.group(i)) for i in range(2, 6))))
    return nodes


def home_count() -> tuple[int, int] | None:
    """Read 课外跑 count and the semester target from the home page."""
    nodes = ui_texts()
    for attempt in range(3):
        if any("跑步未完成" in t for t, _ in nodes):
            tap_text("放弃")
            time.sleep(1.5)
            nodes = ui_texts()
            continue
        if any(t.startswith("课外跑") for t, _ in nodes):
            break
        tap(108, 1886)  # 首页 tab
        time.sleep(2.5)
        nodes = ui_texts()
    target = None
    for text, _ in nodes:
        match = re.search(r"目标\s*(\d+)\s*次", text)
        if match:
            target = int(match.group(1))
            break
    label = next((b for t, b in nodes if t.startswith("课外跑")), None)
    candidates = []
    for text, box in nodes:
        if text.isdigit() and label:
            candidates.append((abs(box[1] - (label[3] + 40)), int(text)))
    if not candidates:
        return None
    count = min(candidates)[1]
    return count, target or 0


# ------------------------------------------------------------------------ flows
APP_ACTS = ("MainActivity", "RunStartActivity", "RunAdvancedActivity", "RunRecordActivity",
            "RunRecordDetailActivity", "SportSettingActivity", "XBDWebViewActivity",
            "SplashActivity")


AD_ACTIVITY_MARKERS = ("Stub_", "Landing", "com.byazt", "pangle", "BeiZi")

# The reward-banner ad has no exposed close node; its ✕ sits at the banner's
# right edge, roughly 360 px down.  Several candidates in case the banner moves.
AD_CLOSE_POINTS = ((1030, 360), (1030, 300), (1040, 420), (1030, 250))


def is_ad(current: str | None = None) -> bool:
    current = focus() if current is None else current
    return any(marker.lower() in current.lower() for marker in AD_ACTIVITY_MARKERS)


def dismiss_ad() -> bool:
    """Close an ad activity/overlay. Returns True when something was dismissed."""
    if not is_ad():
        return False
    log(f"ad activity: {focus()}")
    for point in AD_CLOSE_POINTS:
        tap(*point)
        time.sleep(1.5)
        if not is_ad():
            log("ad closed")
            return True
    back()
    time.sleep(1.5)
    if not is_ad():
        log("ad closed with back")
        return True
    log("ad persists, restarting the app")
    force_restart_app()
    return True


def goto_home() -> bool:
    for _ in range(8):
        if dismiss_ad():
            continue
        current = focus()
        if HOME in current:
            return True
        if "com.mumu.store" in adb("shell", "dumpsys window | grep mCurrentFocus"):
            # MuMu's own store sometimes pops over the app (and breaks taps).
            adb("shell", "am force-stop com.mumu.store")
            time.sleep(1.5)
            continue
        if not current or not current.endswith(APP_ACTS):
            adb("shell", f"am start -n {PKG}/.mvp.ui.other.SplashActivity")
            time.sleep(8)
            continue
        back()
        time.sleep(2)
    return HOME in focus()


def tap_text(needle: str, contains: bool = True) -> bool:
    for text, box in ui_texts():
        hit = needle in text if contains else text == needle
        if hit:
            x = (box[0] + box[2]) // 2
            y = (box[1] + box[3]) // 2
            log(f"tap_text '{text}' at {x},{y}")
            tap(x, y)
            return True
    return False


def dismiss_unfinished() -> bool:
    """'您有跑步未完成' prompt: pick 放弃."""
    for text, _ in ui_texts():
        if "跑步未完成" in text or "跑步提示" in text:
            return tap_text("放弃")
    return False


def open_start() -> bool:
    if not goto_home():
        log(f"open_start: cannot reach home (focus={focus()})")
        return False
    for attempt in range(3):
        dismiss_ad()
        tap(*TAP_RUN_FAB)
        if wait_activity(START, 10):
            break
        if is_ad():
            log("open_start: ad intercepted the tap, closing it")
            dismiss_ad()
            continue
        if dismiss_unfinished():
            time.sleep(2)
            continue
        log(f"open_start: no RunStartActivity (focus={focus()})")
        shot("open_start_failed")
        back()
        time.sleep(2)
    if START not in focus():
        return False
    time.sleep(1.0)
    # Make sure 学分跑 (not 休闲跑) is the selected tab.
    tree = views()
    score = tree.get("tvScore")
    if score:
        tap(*score.center)
        time.sleep(1.5)
    return True


def module_pos() -> tuple[float, float] | None:
    send("cam")
    time.sleep(0.9)
    raw = adb("logcat", "-d", "-t", "300")
    for line in reversed(raw.splitlines()):
        if "XbudLoc: CAM" in line and "pos=" in line:
            try:
                lat, lng = line.split("pos=")[-1].strip().split(",")
                return float(lat), float(lng)
            except ValueError:
                return None
    return None


def start_session(settle: float = 9.0) -> bool:
    if RUN in focus():
        log("start_session: already on the run page")
        return True
    if not open_start():
        return False
    send("reset")
    time.sleep(0.8)
    send("hold")
    time.sleep(0.8)
    tree = views()
    tap_view(tree, "btnStart", TAP_PLAY)
    if not wait_activity(RUN, 25):
        log(f"start_session: run page did not open (focus={focus()})")
        shot("start_failed")
        return False
    log(f"start_session: running, holding start point for {settle:.0f}s")
    time.sleep(settle)
    for attempt in range(3):
        send("go")
        first = module_pos()
        time.sleep(8)
        second = module_pos()
        if first and second:
            moved = math.hypot((second[0] - first[0]) * 111320,
                               (second[1] - first[1]) * 93000)
            log(f"start_session: go check #{attempt + 1}, moved {moved:.1f} m in 8 s")
            if moved >= 15:
                return True
        else:
            log("start_session: no position readback")
    log("start_session: module still frozen")
    return False


def confirm_dialog() -> bool:
    """Tap 结束运动 on the finish confirmation sheet (after its countdown)."""
    deadline = time.time() + 25
    while time.time() < deadline:
        nodes = ui_texts()
        for text, box in nodes:
            if ("结束运动" in text or "结束本次" in text) and "(" not in text:
                x = (box[0] + box[2]) // 2
                y = (box[1] + box[3]) // 2
                log(f"confirm: tap '{text}' at {x},{y}")
                tap(x, y)
                time.sleep(3)
                return True
        time.sleep(1.5)
    return False


def force_restart_app() -> None:
    """Restart the app: clears ad overlays/animation stalls before UI scraping."""
    adb("shell", f"am force-stop {PKG}")
    time.sleep(2)
    adb("shell", f"am start -n {PKG}/.mvp.ui.other.SplashActivity")
    for _ in range(10):
        time.sleep(1.5)
        if HOME in focus():
            return


def reupload_latest() -> bool:
    """我的 -> 跑步记录 -> 最新一条『重新上传』."""
    goto_home()
    views_buttons = []
    for _ in range(6):
        tap(970, 1885)  # 我的
        time.sleep(2.2)
        nodes = ui_texts()
        views_buttons = [b for t, b in nodes if t == "查看记录"]
        if views_buttons:
            break
    if not views_buttons:
        log("reupload: no 查看记录 entry")
        return False
    box = max(views_buttons, key=lambda b: b[1])
    tap((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
    time.sleep(3)
    for attempt in range(4):
        nodes = ui_texts()
        buttons = [b for t, b in nodes if "重新上传" in t and abs((b[2] - b[0]) - 360) < 120]
        if not buttons:
            if any("上传失败" in t for t, _ in nodes):
                log("reupload: failure marker without a button")
                return False
            log("reupload: nothing left to upload")
            return True
        box = min(buttons, key=lambda b: b[1])  # newest record is on top
        log(f"reupload: tap at {(box[0] + box[2]) // 2},{(box[1] + box[3]) // 2}")
        tap((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        time.sleep(6)
    nodes = ui_texts()
    return not any("上传失败" in t for t, _ in nodes)


def after_finish() -> None:
    """Handle the reward popup, upload failures and the score card."""
    retries = 0
    stuck = False
    for _ in range(10):
        texts = [t for t, _ in ui_texts()]
        if any("上传失败" in t for t in texts):
            retries += 1
            shot("upload_failed")
            if retries > 2:
                log("upload keeps failing in-run, switching to 跑步记录 re-upload")
                stuck = True
                break
            log(f"upload failed, retry {retries}")
            tap_text("重新上传")
            time.sleep(5)
            continue
        if any("伤害值" in t for t in texts) or any(t == "确认" for t in texts):
            tap_text("确认")
            time.sleep(2)
            continue
        if any("成绩达标" in t or "再接再厉" in t for t in texts):
            shot("score_card")
            log("score card: " + " | ".join(t for t in texts if t in ("成绩达标", "再接再厉")))
            back()
            time.sleep(2)
            continue
        break
    if stuck or reupload_needed():
        if not reupload_latest():
            # A fresh process has no ad overlay and no running animations.
            log("reupload: retrying after an app restart")
            force_restart_app()
            reupload_latest()


def reupload_needed() -> bool:
    texts = [t for t, _ in ui_texts()]
    return any("上传失败" in t for t in texts)


def pause_run() -> bool:
    """Pause from whichever layout is showing (operate or map)."""
    for attempt in range(3):
        tree = views()
        finish = tree.get("btnFinish")
        if finish and finish.vis == 0 and finish.w > 0:
            return True
        pause = tree.get("btnPause")
        if pause and pause.vis == 0 and attempt < 2:
            tap(*pause.center)
        else:
            op = tree.get("btnGoOperate")
            if op:
                tap(*op.center)
                time.sleep(1.2)
                tree = views()
                pause = tree.get("btnPause")
                if pause:
                    tap(*pause.center)
        time.sleep(1.8)
    tree = views()
    finish = tree.get("btnFinish")
    return bool(finish and finish.vis == 0 and finish.w > 0)


def finish_session() -> bool:
    if RUN not in focus():
        log(f"finish_session: not on the run page (focus={focus()})")
        return False
    if not pause_run():
        log("finish: could not reach the pause/end layout")
        shot("pause_failed")
        return False
    log("finish: paused")
    tree = views()
    finish = tree.get("btnFinish")
    if finish and finish.w > 0:
        log(f"finish: long-press btnFinish at {finish.center}")
        long_press(*finish.center, 5000)
    else:
        log(f"finish: long-press fallback {TAP_FINISH}")
        long_press(*TAP_FINISH, 5000)
    time.sleep(2.0)
    shot("after_longpress")
    if not confirm_dialog():
        log("finish: confirmation sheet not found")
        shot("no_confirm")
        return False
    log("finish: confirmed")
    return True


def close_ads(rounds: int = 6) -> None:
    for _ in range(rounds):
        current = focus()
        if "Ad" in current or "Splash" in current:
            log(f"ad: {current}, closing")
            tap(*TAP_AD_CLOSE)
            time.sleep(2.5)
            continue
        break


def runinfo() -> None:
    """Show the assignment the app received (min length, checkpoints)."""
    raw = adb("logcat", "-d", "-t", "4000")
    for line in raw.splitlines():
        if "XbudLoc: INTENT " in line:
            body = line.split("XbudLoc: INTENT ", 1)[1].strip()
            if len(body) > 400:
                body = body[:400] + "..."
            log(f"  {body}")


def one_run(seconds: float, min_km: float = 2.5) -> bool:
    if not start_session():
        return False
    runinfo()
    started = time.time()
    runway = seconds * 1.45
    last = None
    while True:
        elapsed = time.time() - started
        info = progress()
        if info:
            current = (round(info.km, 2), info.cadence, info.pace)
            if current != last:
                log(
                    f"run {elapsed:6.1f}s | {info.km:.2f} km | "
                    f"{info.seconds:.0f}s app | pace "
                    f"{info.pace if info.pace is None else int(info.pace)}s | "
                    f"cadence {info.cadence}"
                )
                last = current
            if elapsed > 45 and info.km >= min_km:
                log(f"run: distance target reached ({info.km:.2f} km)")
                break
            if info.km >= 3.6:
                log("run: overshoot guard")
                break
        if elapsed > runway:
            log("run: time cap reached")
            break
        time.sleep(20)
    if not finish_session():
        return False
    after_finish()
    return True


def verify(expect: int) -> bool:
    """Return home and read the 计入次数 counter."""
    close_ads()
    for _ in range(5):
        if HOME in focus():
            break
        back()
        time.sleep(2.5)
    time.sleep(2)
    counts = home_count()
    if counts is None:
        log("verify: home count unreadable")
        shot("verify_fail")
        return False
    count, target = counts
    log(f"verify: 课外跑 {count}/{target} (expected {expect})")
    return count >= expect


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "start", "progress", "finish", "run", "loop", "verify"])
    parser.add_argument("--seconds", type=float, default=760.0)
    parser.add_argument("--min-km", type=float, default=2.5)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--expect", type=int, default=0)
    args = parser.parse_args()

    log(f"=== {args.action} (focus={focus()}) ===")
    if args.action == "status":
        log(f"focus: {focus()}")
        for vid, view in sorted(views().items()):
            if view.vis == 0 and view.text:
                log(f"  {vid:<22} {view.text}")
        return 0
    if args.action == "start":
        return 0 if start_session() else 1
    if args.action == "progress":
        info = progress()
        log(f"progress: {info}")
        return 0 if info else 1
    if args.action == "finish":
        return 0 if finish_session() else 1
    if args.action == "verify":
        return 0 if verify(args.expect) else 1
    if args.action == "run":
        return 0 if one_run(args.seconds, args.min_km) else 1
    if args.action == "loop":
        for index in range(args.runs):
            log(f"--- run {index + 1}/{args.runs} ---")
            if not one_run(args.seconds, args.min_km):
                log("loop: run failed, aborting")
                return 1
            time.sleep(8)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

