#!/usr/bin/env python3
"""Build a privacy-safe structural report from an explore-* folder.

Usage: python3 sanitize.py explore-YYYYmmdd-HHMMSS/ [--mask-times]

Writes <folder>/sanitized/. Nothing in there contains GPS values, coordinates,
speeds or image/video pixels - only formats, box/track types, tag NAMES,
counts and sizes. Raw files are read locally and never copied.
--mask-times additionally masks dates/times (they reveal when you drove).
"""
import collections
import os
import re
import struct
import subprocess
import sys

SRC = os.path.abspath(sys.argv[1])
MASK_TIMES = "--mask-times" in sys.argv
OUT = os.path.join(SRC, "sanitized")
os.makedirs(OUT, exist_ok=True)

SENSITIVE = re.compile(r"gps|locat|latit|longit|\blat\b|\blon\b|coord|altitude|speed|"
                       r"heading|bearing|position|geo|nmea|xyz|accel", re.I)
SAFE = re.compile(r"file type|mime|width|height|duration|frame rate|frame count|"
                  r"compressor|handler|brand|compatible|codec|sample rate|channels|"
                  r"bits|resolution|format|encoder|make|model|software|version|"
                  r"track id|media|rotation|pixel|bit ?rate|profile|level|"
                  r"nb_frames|time_base|stream|index|tag_string|type", re.I)
TIME = re.compile(r"\d{4}[-:_/]?\d{2}[-:_/]?\d{2}[ T_-]?\d{2}[:_]?\d{2}[:_]?\d{2}|"
                  r"\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}")


def scrub(text):
    """Last-line defence applied to everything written to sanitized/."""
    text = re.sub(r"[-+]?\d+\.\d{3,}", "<num>", text)              # decimals w/ precision
    text = re.sub(r"\d+\s*deg\s*\d+'\s*[\d.]+\"\s*[NSEW]?", "<dms>", text)
    text = re.sub(r"(?<![\w.])\d{7,}(?![\w.])", "<bignum>", text)  # raw ints / packed coords
    text = re.sub(r"[-+]\d{1,3}\.\d+[-+]\d{1,3}\.\d+/?", "<iso6709>", text)
    if MASK_TIMES:
        text = TIME.sub("<time>", text)
    return text


def write(name, text):
    with open(os.path.join(OUT, name), "w") as f:
        f.write(scrub(text))
    print("wrote sanitized/" + name)


# ------------------------------------------------ plain logs / listings
for name in ["explore.log", "files.tsv", "propfind.xml"]:
    p = os.path.join(SRC, name)
    if os.path.exists(p):
        write(name, open(p, errors="replace").read())

# ------------------------------------------------ exiftool / ffprobe: tag names only
def tag_report(path, sep):
    seen = collections.Counter()
    order, kept = [], {}
    for line in open(path, errors="replace"):
        if sep not in line:
            continue
        key, val = line.split(sep, 1)
        key, val = key.strip(), val.strip()
        if key not in seen:
            order.append(key)
        seen[key] += 1
        if SENSITIVE.search(key):
            kept[key] = "<masked>"
        elif SAFE.search(key) and not SENSITIVE.search(val):
            kept[key] = val[:80]
        else:
            kept[key] = f"<value {len(val)} chars>"
    return "\n".join(f"{seen[k]:6d}x  {k} = {kept[k]}" for k in order) + "\n"


if os.path.exists(os.path.join(SRC, "exiftool.txt")):
    write("exiftool_tags.txt", tag_report(os.path.join(SRC, "exiftool.txt"), " : "))
if os.path.exists(os.path.join(SRC, "ffprobe.txt")):
    write("ffprobe_tags.txt", tag_report(os.path.join(SRC, "ffprobe.txt"), "="))

# ------------------------------------------------ per-sample structure
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts",
              b"dinf", b"mvex", b"moof", b"traf", b"ilst"}
MARKERS = [b"freeGPS", b"GPS ", b"gpmd", b"$GPRMC", b"$GNRMC", b"$GPGGA",
           b"$GNGGA", b"camm", b"tx3g", b"gps0", b"GPSDATA", b"LIGOGPSINFO"]


