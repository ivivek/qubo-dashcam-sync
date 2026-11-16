#!/usr/bin/env python3
"""Raspberry Pi garage watcher: detect the car's dashcam Wi-Fi, sync, notify on Telegram.

A "session" starts when the dashcam SSID becomes visible (car arriving, or
ignition on when leaving) and ends when the camera disappears (ignition off
+ ~1 min, or car drove away). During a session sync.py runs repeatedly so the
last GPS points written before shutdown are captured. When the session ends a
Telegram summary is sent (queued and retried if the Pi has no internet).

Usage:
    python3 car_watch.py                 # run forever (systemd service)
    python3 car_watch.py --once          # one session now if camera visible, then exit
    python3 car_watch.py --test-telegram # send a test message

Config: dashcam.conf next to this script (see dashcam.conf.example).
Needs NetworkManager (nmcli). Runs as root (nmcli connection control).
"""
import ast
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout.reconfigure(line_buffering=True)


# ------------------------------------------------------------------ config
def load_conf():
    conf = {
        "CAM_SSID": "", "CAM_PASSWORD": "", "CAM_IP": "192.168.1.1",
        "CAM_IFACE": "wlan0",          # use a 2nd USB Wi-Fi adapter (e.g. wlan1) if you have one
        "CAM_STATIC_IP": "192.168.1.200",  # fixed Pi address on the camera network ("" = use DHCP)
        "SYNC_ARGS": "--images",       # "" = GPS only, "--images", "--images --video"
        "DEST": os.path.join(HERE, "footage"),
        "SCAN_INTERVAL": "5",          # s between Wi-Fi scans while idle
        "RESYNC_INTERVAL": "20",       # s between sync passes while camera is reachable
        "SYNC_TIMEOUT": "900",         # s max for one sync pass
        "TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": "",
        "TELEGRAM_SEND_GPX": "0",      # 1 = also send the day's GPX file (location leaves the Pi!)
        "NOTIFY_EMPTY": "0",           # 1 = also send the summary when a session found nothing new
        "NOTIFY_LEVEL": "debug",       # debug = start/detected/connected/transfer/disconnected + summary
                                       # summary = only the end-of-session summary
        "COOLDOWN": "180",             # s to ignore the camera after a session (flapping signal)
        "DEST_OWNER": "",              # e.g. pi:pi - chown synced files (service runs as root)
    }
    path = os.path.join(HERE, "dashcam.conf")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    if not conf["CAM_SSID"]:
        sys.exit("CAM_SSID not set in dashcam.conf")
    return conf


C = load_conf()
STATE_DIR = os.path.join(HERE, "state")
os.makedirs(STATE_DIR, exist_ok=True)
PENDING = os.path.join(STATE_DIR, "pending_telegram.json")
NM_PROFILE = "dashcam"
HOME_FILE = os.path.join(STATE_DIR, "home_wifi.txt")   # home connection to restore after a session


def log(*a):
    print(time.strftime("%H:%M:%S"), *a)


def sh(*cmd, timeout=60):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).strip()


# ------------------------------------------------------------------ Wi-Fi
def camera_visible():
    rc, out = sh("nmcli", "-t", "-f", "SSID", "dev", "wifi", "list",
                 "ifname", C["CAM_IFACE"], "--rescan", "yes", timeout=30)
    ssids = {s.replace("\\:", ":") for s in out.splitlines()}
    return C["CAM_SSID"] in ssids


def active_connection(iface):
    _, out = sh("nmcli", "-t", "-f", "NAME,DEVICE", "con", "show", "--active")
    for line in out.splitlines():
        name, _, dev = line.rpartition(":")
        if dev == iface and name != NM_PROFILE:
            return name.replace("\\:", ":")
    return None


def ensure_profile():
    """NM profile for the camera: never the default route, no DNS, no autoconnect."""
    settings = ["connection.interface-name", C["CAM_IFACE"],
                "802-11-wireless.ssid", C["CAM_SSID"],
                "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", C["CAM_PASSWORD"],
                "ipv4.never-default", "yes", "ipv4.ignore-auto-dns", "yes",
                "ipv6.method", "ignore", "connection.autoconnect", "no"]
    if C["CAM_STATIC_IP"]:        # skip DHCP: faster, and works when the camera's DHCP is flaky
        settings += ["ipv4.method", "manual", "ipv4.addresses", f"{C['CAM_STATIC_IP']}/24",
                     "ipv4.gateway", ""]
    else:
        settings += ["ipv4.method", "auto", "ipv4.addresses", "", "ipv4.dhcp-timeout", "20"]
    rc, _ = sh("nmcli", "-t", "con", "show", NM_PROFILE)
    if rc != 0:
        rc, out = sh("nmcli", "con", "add", "type", "wifi", "con-name", NM_PROFILE,
                     "ifname", C["CAM_IFACE"], "ssid", C["CAM_SSID"])
        if rc != 0:
            raise RuntimeError(f"nmcli con add failed: {out}")
    rc, out = sh("nmcli", "con", "modify", NM_PROFILE, *settings)
    if rc != 0:
        raise RuntimeError(f"nmcli con modify failed: {out}")


