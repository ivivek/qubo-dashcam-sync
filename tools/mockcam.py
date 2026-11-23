#!/usr/bin/env python3
"""Mock dashcam file server for tests: synthetic files only, no real data.

    python3 tools/mockcam.py 18091           # then use 127.0.0.1:18091 as the camera address

Serves the camera's "Index of" HTML format with GET/HEAD/Range like the real
camera on :8080. Contents (relative to start time):
  yesterday: 20 one-minute clips starting 10 min before now-24h
  today:     the last 30 min of clips; the newest is "still recording" (no moov)
  one dummy .gps file
"""
import http.server
import struct
import sys
import time

NOW = time.time()
DEV = "/HERO_IOTG/AA_BB_CC_DD_EE_FF/"
FILES = {}          # path -> bytes


def mp4(n, complete=True):
    body = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
    body += struct.pack(">I4s", 8 + n, b"mdat") + b"\0" * n
    if complete:
        body += struct.pack(">I4s", 16, b"moov") + b"\0" * 8
    return body


def add_clips(start, count, recording_last=False):
    for k in range(count):
        t = start + 60 * k
        lt = time.localtime(t)
        name = f"VIDEO_{time.strftime('%d%m%Y%H%M%S', lt)}_{int(t * 1000)}.mp4"
        last = recording_last and k == count - 1
        FILES[f"{DEV}Recordings/{time.strftime('%d-%b-%Y', lt)}/{name}"] = mp4(400_000, not last)


add_clips(NOW - 86400 - 600, 20)
add_clips(NOW - 1800, 30, recording_last=True)
FILES[f"{DEV}Recordings/{time.strftime('%d-%b-%Y')}/GPS/G_{int(NOW * 1000)}.gps"] = b"T-0\n"


def listing(path):
    kids = set()
    for p in FILES:
        if p.startswith(path) and p != path:
            rest = p[len(path):]
            kids.add(path + rest.split("/")[0] + ("/" if "/" in rest else ""))
    if not kids:
        return None
    rows = [f'<tr><td><a href="{path}..">..</a></td><td>&nbsp;-</td><td>&nbsp;-</td></tr>']
    rows += [f'<tr><td><a href="{k}">{k.rstrip("/").rsplit("/", 1)[-1]}</a></td>'
             f'<td>&nbsp;01-Jan-2026 10:00</td><td>&nbsp;&nbsp;1M</td></tr>' for k in sorted(kids)]
    return ("<html><body><table>" + "".join(rows) + "</table></body></html>").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write(f"{self.command} {self.path}\n")

    def _send(self, head):
        if self.path.endswith("/"):
            body = listing(self.path)
            if body is None:
                return self.send_error(404)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return None if head else self.wfile.write(body)
        if self.path not in FILES:
            return self.send_error(404)
        data = FILES[self.path]
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return self.send_error(416)
            self.send_response(206)
            data = data[start:]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def do_GET(self):
        self._send(False)

    def do_HEAD(self):
        self._send(True)


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
