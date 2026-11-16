"""Wanted-clips list: Telegram `clip [day] [time] [duration] [label]` requests.

The Pi keeps the list in state/wanted_clips.json. car_watch.py fetches the
matching video clips from the camera (sync.py --clips) the next time the car
is home, then links them into DEST/clips/<id>_<day>_<time>_<label>/.
Only clip times and labels are stored here - never GPS values or frames.
"""
import json
import os
import re
import threading
import time

DEFAULT_HALF_S = 60          # `clip 08:42` -> about ±1 min
MAX_DURATION_S = 30 * 60     # `clip 08:42 500m` is capped at 30 min
EDGE_S = 30                  # extra lead-in so an event at a clip boundary is covered
CLIP_MB = 4.4                # median clip size (60 s, HEVC 1080p)

HELP = ("Commands:\n"
        "clip - the moment you send it\n"
        "clip 08:42 / clip 08:42:30 - today (yesterday if that's in the future)\n"
        "clip yesterday 18:10 / clip 15-Nov 18:10 - another day\n"
        "clip 08:42 5m - 5 minutes centred on 08:42 (default ±1 min, max 30m)\n"
        "clip 08:42 deer crossing - anything after the time is a label\n"
        "clips - list requests\n"
        "clip cancel 3 - drop request #3")

DAY_WORD = re.compile(r"^(today|yesterday)$", re.I)
DAY_DATE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})(?:-(\d{4}))?$")
CLOCK = re.compile(r"^(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?$")
DURATION = re.compile(r"^(\d+)(m|min|s)$", re.I)


def window(item):
    """Camera clip start times (epoch ms) that belong to a request."""
    t, half = item["t_ms"], item["half_s"] * 1000
    return t - half - EDGE_S * 1000, t + half


def est_clips(item):
    lo, hi = window(item)
    return int((hi - lo) / 60000) + 1


def fmt_time(t_ms):
    t = time.localtime(t_ms / 1000)
    fmt = "%d-%b %H:%M:%S" if t.tm_year == time.localtime().tm_year else "%d-%b-%Y %H:%M:%S"
    return time.strftime(fmt, t)


def fmt_window(item):
    half = item["half_s"]
    span = f"±{half // 60} min" if half % 60 == 0 else f"±{half} s"
    return f"{fmt_time(item['t_ms'])} {span}"


