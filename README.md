# qubo-dashcam-sync

Pull GPS tracks (and optionally timelapse photos / video) off a **Qubo GPS dashcam** without the
phone app. A Raspberry Pi in the garage watches for the dashcam's Wi-Fi, joins it whenever the
car is nearby with the ignition on, downloads what's new, and sends Telegram notifications.

```
car arrives / leaves  ->  Pi sees "DashCam GPS_<MAC>" Wi-Fi  ->  join  ->  sync passes every 20 s
                      ->  camera gone (ignition off / out of range)  ->  disconnect  ->  Telegram summary
```

> **Tested setups**
> - ✅ **Pi on Ethernet, `wlan0` used only for the camera**: tested and running.
> - ⚠️ **Pi on Wi-Fi only** (the same adapter is used for home internet and the camera): implemented
>   (the Pi leaves home Wi-Fi for the session and reconnects afterwards) but **not tested yet**.

## How it works

- The camera is an isolated Wi-Fi access point at `192.168.1.1`. Port `8080` serves the SD card over
  plain HTTP/WebDAV with no auth. Full findings are in [DESIGN.md](DESIGN.md).
- `sync.py` downloads incrementally:
  - GPS files (`G_<epoch-ms>.gps`, one per ~5 min of driving) plus per-day GPX tracks;
  - optionally photos (`--images`) and video (`--video`);
  - finished files and folders are remembered in `.sync_state.json`, so a repeat pass takes ~2 s;
  - interrupted downloads resume.
- `car_watch.py` is the Pi service:
  - scans for the camera every 5 s and joins it with a NetworkManager profile (static IP, never the
    default route);
  - re-runs `sync.py` while the camera is reachable;
  - queues Telegram messages and sends them once internet is available.

Rough data per driving hour: GPS ~200 kB, photos ~6.5 MB, video ~260 MB. The camera serves about
0.1–0.5 MB/s, so GPS and photos fit in the garage window. Video does not (see DESIGN.md §2.9).

## Files

| File | Purpose |
|---|---|
| `car_watch.py` | Pi watcher service (detect, join, sync, notify) |
| `sync.py` | Downloader: `python3 sync.py [IP[:port]] [--images] [--video] [--state] [--gpx-only]` |
| `install-pi.sh` | Installs the systemd service on the Pi |
| `dashcam.conf.example` | Config template (copied to `dashcam.conf`, which git ignores) |
| `dashcam-watch.service` | systemd unit template |
| `analyze.py` / `sanitize.py` | Privacy-safe summaries: counts and structure only, no coordinates or frames |
| `probe.sh` / `explore.py` | One-off tools used to discover the camera's IP, ports and SD card layout |
| `DESIGN.md` | Camera facts, GPS file format, Android app design |

## Raspberry Pi setup

Needs Raspberry Pi OS with NetworkManager (`nmcli`), Python 3 (standard library only), a Wi-Fi
adapter that can reach the car, and a Telegram bot (token from @BotFather plus your chat id).

1. Copy the code to the Pi (from your laptop):
   `rsync -av --exclude-from=.gitignore --exclude=.git ./ pi@<pi-host>:dev/dashcam/`
2. On the Pi, run `sudo bash ~/dev/dashcam/install-pi.sh`. The first run creates `dashcam.conf`.
3. Edit `~/dev/dashcam/dashcam.conf` and set at least:
   - `CAM_SSID` and `CAM_PASSWORD` for the camera Wi-Fi;
   - `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`;
   - `DEST`, the folder for synced data;
   - `CAM_IFACE`, the Wi-Fi adapter to use for the camera.
4. Test Telegram: `sudo python3 ~/dev/dashcam/car_watch.py --test-telegram` (prints `True`).
   `--simulate` sends the full sequence of notifications with made-up numbers.
5. Run `sudo bash ~/dev/dashcam/install-pi.sh` again. This installs and starts `dashcam-watch`.
6. Watch it: `journalctl -u dashcam-watch -f`

To try a single scan by hand, stop the service and run `sudo python3 ~/dev/dashcam/car_watch.py --once`.

### Config options

| Setting | Default | Notes |
|---|---|---|
| `SYNC_ARGS` | *(empty)* | Empty = GPS only. Add `--images` for photos. `--video` is rarely practical. |
| `NOTIFY_LEVEL` | `debug` | `debug` sends every event: 🟢 start, 📡 detected, 🔗 joined, ✅/❌ transfer, 🔌 disconnected and the 🚗 summary. `summary` sends only the summary. |
| `CAM_STATIC_IP` | `192.168.1.200` | Skips DHCP, which is faster and avoids join failures. |
| `SCAN_INTERVAL` / `RESYNC_INTERVAL` / `COOLDOWN` | 5 / 20 / 180 s | |
| `TELEGRAM_SEND_GPX` | `0` | `1` also uploads the GPX track to Telegram, so your location leaves the Pi. |

### Wi-Fi-only Pi (untested)

If `CAM_IFACE` is the adapter the Pi uses for internet:

- When the camera appears, the watcher saves the name of the home connection, joins the camera,
  and reconnects home after the session (retrying, then falling back to NetworkManager autoconnect).
- A crash or `systemctl restart` during a session also restores home Wi-Fi.
- While connected to the camera, the Pi is **offline**: SSH over Wi-Fi drops, and Telegram messages
  arrive in a burst afterwards.
- A second USB Wi-Fi adapter used only for the camera (`CAM_IFACE=wlan1`) avoids this.

Please report results if you run this setup.

## Output

Synced data goes under `DEST`:
- the camera's folder tree, mirrored;
- `gpx/<dd-Mon-yyyy>.gpx` with one track per day;
- `.sync_state.json`.

The watcher keeps its runtime state (Telegram queue, last sync log) in `state/`.

## Privacy and security

- **The camera's file server has no authentication and allows PUT/DELETE.** Anyone on its Wi-Fi
  can wipe the card, so change the camera's Wi-Fi password from the factory default. These tools only
  read (GET/HEAD, plus OPTIONS/PROPFIND in the probe scripts) and never write or delete.
- Synced footage, GPS tracks, logs and `dashcam.conf` (which holds secrets) are excluded by
  `.gitignore`. Never commit them.