def connect_camera():
    ensure_profile()
    for attempt in range(1, 4):
        t0 = time.time()
        rc, out = sh("nmcli", "--wait", "30", "con", "up", NM_PROFILE,
                     "ifname", C["CAM_IFACE"], timeout=40)
        if rc == 0:
            log(f"joined camera Wi-Fi in {time.time() - t0:.1f} s")
            return True
        reason = out.splitlines()[0] if out else f"exit {rc}"
        log(f"connect attempt {attempt} failed after {time.time() - t0:.0f} s: {reason[-200:]}")
        LAST_CONNECT_ERROR[0] = reason[-150:]
        if "could not be found" in out:                # camera gone (ignition off / out of range)
            break
        time.sleep(3)
    return False


LAST_CONNECT_ERROR = [""]


def disconnect_camera(home_conn):
    sh("nmcli", "con", "down", NM_PROFILE)
    if home_conn:                                   # single-adapter setup: go back home
        for attempt in range(1, 6):
            t0 = time.time()
            rc, out = sh("nmcli", "--wait", "30", "con", "up", home_conn, timeout=40)
            if rc == 0:
                log(f"restored home Wi-Fi '{home_conn}' in {time.time() - t0:.1f} s")
                break
            log(f"restore '{home_conn}' attempt {attempt} failed: {out.strip()[-200:]}")
            time.sleep(10)
        else:
            log("could not restore home Wi-Fi - leaving it to NetworkManager autoconnect")
    try:
        os.remove(HOME_FILE)
    except FileNotFoundError:
        pass
    _wake.set()                                     # send queued Telegram messages now


def recover_network():
    """Startup: undo a session that was cut short (crash, power loss, kill -9)."""
    if NM_PROFILE in sh("nmcli", "-t", "-f", "NAME", "con", "show", "--active")[1].splitlines():
        log("camera profile still active from an interrupted session - disconnecting")
        home = open(HOME_FILE).read().strip() if os.path.exists(HOME_FILE) else None
        disconnect_camera(home)


def remember_home(home_conn):
    if home_conn:
        with open(HOME_FILE, "w") as f:
            f.write(home_conn)


def camera_reachable():
    try:
        with urllib.request.urlopen(f"http://{C['CAM_IP']}:8080/", timeout=4):
            return True
    except Exception:
        return False


# ------------------------------------------------------------------ snapshot / stats
GPS_LINE = re.compile(r"^T-?(\d+)")
FIELD = re.compile(r"^(LT|LG|S)-?([NSEW]?)(-?[\d.]+)$")


def read_points():
    """{T: (lat, lon, sats)} from every .gps file. Stays on the Pi; never printed."""
    pts = {}
    for dp, _, fs in os.walk(C["DEST"]):
        for f in fs:
            if not f.lower().endswith(".gps"):
                continue
            for line in open(os.path.join(dp, f), errors="replace"):
                m = GPS_LINE.match(line.strip())
                if not m:
                    continue
                vals = {}
                for fld in line.strip().split("\t"):
                    fm = FIELD.match(fld.strip())
                    if fm:
                        k, hemi, num = fm.groups()
                        vals[k] = float(num) * (-1 if hemi in ("S", "W") else 1)
                pts[int(m.group(1))] = (vals.get("LT"), vals.get("LG"), vals.get("S"))
    return pts


def file_snapshot():
    snap = {}
    for dp, _, fs in os.walk(C["DEST"]):
        for f in fs:
            p = os.path.join(dp, f)
            if not f.endswith(".part"):
                snap[p] = os.path.getsize(p)
    return snap


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def human(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} kB"


