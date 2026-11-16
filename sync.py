#!/usr/bin/env python3
"""Download dashcam footage + GPS over Wi-Fi and build a GPX track.

Run while connected to the dashcam Wi-Fi:
    python3 sync.py [camera-ip]          # GPS + small state files only (fast)
    python3 sync.py --images             # + timelapse/event JPEGs
    python3 sync.py --video              # + MP4 clips (slow: ~4.4 MB per minute)
    python3 sync.py --gpx-only           # offline: just rebuild the GPX files

Mirrors http://<ip>:8080/HERO_IOTG/ into ./footage/, skipping files already
complete. Partial downloads resume. MP4s without a 'moov' index (the clip
still being recorded) are kept as .part and finished on the next run.
Prints only file names, sizes and counts - never GPS values.
"""
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
IP = ARGS[0] if ARGS else "192.168.1.1"
BASE = f"http://{IP}" if ":" in IP else f"http://{IP}:8080"
ROOT = "/HERO_IOTG/"
HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.environ.get("DASHCAM_DEST") or os.path.join(HERE, "footage")
ROW = re.compile(r'<a href="([^"?]+)">[^<]*</a></td><td>(?:&nbsp;)*([^<]*)</td>'
                 r'<td>(?:&nbsp;)*([^<]*)</td>', re.I)
VIDEO_NAME = re.compile(r"VIDEO_(\d{14})_\d+\.mp4$", re.I)
WANT_VIDEO = "--video" in sys.argv
WANT_IMAGES = "--images" in sys.argv
WANT_STATE = "--state" in sys.argv            # camera's own .bin/.txt index files (not needed)
GPS_NAME = re.compile(r"G_(\d+)\.gps$", re.I)
STATE_FILE = os.path.join(DEST, ".sync_state.json")
STATE = {"closed": [], "done_dirs": []}         # loaded in sync()
TTY = sys.stdout.isatty()
sys.stdout.reconfigure(line_buffering=True)   # show progress live, even via `| tee`


def human(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB" if n >= 1e3 else f"{n} B"


class Progress:
    """In-place progress line on a terminal; periodic plain lines when piped."""

    def __init__(self, label, total):
        self.label, self.total, self.t0, self.last = label, total, time.time(), 0.0

    def update(self, done, final=False):
        now = time.time()
        if not final and now - self.last < (0.3 if TTY else 5):
            return
        self.last = now
        rate = done / max(now - self.t0, 0.01)
        pct = f"{100 * done / self.total:3.0f}%" if self.total > 0 else "  ?%"
        eta = f" eta {int((self.total - done) / rate)}s" if self.total > done and rate > 0 else ""
        line = f"    {pct} {human(done)}/{human(max(self.total, done))} {rate / 1e6:.2f} MB/s{eta}"
        if TTY:
            print(f"\r{line:<70}", end="" if not final else "\n", flush=True)
        elif not final:
            print(line)


def get(url, headers=None, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}),
                                  timeout=timeout)


MEDIA_DIR = re.compile(r"/(Timelapse|Images)/")
REC_DATE_DIR = re.compile(r"/Recordings/[^/]+/$")


def crawl(path, out):
    # Don't even list folders we won't download from: the timelapse and
    # recording folders hold hundreds of entries and are slow on a weak link.
    if not WANT_IMAGES and MEDIA_DIR.search(path):
        return
    if not WANT_VIDEO and REC_DATE_DIR.search(path):
        return crawl(path + "GPS/", out)           # only the GPS subfolder of each day
    if path in STATE["done_dirs"]:                  # older day, every GPS file already final
        return
    print(f"  listing {path[len(ROOT):] or '/'}")
    t0 = time.time()
    try:
        with get(BASE + urllib.parse.quote(path)) as r:
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:                            # e.g. a day without a GPS folder
            print("    (not found, skipped)")
            return
        raise
    if time.time() - t0 > 3:
        print(f"    slow listing: {time.time() - t0:.1f} s, {len(html) // 1024} kB")
    for href, _mod, _size in ROW.findall(html):
        if href.endswith("/.."):
            continue
        full = href if href.startswith("/") else path + href
        if not full.startswith(path) or full == path:
            continue
        if full.endswith("/"):
            crawl(full, out)
        else:
            out.append(full)


def mp4_complete(path):
    """True if the file's top-level boxes are intact and include moov."""
    size, pos, seen = os.path.getsize(path), 0, set()
    with open(path, "rb") as f:
        while pos + 8 <= size:
            f.seek(pos)
            n, typ = struct.unpack(">I4s", f.read(8))
            if n == 1:
                n = struct.unpack(">Q", f.read(8))[0]
            elif n == 0:
                n = size - pos
            if n < 8 or pos + n > size:
                return False
            seen.add(typ)
            pos += n
    return b"moov" in seen and b"mdat" in seen


