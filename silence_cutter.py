#!/usr/bin/env python3
"""
silence_cutter.py - remove silent sections from a video and stitch what's left together.

Requires: Python 3.8+ and ffmpeg/ffprobe on your PATH (https://ffmpeg.org/download.html)

Examples
--------
    python silence_cutter.py my_video.mp4
    python silence_cutter.py my_video.mp4 -o final.mp4
    python silence_cutter.py my_video.mp4 --threshold -30 --min-silence 0.4 --padding 0.15
    python silence_cutter.py my_video.mp4 --dry-run          # just show what would be cut
    python silence_cutter.py big_video.mp4 --encoder nvenc   # GPU encode (NVIDIA)

Tuning
------
  --threshold    How quiet counts as "silence", in dB. Closer to 0 is more aggressive.
                 Noisy room / background hum?  Try -30 or -25.
                 Very clean audio?             Try -40 or -45.
  --min-silence  Only cut gaps at least this long (seconds). Lower = tighter, choppier edit.
  --padding      Breathing room kept on each side of a cut (seconds), so words don't get clipped.

Big files
---------
  Videos with many cuts are rendered in chunks (--batch-size clips at a time) and the chunks
  are joined at the end, which keeps memory use flat no matter how long the video is.
  Rendering is CPU-heavy; --encoder nvenc / qsv / videotoolbox uses your GPU instead.
  Make sure you have free disk space of roughly 2-3x the size of the finished video.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- helpers

def die(msg):
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


def fmt_time(seconds):
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:d}:{s:05.2f}"


def fmt_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024 or unit == "TB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{int(nbytes)} B"
        nbytes /= 1024


def check_tools():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"'{tool}' was not found on your PATH. Install ffmpeg first: https://ffmpeg.org/download.html")


def probe(path):
    """Return (duration_seconds, has_video, has_audio)."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        die(f"ffprobe couldn't read '{path}':\n{result.stderr.strip()}")
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    has_video = any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1
                    for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError):
        die(f"Couldn't determine the duration of '{path}'.")
    return duration, has_video, has_audio


def run_ffmpeg(cmd, what):
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(f"ffmpeg failed while {what} (see message above).")


# --------------------------------------------------------------------------- detection

def detect_silences(path, threshold_db, min_silence, duration):
    """Use ffmpeg's silencedetect filter. Returns a list of (start, end) silent spans."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
        "-vn", "-map", "0:a:0",
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"Silence detection failed:\n{result.stderr.strip()[-1500:]}")

    silences, pending_start = [], None
    for line in result.stderr.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            pending_start = max(0.0, float(m.group(1)))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and pending_start is not None:
            silences.append((pending_start, min(duration, float(m.group(1)))))
            pending_start = None

    # File ended while still silent (no silence_end is printed in that case)
    if pending_start is not None:
        silences.append((pending_start, duration))
    return silences


def build_keep_segments(silences, duration, padding, min_keep):
    """Invert the silent spans into the spans we want to keep."""
    edge = 0.001
    shrunk = []
    for s, e in silences:
        # Leave a little silence on the inside edges of each cut so speech isn't clipped,
        # but don't pad the very start/end of the file.
        s2 = s if s <= edge else s + padding
        e2 = e if e >= duration - edge else e - padding
        if e2 - s2 > 0:
            shrunk.append((s2, e2))

    keeps, cursor = [], 0.0
    for s, e in shrunk:
        if s - cursor > 0:
            keeps.append((cursor, s))
        cursor = e
    if duration - cursor > 0:
        keeps.append((cursor, duration))

    return [(a, b) for a, b in keeps if b - a >= min_keep]


# --------------------------------------------------------------------------- rendering

def video_args(args):
    """Encoder settings for the chosen encoder."""
    q = str(args.crf)
    if args.encoder == "cpu":
        return ["-c:v", "libx264", "-preset", args.preset, "-crf", q, "-pix_fmt", "yuv420p"]
    if args.encoder == "nvenc":      # NVIDIA GPUs
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0",
                "-pix_fmt", "yuv420p"]
    if args.encoder == "qsv":        # Intel Quick Sync
        return ["-c:v", "h264_qsv", "-global_quality", q, "-pix_fmt", "nv12"]
    if args.encoder == "videotoolbox":  # Apple Silicon / macOS
        return ["-c:v", "h264_videotoolbox", "-q:v", "65", "-pix_fmt", "yuv420p"]
    die(f"Unknown encoder '{args.encoder}'.")


def build_filter_script(keeps, offset=0.0):
    """
    One filtergraph: split the video/audio once per kept segment, trim each,
    then concat them all back-to-back. Using the concat *filter* (rather than
    concatenating files) keeps audio and video in sync at every join.
    `offset` is subtracted from all times (used when the input was seeked).
    """
    segs = [(max(0.0, a - offset), max(0.0, b - offset)) for a, b in keeps]
    n = len(segs)
    lines = []
    if n == 1:
        a, b = segs[0]
        lines.append(f"[0:v:0]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[outv];")
        lines.append(f"[0:a:0]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS[outa]")
        return "\n".join(lines)

    vsplit = "".join(f"[vs{i}]" for i in range(n))
    asplit = "".join(f"[as{i}]" for i in range(n))
    lines.append(f"[0:v:0]split={n}{vsplit};")
    lines.append(f"[0:a:0]asplit={n}{asplit};")
    for i, (a, b) in enumerate(segs):
        lines.append(f"[vs{i}]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[v{i}];")
        lines.append(f"[as{i}]atrim=start={a:.4f}:end={b:.4f},asetpts=PTS-STARTPTS[a{i}];")
    joined = "".join(f"[v{i}][a{i}]" for i in range(n))
    lines.append(f"{joined}concat=n={n}:v=1:a=1[outv][outa]")
    return "\n".join(lines)


def render_direct(path, out_path, keeps, args):
    """Single ffmpeg pass. Used when there are only a handful of clips."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "filter.txt"
        script.write_text(build_filter_script(keeps), encoding="utf-8")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
            "-i", str(path),
            "-/filter_complex", str(script),
            "-map", "[outv]", "-map", "[outa]",
            *video_args(args),
            "-c:a", "aac", "-b:a", args.audio_bitrate,
            "-movflags", "+faststart",
            str(out_path),
        ]
        run_ffmpeg(cmd, "rendering")


