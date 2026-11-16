#!/usr/bin/env python3
"""Privacy-safe structural summary of ./footage (no coordinates, no frames).

Prints counts, formats, sampling intervals and per-field behaviour
(monotonic / constant / range class) - never raw GPS values or pixels.
"""
import collections
import json
import os
import re
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sync import mp4_complete  # noqa: E402

ROOT = os.environ.get("DASHCAM_DEST") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "footage")
FIELD = re.compile(r"^([A-Za-z]+)-?([NSEW]?)(-?[\d.]+)$")


def shape(line):
    s = re.sub(r"\d", "9", line.rstrip("\r\n"))
    s = re.sub(r"(?<![A-Za-z])[NSEW](?=[-\d9.]|$)", "H", s)
    return re.sub(r"9[9.]*", "#", s).replace("\t", "<TAB>")


def describe(vals):
    """Behaviour of a numeric series without revealing its values."""
    if not vals:
        return "empty"
    uniq = len(set(vals))
    d = [b - a for a, b in zip(vals, vals[1:])]
    mono = ("increasing" if d and all(x >= 0 for x in d) else
            "decreasing" if d and all(x <= 0 for x in d) else "mixed")
    ints = all(float(v).is_integer() for v in vals)
    zero = sum(1 for v in vals if v == 0)
    return (f"n={len(vals)} unique={uniq} {mono} {'int' if ints else 'float'} "
            f"zeros={zero} {'constant' if uniq == 1 else ''}").strip()


print(f"== Inventory of {ROOT}")
by_dir = collections.defaultdict(collections.Counter)
partials = []
for dp, _, fs in os.walk(ROOT):
    rel = os.path.relpath(dp, ROOT)
    rel = re.sub(r"\d{2}-[A-Za-z]{3}-\d{4}", "<date>", rel)
    for f in fs:
        if f.endswith(".part"):
            partials.append(f)
        by_dir[rel][os.path.splitext(f)[1].lower() or "(none)"] += 1
for d in sorted(by_dir):
    print(f"  {d}: {dict(by_dir[d])}")
print(f"  .part (incomplete) files: {len(partials)}")
days = sorted({m for dp, _, _ in os.walk(ROOT)
               for m in re.findall(r"\d{2}-[A-Za-z]{3}-\d{4}", dp)})
print(f"  distinct date folders: {len(days)}")

# ------------------------------------------------------------------ GPS
print("\n== GPS files")
gps = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(ROOT)
             for f in fs if f.lower().endswith(".gps"))
shapes = collections.Counter()
series = collections.defaultdict(list)
per_file, intervals, all_t = [], [], []
for p in gps:
    ts = []
    lines = open(p, errors="replace").read().splitlines()
    per_file.append(len(lines))
    for line in lines:
        if not line.strip():
            continue
        shapes[shape(line)] += 1
        for fld in line.split("\t"):
            m = FIELD.match(fld.strip())
            if m:
                key, hemi, num = m.groups()
                if key not in ("LT", "LG"):          # never collect coordinates
                    series[key].append(float(num))
                else:
                    series[key + "_hemi"].append(hemi or "?")
                if key == "T":
                    ts.append(float(num))
    intervals += [(b - a) / 1000 for a, b in zip(ts, ts[1:])]
    all_t += ts
    # file name epoch vs first point (seconds), structure only
    m = re.search(r"G_(\d+)\.gps", p)
    if m and ts:
        series["_name_minus_first_T_s"].append((ts[0] - int(m.group(1))) / 1000)

print(f"  files: {len(gps)}; lines per file: min={min(per_file, default=0)} "
      f"median={statistics.median(per_file) if per_file else 0} max={max(per_file, default=0)} "
      f"total={sum(per_file)}")
print("  line shapes (digits->#, hemisphere->H):")
for s, c in shapes.most_common(8):
    print(f"    {c:6d}x {s[:150]}")
if intervals:
    iv = sorted(intervals)
    q = lambda f: iv[min(len(iv) - 1, int(f * len(iv)))]  # noqa: E731
    print(f"  interval between points (s): min={iv[0]:.1f} p10={q(.1):.1f} median={q(.5):.1f} "
          f"p90={q(.9):.1f} max={iv[-1]:.1f}  negative={sum(1 for x in iv if x < 0)}")
    gaps = sum(1 for x in iv if x > 60)
    print(f"  gaps > 60s: {gaps}")
if all_t:
    span = (max(all_t) - min(all_t)) / 60000
    print(f"  total span covered by points: {span:.1f} min")
