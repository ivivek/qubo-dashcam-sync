# Dashcam Sync — Design

Goal: **frictionless sync of dashcam data to the phone and on to my own server, so the data
is always available without opening the Qubo app.**
Priority 1: GPS / map data. Priority 1.5: timelapse photos. Priority 2 (later): video clips.

Status (2026-11-13): camera fully probed from a laptop; GPS behaviour during a real drive
still unknown (all data so far was captured with the car parked, no GPS fix).

---

## 1. The camera (everything we know)

### 1.1 Device

| Item | Value |
|---|---|
| Product | Qubo (Hero Electronix) dashcam, GPS variant — exact model unconfirmed (likely *Dashcam Pro GPS* HCA02 or *Dashcam GPS N*) |
| Wi-Fi SSID | `DashCam GPS_<MAC>` (SSID = prefix + MAC). The GPS N manual documents default SSID `Qubo_DashCAM_XXXXXXX` — ours differs |
| Wi-Fi password | Manual default `12345678` (app asks to change at setup). Ours: _not recorded here_ |
| Wi-Fi radio | 2.4 GHz 802.11 b/g/n (GPS N spec) |
| MAC | `AA:BB:CC:DD:EE:FF` style (also used as the device folder name `AA_BB_CC_DD_EE_FF`) |
| Internet on its AP | None (camera is an isolated AP) |
| Internal battery | Li-ion (240 mAh on GPS N); keeps clock for ~3 days standby — not for recording |

### 1.2 Network

| Item | Value |
|---|---|
| Camera IP | `192.168.1.1` (is the DHCP server and default gateway) |
| Client IP received | `192.168.1.11/24` (laptop) |
| Ping RTT | ~10–40 ms |
| Other hosts | none seen (ARP) |

### 1.3 Open ports (full TCP scan)

| Port | Service | Notes |
|---|---|---|
| **8080** | HTTP + WebDAV file server | **No authentication.** Serves the SD card. The sync path. |
| 554 | RTSP | `OPTIONS` → 200 on *any* path (`Public: DESCRIBE, OPTIONS, PAUSE, PLAY, SETUP, TEARDOWN, SET_PARAMETER, GET_PARAMETER`). `DESCRIBE` → **400 Bad Request** on `/`, `/live`, `/stream0`, `/stream1`, `/ch0`, `/live/ch00_0`, `/h264`. Live-view URL not found yet. |
| 8081 | Unknown | Resets every HTTP request and the JSON/raw probes we tried. Probably the Qubo app's proprietary control channel (settings, GPS mode, live view). |
| — | Closed/silent | 21 (FTP), 23 (Telnet), 3333 (Novatek notify), 7878 (Ambarella JSON) |
| — | Not matched | Novatek `?custom=1&cmd=…`, MStar `cgi-bin/Config.cgi`, HiSilicon `cgi-bin/hisnet/*` APIs all absent → not a stock Novatek/MStar/HiSilicon/Ambarella firmware API |
| UDP | Not scanned | (needs sudo) |

### 1.4 HTTP / WebDAV server (port 8080)

- `OPTIONS /` → `Allow: GET, POST, HEAD, CONNECT, PUT, DELETE, OPTIONS, PROPFIND, MKCOL`, `DAV: 1`.
  - ⚠ **PUT/DELETE are allowed without auth** — anyone on the camera Wi-Fi can modify or wipe the card. Change the Wi-Fi password from the default.
- `PROPFIND` (Depth 1) → `207 Multi-Status` (works; XML listing available as an alternative to HTML).
- `GET <dir>/` → HTML "Index of" page:
  ```html
  <tr><td><a href="/HERO_IOTG/…/name">name</a></td><td>&nbsp;01-Jan-2026 10:00</td><td>&nbsp;&nbsp;4.2M</td></tr>
  ```
  - hrefs are absolute paths; folders end in `/`; first row is `/..` (parent).
  - Sort links `?nd`, `?dd`, `?sd` (name/date/size).
  - Size column is human-rounded (`344`, `2.1k`, `4.2M`) → use `HEAD` `Content-Length` for exact size.
  - Modified-time column is shifted by the **local UTC offset** (local time treated as UTC, then offset again) — don't trust it; use timestamps in file names.