def render_chunked(path, out_path, keeps, args):
    """
    Render `batch_size` clips at a time (seeking straight to each chunk, so the whole
    file is never decoded at once), then join the chunks without re-encoding video.
    Chunk audio is stored losslessly (FLAC) and encoded to AAC once at the end, so
    there are no audio glitches where chunks meet.
    """
    batches = [keeps[i:i + args.batch_size] for i in range(0, len(keeps), args.batch_size)]
    # Temp files live next to the output so a big video doesn't fill the system drive.
    with tempfile.TemporaryDirectory(dir=out_path.parent, prefix=".silencecut_") as tmp:
        tmp = Path(tmp)
        parts = []
        for i, batch in enumerate(batches, 1):
            window_start, window_end = batch[0][0], batch[-1][1]
            script = tmp / f"filter_{i:04d}.txt"
            script.write_text(build_filter_script(batch, offset=window_start), encoding="utf-8")
            part = tmp / f"part_{i:04d}.mkv"
            print(f"  Chunk {i}/{len(batches)}  ({fmt_time(window_start)} -> {fmt_time(window_end)}, "
                  f"{len(batch)} clips)")
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
                "-ss", f"{window_start:.4f}", "-t", f"{window_end - window_start + 0.5:.4f}",
                "-i", str(path),
                "-/filter_complex", str(script),
                "-map", "[outv]", "-map", "[outa]",
                *video_args(args),
                "-c:a", "flac",
                str(part),
            ]
            run_ffmpeg(cmd, f"rendering chunk {i}/{len(batches)}")
            parts.append(part)
            print()

        listing = tmp / "parts.txt"
        with open(listing, "w", encoding="utf-8") as f:
            for part in parts:
                safe = part.resolve().as_posix().replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        print("  Joining chunks and encoding final audio...")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", args.audio_bitrate,
            "-movflags", "+faststart",
            str(out_path),
        ]
        run_ffmpeg(cmd, "joining chunks")


# --------------------------------------------------------------------------- main flow