def summarize(before_files, after_files, before_pts, after_pts, t_start, t_end, passes, errors):
    kinds = {"video": (".mp4",), "photo": (".jpeg", ".jpg"), "gps": (".gps",)}
    new = {k: [0, 0] for k in kinds}
    for p, size in after_files.items():
        if p in before_files:
            continue
        for k, ext in kinds.items():
            if p.lower().endswith(ext):
                new[k][0] += 1
                new[k][1] += size

    new_t = sorted(t for t in after_pts if t not in before_pts)
    good = [(t, after_pts[t]) for t in new_t
            if after_pts[t][0] is not None and after_pts[t][1] is not None
            and (after_pts[t][2] is None or after_pts[t][2] > 0)
            and not (after_pts[t][0] == 0 and after_pts[t][1] == 0)]
    dist = 0.0
    for (t1, p1), (t2, p2) in zip(good, good[1:]):
        if t2 - t1 <= 60_000:                      # skip across gaps (separate trips)
            d = km(p1, p2)
            if d < 3:                              # ignore GPS jumps
                dist += d
    fmt = lambda ms: time.strftime("%d %b %H:%M", time.localtime(ms / 1000))  # noqa: E731

    lines = [f"🚗 Dashcam sync done ({time.strftime('%H:%M', time.localtime(t_start))}–"
             f"{time.strftime('%H:%M', time.localtime(t_end))}, {int(t_end - t_start)} s, {passes} pass{'es' if passes != 1 else ''})"]
    if new_t:
        lines.append(f"📍 GPS: +{len(new_t)} points ({len(good)} with fix), "
                     f"{fmt(new_t[0])} → {fmt(new_t[-1])}, ≈{dist:.1f} km")
    else:
        lines.append("📍 GPS: no new points")
    lines.append(f"🖼 Photos: +{new['photo'][0]} ({human(new['photo'][1])})")
    lines.append(f"🎞 Video: +{new['video'][0]} ({human(new['video'][1])})"
                 if "--video" in C["SYNC_ARGS"] else "🎞 Video: not synced (disabled)")
    total = sum(after_files.values())
    st = os.statvfs(C["DEST"])
    lines.append(f"💾 Stored: {human(total)}, disk free {human(st.f_bavail * st.f_frsize)}")
    if errors:
        lines.append("⚠️ " + "; ".join(errors[-3:]))
    return "\n".join(lines)


# ------------------------------------------------------------------ Telegram
def tg(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{C['TELEGRAM_TOKEN']}/{method}"
    if files:
        boundary = "----dashcam" + str(int(time.time() * 1000))
        body = b""
        for k, v in (data or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, path in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{os.path.basename(path)}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode()
            body += open(path, "rb").read() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data or {}).encode())
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# Messages go to a persistent queue; a background thread sends them, so a slow
# or missing internet connection never delays the camera sync. With a single
# Wi-Fi adapter the Pi is offline during a session and messages arrive in a
# burst afterwards - every message carries its own event time for that reason.
_q_lock = threading.Lock()
_wake = threading.Event()


def _load_q():
    try:
        return json.load(open(PENDING))
    except Exception:
        return []


def queue_message(text, gpx=None):
    with _q_lock:
        q = _load_q()
        q.append({"id": time.time_ns(), "text": text, "gpx": gpx})
        json.dump(q, open(PENDING, "w"))
    log("notify: " + text.splitlines()[0])
    _wake.set()


def flush_telegram():
    if not (C["TELEGRAM_TOKEN"] and C["TELEGRAM_CHAT_ID"]):
        return
    while True:
        with _q_lock:
            q = _load_q()
        if not q:
            return
        msg = q[0]
        try:
            tg("sendMessage", {"chat_id": C["TELEGRAM_CHAT_ID"], "text": msg["text"]})
            if msg.get("gpx") and os.path.exists(msg["gpx"]):
                tg("sendDocument", {"chat_id": C["TELEGRAM_CHAT_ID"]}, {"document": msg["gpx"]})
        except Exception as e:
            log(f"telegram send failed, {len(q)} queued (will retry): {str(e)[:100]}")
            return
        with _q_lock:
            json.dump([m for m in _load_q() if m["id"] != msg["id"]], open(PENDING, "w"))


def sender_loop():
    while True:
        _wake.wait(15)
        _wake.clear()
        try:
            flush_telegram()
        except Exception as e:
            log(f"sender error: {e}")


def event(kind, text):
    """Debug/progress notification; suppressed when NOTIFY_LEVEL=summary."""
    if C["NOTIFY_LEVEL"] == "debug" or kind == "summary":
        queue_message(text)


# ------------------------------------------------------------------ session
def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