print("  per-field behaviour (values not shown):")
for k in sorted(series):
    v = series[k]
    if k.endswith("_hemi"):
        print(f"    {k}: {len(v)} values, {len(set(v))} distinct")
    elif k == "_name_minus_first_T_s":
        print(f"    file-name epoch -> first point offset: "
              f"min={min(v):.1f}s max={max(v):.1f}s")
    elif k == "T":
        print(f"    T: {describe(v)} (sorted-within-file={all(x >= 0 for x in intervals)})")
    else:
        lo, hi = min(v), max(v)
        rng = ("<=1" if hi <= 1 else "<=10" if hi <= 10 else "<=360" if hi <= 360
               else "<=1e4" if hi <= 1e4 else ">1e4")
        print(f"    {k}: {describe(v)} max-class={rng} negatives={sum(1 for x in v if x < 0)}")

# --------------------------------------------- field semantics (aggregates only)
# Compare each unknown field with speed / bearing / distance derived from the
# coordinates. Coordinates stay in memory; only correlations, ratios and
# fractions are printed - no values, no locations.
import math  # noqa: E402


def hav_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12_742_000 * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return math.degrees(math.atan2(y, x)) % 360


def pearson(xs, ys):
    if len(xs) < 10:
        return float("nan")
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else float("nan")


def med(v):
    return statistics.median(v) if v else float("nan")


files_pts = []
for p in gps:
    pts = []
    for line in open(p, errors="replace"):
        d = {}
        for fld in line.strip().split("\t"):
            m = FIELD.match(fld.strip())
            if m:
                key, hemi, num = m.groups()
                d[key] = float(num) * (-1 if hemi in ("S", "W") else 1)
        if {"T", "LT", "LG"} <= d.keys() and not (d["LT"] == 0 and d["LG"] == 0):
            pts.append(d)
    pts.sort(key=lambda d: d["T"])
    if pts:
        files_pts.append(pts)

if files_pts:
    print("\n== GPS field semantics (aggregates only, no values)")
    durs = [(f[-1]["T"] - f[0]["T"]) / 60000 for f in files_pts]
    print(f"  file duration (min): min={min(durs):.1f} median={med(durs):.1f} max={max(durs):.1f}")
    order = sorted(files_pts, key=lambda f: f[0]["T"])
    between = [(b[0]["T"] - a[-1]["T"]) / 1000 for a, b in zip(order, order[1:])]
    if between:
        short = sum(1 for g in between if g <= 10)
        print(f"  gap between consecutive files (s): median={med(between):.1f}; "
              f"{short}/{len(between)} files start within 10 s of the previous one "
              f"(= continuous drive split into several files)")
        overlap = sum(1 for g in between if g < 0)
        print(f"  overlapping files: {overlap}")

    sp_calc, sp_field, brg_diff, step_m, pds_vals, ds_cum, ds_vals, ds_sp = [], [], [], [], [], [], [], []
    mono = {"DS": [0, 0], "pDS": [0, 0]}
    pds_eq_prev_ds = [0, 0]
    for f in files_pts:
        cum = 0.0
        for a, b in zip(f, f[1:]):
            dt = (b["T"] - a["T"]) / 1000
            if not 0.5 <= dt <= 10:
                continue
            dist = hav_m((a["LT"], a["LG"]), (b["LT"], b["LG"]))
            cum += dist
            if "S" in b:
                sp_calc.append(dist / dt * 3.6)
                sp_field.append(b["S"])
            if "D" in b and dist > 5:
                diff = abs((b["D"] - bearing((a["LT"], a["LG"]), (b["LT"], b["LG"])) + 180) % 360 - 180)
                brg_diff.append(diff)
            if "pDS" in b:
                step_m.append(dist)
                pds_vals.append(b["pDS"])
            if "DS" in b:
                ds_cum.append(cum)
                ds_vals.append(b["DS"])
                ds_sp.append((dist / dt * 3.6, b["DS"]))
            for k in mono:
                if k in a and k in b:
                    mono[k][0] += b[k] >= a[k]
                    mono[k][1] += 1
            if "pDS" in b and "DS" in a:
                pds_eq_prev_ds[0] += abs(b["pDS"] - a["DS"]) < 1e-6
                pds_eq_prev_ds[1] += 1

    moving = [(c, s) for c, s in zip(sp_calc, sp_field) if c > 5]
    print(f"  S   vs computed speed (km/h): r={pearson(sp_calc, sp_field):.3f}; "
          f"median S/speed while moving={med([s / c for c, s in moving]):.2f} "
          f"(~1.00 => km/h, ~0.28 => m/s, ~0.62 => mph)")
    print(f"  D   vs computed bearing: median |diff|={med(brg_diff):.1f} deg over {len(brg_diff)} moving steps "
          f"(small => heading in degrees)")
    for k, (up, n) in mono.items():
        if n:
            print(f"  {k:3s} non-decreasing within a file: {100 * up / n:.0f}% of {n} steps")
    print(f"  DS  vs cumulative distance in file: r={pearson(ds_cum, ds_vals):.3f}; "
          f"median DS/cum_m={med([d / c for c, d in zip(ds_cum, ds_vals) if c > 50]):.4f} "
          f"(~1 => m, ~0.001 => km)")
    print(f"  DS  vs computed speed: r={pearson([a for a, _ in ds_sp], [b for _, b in ds_sp]):.3f}")
    print(f"  pDS vs step distance: r={pearson(step_m, pds_vals):.3f}; "
          f"median pDS/step_m={med([p / s for s, p in zip(step_m, pds_vals) if s > 5]):.4f}")
    print(f"  pDS equals previous point's DS: "
          f"{100 * pds_eq_prev_ds[0] / max(pds_eq_prev_ds[1], 1):.0f}% of {pds_eq_prev_ds[1]} steps")

    # Increments (offset-free) and continuity across consecutive files of one drive
    for k in ("DS", "pDS"):
        inc, stp = [], []
        for f in files_pts:
            for a, b in zip(f, f[1:]):
                dt = (b["T"] - a["T"]) / 1000
                if k in a and k in b and 0.5 <= dt <= 10:
                    inc.append(b[k] - a[k])
                    stp.append(hav_m((a["LT"], a["LG"]), (b["LT"], b["LG"])))
        moving_ratio = [i / s for i, s in zip(inc, stp) if s > 5]
        print(f"  {k:3s} increment vs step distance: r={pearson(stp, inc):.3f}; "
              f"median inc/step_m={med(moving_ratio):.4f} (~0.001 => km counter, ~1 => m)")
        cont = reset = 0
        for a, b in zip(order, order[1:]):
            if k in a[-1] and k in b[0] and (b[0]["T"] - a[-1]["T"]) <= 10_000 and a[-1][k] > 0:
                if b[0][k] >= a[-1][k] * 0.99:
                    cont += 1
                elif b[0][k] <= a[-1][k] * 0.05:
                    reset += 1
        print(f"  {k:3s} at a 5-min file boundary within a drive: continues={cont}, resets to ~0={reset}")