def ascii_tokens(data, n=6):
    toks = re.findall(rb"[A-Za-z_ ]{4,}", data)
    return [t.decode().strip() for t in toks[:n] if t.strip()]


def mp4_tree(f, start, end, depth, lines):
    pos = start
    while pos + 8 <= end and len(lines) < 400:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        size, typ = struct.unpack(">I4s", hdr)
        hlen = 8
        if size == 1:
            size = struct.unpack(">Q", f.read(8))[0]
            hlen = 16
        elif size == 0:
            size = end - pos
        if size < hlen or pos + size > end + 8:
            lines.append(f"{'  ' * depth}<bad box at {pos}>")
            break
        t = typ.decode("latin1")
        extra = ""
        if typ == b"hdlr":
            f.seek(pos + hlen + 8)
            extra = f" handler={f.read(4).decode('latin1')!r}"
        elif typ == b"stsd":
            f.seek(pos + hlen + 8 + 4)
            extra = f" sample_entry={f.read(4).decode('latin1')!r}"
        elif typ not in CONTAINERS and typ != b"mdat" and size - hlen <= 1 << 20:
            f.seek(pos + hlen)
            toks = ascii_tokens(f.read(size - hlen))
            extra = f" strings={toks}" if toks else ""
        lines.append(f"{'  ' * depth}{t:4s} size={size}{extra}")
        if typ in CONTAINERS:
            mp4_tree(f, pos + hlen, pos + size, depth + 1, lines)
        elif typ == b"meta":
            mp4_tree(f, pos + hlen + 4, pos + size, depth + 1, lines)
        pos += size


def marker_counts(path):
    counts = collections.Counter()
    tail = b""
    with open(path, "rb") as f:
        while chunk := f.read(8 << 20):
            buf = tail + chunk
            for m in MARKERS:
                counts[m.decode()] += buf.count(m)
            tail = buf[-16:]
    return {k: v for k, v in counts.items() if v}


def text_shapes(path):
    shapes = collections.Counter()
    n = 0
    for line in open(path, errors="replace"):
        n += 1
        line = line.rstrip("\r\n")
        sep = "\t" if "\t" in line else ","
        fields = line.split(sep)
        shape = re.sub(r"\d", "9", line)                       # digits -> 9
        shape = re.sub(r"(?<![A-Za-z])[NSEW](?=[-\d9.,\t ]|$)", "H", shape)  # hemisphere
        shape = shape.replace("\t", "<TAB>")[:160]
        shapes[f"{shape}  ({len(fields)} fields)"] += 1
    return n, shapes.most_common(15)


report = []
sdir = os.path.join(SRC, "samples")
for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
    p = os.path.join(sdir, name)
    size = os.path.getsize(p)
    ftype = subprocess.run(["file", "-b", p], capture_output=True, text=True).stdout.strip()
    report.append(f"\n### {name}\nsize={size}\nfile: {ftype[:200]}")
    with open(p, "rb") as f:
        head = f.read(12)
    if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide"):
        lines = []
        with open(p, "rb") as f:
            mp4_tree(f, 0, size, 0, lines)
        report.append("mp4 boxes:\n" + "\n".join(lines))
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=index,codec_type,codec_name,codec_tag_string,width,height,"
                            "r_frame_rate,nb_frames,duration:stream_tags=handler_name",
                            "-of", "compact", p], capture_output=True, text=True)
        report.append("streams:\n" + r.stdout + r.stderr[:500])
    elif "text" in ftype.lower():
        n, shapes = text_shapes(p)
        report.append(f"text lines={n}\nline shapes (digits->9, values dropped):\n" +
                      "\n".join(f"  {c:6d}x {s}" for s, c in shapes))
    elif "image" in ftype.lower():
        report.append("image (pixels not inspected)")
    report.append(f"embedded markers: {marker_counts(p)}")

write("samples_report.txt", "\n".join(report) + "\n")

# ------------------------------------------------ self-check
leaks = 0
for name in os.listdir(OUT):
    txt = open(os.path.join(OUT, name), errors="replace").read()
    leaks += len(re.findall(r"\d+\.\d{3,}|\$G[PN](RMC|GGA)\S{10,}", txt))
print(f"self-check: {leaks} coordinate-like strings remain in sanitized/")