def parse(text, msg_ts, now=None):
    """Parse a `clip ...` request. Returns (item dict, None) or (None, error)."""
    now = now or time.time()
    words = text.split()[1:]
    day = time.localtime(msg_ts)
    explicit_day = False
    if words and DAY_WORD.match(words[0]):
        day = time.localtime(now - (86400 if words[0].lower() == "yesterday" else 0))
        explicit_day = True
        words = words[1:]
    elif words and DAY_DATE.match(words[0]):
        d, mon, year = DAY_DATE.match(words[0]).groups()
        try:
            day = time.strptime(f"{d}-{mon.title()}-{year or day.tm_year}", "%d-%b-%Y")
        except ValueError:
            return None, f"Don't understand the date '{words[0]}' (use e.g. 15-Nov)"
        explicit_day = "year" if not year else True
        words = words[1:]
    clock = CLOCK.match(words[0]) if words else None
    if clock:
        h, m, s = int(clock.group(1)), int(clock.group(2)), int(clock.group(3) or 0)
        if h > 23 or m > 59 or s > 59:
            return None, f"Don't understand the time '{words[0]}'"
        words = words[1:]
        t = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, h, m, s, 0, 0, -1))
        if t > now + 120:                     # a time later than now means an earlier day
            if explicit_day == "year":
                t = time.mktime((day.tm_year - 1, day.tm_mon, day.tm_mday, h, m, s, 0, 0, -1))
            elif not explicit_day:
                t -= 86400
            else:
                return None, f"{fmt_time(t * 1000)} is in the future"
    elif explicit_day:
        return None, "Add a time, e.g. clip yesterday 18:10"
    else:
        t = msg_ts                            # plain `clip`: the moment it was sent
    half = DEFAULT_HALF_S
    if words and DURATION.match(words[0]):
        n, unit = DURATION.match(words[0]).groups()
        secs = min(int(n) * (1 if unit.lower() == "s" else 60), MAX_DURATION_S)
        half = max(secs // 2, 30)
        words = words[1:]
    return {"t_ms": int(t * 1000), "half_s": half, "label": " ".join(words)[:60]}, None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def folder_name(item):
    t = time.localtime(item["t_ms"] / 1000)
    name = f"{item['id']:04d}_{time.strftime('%d-%b_%H%M', t)}"
    return name + (f"_{slug(item['label'])}" if slug(item["label"]) else "")


class Store:
    """Thread-safe JSON list of requests: status pending -> ready | missing | cancelled."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"next_id": 1, "items": []}

    def _save(self, data):
        done = [i for i in data["items"] if i["status"] != "pending"]
        drop = {i["id"] for i in done[:-50]}           # keep the last 50 finished requests
        data["items"] = [i for i in data["items"] if i["id"] not in drop]
        with open(self.path + ".tmp", "w") as f:
            json.dump(data, f, indent=1)
        os.replace(self.path + ".tmp", self.path)

    def add(self, item):
        with self.lock:
            data = self._load()
            item.update(id=data["next_id"], status="pending", requested=int(time.time()),
                        got=0, total=0)
            data["next_id"] += 1
            data["items"].append(item)
            self._save(data)
            return item

    def cancel(self, item_id):
        with self.lock:
            data = self._load()
            for i in data["items"]:
                if i["id"] == item_id and i["status"] == "pending":
                    i["status"] = "cancelled"
                    self._save(data)
                    return True
            return False

    def items(self):
        with self.lock:
            return self._load()["items"]

    def pending(self):
        return [i for i in self.items() if i["status"] == "pending"]

    def update(self, item_id, **fields):
        with self.lock:
            data = self._load()
            for i in data["items"]:
                if i["id"] == item_id:
                    i.update(fields)
                    self._save(data)
                    return i
            return None


def describe(item):
    label = f' "{item["label"]}"' if item["label"] else ""
    head = f"#{item['id']} {fmt_window(item)}{label}"
    st = item["status"]
    if st == "ready":
        return f"{head} - ready ({item['total']} files, {item.get('mb', 0):.0f} MB)"
    if st == "missing":
        return f"{head} - not available: {item.get('reason', '')}"
    if st == "pending" and item["total"]:
        return f"{head} - {item['got']} of {item['total']} downloaded"
    return f"{head} - {st}"


def handle(store, text, msg_ts):
    """Handle one Telegram message. Returns the reply text, or None to ignore it."""
    words = text.strip().split()
    if not words:
        return None
    cmd = words[0].lower().lstrip("/").split("@")[0]
    if cmd in ("help", "start"):
        return HELP
    if cmd == "clips":
        items = store.items()[-10:]
        return "\n".join(describe(i) for i in items) if items else "No clip requests yet."
    if cmd != "clip":
        return "Unknown command.\n" + HELP
    if len(words) >= 2 and words[1].lower() == "cancel":
        try:
            n = int(words[2].lstrip("#"))
        except (IndexError, ValueError):
            return "Usage: clip cancel 3"
        return f"🗑 Clip #{n} cancelled." if store.cancel(n) else f"No pending clip #{n}."
    item, err = parse(" ".join(words), msg_ts)
    if err:
        return f"❌ {err}\n\n{HELP}"
    item = store.add(item)
    n = est_clips(item)
    label = f' "{item["label"]}"' if item["label"] else ""
    return (f"📝 Clip #{item['id']} queued: {fmt_window(item)}{label} "
            f"(~{n} clips, ~{n * CLIP_MB:.0f} MB).\n"
            f"Fetched next time the car is home - keep the ignition on until you get the ✅.")
