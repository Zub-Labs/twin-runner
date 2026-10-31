"""Explore mode: drive each app like a curious user for up to --minutes and record every step.

  python -m harness.explore --apks /data/apks-x --out /data/explore/x [--minutes 20] [--keys "DOWN*2,OK,WAIT5"]

Per app (one folder <out>/<apk stem>/, DONE written last):
  install + launch (as harness.apptest), then a depth-first walk: each screenshot is reduced to a 96x54 grey thumbnail; a screen
  is "seen" when less than 0.4 % of its thumbnail pixels differ from a screen already visited. A new screen gets the keys
  OK, DOWN, RIGHT, UP, LEFT (+ MENU on the first screen and on screens opened with OK); a screen with no keys left gets BACK
  when it was opened with OK, else a random arrow. Stops when 40 actions in a row give no new screen or the time is up.
  Playback: the screen keeps changing with no key pressed (3 checks) -> hold 60 s, a screenshot every 5 s (once per app).
  End: HOME, relaunch (resumed?), BACK until the app exits (max 3).
Files: steps/NNNN.jpg (640x360, every step), screens/NNNN.jpg (1920x1080, first visit of each distinct screen),
  events/NNNN-<event>-before|after.jpg (crash, black screen, stall), steps.tsv, result.json, app.log, atld.log, errors.txt.
"""
import argparse
import glob
import io
import json
import os
import random
import re
import time
import traceback

import numpy as np
from PIL import Image

from . import apptest, atl, twin
from .apptest import APPS, ATLD_LOG, conf, exited, lines, new_lines, oom_kills, sh, signature, visible_window

ARROWS = ["DOWN", "RIGHT", "UP", "LEFT"]
SAME = 0.004       # changed-pixel fraction below which two thumbnails are the same screen
MOVING = 0.02      # changed-pixel fraction between two shots 1 s apart (no key) that counts as motion
IDLE_STOP = 40
POINTER = False    # --pointer: opt-in pointer-grid fallback (off by default)
# pointer clicks (Magic Remote) on a 3x3 grid, tried on a screen where no key gave a new screen
GRID = ["CLICK%d_%d" % (x, y) for y in (200, 540, 880) for x in (480, 960, 1440)]
GREP_ERR = ("FATAL EXCEPTION|UnsatisfiedLinkError|NoClassDefFoundError|ClassNotFoundException|NoSuchMethodError|AbstractMethodError|"
            "SIGSEGV|SIGABRT|SIGBUS|Fatal signal|Segmentation fault|(java|android|kotlin)[.][A-Za-z0-9_.]+(Exception|Error)")
TOKEN_RE = re.compile(r"^(WAIT\d{1,3}|CLICK\d{1,4}_\d{1,4}|[A-Z0-9_]{1,12}(\*\d{1,2})?)$")


class Frame:
    def __init__(self, png):
        self.img = Image.open(io.BytesIO(png)).convert("RGB")
        self.small = np.asarray(self.img.convert("L").resize((96, 54), Image.BOX), dtype=np.int16)
        self.std = float(self.small.std()) / 255.0

    def dist(self, other):
        return float((np.abs(self.small - other.small) > 24).mean())

    @property
    def blank(self):
        return self.std < 0.012


def grab():
    return Frame(twin.shot("explore"))


def parse_keys(s):
    out = []
    for t in re.split(r"[\s,]+", (s or "").strip().upper()):
        if not t:
            continue
        if not TOKEN_RE.match(t):
            raise SystemExit("bad key script token %r" % t)
        out.append(t)
    return out


