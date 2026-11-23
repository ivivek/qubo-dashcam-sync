# CLAUDE.md

Qubo GPS dashcam → Raspberry Pi garage sync. See README.md for usage and DESIGN.md for everything
known about the camera (ports, SD card layout, GPS format, timing).

## Privacy: hard rules

- **Never read raw dashcam data**: no GPS coordinates, image or video frames, `.gps`/`.gpx`
  contents, or the binary state files.
  - Applies to `footage/`, `explore-*/`, and any synced copy on the Pi or laptop.
  - Read only aggregate or sanitized output: counts, sizes, file names, status lines.
  - These folders are already in the global Read/Edit deny list. Don't get around that with
    `cat`/`head`/`python -c` either.
- **Tools that touch GPS or video must print only counts and status.** This includes `sync.py`,
  `analyze.py` and `sanitize.py`.
  - When a semantics question needs the data, write a script that prints aggregates only
    (correlations, increments, ranges of differences), never values.
  - The user runs it and pastes the output.
- **Nothing personal goes into the repo.** Before every commit, `git grep --cached` for:
  - the camera MAC/SSID (use `<MAC>` / `AA:BB:CC:DD:EE:FF`);
  - the Pi hostname, home IPs (`192.168.0.x`), usernames and emails;
  - real recording timestamps and timezone. Use made-up examples, e.g. `01-Jan-2026 10:00`.

  Host-specific details live in `CLAUDE.local.md` (git-ignored).
- Secrets (`dashcam.conf`: Wi-Fi password, Telegram token, chat id) never go in the repo or the
  chat. `dashcam.conf.example` keeps `CAM_PASSWORD=` empty.

## Camera: safety

- The file server on `192.168.1.1:8080` has no auth and **allows PUT/DELETE**. Only ever use
  GET/HEAD (plus OPTIONS/PROPFIND in probe scripts). Never write or delete on the camera.
- Claude can't reach the camera. The laptop/Pi loses internet while on the camera Wi-Fi, and the
  camera is only on with the car's ignition. Test against the **mock camera** `tools/mockcam.py` instead:
  - a local `http.server` that serves the "Index of" HTML format (see DESIGN.md §1.4);
  - Range/HEAD support;
  - synthetic files only: fake MP4 boxes `ftyp`+`mdat`(+`moov`), dummy `.gps`;
  - run it on a high port (e.g. 18091) and pass `127.0.0.1:18091` as the camera address.

## Code conventions

- Python 3.11 (Raspberry Pi OS), **standard library only**. The Pi has no venv.
- `car_watch.py` runs as root under systemd (`dashcam-watch.service`) and calls `sync.py` as a
  subprocess.
  - Logs: one line per sync pass or clip pass. Full output goes to `state/last_sync.log` /
    `state/last_clips.log`.
  - Telegram: messages go through the persistent queue (`queue_message`/`event`).
    - `event("action", …)` is always sent.
    - Debug events are suppressed when `NOTIFY_LEVEL=summary`.
    - Messages carry their own clock time, since they may arrive late.
- `sync.py` is incremental. `.sync_state.json` tracks closed GPS files and finished day folders.
  - GPS-only mode never lists video or photo folders. Keep repeat passes ~2 s.
  - The newest clip is still being recorded: never treat it as complete.
- `clips.py`: the wanted-clips list and the Telegram `clip` command parser. `sync.py --clips FILE`
  downloads by clip start time (`VIDEO_<local>_<epoch-ms>.mp4`).
- Runtime files: `state/` (watcher) and `DEST` (`footage/`). Both are git-ignored.

## Workflow

- **Commit or push only when asked.** Commit as the repo's local identity, which is already set.
- **Deploy to the Pi:** rsync the repo (excluding `.gitignore` entries and `.git`), then restart
  the service. Exact commands are in `CLAUDE.local.md`.
- **Commands for the user:** short single lines, or a script plus `bash script.sh`. No heredocs.
- **Status:** the Pi-on-Ethernet setup is tested. The Wi-Fi-only Pi, a second Wi-Fi adapter, real
  drive timing and clip fetching from the real camera are not tested yet. Keep README's "Tested
  setups" honest.
