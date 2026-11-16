#!/usr/bin/env python3
"""Map the dashcam SD card and grab samples for offline analysis.

Run while connected to the dashcam Wi-Fi:  python3 explore.py [camera-ip]
Everything is written to ./explore-<timestamp>/ (no internet needed).
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

IP = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.1"
BASE = f"http://{IP}:8080"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   time.strftime("explore-%Y%m%d-%H%M%S"))
VIDEO_EXT = {".mp4", ".mov", ".avi", ".ts", ".mkv"}
MAX_SIDECAR = 5 * 1024 * 1024        # download non-video files up to 5 MB
os.makedirs(os.path.join(OUT, "samples"), exist_ok=True)
log_f = open(os.path.join(OUT, "explore.log"), "w")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    log_f.write(s + "\n")
    log_f.flush()


def save(name, data):
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


def http(url, method="GET", headers=None, body=None, timeout=10):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:
        return None, {}, str(e).encode()


# ---------------------------------------------------------------- 1. crawl
SIZE_UNITS = {"": 1, "B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20,
              "MB": 1 << 20, "G": 1 << 30, "GB": 1 << 30}
ROW = re.compile(r'<a href="([^"?]+)">[^<]*</a></td><td>(?:&nbsp;)*([^<]*)</td>'
                 r'<td>(?:&nbsp;)*([^<]*)</td>', re.I)


def parse_size(s):
    m = re.match(r"([\d.]+)\s*([KMG]?B?)", s.strip(), re.I)
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2).upper()]) if m else None


def crawl(path, files, depth=0):
    st, _, body = http(BASE + path)
    if st != 200 or depth > 8:
        log(f"  ! {path}: {st}")
        return
    save("listing" + path.replace("/", "_") + ".html", body)
    for href, mod, size in ROW.findall(body.decode("utf-8", "replace")):
        if href.startswith("/..") or href in ("../", "/"):
            continue
        full = href if href.startswith("/") else path + href
        if full.endswith("/"):
            log(f"  [dir] {full}  {mod.strip()}")
            crawl(full, files, depth + 1)
        else:
            files.append((full, mod.strip(), size.strip(), parse_size(size)))


log(f"== Crawling {BASE}/")
files = []
crawl("/", files)
log(f"\n== {len(files)} files")
with open(os.path.join(OUT, "files.tsv"), "w") as f:
    for p, mod, size, _ in files:
        f.write(f"{p}\t{mod}\t{size}\n")
by_ext = {}
for p, *_ in files:
    ext = os.path.splitext(p)[1].lower()
    by_ext[ext] = by_ext.get(ext, 0) + 1
log("by extension:", by_ext)
for p, mod, size, _ in files[:30]:
    log(f"  {p}  {mod}  {size}")

# ---------------------------------------------------------------- 2. WebDAV
log("\n== WebDAV OPTIONS / PROPFIND")
st, hd, _ = http(BASE + "/", method="OPTIONS")
log("OPTIONS", st, {k: v for k, v in hd.items() if k.lower() in ("allow", "dav", "server")})
st, _, body = http(BASE + "/", method="PROPFIND", headers={"Depth": "1"})
log("PROPFIND", st, len(body), "bytes")
save("propfind.xml", body)

# ---------------------------------------------------------------- 3. port 8081
log("\n== Port 8081")
for path in ["/", "/api", "/cgi-bin/", "/status", "/info", "/getdeviceinfo"]:
    st, hd, body = http(f"http://{IP}:8081{path}", timeout=4)
    log(f"GET {path}: {st} {hd.get('Content-Type', '')} {body[:300]!r}")
for payload in [b"", b"{}\n", b'{"cmd":"getinfo"}\n', b"GET / HTTP/1.0\r\n\r\n"]:
    try:
        s = socket.create_connection((IP, 8081), timeout=3)
        if payload:
            s.sendall(payload)
        s.settimeout(3)
        data = s.recv(2048)
        log(f"raw {payload!r} -> {data[:400]!r}")
        s.close()
    except Exception as e:
        log(f"raw {payload!r} -> {e}")

# ---------------------------------------------------------------- 4. RTSP
log("\n== RTSP DESCRIBE")
for path in ["", "live", "stream0", "stream1", "ch0", "live/ch00_0", "h264"]:
    url = f"rtsp://{IP}:554/{path}"
    try:
        s = socket.create_connection((IP, 554), timeout=3)
        s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n".encode())
        s.settimeout(3)
        data = s.recv(4096).decode("utf-8", "replace")
        s.close()
        log(f"--- {url}\n{data.strip()[:600]}")
    except Exception as e:
        log(f"--- {url}: {e}")
log("ffprobe on rtsp://.../live :")
r = subprocess.run(["ffprobe", "-hide_banner", "-rtsp_transport", "tcp", "-i",
                    f"rtsp://{IP}:554/live"], capture_output=True, text=True, timeout=20)
log(r.stderr[-1500:])

# ---------------------------------------------------------------- 5. samples
log("\n== Downloading samples")


def download(path, limit=None):
    dest = os.path.join(OUT, "samples", path.strip("/").replace("/", "__"))
    hdr = {"Range": f"bytes=0-{limit - 1}"} if limit else {}
    t = time.time()
    st, hd, body = http(BASE + urllib.parse.quote(path), headers=hdr, timeout=120)
    with open(dest, "wb") as f:
        f.write(body)
    dt = time.time() - t
    log(f"  {path}: HTTP {st}, {len(body)} B in {dt:.1f}s "
        f"({len(body) / max(dt, 0.01) / 1e6:.2f} MB/s), "
        f"Accept-Ranges={hd.get('Accept-Ranges')} Content-Range={hd.get('Content-Range')}")
    return dest


videos = [f for f in files if os.path.splitext(f[0])[1].lower() in VIDEO_EXT]
others = [f for f in files if f not in videos]

# all small non-video files (GPS logs, thumbnails, config) – up to 40
for p, _, _, sz in others[:40]:
    if sz is None or sz <= MAX_SIDECAR:
        download(p)

# smallest video in full (GPS is often embedded) + head/tail of the newest one
if videos:
    small = min(videos, key=lambda f: f[3] or 1 << 62)
    log(f"smallest video: {small[0]} ({small[2]})")
    if (small[3] or 0) <= 200 * 1024 * 1024:
        vpath = download(small[0])
        for cmd in (["ffprobe", "-hide_banner", "-show_streams", "-show_format", vpath],
                    ["exiftool", "-ee", "-G3", "-a", vpath]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            name = cmd[0] + ".txt"
            save(name, r.stdout + r.stderr)
            log(f"  wrote {name} ({len(r.stdout)} chars)")
    download(videos[-1][0], limit=2 * 1024 * 1024)   # tests Range support + speed

log(f"\n== Done. Results in {OUT}")