- File `GET`: `Accept-Ranges: bytes`; `Range` requests return **206** (resume works). `HEAD` returns `Content-Length`.
- Measured throughput (laptop, while camera recording): **0.12–0.5 MB/s**. Small files return in <0.1 s; directory pages ~0.05 s.

### 1.5 SD card layout

```
/HERO_IOTG/
└── <MAC_with_underscores>/                 # device MAC
    ├── rootTableMain.bin   rootTableBk.bin  # 768 B binary (index), Main + BacKup copy
    ├── heartbeat.txt                         # 8 B binary
    ├── Images/
    │   ├── rideStart.txt                     # 88 B binary
    │   └── <dd-Mon-yyyy>/
    │       ├── <dd-Mon-yyyy>_EVENT.bin  BK_<dd-Mon-yyyy>_EVENT.bin   # 344 B binary
    │       └── EVENT_<ddmmyyyyHHMMSS>.jpeg   # 640x480 JPEG
    ├── Recordings/
    │   ├── <dd-Mon-yyyy>/                    # e.g. 01-Jan-2026
    │   │   ├── VIDEO_<ddmmyyyyHHMMSS>_<epoch-ms>.mp4
    │   │   ├── <dd-Mon-yyyy>_DB.bin  BK_<dd-Mon-yyyy>_DB.bin  # clip index, grows 112 B per clip
    │   │   ├── folderTableMain.bin  folderTableBk.bin         # 512 B binary
    │   │   └── GPS/
    │   │       ├── G_<epoch-ms>.gps          # GPS track (text, see 1.7)
    │   │       └── activeGps.txt             # 80 B binary (GPS state/pointer?)
    │   └── Emergency00/                      # empty so far (event-locked clips?)
    └── Timelapse/
        └── <dd-Mon-yyyy>/DRIVE_MODE/1/
            └── timelapse_<epoch-ms>.jpeg     # 640x480, ~22 KB, one every ~12.3 s
```

- Binary `.bin`/`.txt` state files are undecoded (not needed so far). `Main`/`BK_` pairs are primary + backup copies.
- After ~1 h powered (parked): 46 videos, 211 images, 12 GPS/state files.

#### File reference — known

| File / folder | What it holds |
|---|---|
| `Recordings/<date>/VIDEO_<localtime>_<epoch-ms>.mp4` | 60 s clip, HEVC 1920×1080 @ 30 fps + AAC, 3.4–6.6 MB. The newest clip is still being written (no `moov`, unplayable). |
| `Recordings/<date>/GPS/G_<epoch-ms>.gps` | GPS text track, one line every 1–4 s: time, lat, lon, heading, speed km/h, trip km, per-file km, altitude (see 1.7). New file every ~5 min of driving. |
| `Timelapse/<date>/DRIVE_MODE/1/timelapse_<epoch-ms>.jpeg` | 640×480 photo (~22 KB) every ~12.3 s while the camera is powered. |
| `Images/<date>/EVENT_<localtime>.jpeg` | 640×480 event photo. Trigger unknown (G-sensor? button?). |
| `Emergency00/` | Empty so far. Presumably locked emergency clips. |

#### File reference — unknown (binary, not decoded)

| File | Size / behaviour | Best guess |
|---|---|---|
| `rootTableMain.bin`, `rootTableBk.bin` | 768 B | Top-level card index |
| `heartbeat.txt` | 8 B binary | Liveness marker or timestamp |
| `Images/rideStart.txt` | 88 B binary | Current trip start time/position |
| `Images/<date>/<date>_EVENT.bin`, `BK_<date>_EVENT.bin` | 344 B | That day's event-photo index |
| `Recordings/<date>/<date>_DB.bin`, `BK_<date>_DB.bin` | +112 B per clip | That day's clip index (one 112 B entry per video) |
| `Recordings/<date>/folderTableMain.bin`, `folderTableBk.bin` | 512 B | Day folder index |
| `Recordings/<date>/GPS/gps.bin`, `gpsBk.bin` | binary | GPS index/buffer |
| `Recordings/<date>/GPS/activeGps.txt` | 80 B binary | Pointer to the GPS file being written |