def run_sync_pass(env):
    """Run sync.py once. Returns (ok, stats dict, error text, seconds)."""
    t0 = time.time()
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "sync.py"), C["CAM_IP"],
                          *C["SYNC_ARGS"].split()], env=env, text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    timed_out = []
    timer = threading.Timer(int(C["SYNC_TIMEOUT"]), lambda: (timed_out.append(1), p.kill()))
    timer.start()
    lines = []
    last_note = time.time()
    for line in p.stdout:                            # full output -> state/last_sync.log only
        line = line.rstrip("\n").split("\r")[-1]
        lines.append(line)
        if time.time() - last_note > 15 and line.strip():   # long pass: show it's alive
            log(f"  ...still syncing ({time.time() - t0:.0f} s): {line.strip()[:120]}")
            last_note = time.time()
    p.wait()
    timer.cancel()
    with open(os.path.join(STATE_DIR, "last_sync.log"), "w") as f:
        f.write("\n".join(lines) + "\n")
    if timed_out:
        return False, {}, f"timed out after {C['SYNC_TIMEOUT']} s", time.time() - t0
    stats = {}
    for line in lines:
        if line.startswith("sync:"):
            try:
                stats = ast.literal_eval(line[5:].strip())
            except Exception:
                pass
    if p.returncode != 0:
        errs = [l for l in lines if re.search(r"Error|error|refused|timed out", l)]
        err = (errs or [f"exit {p.returncode}"])[-1].strip()
        return False, stats, err[:200], time.time() - t0
    if stats.get("failed"):
        return False, stats, f"{stats['failed']} file(s) failed", time.time() - t0
    return True, stats, "", time.time() - t0


def run_session(t_detect, t_last_absent):
    since = f"{t_detect - t_last_absent:.0f} s after last empty scan" if t_last_absent else "first scan"
    log(f"camera '{C['CAM_SSID']}' visible -> session start")
    event("detect", f"📡 {clock(t_detect)} Camera detected ({since})")
    before_files, before_pts = file_snapshot(), read_points()
    home = active_connection(C["CAM_IFACE"])
    if home:
        log(f"'{C['CAM_IFACE']}' is on home Wi-Fi '{home}' - leaving it for the session")
    remember_home(home)
    passes, errors, first_done, last_err, joined = 0, [], False, None, False
    rel = lambda: f"+{time.time() - t_detect:.0f} s"  # noqa: E731
    try:
        if not connect_camera():
            errors.append("could not join camera Wi-Fi")
            event("connect", f"❌ {clock(time.time())} Could not join camera Wi-Fi ({rel()}): "
                                  f"{LAST_CONNECT_ERROR[0]}")
            return
        joined = True
        event("connect", f"🔗 {clock(time.time())} Joined camera Wi-Fi ({rel()})")
        env = dict(os.environ, DASHCAM_DEST=C["DEST"])
        while camera_reachable():
            passes += 1
            pts_before = len(read_points())
            ok, stats, err, took = run_sync_pass(env)
            new_pts = len(read_points()) - pts_before
            log(f"pass {passes}: {'ok' if ok else 'FAILED'} in {took:.1f}s - "
                f"new {stats.get('new', 0)}, updated {stats.get('updated', 0)}, "
                f"unchanged {stats.get('skipped', 0)}, failed {stats.get('failed', 0)}, "
                f"{stats.get('bytes', '0 B')}, GPS +{new_pts} pts" + (f" - {err}" if err else ""))
            if ok and not first_done:
                first_done = True
                event("transfer", f"✅ {clock(time.time())} Transfer complete ({rel()}, took {took:.0f} s): "
                                  f"{stats.get('new', 0)} new/updated files, GPS +{new_pts} points")
            elif not ok:
                lost = not camera_reachable()
                msg = ("camera lost during transfer - partial, resumes next time" if lost else err)
                errors.append(msg)
                if msg != last_err:                       # don't repeat identical failures
                    event("transfer", f"❌ {clock(time.time())} Transfer failed ({rel()}, pass {passes}): {msg}")
                last_err = msg
                if lost:
                    break
            else:
                last_err = None
            time.sleep(int(C["RESYNC_INTERVAL"]))
        log("camera gone -> session end")
    finally:
        disconnect_camera(home)
        t_end = time.time()
        if joined:
            event("disconnect", f"🔌 {clock(t_end)} Disconnected from camera "
                                f"(+{t_end - t_detect:.0f} s, {passes} pass{'es' if passes != 1 else ''})")
        if C["DEST_OWNER"]:
            sh("chown", "-R", C["DEST_OWNER"], C["DEST"], timeout=300)
        notify(before_files, before_pts, t_detect, t_end, passes, errors)