def remote_size(remote):
    req = urllib.request.Request(BASE + urllib.parse.quote(remote), method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return int(r.headers.get("Content-Length", -1))
    except Exception:
        return -1


def download(remote, local):
    part = local + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    hdr = {"Range": f"bytes={have}-"} if have else {}
    t0 = time.time()
    try:
        r = get(BASE + urllib.parse.quote(remote), hdr, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:                      # nothing new past what we have
            return have, 0.0
        raise
    with r:
        if have and r.status != 206:           # server ignored Range: restart
            have = 0
        total = have + int(r.headers.get("Content-Length", -1))
        bar = Progress(os.path.basename(local), total) if total > 512 * 1024 else None
        done = have
        with open(part, "ab" if have else "wb") as f:
            while chunk := r.read(64 * 1024):
                f.write(chunk)
                done += len(chunk)
                if bar:
                    bar.update(done)
        if bar:
            bar.update(done, final=True)
    return os.path.getsize(part), time.time() - t0


def load_state():
    try:
        with open(STATE_FILE) as f:
            STATE.update(json.load(f))
    except (OSError, ValueError):
        pass


def save_state():
    os.makedirs(DEST, exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(STATE, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def gps_epoch(f):
    m = GPS_NAME.search(f)
    return int(m.group(1)) if m else None


def sync():
    load_state()
    closed = set(STATE["closed"])
    print(f"Listing {BASE}{ROOT} ...")
    files = []
    crawl(ROOT, files)
    if STATE["done_dirs"]:
        print(f"  ({len(STATE['done_dirs'])} finished GPS day folders not re-listed)")
    gps_files = [f for f in files if gps_epoch(f) is not None]
    newest_gps = max((gps_epoch(f) for f in gps_files), default=0)
    videos = sorted((f for f in files if VIDEO_NAME.search(f)),
                    key=lambda f: VIDEO_NAME.search(f).group(1)[4:8] +
                    VIDEO_NAME.search(f).group(1)[2:4] + VIDEO_NAME.search(f).group(1)[0:2] +
                    VIDEO_NAME.search(f).group(1)[8:])
    recording = videos[-1] if videos else None   # newest clip is still being written
    images = [f for f in files if f.lower().endswith((".jpeg", ".jpg"))]

    def wanted(f):
        if f == recording:
            return False
        if f in videos or f.lower().endswith(".mp4"):
            return WANT_VIDEO
        if f in images:
            return WANT_IMAGES
        if gps_epoch(f) is not None:
            return True
        return WANT_STATE                         # .txt/.bin index files

    def is_new(f):
        local = os.path.join(DEST, f[len(ROOT):])
        return not os.path.exists(local)

    todo = [f for f in files if wanted(f)]
    media_new = sum(1 for f in todo if f.lower().endswith((".mp4", ".jpeg", ".jpg")) and is_new(f))
    print(f"\n{len(files)} files on camera: {len(videos)} videos, {len(images)} images, "
          f"{len(files) - len(videos) - len(images)} GPS/state files")
    print(f"mode: GPS{' + state' if WANT_STATE else ''}{' + images' if WANT_IMAGES else ''}"
          f"{' + video' if WANT_VIDEO else ''}"
          f"  ({'' if WANT_VIDEO else 'videos skipped, use --video; '}"
          f"{'' if WANT_IMAGES else 'images skipped, use --images; '}"
          f"in-progress clip always skipped)")
    print(f"to check: {len(todo)} files ({media_new} new media files to download)\n")

    stats = {"new": 0, "updated": 0, "skipped": 0, "incomplete": 0, "failed": 0, "bytes": 0}
    width = len(str(len(todo)))
    try:
        for i, remote in enumerate(todo, 1):
            sync_one(i, remote, todo, width, stats, closed, newest_gps)
    finally:
        finish_state(files, gps_files, closed)
    if TTY:
        print()
    stats["bytes"] = human(stats["bytes"])
    print("\nsync:", stats)


def finish_state(files, gps_files, closed):
    """Mark day folders whose GPS files are all final, then persist."""
    present = set(files)
    by_dir = {}
    for f in gps_files:
        by_dir.setdefault(f.rsplit("/", 1)[0] + "/", []).append(f)
    newest_dir = max(by_dir, key=lambda d: max(gps_epoch(f) for f in by_dir[d])) if by_dir else None
    for d, fs in by_dir.items():
        if d != newest_dir and all(f in closed for f in fs) and d not in STATE["done_dirs"]:
            STATE["done_dirs"].append(d)
    done = tuple(STATE["done_dirs"])
    STATE["closed"] = sorted(f for f in closed if f in present or f.startswith(done))
    save_state()


def sync_one(i, remote, todo, width, stats, closed, newest_gps):
    # A .gps file is final once a newer one exists AND we checked it after that.
    ep = gps_epoch(remote)
    older = ep is not None and ep < newest_gps
    local = os.path.join(DEST, remote[len(ROOT):])
    tag = f"[{i:>{width}}/{len(todo)}] {os.path.basename(local)}"
    if remote in closed and os.path.exists(local):
        stats["skipped"] += 1
        return
    if os.path.exists(local):
        # media files never change once closed; small state/GPS files can grow
        if local.lower().endswith((".mp4", ".jpeg", ".jpg")) or \
                remote_size(remote) == os.path.getsize(local):
            stats["skipped"] += 1
            if older:
                closed.add(remote)
            if TTY:
                print(f"\r{tag}  unchanged{' ' * 20}", end="", flush=True)
            return
        os.remove(local)                     # small file: re-fetch whole (may be rewritten, not just appended)
        stats["updated"] += 1
        print(f"\r{tag}  changed on camera, re-downloading")
    else:
        print(f"\r{tag}  downloading{' ' * 20}")
    os.makedirs(os.path.dirname(local), exist_ok=True)
    try:
        size, dt = download(remote, local)
    except Exception as e:
        print(f"    FAIL: {e}")
        stats["failed"] += 1
        return
    stats["bytes"] += size
    part = local + ".part"
    if local.lower().endswith(".mp4") and not mp4_complete(part):
        print("    incomplete (no moov yet), will resume next run")
        stats["incomplete"] += 1
        return
    os.replace(part, local)
    if older:
        closed.add(remote)
    stats["new"] += 1
    print(f"    ok {human(size)} in {dt:.1f}s")


# ---------------------------------------------------------------- GPS -> GPX
GPS_FIELD = re.compile(r"^(T|LT|LG|Alt)-?([NSEW]?)(-?[\d.]+)$")


def to_degrees(v, is_lat):
    """Accept decimal degrees or NMEA-style dddmm.mmmm (auto-detected)."""
    if abs(v) > (90 if is_lat else 180):
        d = int(abs(v) // 100)
        return (d + (abs(v) - d * 100) / 60) * (1 if v >= 0 else -1), "ddmm"
    return v, "deg"


def build_gpx():
    gps_files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(DEST)
                       for f in fs if f.lower().endswith(".gps"))
    if not gps_files:
        print("gpx: no .gps files yet")
        return
    by_day = {}
    fmts, bad = set(), 0
    for path in gps_files:
        day = os.path.basename(os.path.dirname(os.path.dirname(path)))
        for line in open(path, errors="replace"):
            pt = {}
            for field in line.strip().split("\t"):
                m = GPS_FIELD.match(field.strip())
                if m:
                    key, hemi, num = m.groups()
                    val = float(num) * (-1 if hemi in ("S", "W") else 1)
                    pt[key] = val
            if not {"T", "LT", "LG"} <= pt.keys() or (pt["LT"] == 0 and pt["LG"] == 0):
                bad += 1
                continue
            pt["LT"], f1 = to_degrees(pt["LT"], True)
            pt["LG"], f2 = to_degrees(pt["LG"], False)
            fmts |= {f1, f2}
            by_day.setdefault(day, []).append(pt)

    gpx_dir = os.path.join(DEST, "gpx")
    os.makedirs(gpx_dir, exist_ok=True)
    for day, pts in sorted(by_day.items()):
        pts.sort(key=lambda p: p["T"])
        out = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<gpx version="1.1" creator="dashcam sync.py" xmlns="http://www.topografix.com/GPX/1/1">',
            f"<trk><name>{day}</name><trkseg>",
        ]
        for p in pts:
            t = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(p["T"] / 1000))
            ele = f"<ele>{p['Alt']}</ele>" if "Alt" in p else ""
            out.append(f'<trkpt lat="{p["LT"]:.7f}" lon="{p["LG"]:.7f}">{ele}<time>{t}</time></trkpt>')
        out.append("</trkseg></trk></gpx>")
        with open(os.path.join(gpx_dir, f"{day}.gpx"), "w") as f:
            f.write("\n".join(out) + "\n")
        print(f"gpx: {day}.gpx  {len(pts)} points")
    print(f"gpx: {len(gps_files)} .gps files, {bad} unparsed/empty lines, coord format {sorted(fmts)}")


if __name__ == "__main__":
    if "--gpx-only" not in sys.argv:
        sync()
    build_gpx()