def process(path, out_path, args):
    print(f"\n=== {path.name} ({fmt_size(path.stat().st_size)}) ===")
    duration, has_video, has_audio = probe(path)
    if not has_video:
        die(f"'{path.name}' has no video stream.")
    if not has_audio:
        die(f"'{path.name}' has no audio track, so there's no way to detect silence.")

    print(f"Length: {fmt_time(duration)}  |  detecting silence "
          f"(threshold {args.threshold} dB, min {args.min_silence}s)...")
    silences = detect_silences(path, args.threshold, args.min_silence, duration)
    keeps = build_keep_segments(silences, duration, args.padding, args.min_clip)

    if not silences:
        print("No silences found at these settings. Nothing to cut. "
              "(Try a higher threshold like -30 or a smaller --min-silence.)")
        return
    if not keeps:
        print("Everything was detected as silence at these settings. "
              "Try a lower threshold like -45.")
        return

    new_duration = sum(b - a for a, b in keeps)
    removed = duration - new_duration
    chunked = len(keeps) > args.batch_size
    print(f"Found {len(silences)} silent gap(s). Keeping {len(keeps)} clip(s).")
    print(f"Result: {fmt_time(new_duration)}  (removing {fmt_time(removed)}, "
          f"{removed / duration * 100:.0f}% of the video)")

    if args.dry_run:
        print("\nClips that would be kept:")
        for i, (a, b) in enumerate(keeps, 1):
            print(f"  {i:>3}.  {fmt_time(a)} -> {fmt_time(b)}   ({b - a:.2f}s)")
        n_chunks = -(-len(keeps) // args.batch_size)
        print(f"\nWould render in {n_chunks} chunk(s)." if chunked else "\nWould render in a single pass.")
        return

    if out_path.exists() and not args.overwrite:
        die(f"'{out_path}' already exists. Use --overwrite or pick a different -o name.")

    # Rough disk-space sanity check (warning only)
    kept_fraction = new_duration / duration
    need = path.stat().st_size * kept_fraction * (2.5 if chunked else 1.3)
    free = shutil.disk_usage(out_path.parent).free
    if free < need:
        print(f"WARNING: only {fmt_size(free)} free on the output drive; this job may need "
              f"around {fmt_size(need)}.")

    mode = f"in {-(-len(keeps) // args.batch_size)} chunks" if chunked else "in a single pass"
    print(f"Rendering {mode} to {out_path} ...\n")
    (render_chunked if chunked else render_direct)(path, out_path, keeps, args)
    print(f"\nDone: {out_path}  ({fmt_size(out_path.stat().st_size)})")


def main():
    p = argparse.ArgumentParser(
        description="Remove silent sections from a video and join the rest together.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("inputs", nargs="*", help="Video file(s) to process")
    p.add_argument("-o", "--output", help="Output file (only when processing a single input)")
    p.add_argument("-t", "--threshold", type=float, default=-35.0,
                   help="Silence threshold in dB (default: -35)")
    p.add_argument("-s", "--min-silence", type=float, default=0.5,
                   help="Minimum silence length to cut, in seconds (default: 0.5)")
    p.add_argument("-p", "--padding", type=float, default=0.1,
                   help="Seconds of silence kept on each side of a cut (default: 0.1)")
    p.add_argument("--min-clip", type=float, default=0.1,
                   help="Drop kept clips shorter than this, e.g. stray clicks (default: 0.1)")
    p.add_argument("--encoder", choices=["cpu", "nvenc", "qsv", "videotoolbox"], default="cpu",
                   help="cpu = libx264 (default, best quality/size); nvenc = NVIDIA GPU; "
                        "qsv = Intel GPU; videotoolbox = Mac")
    p.add_argument("--crf", type=int, default=18, help="Quality, lower = better (default: 18)")
    p.add_argument("--preset", default="medium",
                   help="x264 speed preset: ultrafast ... veryslow (cpu encoder only, default: medium)")
    p.add_argument("--audio-bitrate", default="192k", help="AAC bitrate (default: 192k)")
    p.add_argument("--batch-size", type=int, default=40,
                   help="Clips rendered per chunk on big jobs (default: 40)")
    p.add_argument("--dry-run", action="store_true", help="Show what would be cut without rendering")
    p.add_argument("--overwrite", action="store_true", help="Overwrite the output file if it exists")
    args = p.parse_args()

    check_tools()

    inputs = list(args.inputs)
    if not inputs:  # friendly fallback if launched by double-click / no args
        typed = input("Path to video file (you can drag it into this window): ").strip().strip('"').strip("'")
        if not typed:
            die("No input file given.")
        inputs = [typed]

    if args.output and len(inputs) > 1:
        die("-o/--output only works with a single input file.")
    if args.padding < 0 or args.min_silence <= 0 or args.batch_size < 1:
        die("--padding must be >= 0, --min-silence > 0, and --batch-size >= 1.")

    for name in inputs:
        path = Path(name).expanduser()
        if not path.is_file():
            die(f"File not found: {path}")
        out_path = Path(args.output).expanduser() if args.output else \
            path.with_name(f"{path.stem}_trimmed{path.suffix or '.mp4'}")
        if out_path.resolve() == path.resolve():
            die("Output would overwrite the input file. Choose a different -o name.")
        process(path, out_path, args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