def notify(before_files, before_pts, t_start, t_end, passes, errors):
    gpx = None
    if passes:
        after_files, after_pts = file_snapshot(), read_points()
        nothing_new = (set(after_files) <= set(before_files)
                       and set(after_pts) <= set(before_pts) and not errors)
        if nothing_new and C["NOTIFY_EMPTY"] != "1":
            log("nothing new - no summary")
            return
        text = summarize(before_files, after_files, before_pts, after_pts,
                         t_start, t_end, passes, errors)
        if C["TELEGRAM_SEND_GPX"] == "1":
            gpx_dir = os.path.join(C["DEST"], "gpx")
            gpxs = [os.path.join(gpx_dir, f) for f in os.listdir(gpx_dir)] if os.path.isdir(gpx_dir) else []
            gpx = max(gpxs, key=os.path.getmtime) if gpxs else None   # names are dd-Mon-yyyy
    else:
        text = "⚠️ Dashcam seen but sync failed: " + "; ".join(errors or ["unknown"])
    queue_message(text, gpx)


def simulate():
    """Send the full notification sequence with made-up numbers. No Wi-Fi/camera use."""
    t = time.time()
    T = "[TEST] "
    steps = [
        ("start", f"{T}🟢 {clock(t)} Watcher started: scanning for camera every "
                  f"{C['SCAN_INTERVAL']} s on {C['CAM_IFACE']} (sync: {C['SYNC_ARGS'] or 'GPS only'})"),
        ("detect", f"{T}📡 {clock(t + 2)} Camera detected (5 s after last empty scan)"),
        ("connect", f"{T}🔗 {clock(t + 7)} Joined camera Wi-Fi (+5 s)"),
        ("transfer", f"{T}✅ {clock(t + 12)} Transfer complete (+10 s, took 4 s): 37 new/updated files, GPS +612 points"),
        ("transfer", f"{T}❌ {clock(t + 40)} Transfer failed (+38 s, pass 3): camera lost during transfer - partial, resumes next time"),
        ("disconnect", f"{T}🔌 {clock(t + 41)} Disconnected from camera (+39 s, 3 passes)"),
    ]
    for kind, text in steps:
        event(kind, text)
    queue_message(f"{T}🚗 Dashcam sync done ({clock(t)[:5]}–{clock(t + 41)[:5]}, 41 s, 3 passes)\n"
                  f"📍 GPS: +612 points (600 with fix), ≈8.4 km\n"
                  f"🖼 Photos: +35 (0.8 MB)\n🎞 Video: not synced (disabled)\n"
                  f"💾 Stored: 1.2 GB, disk free 40 GB\n"
                  f"⚠️ camera lost during transfer - partial, resumes next time")
    flush_telegram()
    with _q_lock:
        left = len(_load_q())
    print("all sent" if not left else f"{left} message(s) still queued - check token/chat id/internet")


def main():
    if "--test-telegram" in sys.argv:
        print(tg("sendMessage", {"chat_id": C["TELEGRAM_CHAT_ID"],
                                 "text": "✅ dashcam watcher: Telegram works"}).get("ok"))
        return
    threading.Thread(target=sender_loop, daemon=True).start()
    if "--simulate" in sys.argv:
        simulate()
        return
    # systemctl stop/restart sends SIGTERM: raise SystemExit so `finally` restores home Wi-Fi
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    recover_network()
    log(f"watching for '{C['CAM_SSID']}' on {C['CAM_IFACE']}, dest {C['DEST']}")
    event("start", f"🟢 {clock(time.time())} Watcher started: scanning for camera every "
                   f"{C['SCAN_INTERVAL']} s on {C['CAM_IFACE']} (sync: {C['SYNC_ARGS'] or 'GPS only'})")
    t_last_absent = None
    while True:
        try:
            t_scan = time.time()
            visible = camera_visible()
            scan_s = time.time() - t_scan
            if scan_s > int(C["SCAN_INTERVAL"]):
                log(f"slow scan: {scan_s:.1f} s")
            if visible:
                run_session(time.time(), t_last_absent)
                t_last_absent = None
                if "--once" not in sys.argv:
                    log(f"cooldown {C['COOLDOWN']} s")
                    time.sleep(int(C["COOLDOWN"]))
            else:
                t_last_absent = t_scan
                if "--once" in sys.argv:
                    log("camera not visible")
        except Exception as e:
            log(f"error: {e}")
            event("error", f"⚠️ {clock(time.time())} Watcher error: {str(e)[:200]}")
        if "--once" in sys.argv:
            _wake.set()
            time.sleep(1)
            flush_telegram()                           # send before exiting
            return
        time.sleep(int(C["SCAN_INTERVAL"]))


if __name__ == "__main__":
    main()