# --------------------------------------------- GPS vs video time coverage
vids = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(ROOT)
              for f in fs if f.lower().endswith(".mp4"))
vid_t = sorted(int(m.group(1)) for v in vids
               if (m := re.search(r"_(\d{13})\.mp4$", v)))
if vid_t and all_t:
    covered = sum(1 for t in vid_t if any(abs(t - g) < 90_000 for g in all_t))
    print(f"\n  videos with a GPS point within 90s of their start: {covered}/{len(vid_t)}")

# ------------------------------------------------------------------ videos
print("\n== Videos")
bad, durs, sizes, codecs = [], [], [], collections.Counter()
for v in vids:
    sizes.append(os.path.getsize(v))
    if not mp4_complete(v):
        bad.append(os.path.basename(v))
        continue
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate",
                        "-of", "json", v], capture_output=True, text=True)
    try:
        j = json.loads(r.stdout)
        durs.append(float(j["format"]["duration"]))
        codecs[" | ".join(f"{s['codec_type']}:{s['codec_name']}"
                          f"{' ' + str(s.get('width')) + 'x' + str(s.get('height')) + '@' + s.get('r_frame_rate', '') if s['codec_type'] == 'video' else ''}"
                          for s in j["streams"])] += 1
    except Exception as e:
        bad.append(f"{os.path.basename(v)} ({e})")
print(f"  mp4 files: {len(vids)}, complete+probed: {len(durs)}, bad: {len(bad)}")
for b in bad[:10]:
    print(f"    bad: {b}")
if durs:
    print(f"  duration s: min={min(durs):.1f} median={statistics.median(durs):.1f} "
          f"max={max(durs):.1f} total={sum(durs) / 60:.1f} min")
    print(f"  size MB: min={min(sizes) / 1e6:.1f} median={statistics.median(sizes) / 1e6:.1f} "
          f"max={max(sizes) / 1e6:.1f} total={sum(sizes) / 1e6:.0f}")
    print(f"  bitrate kbps (median): {statistics.median(s * 8 / d / 1000 for s, d in zip(sizes, durs) if d):.0f}")
for c, n in codecs.items():
    print(f"  streams: {n}x {c}")

# ------------------------------------------------------------------ other
print("\n== Other files")
for dp, _, fs in os.walk(ROOT):
    for f in fs:
        ext = os.path.splitext(f)[1].lower()
        if ext in (".gps", ".mp4", ".jpeg", ".jpg", ".gpx", ".part"):
            continue
        p = os.path.join(dp, f)
        ft = subprocess.run(["file", "-b", p], capture_output=True, text=True).stdout.strip()
        name = re.sub(r"\d{2}-[A-Za-z]{3}-\d{4}", "<date>", f)
        print(f"  {name}: {os.path.getsize(p)} B, {ft[:60]}")