class App:
    def __init__(self, apk, base_url, out, minutes, keys):
        self.apk, self.base, self.minutes, self.keys = apk, base_url, minutes, keys
        self.name = os.path.basename(apk)
        self.dir = os.path.join(out, os.path.splitext(self.name)[0])
        for d in ("steps", "screens", "events"):
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
        self.r = {"apk": self.name, "size_mb": round(os.path.getsize(apk) / 1048576, 1), "events": [], "relaunches": 0,
                  "playback_seen": False, "crashes": 0}
        self.rows = []          # step log
        self.screens = []       # [(id, Frame-small, step)]
        self.todo = {}          # screen id -> untried keys
        self.opened = {}        # screen id -> reached with OK
        self.fruitful = set()   # screen ids from which a key led to a new screen
        self.clicked = set()    # screen ids that got the pointer grid
        self.peak_kb = 0
        self.visible = False    # the app had a visible window at the last alive() check
        self.ever_visible = False
        self.step = 0
        self.cur = None         # current screen id
        self.prev = None        # last Frame
        self.appid = ""
        self.atld0 = 0
        self.logoff = 0
        self.rng = random.Random(self.name)
        self.deadline = time.time() + minutes * 60

    # ---- recording
    def save(self, frame, sub, fname, full=False):
        im = frame.img if full else frame.img.resize((640, 360), Image.BILINEAR)
        im.save(os.path.join(self.dir, sub, fname), "JPEG", quality=78 if full else 70)

    def event(self, kind, before, after, detail=""):
        n = "%04d-%s" % (self.step, kind)
        if before is not None:
            self.save(before, "events", n + "-before.jpg", full=True)
        if after is not None:
            self.save(after, "events", n + "-after.jpg", full=True)
        self.r["events"].append({"step": self.step, "event": kind, "detail": detail[:300]})

    def identify(self, f):
        best, bid = 1.0, None
        for sid, small, _ in self.screens:
            d = float((np.abs(f.small - small) > 24).mean())
            if d < best:
                best, bid = d, sid
        if bid is not None and best < SAME:
            return bid, False
        sid = len(self.screens) + 1
        self.screens.append((sid, f.small, self.step))
        self.save(f, "screens", "%04d.jpg" % self.step, full=True)
        return sid, True

    def new_errors(self):
        p = "%s/%s/app.log" % (APPS, self.appid)
        r = sh("f=%s; s=$(stat -c %%s $f 2>/dev/null || echo 0); echo $s; [ $s -gt %d ] && tail -c +%d $f | head -c 400000 | "
               "grep -a -m 3 -E '%s'" % (p, self.logoff, self.logoff + 1, GREP_ERR), timeout=30)["stdout"]
        ls = r.splitlines()
        if ls and ls[0].strip().isdigit():
            self.logoff = int(ls[0])
        return [l.replace("\x00", "")[:240] for l in ls[1:]]

    def sample_mem(self):
        try:
            pids = sorted({w["pid"] for w in visible_window(self.appid)})
            if pids:
                out = sh("cat %s 2>/dev/null | awk '/VmHWM/{print $2}'" % " ".join("/proc/%d/status" % p for p in pids))["stdout"]
                self.peak_kb = max([self.peak_kb] + [int(x) for x in out.split() if x.isdigit()])
        except twin.TwinError:
            pass

    def alive(self):
        rc = exited(self.appid, new_lines(ATLD_LOG, self.atld0))
        self.visible = rc is None and bool(visible_window(self.appid))
        self.ever_visible = self.ever_visible or self.visible
        return rc, rc is None and (self.visible or self.appid in twin.running())

    def act(self, key, settle=2.0, count=True):
        """press key (or wait), screenshot, classify; returns (Frame, new?, moving?, rc, alive)
        count=False (after HOME), or the app alive without a visible window: the frame is not an app screen"""
        self.step += 1
        before = self.prev
        if key.startswith("WAIT"):
            time.sleep(int(key[4:] or 5))
        elif key.startswith("CLICK"):     # pointer click at x_y of the 1920x1080 screen
            x, y = (int(v) for v in key[5:].split("_"))
            twin.pointer("move", x, y)
            time.sleep(0.5)
            twin.pointer("click", x, y)
        elif key != "(look)":
            k, _, n = key.partition("*")
            twin.key(k, repeat=int(n or 1))
        time.sleep(settle)
        a = grab()
        time.sleep(1.0)
        f = grab()
        moving = a.dist(f) > MOVING
        rc, alive = self.alive()
        if alive and self.step % 10 == 1:
            self.sample_mem()
        errs = self.new_errors()
        new = False
        away = alive and not (count and (self.visible or not self.ever_visible))
        if away:
            sid = None
        elif alive:
            if moving and self.r["playback_seen"]:
                sid = self.cur          # video again: not a new screen
            else:
                sid, new = self.identify(f)
            self.cur = sid
        else:
            sid = None
        self.save(f, "steps", "%04d.jpg" % self.step)
        ev = []
        if errs:
            ev.append("error")
            self.r.setdefault("first_error_step", self.step)
        if not alive and rc not in (None, 0):
            ev.append("crash")
            self.r["crashes"] += 1
            self.r.setdefault("failed_step", self.step)
            self.r.setdefault("failed_key", key)
            self.event("crash", before, f, "rc %s" % rc)
        elif alive and f.blank and before is not None and not before.blank:
            ev.append("black")
            self.event("black", before, f)
        self.rows.append([self.step, round(time.time() - self.t0), key, sid if sid else "-",
                          "new" if new else ("away" if away else ("seen" if alive else "exit")),
                          "moving" if moving else "", " ".join(ev), " | ".join(errs)[:300]])
        self.prev = f
        return f, new, moving, rc, alive

    # ---- app life
    def install(self):
        t0 = time.time()
        r = atl.api("/api/install-url", {"url": "%s/%s" % (self.base, self.name), "title": ""})
        self.appid = r.get("msg", "").replace("installed ", "").strip()
        apptest.LAST_APPID[0] = self.appid
        self.r.update(appid=self.appid, install_s=round(time.time() - t0, 1))
        c = conf(self.appid)
        self.r.update(package=c.get("PKG"), leanback=c.get("LEANBACK") == "1")
        if not c.get("DPAD"):       # explore drives with the remote's arrows: focus navigation for every app
            sh("echo DPAD=keys >> %s/%s/apk.conf" % (APPS, self.appid))

    def launch(self, wait_s):
        twin.close(self.appid)
        time.sleep(2)
        self.atld0 = lines(ATLD_LOG)
        t1 = time.time()
        twin.launch(self.appid)
        while time.time() - t1 < wait_s:
            if visible_window(self.appid):
                return None, round(time.time() - t1, 1)
            rc = exited(self.appid, new_lines(ATLD_LOG, self.atld0))
            if rc is not None:
                return rc, round(time.time() - t1, 1)
            time.sleep(2)
        return "timeout", round(time.time() - t1, 1)

    def settle(self, max_s):
        f0 = grab()
        t = time.time()
        while time.time() - t < max_s:
            time.sleep(4)
            f1 = grab()
            if f0.dist(f1) < SAME:
                return f1
            f0 = f1
        return f0

    def playback(self):
        """confirm motion with no key over 2 more checks, then hold 60 s"""
        for _ in range(2):
            time.sleep(1.0)
            a = grab()
            time.sleep(1.0)
            if a.dist(grab()) <= MOVING:
                return False
        self.r["playback_seen"] = True
        self.r["playback_step"] = self.step
        for _ in range(12):
            f, *_rest, alive = self.act("WAIT4", settle=0)
            self.rows[-1][2] = "(playback)"
            if not alive:
                break
        return True

    def run(self):
        self.t0 = time.time()
        r = self.r
        try:
            self.install()
        except twin.TwinError as e:
            r.update(category="install-failed", detail=str(e)[-400:])
            return
        ook0 = oom_kills()
        rc, r["launch_s"] = self.launch(240)
        if rc is not None:
            self.prev = grab()
            self.save(self.prev, "events", "0000-launch-after.jpg", full=True)
            if rc == "timeout":
                r["category"] = "no-window"
            else:
                r["exit_rc"] = rc
                r["category"] = "oom-killed" if rc == -9 and oom_kills() > ook0 else "crashed"
                r["failed_step"] = 0
            return
        f = self.settle(40)
        rc, alive = self.alive()
        if not alive:                   # the window came, then the app died while it settled
            self.prev = f
            self.save(f, "events", "0000-launch-after.jpg", full=True)
            r["exit_rc"] = rc
            r["category"] = "crashed" if rc not in (None, 0) else "no-window"
            r["failed_step"] = 0
            r["died_after_window"] = True
            return
        self.prev = f
        self.step = 0
        self.cur, _ = self.identify(f)
        self.save(f, "steps", "0000.jpg")
        self.rows.append([0, round(time.time() - self.t0), "(launch)", self.cur, "new", "", "", ""])
        self.todo[self.cur] = ["OK"] + ARROWS + ["MENU"]
        self.opened[self.cur] = False
        r["first_screen_blank"] = f.blank
        for k in self.keys:             # tester's key script first
            if time.time() > self.deadline:
                break
            f, new, moving, rc, alive = self.act(k, settle=1.5)
            if new:
                self.todo[self.cur] = ["OK"] + ARROWS
                self.opened[self.cur] = k == "OK"
            if not alive:
                break
        idle, stalls = 0, 0
        while time.time() < self.deadline - 60 and idle < IDLE_STOP:
            sid = self.cur
            todo = self.todo.get(sid) or []
            if POINTER and not todo and sid not in self.fruitful and sid not in self.clicked:
                self.clicked.add(sid)
                todo = self.todo[sid] = list(GRID)
            if todo:
                key = todo.pop(0)
            elif self.opened.get(sid):
                key = "BACK"
            else:
                key = self.rng.choice(ARROWS)
            try:
                f, new, moving, rc, alive = self.act(key)
                stalls = 0
            except twin.TwinError as e:
                stalls += 1
                self.event("stall", self.prev, None, str(e)[-200:])
                self.rows.append([self.step, round(time.time() - self.t0), key, "-", "stall", "", "stall", str(e)[-200:]])
                if stalls >= 3 or not twin.wait_ready(120):
                    r["stalled_at"] = self.step
                    break
                continue
            if not alive:
                r["relaunches"] += 1
                if r["relaunches"] > 3:
                    r["stop"] = "relaunch-limit"
                    break
                rc2, _ = self.launch(120)
                if rc2 is not None:
                    self.event("relaunch-failed", None, grab(), str(rc2))
                    break
                f = self.settle(20)
                if not self.alive()[1]:
                    self.event("died-after-relaunch", None, f)
                    r["stop"] = "died-after-relaunch"
                    break
                self.prev = f
                self.cur, new = self.identify(f)
                if new:
                    self.todo[self.cur] = ["OK"] + ARROWS
                idle += 1
                continue
            if new:
                idle = 0
                self.fruitful.add(sid)
                opener = key == "OK" or key.startswith("CLICK")
                self.todo[self.cur] = ["OK"] + ARROWS + (["MENU"] if opener else [])
                self.opened[self.cur] = opener or (self.opened.get(sid, False) and key in ARROWS)
                if moving and not r["playback_seen"]:
                    self.playback()
            else:
                idle += 1
        r.setdefault("stop", "idle" if idle >= IDLE_STOP else ("stall" if r.get("stalled_at") else "time"))
        self.finish()

    def finish(self):
        r = self.r
        rc, alive = self.alive()
        if not alive:
            return
        self.sample_mem()
        pid0 = [w["pid"] for w in visible_window(self.appid)]
        last = self.prev
        f, new, moving, rc, alive = self.act("HOME", settle=3, count=False)
        r["running_after_home"] = self.appid in twin.running()
        twin.launch(self.appid)
        time.sleep(6)
        f, new, moving, rc, alive = self.act("(look)", settle=0)
        self.rows[-1][2] = "(relaunch)"
        pid1 = [w["pid"] for w in visible_window(self.appid)]
        r["resumed"] = bool(alive and pid0 and pid0 == pid1 and last is not None and last.dist(f) < 0.05)
        r["resume_same_process"] = bool(pid0 and pid0 == pid1)
        for i in range(3):
            f, new, moving, rc, alive = self.act("BACK", settle=2)
            if not alive:
                r["exit_on_back"] = i + 1
                break

    def report(self):
        r = self.r
        r["steps"] = self.step
        r["screens"] = len(self.screens)
        r["minutes"] = round((time.time() - self.t0) / 60, 1)
        atxt = new_lines(ATLD_LOG, max(0, self.atld0 - 400)) if self.appid else ""
        if self.appid:
            twin.close(self.appid)
            time.sleep(4)
            atxt = new_lines(ATLD_LOG, max(0, self.atld0 - 400))
            peak = re.findall(r"peak_rss=(\d+)MB", "\n".join(l for l in atxt.splitlines() if self.appid in l))
            r["peak_rss_mb"] = max([int(p) for p in peak] + [self.peak_kb // 1024]) or None
            rl = [l for l in atxt.splitlines() if re.search(r"runtime.*\b(32|64)-bit", l)]
            r["runtime_line"] = rl[-1][-200:] if rl else ""
            applog = sh("cat %s/%s/app.log 2>/dev/null | tail -c 400000" % (APPS, self.appid), timeout=60)["stdout"]
            open(os.path.join(self.dir, "app.log"), "w").write(applog)
            r["signature"], r["evidence"] = signature(applog)
            L = applog.replace("\x00", "").splitlines()
            errs = []
            for pat in (r"(java|android|kotlin)\.[\w.]+(Exception|Error)\b|FATAL EXCEPTION", r"SIGSEGV|SIGABRT|SIGBUS|Fatal signal|Segmentation fault|backtrace"):
                i = next((i for i, l in enumerate(L) if re.search(pat, l)), None)
                if i is not None:
                    errs += ["== line %d" % (i + 1)] + L[i:i + 12]
            open(os.path.join(self.dir, "errors.txt"), "w").write("\n".join(errs) + "\n")
            r["first_java_exception"] = next((l[:240] for l in L if re.search(r"(java|android|kotlin)\.[\w.]+(Exception|Error)\b", l)), "")
            r["first_native_crash"] = next((l[:240] for l in L if re.search(r"SIGSEGV|SIGABRT|SIGBUS|Fatal signal|Segmentation fault", l)), "")
        open(os.path.join(self.dir, "atld.log"), "w").write("\n".join(atxt.splitlines()[-400:]) + "\n")
        if "category" not in r:
            if r.get("crashes"):
                r["category"] = "died-on-input"
            elif self.screens and all(float(s.std()) / 255.0 < 0.012 for _, s, _ in self.screens):   # in-app screens all near-uniform
                r["category"] = "blank-window"
            elif r["screens"] <= 1:
                r["category"] = "no-input"
            else:
                r["category"] = "works"
        with open(os.path.join(self.dir, "steps.tsv"), "w") as fh:
            fh.write("step\tt_s\tkey\tscreen\tnew_seen\tmotion\tevents\terrors\n")
            for row in self.rows:
                fh.write("\t".join(str(x).replace("\t", " ") for x in row) + "\n")
        json.dump(r, open(os.path.join(self.dir, "result.json"), "w"), indent=1)
        open(os.path.join(self.dir, "DONE"), "w").write("ok\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apks", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--keys", default="", help="key script run after the first screen: KEY, KEY*n, WAITn (s), CLICKx_y, comma separated")
    ap.add_argument("--pointer", action="store_true", help="opt-in: 3x3 pointer clicks on screens where no key gave a new screen")
    a = ap.parse_args()
    global POINTER
    POINTER = a.pointer
    keys = parse_keys(a.keys)
    os.makedirs(a.out, exist_ok=True)
    twin.wait_ready()
    ev = atl.ensure_installed(log=lambda m: None)
    print("ATL ready", flush=True)
    srv, base = apptest.serve(a.apks)
    try:
        for apk in sorted(glob.glob(os.path.join(a.apks, "*.apk"))):
            app = App(apk, base, a.out, a.minutes, keys)
            apptest.LAST_APPID[0] = ""
            try:
                from . import scenario
                scenario.reset_state(lambda m: None)
            except Exception:  # noqa: BLE001
                pass
            try:
                app.run()
            except Exception as e:  # noqa: BLE001  one broken app must not stop the batch
                app.r["category"] = app.r.get("category") or "harness-error"
                app.r["detail"] = "%s %s" % (e, traceback.format_exc()[-600:])
            try:
                app.report()
            except Exception as e:  # noqa: BLE001
                app.r.setdefault("category", "harness-error")
                app.r["report_error"] = traceback.format_exc()[-600:]
                json.dump(app.r, open(os.path.join(app.dir, "result.json"), "w"), indent=1)
                open(os.path.join(app.dir, "DONE"), "w").write("ok\n")
            print("app done: %d steps, %d screens" % (app.step, len(app.screens)), flush=True)
            try:
                if app.appid:
                    twin.close(app.appid)
                apptest.cleanup(app.appid)
            except Exception:  # noqa: BLE001
                pass
    finally:
        srv.shutdown()
    _ = ev


if __name__ == "__main__":
    main()
