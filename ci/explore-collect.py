#!/usr/bin/env python3
"""explore-collect.py <report name> <n apps> <twin...>   (root on the runner, GH_TOKEN = DATA_TOKEN, optional RERUN_ID)

Picks up each app folder that harness.explore finished (DONE) in a twin's harness container, adds OCR text, needs-login and
contact sheets, packs it and uploads it to the private sarahubprivate/twinci-data:
  tarball  release shots-625,             asset <number>-<package>.tar.gz         (re-run: <number>-<package>.rerun-<RERUN_ID>.tar.gz)
  sheets   release sheets-625-<1st digit>, asset <number>-sheet<k>.jpg (k = 1..6)  (re-run: <number>-rerun-<RERUN_ID>-sheet<k>.jpg)
Public stdout: counts only. Details go to stderr (a private file)."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFont

NAME, TOTAL, TWINS = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
X = "/opt/twinci/explore"
APPS = os.path.join(X, "apps")
REPO = "sarahubprivate/twinci-data"
RERUN = os.environ.get("RERUN_ID", "")
MAX_SHEET_ASSETS = 6
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
LOGIN_RE = re.compile(r"sign[ -]?in|log[ -]?in|sign[ -]?up|create (an )?account|e-?mail|password|activation code|activate|"
                      r"enter (the |this )?code|link (your|this)|pair (this|your)|free trial|subscribe now|payment|credit card", re.I)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def font(n):
    try:
        return ImageFont.truetype(FONT, n)
    except OSError:
        return ImageFont.load_default()


def sheets(d, r):
    """3x3 grids (1920x1080) of the distinct screens in visit order, caption '#step KEY', red frame where an error happened"""
    rows = {}
    for l in open(os.path.join(d, "steps.tsv")).read().splitlines()[1:]:
        c = l.split("\t")
        if len(c) >= 7:
            rows[int(c[0])] = c
    bad_screens = {c[3] for c in rows.values() if c[6].strip()}
    tiles = []
    for f in sorted(glob.glob(os.path.join(d, "screens", "*.jpg"))):
        st = int(os.path.basename(f)[:4])
        c = rows.get(st, [st, 0, "?", "?", "", "", "", ""])
        tiles.append((f, st, c[2], c[3] in bad_screens, ""))
    for f in sorted(glob.glob(os.path.join(d, "events", "*-after.jpg"))):
        kind = os.path.basename(f)[5:-10]
        if kind in ("crash", "black", "launch", "relaunch-failed", "died-after-relaunch"):
            st = int(os.path.basename(f)[:4])
            tiles.append((f, st, rows[st][2] if st in rows else "", True, kind.upper()))
    tiles.sort(key=lambda t: t[1])
    out, F, Fs = [], font(30), font(40)
    for k in range(0, len(tiles), 9):
        sheet = Image.new("RGB", (1920, 1080), (40, 40, 40))
        dr = ImageDraw.Draw(sheet)
        for j, (f, st, key, bad, tag) in enumerate(tiles[k:k + 9]):
            x, y = (j % 3) * 640, (j // 3) * 360
            sheet.paste(Image.open(f).convert("RGB").resize((640, 360), Image.BILINEAR), (x, y))
            dr.rectangle([x, y + 314, x + 639, y + 359], fill=(0, 0, 0))
            dr.text((x + 10, y + 318), "#%d %s %s" % (st, key, tag), fill=(255, 255, 255), font=F)
            if bad:
                dr.rectangle([x + 2, y + 2, x + 637, y + 357], outline=(255, 0, 0), width=8)
                dr.rectangle([x + 560, y + 8, x + 630, y + 58], fill=(220, 0, 0))
                dr.text((x + 568, y + 10), "ERR", fill=(255, 255, 255), font=font(28))
            dr.rectangle([x, y, x + 639, y + 359], outline=(90, 90, 90), width=1)
        p = os.path.join(d, "sheets", "sheet%d.jpg" % (len(out) + 1))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        sheet.save(p, "JPEG", quality=85)
        out.append(p)
    _ = Fs
    return out


def ocr(d, r):
    texts, login = [], 0
    for f in sorted(glob.glob(os.path.join(d, "screens", "*.jpg")))[:40]:
        p = run(["nice", "-n", "19", "tesseract", f, "stdout", "--psm", "3"], env=dict(os.environ, OMP_THREAD_LIMIT="1"), timeout=120)
        t = " ".join(p.stdout.split())
        texts.append("== %s\n%s" % (os.path.basename(f), t))
        if LOGIN_RE.search(t):
            login += 1
    open(os.path.join(d, "ocr.txt"), "w").write("\n".join(texts) + "\n")
    r["login_screens"] = login
    n = r.get("screens", 0)
    r["needs_login"] = bool(login >= 1 and n - login <= 2 and not r.get("playback_seen"))


def upload(rel, path, asset):
    for t in range(4):
        tmp = os.path.join(os.path.dirname(path), asset)
        if tmp != path:
            shutil.copyfile(path, tmp)
        p = run(["gh", "release", "upload", rel, tmp, "-R", REPO, "--clobber"], timeout=900)
        if p.returncode == 0:
            return True
        log("upload %s try %d: %s" % (asset, t, p.stderr[-300:]))
        time.sleep(15 * (t + 1))
    return False


def process(twin, stem, partial=False):
    d = os.path.join(APPS, stem)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(APPS, exist_ok=True)
    run(["docker", "cp", "%s-harness:/data/explore/%s/%s" % (twin, NAME, stem), APPS])
    rp = os.path.join(d, "result.json")
    r = json.load(open(rp)) if os.path.exists(rp) else {"apk": stem + ".apk", "category": "harness-error", "detail": "no result (twin stopped)"}
    if partial:
        r["incomplete"] = True
    r["twin"] = twin
    try:
        ocr(d, r)
    except Exception as e:  # noqa: BLE001
        log("ocr %s: %s" % (stem, e))
    try:
        sh = sheets(d, r) if os.path.exists(os.path.join(d, "steps.tsv")) else []
    except Exception as e:  # noqa: BLE001
        log("sheets %s: %s" % (stem, e)); sh = []
    r["sheets"] = len(sh)
    json.dump(r, open(rp, "w"), indent=1)
    num = stem.split("-")[0]
    tb = os.path.join(X, stem + (".rerun-%s" % RERUN if RERUN else "") + ".tar.gz")
    run(["tar", "-C", APPS, "-czf", tb, stem])
    ok = upload("shots-625", tb, os.path.basename(tb))
    rel = "sheets-625-%s" % num[0]
    n_ok = 0
    for k, s in enumerate(sh[:MAX_SHEET_ASSETS], 1):
        a = "%s-rerun-%s-sheet%d.jpg" % (num, RERUN, k) if RERUN else "%s-sheet%d.jpg" % (num, k)
        n_ok += upload(rel, s, a)
    r["uploaded"] = ok
    log("%s %s screens=%s steps=%s playback=%s login=%s tar=%dMB sheets=%d/%d" % (
        stem, r.get("category"), r.get("screens"), r.get("steps"), r.get("playback_seen"), r.get("needs_login"),
        os.path.getsize(tb) >> 20, n_ok, min(len(sh), MAX_SHEET_ASSETS)))
    os.remove(tb)
    run(["docker", "exec", "%s-harness" % twin, "rm", "-rf", "/data/explore/%s/%s" % (NAME, stem)])
    return r, ok, n_ok, os.path.getsize(os.path.join(d, "result.json"))


def main():
    done, results = set(), []
    final = False
    while True:
        last = os.path.exists(os.path.join(X, "ALL-DONE"))
        for t in TWINS:
            p = run(["docker", "exec", "%s-harness" % t, "sh", "-c", "ls -d /data/explore/%s/*/ 2>/dev/null; ls /data/explore/%s/*/DONE 2>/dev/null" % (NAME, NAME)])
            dirs = [l.rstrip("/").split("/")[-1] for l in p.stdout.split() if l.endswith("/")]
            fin = {l.split("/")[-2] for l in p.stdout.split() if l.endswith("/DONE")}
            for s in dirs:
                if s in done or (s not in fin and not final):
                    continue
                done.add(s)
                r, ok, n_ok, _ = process(t, s, partial=s not in fin)
                results.append(r)
                print("uploaded app %d/%d: tarball %s, %d sheets" % (len(results), TOTAL, "ok" if ok else "FAILED", n_ok), flush=True)
        if final:
            break
        if last:
            final = True
            continue
        time.sleep(15)
    with open(os.path.join(X, "SUMMARY.tsv"), "w") as f:
        keys = ["apk", "category", "playback_seen", "screens", "steps", "needs_login", "failed_step", "crashes", "resumed",
                "peak_rss_mb", "runtime_line", "signature", "uploaded"]
        f.write("\t".join(keys) + "\n")
        for r in results:
            f.write("\t".join(str(r.get(k, "")).replace("\t", " ") for k in keys) + "\n")
    n32 = sum(1 for r in results if "32-bit" in (r.get("runtime_line") or ""))
    up = sum(1 for r in results if r.get("uploaded"))
    print("apps uploaded: %d/%d; runtime 32-bit logged for %d apps" % (up, TOTAL, n32), flush=True)
    sys.exit(0 if up == TOTAL else 1)


if __name__ == "__main__":
    main()