None of these are needed for sync. `sync.py --state` downloads them if we ever want to decode them.
Other unknowns: port 8081 (resets every request; probably the app's control channel) and the RTSP
live-view path on 554 (every guess → 400).

### 1.6 Timestamps

- `ddmmyyyyHHMMSS` in names = **local time** (camera clock timezone).
- `<epoch-ms>` in names = **real Unix epoch in ms (UTC)**; e.g. `VIDEO_01012026100000_1767261600000` ↔ 2026-01-01 10:00:00 (camera timezone = UTC in this example). Prefer epoch for sorting/joins.
- `.gps` file name epoch == `T` of its first point.

### 1.7 GPS data

- Stored **only** in `Recordings/<date>/GPS/G_<epoch-ms>.gps` — **not** embedded in the MP4 (no GPS boxes/markers found; exiftool `-ee` found nothing).
- Text, one point per line, tab-separated, 8 fields (values masked):
  ```
  T-<epoch ms>  LT-<N|S><deg>  LG-<E|W><deg>  D-<n>  S-<n>  DS-<n>  pDS-<n>  Alt-<n>
  ```

  | Field | Meaning | Evidence (2026-11-16, 5216 real points, aggregates only) |
  |---|---|---|
  | `T` | point time, epoch ms (UTC) | unique, increasing within file |
  | `LT` / `LG` | latitude / longitude, hemisphere letter + decimal degrees | — |
  | `D` | **heading, degrees** ✅ | median 0.3° from bearing computed from coordinates |
  | `S` | **speed, km/h (integer)** ✅ | r=0.944 vs computed speed, ratio 1.00 while moving; 0 when stopped |
  | `DS` | **trip distance, km** ✅ — continues across the ~5-min files of one drive | increments match step distance r=1.000 (×0.001 → km); continued at 30/30 file boundaries |
  | `pDS` | **distance within the current file, km** ✅ — resets to 0 in each new file | increments r=1.000 (km); reset at 30/30 file boundaries |
  | `Alt` | altitude (m), presumably | float, never negative |
- **Sampling:** one point every 1–4 s (median 2 s).
- **File rotation:** a drive is split into **~5-minute files** (median 5.0 min; 30/40 files start ≤10 s after the previous one ends); new file after longer breaks (trip start). Max file span seen: 221 min.
- **Files grow** while open (re-download by size works); closed files don't change.
- Each `GPS/` folder also has `gps.bin` / `gpsBk.bin` (binary, likely index of the `.gps` files).
- Indoors/parked: no fix → no points (first observation: 2 points then nothing).
- Qubo app has a **"GPS Mode" enable/disable** toggle (GPS N manual) — must be ON.
- Volume: ~120 B per point → ~200 kB per driving hour.

### 1.8 Video & images

| | Value |
|---|---|
| Clip length | 60 s (loop recording) |
| Codec | **HEVC/H.265** 1920×1080 @ 30 fps + AAC audio |
| Size | 3.4–6.6 MB per clip, ~4.4 MB median, ~580 kbps → **~260 MB/hour** |
| MP4 layout | `ftyp` (24 B, brands `isom`/`mp42`) → `free` (136 B) → `mdat` @160 → `moov` **at end** |
| In-progress clip | newest `VIDEO_*` has no `moov` yet → unplayable/partial; always skip it |
| Images | 640×480 JPEG: timelapse every ~12.3 s while powered; `EVENT_*` on events |

**Timelapse photos** (`Timelapse/<date>/DRIVE_MODE/1/timelapse_<epoch-ms>.jpeg`):
- Created **automatically** whenever the camera is powered/recording — no app or user action (91 files ~20 min after power-on, 211 later while parked).
- Interval **~12.3 s** (most gaps exactly 12.3 s; occasional skips of 24.6 / 36.9 s).
- 640×480 JPEG, ~22 KB each → **~290 photos ≈ 6.5 MB per hour** (vs ~260 MB/h of video).
- Name carries real UTC epoch-ms → can be matched to GPS points by time.
- No setting found to disable them (not mentioned in the manual pages checked). The `DRIVE_MODE` folder suggests the Qubo app uses them as trip thumbnails / slideshow; parking mode (hardwiring kit) separately takes one photo per 30 s.

### 1.9 Power & trips (from Qubo documentation)

- Camera powers on with ignition; **stays active ~1 minute after ignition off** (Pro GPS support page).
- Ignition off < 5 min → same trip; > 5 min → new trip.
- Parking mode (30 s snapshots + jolt recording) needs the separate hardwiring kit.
- Auto shutdown on high cabin temperature; restarts with engine.
- Boot time until Wi-Fi AP is visible: **unknown** (measure).

### 1.10 Open questions about the camera

1. ~~GPS sampling rate, file rotation, field meanings~~ — answered (§1.7). Trip boundaries: likely where `DS` resets; confirm against the camera's 5-min ignition rule.
2. Boot time to Wi-Fi AP after ignition on.
3. Does it flush the last GPS points before the 1-minute shutdown?
4. Port 8081 protocol and RTSP live-view path (only needed for live view / changing settings).
5. Meaning of the binary state files (`activeGps.txt`, `rideStart.txt`, `*_DB.bin`, `*_EVENT.bin`).
6. Exact model number.

### 1.11 Tools in this repo

| File | Purpose |
|---|---|
| `probe.sh` | Find camera IP, port-scan, try known dashcam APIs |
| `explore.py` | Crawl SD card, test 8081/RTSP, grab samples (raw — contains private data) |
| `sanitize.py` | Turn an `explore-*` folder into a privacy-safe structural report |
| `sync.py` | Mirror camera → `./footage/` (GPS/state by default, `--images`, `--video`), resumable, live progress, builds `footage/gpx/<date>.gpx` |
| `analyze.py` | Privacy-safe summary of `./footage` (counts, formats, intervals — never coordinates/frames) |
| `car_watch.py` | Raspberry Pi garage watcher: detects camera SSID, joins it (NetworkManager), runs `sync.py` repeatedly until the camera disappears, sends a Telegram summary (points, km, photos, storage) |
| `dashcam.conf.example`, `dashcam-watch.service`, `install-pi.sh` | Config template, systemd unit, installer for the watcher |

### 1.12 Garage sync on Raspberry Pi (implemented, alternative to the phone app)

- Pi scans for the camera SSID every 15 s. Session = camera visible → camera gone (arrival: until ~1 min after ignition off; departure: until the car leaves range).
- Camera NM profile: `never-default`, no DNS, no autoconnect → home routing untouched. With one Wi-Fi adapter the Pi leaves home Wi-Fi during the session and reconnects after; a 2nd USB adapter avoids that and can sit nearer the car.
- `sync.py --images` repeated every 20 s while reachable, so the final GPS points before shutdown are captured.
- Telegram message per session (queued and retried if offline); no message when nothing new; GPX attachment opt-in only.
- Limit: only syncs when the car is at home; video usually won't fit the window.

---

## 2. Android app — requirements

### 2.1 Goal & priorities

- **Frictionless:** after a one-time setup, data syncs with zero taps in normal use.
- **Always available:** at any moment the server has GPS data up to at least the previous trip.
- **P1: GPS/map data.**
- **P1.5: timelapse photos** — a cheap visual record of each trip (~6.5 MB/h).
- **P2 (later): video clips.**
- Architecture must allow P1.5 and P2 without redesign (same downloader, per-type enable switches).

### 2.2 Sync triggers

| Trigger | Behaviour |
|---|---|
| **Car Bluetooth connects** (primary, automatic) | Camera is booting with ignition → retry joining its AP for up to ~2 min, then sync everything not yet synced (covers the previous trip, any date folders since last sync). Silent; notify only on failure. |
| **Widget "Sync now" button** (manual, end of trip) | Sync the trip just finished while ignition is still on. Widget shows *Syncing… keep ignition on* → *✓ Done – safe to switch off* + vibration. Expected ~10–20 s. |
| Missed / interrupted sync | Nothing lost: next Bluetooth-connect sync picks up the remainder (incremental by file size). |

Not planned: sync on Bluetooth *disconnect* (races the camera's 1-minute shutdown).

### 2.3 Sync behaviour

1. Join camera Wi-Fi via `WifiNetworkSpecifier` (SSID + password); no prompt thanks to a CompanionDeviceManager association. Bind only camera traffic to that network; mobile data stays the default route.
2. List only what's needed: `Recordings/` → date folders newer than or equal to last synced date → `GPS/`.
3. For each `.gps`: `HEAD` → if new or `Content-Length` changed → download whole file (small) and store.
4. Release the camera network as soon as done (timeout ~45 s for manual sync).
5. Never touch the newest in-progress `VIDEO_*` (relevant for P2).
6. Never use PUT/DELETE on the camera.

### 2.4 Local storage

- Raw `.gps` files in app-private storage, keyed by `<device-MAC>/<date>/<file>`.
- Small DB (Room): per file — remote path, size, last-synced time, upload state; per device — last sync result.
- Optional (after drive data): parsed points table for an in-app map.

### 2.5 Upload to my server

- WorkManager job, constraint *network connected*, exponential retry; runs after every sync and whenever internet returns.
- Upload **raw `.gps` files**; server parses (keeps the app independent of the GPS format, which is not fully known yet).
- **Idempotent:** server stores each file by `(device, path)` and **replaces** it on re-upload (files grow between syncs).
- Proposed API (to confirm / adapt to existing server):
  ```
  PUT /api/v1/devices/{mac}/files/{path}      body: raw file bytes
      Authorization: Bearer <token>
      X-File-Mtime: <epoch-ms>                 -> 200/201
  ```
- Server-side: parse points, split trips on gaps > 5 min (camera's own trip rule), drop points with 0 satellites.

### 2.6 Widget & UI

- Home-screen widget: *Sync now* button, last sync time, status (OK / syncing / error).
- Settings: camera SSID + password, server URL + token, car Bluetooth device picker, enable timelapse sync (P1.5), enable video sync (P2, off).
- Status/history screen: last syncs, files pending upload, errors.

### 2.7 Android constraints & one-time setup

- One-time: CompanionDeviceManager pairing with camera AP, permissions (nearby Wi-Fi devices / location, notifications, Bluetooth connect), **battery-optimisation exemption** (needed on Xiaomi/Oppo/Vivo/Realme/Samsung).
- Cleartext HTTP must be allowed for `192.168.1.1` only (`network_security_config`).
- Bluetooth `ACL_CONNECTED` is allowed as a manifest broadcast; sync runs as an expedited job / foreground service (`connectedDevice`/`dataSync`).
- Possible conflict with **wireless Android Auto** (uses Wi-Fi) — test.
- Network on camera AP has no internet; Android may flag it — the specifier request keeps it scoped to the app.

### 2.8 P1.5 — timelapse photos

- Synced after GPS in the same session (GPS always first, so a short sync window still gets the map data).
- Incremental: list `Timelapse/<date>/DRIVE_MODE/*/`, download only files not yet stored (JPEGs never change once written).
- Time budget: at 0.12–0.5 MB/s an hour of driving (~6.5 MB) takes ~15–55 s → fine on the Bluetooth-connect sync; on the manual end-of-trip sync, download as many as fit in the window and leave the rest for the next sync.
- Optional photo-skip (e.g. keep every 2nd/5th) to reduce volume.
- Upload: same idempotent PUT per file; allowed on mobile data by default (small), switch to Wi-Fi-only in settings.
- Server/app use: show photos along the route on the map by matching photo epoch-ms to the nearest GPS point.
- Privacy: real camera imagery — stored only in app-private storage and on my server.
- Settings: *Sync timelapse photos* (on/off), *Upload photos on mobile data* (on/off), local retention (days).

### 2.9 P2 — video (later)

- Same downloader, Range-resume, skip in-progress clip, verify `moov` present before accepting.
- ~4.4 MB/min at 0.12–0.5 MB/s → video must sync **during the drive** in the background (camera AP only, never mobile data for the download), opt-in, with storage cap and per-trip selection.
- Upload to server only on unmetered Wi-Fi.

### 2.10 Non-functional

- Privacy: data goes only to my server; no third-party analytics.
- Robust to camera firmware changes: log/report unexpected listing or file formats instead of failing silently.
- Battery: no polling/scanning loops; event-driven only.

### 2.11 Still needed from me

- [ ] Phone model + Android version
- [ ] Car Bluetooth present? Wireless Android Auto used?
- [ ] Server: existing API details, or build a minimal one (language?)

---

## 3. Test-drive checklist (unblocks GPS parser & trip logic)

1. In the Qubo app confirm **GPS Mode = ON**.
2. Time from ignition on → `DashCam GPS_…` visible in phone Wi-Fi list.
3. Drive 5+ min under open sky, stop with ignition off > 5 min, drive again a few minutes.
4. With ignition on: `python3 sync.py | tee -a sync.log`; then `python3 analyze.py` (privacy-safe output).
5. After parking, note how long the camera stays on (LED).
