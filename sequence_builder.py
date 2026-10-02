#!/usr/bin/env python3
"""Sequence Builder helpers for Silence Cutter — The Unbound Edition.

The module is intentionally separate from the GUI.  It stores a lightweight
rough-cut sequence, imports curated moments from Footage Analysis sidecars,
exports human-readable edit lists and Resolve-friendly FCPXML, and can render a
simple hard-cut assembly with FFmpeg.

It is *not* a nonlinear editor.  No transitions, color, titles, or mixing live
here; DaVinci Resolve remains the finishing room.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import quote
from xml.etree import ElementTree as ET

from unbound_utils import atomic_write_json, seconds_to_clock


def _creationflags() -> int:
    """Hide ffmpeg/ffprobe console windows when launched from the GUI on Windows."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


@dataclass
class SequenceItem:
    id: str
    source_video: str
    start: float
    end: float
    title: str = ""
    characters: list[str] = field(default_factory=list)
    category: str = ""
    chapter: str = ""
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    source_index: str = ""
    source_moment_id: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))


class SequenceError(RuntimeError):
    pass


def _safe_name(text: str) -> str:
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in (text or "sequence"))
    keep = " ".join(keep.split()).strip().replace(" ", "_")
    return keep or "sequence"


def stable_item_id(source: str, start: float, end: float, title: str = "") -> str:
    key = f"{Path(source).as_posix()}|{start:.3f}|{end:.3f}|{title}"
    return "seq-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def make_item(source_video: str | Path, start: float, end: float, **kwargs) -> SequenceItem:
    source = str(Path(source_video))
    start = max(0.0, float(start))
    end = max(start, float(end))
    if end - start < 0.04:
        raise SequenceError("A sequence clip must be at least 0.04 seconds long.")
    # hashlib (not hash()) so the same clip gets the same ID in every session;
    # Python's built-in string hash is randomized per process.
    uid = kwargs.pop("id", "") or stable_item_id(source, start, end, kwargs.get("title", ""))
    return SequenceItem(id=uid, source_video=source, start=start, end=end, **kwargs)


def items_from_footage_index(
    index_path: str | Path,
    statuses: Iterable[str] = ("KEEP",),
    include_unreviewed: bool = False,
    skipped: list[str] | None = None,
) -> list[SequenceItem]:
    """Import matching moments as sequence items.

    Moments that can't become valid clips are skipped; pass a `skipped` list to
    collect a reason for each instead of losing them silently.
    """
    index_path = Path(index_path)
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "moments" not in data:
        raise SequenceError("That JSON file is not a Silence Slicer footage index.")
    source_video = data.get("source_video", "")
    if not source_video:
        raise SequenceError("The footage index does not contain a source video path.")

    wanted = {str(x).upper() for x in statuses}
    out: list[SequenceItem] = []
    for n, raw in enumerate(data.get("moments", []), 1):
        if not isinstance(raw, dict):
            if skipped is not None:
                skipped.append(f"{index_path.name} moment #{n}: not an object")
            continue
        status = str(raw.get("status", "UNREVIEWED")).upper()
        if status not in wanted and not (include_unreviewed and status == "UNREVIEWED"):
            continue
        try:
            item = make_item(
                source_video,
                raw.get("start", 0.0),
                raw.get("end", 0.0),
                title=raw.get("title", "") or str(raw.get("text", ""))[:82],
                characters=list(raw.get("speakers", []) or []),
                category=raw.get("category", ""),
                chapter=raw.get("chapter", ""),
                notes=raw.get("notes", ""),
                tags=list(raw.get("tags", []) or []),
                source_index=str(index_path),
                source_moment_id=raw.get("id", ""),
            )
            out.append(item)
        except Exception as exc:
            if skipped is not None:
                label = raw.get("title") or raw.get("id") or f"moment #{n}"
                skipped.append(f"{index_path.name} {label}: {exc}")
    return out


def save_sequence(path: str | Path, items: Sequence[SequenceItem], name: str = "Sequence") -> Path:
    path = Path(path)
    payload = {
        "schema_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "item_count": len(items),
        "duration_seconds": sum(i.duration for i in items),
        "items": [asdict(i) | {"duration": i.duration} for i in items],
    }
    return atomic_write_json(path, payload)


def load_sequence(path: str | Path, skipped: list[str] | None = None) -> tuple[str, list[SequenceItem]]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "items" not in data:
        raise SequenceError("That file is not a Silence Slicer sequence.")
    out: list[SequenceItem] = []
    allowed = set(SequenceItem.__dataclass_fields__)
    for n, raw in enumerate(data.get("items", []), 1):
        if not isinstance(raw, dict):
            if skipped is not None:
                skipped.append(f"item #{n}: not an object")
            continue
        clean = {k: v for k, v in raw.items() if k in allowed}
        try:
            out.append(SequenceItem(**clean))
        except TypeError as exc:
            if skipped is not None:
                skipped.append(f"item #{n}: {exc}")
    return str(data.get("name", path.stem)), out


def export_csv(path: str | Path, items: Sequence[SequenceItem]) -> Path:
    path = Path(path)
    cursor = 0.0
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "order", "timeline_in", "timeline_out", "source_in", "source_out",
            "duration_seconds", "title", "characters", "category", "chapter",
            "tags", "notes", "source_video",
        ])
        for n, item in enumerate(items, 1):
            writer.writerow([
                n,
                seconds_to_clock(cursor, True),
                seconds_to_clock(cursor + item.duration, True),
                seconds_to_clock(item.start, True),
                seconds_to_clock(item.end, True),
                f"{item.duration:.3f}",
                item.title,
                "; ".join(item.characters),
                item.category,
                item.chapter,
                "; ".join(item.tags),
                item.notes,
                item.source_video,
            ])
            cursor += item.duration
    return path


def export_txt(path: str | Path, items: Sequence[SequenceItem], name: str = "Sequence") -> Path:
    path = Path(path)
    lines = [f"{name}", "=" * len(name), ""]
    cursor = 0.0
    for n, item in enumerate(items, 1):
        lines.extend([
            f"{n:02d}. TIMELINE {seconds_to_clock(cursor)} - {seconds_to_clock(cursor + item.duration)}",
            f"    SOURCE   {seconds_to_clock(item.start)} - {seconds_to_clock(item.end)}  ({item.duration:.1f}s)",
            f"    {item.title or '(untitled)'}",
            f"    File: {item.source_video}",
            f"    Characters: {', '.join(item.characters) if item.characters else '—'}",
            f"    Category: {item.category or '—'}    Chapter: {item.chapter or '—'}",
            f"    Notes: {item.notes or '—'}",
            "",
        ])
        cursor += item.duration
    lines.append(f"TOTAL: {seconds_to_clock(cursor)}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _file_uri(path: str | Path) -> str:
    p = Path(path).resolve()
    # Resolve/FCPXML expects a file URL. quote preserves slashes while escaping spaces.
    raw = p.as_posix()
    if os.name == "nt" and not raw.startswith("/"):
        raw = "/" + raw
    return "file://" + quote(raw, safe="/:~!$&'()*+,;=@")


def _fcpxml_media_uri(source: str | Path, xml_dir: str | Path) -> str:
    """Prefer a relative media URL when the source lives beside the FCPXML."""
    source_path = Path(source).resolve()
    xml_root = Path(xml_dir).resolve()
    try:
        rel = source_path.relative_to(xml_root)
        return "./" + quote(rel.as_posix(), safe="/:~!$&'()*+,;=@")
    except ValueError:
        return _file_uri(source_path)

# Frame rates the FCPXML export and rough render accept. NTSC rates are exact
# rationals so long timelines don't drift (29.97 is really 30000/1001).
FPS_CHOICES: dict[str, Fraction] = {
    "23.976": Fraction(24000, 1001),
    "24": Fraction(24),
    "25": Fraction(25),
    "29.97": Fraction(30000, 1001),
    "30": Fraction(30),
    "50": Fraction(50),
    "59.94": Fraction(60000, 1001),
    "60": Fraction(60),
}


def parse_fps(value) -> Fraction:
    """Accept "29.97", 30, "30000/1001", or a Fraction; return an exact rational."""
    if isinstance(value, Fraction):
        fps = value
    else:
        text = str(value).strip()
        if text in FPS_CHOICES:
            fps = FPS_CHOICES[text]
        else:
            try:
                fps = Fraction(text)
            except (ValueError, ZeroDivisionError):
                raise SequenceError(f"Unrecognized frame rate: {value!r}") from None
            # Snap near-NTSC decimals like 29.97002997 to the exact rational.
            for exact in FPS_CHOICES.values():
                if abs(float(fps) - float(exact)) < 0.001:
                    fps = exact
                    break
    if fps not in FPS_CHOICES.values():
        raise SequenceError("Supported frame rates: " + ", ".join(FPS_CHOICES))
    return fps


def fps_label(fps) -> str:
    fps = parse_fps(fps)
    for label, exact in FPS_CHOICES.items():
        if exact == fps:
            return label
    return str(fps)


def _ffmpeg_fps(fps) -> str:
    fps = parse_fps(fps)
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def _frames(seconds: float, fps: Fraction) -> int:
    return max(0, round(Fraction(str(float(seconds))) * fps))


def _fcpx_frames(frames: int, fps: Fraction) -> str:
    """Express a whole number of frames as an FCPXML rational time string."""
    if frames <= 0:
        return "0s"
    t = Fraction(frames) / fps
    return f"{t.numerator}s" if t.denominator == 1 else f"{t.numerator}/{t.denominator}s"


def _fcpx_time(seconds: float, fps=30) -> str:
    fps = parse_fps(fps)
    return _fcpx_frames(_frames(seconds, fps), fps)


def probe_duration(ffprobe: str, source: str | Path) -> float | None:
    """Best available playable duration in seconds.

    Prefer the video-stream duration when available, then fall back to the
    container duration. Resolve links against the actual media extent, so the
    FCPXML asset must not be declared longer than the file.
    """
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration:format=duration",
        "-of", "json", str(source),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=30,
            creationflags=_creationflags(),
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        streams = data.get("streams") or []
        if streams:
            value = streams[0].get("duration")
            if value not in (None, "", "N/A"):
                return float(value)
        value = (data.get("format") or {}).get("duration")
        return float(value) if value not in (None, "", "N/A") else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _floor_frames(seconds: float, fps: Fraction) -> int:
    """Largest whole frame that does not extend beyond the media duration."""
    return max(0, int(math.floor(float(seconds) * float(fps) + 1e-7)))


def export_fcpxml(
    path: str | Path,
    items: Sequence[SequenceItem],
    name: str = "Silence Slicer Sequence",
    fps=30,
    width: int = 1920,
    height: int = 1080,
    ffprobe: str | None = None,
) -> Path:
    """Write a simple hard-cut FCPXML timeline importable by DaVinci Resolve.

    Resolve is strict about source-media extents. When ffprobe is available we
    declare each asset at its real playable duration and clamp sub-frame edge
    overshoot to that duration. Large over-runs are rejected as a likely
    transcript/source mismatch instead of producing an offline Resolve timeline.
    """
    if not items:
        raise SequenceError("The sequence is empty.")
    fps = parse_fps(fps)

    path = Path(path)
    root = ET.Element("fcpxml", version="1.8")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", {
        "id": "r1",
        "name": f"FFVideoFormat{height}p{fps_label(fps).replace('.', '')}",
        "frameDuration": _fcpx_frames(1, fps),
        "width": str(width),
        "height": str(height),
        "colorSpace": "1-1-1 (Rec. 709)",
    })

    source_ids: dict[str, str] = {}
    source_duration_frames: dict[str, int | None] = {}
    max_ends: dict[str, float] = {}
    for item in items:
        key = str(Path(item.source_video).resolve())
        max_ends[key] = max(max_ends.get(key, 0.0), float(item.end))

    for idx, source in enumerate(max_ends, 2):
        rid = f"r{idx}"
        source_ids[source] = rid
        real = probe_duration(ffprobe, source) if ffprobe else None

        if real is not None and real > 0:
            asset_frames = max(1, _floor_frames(real, fps))
            source_duration_frames[source] = asset_frames

            requested_end = max_ends[source]
            tolerance = max(0.10, 2.0 / float(fps))
            if requested_end > real + tolerance:
                raise SequenceError(
                    "Resolve export stopped because a sequence range extends "
                    f"past its source media.\n\n"
                    f"Source: {source}\n"
                    f"Media duration: {real:.3f}s\n"
                    f"Latest requested source time: {requested_end:.3f}s\n\n"
                    "This usually means the SRT/footage index belongs to a "
                    "different video cut."
                )
        else:
            asset_frames = max(1, _frames(max_ends[source] + 1.0, fps))
            source_duration_frames[source] = None

        # Use the FCPXML 1.8 asset form for broad DaVinci Resolve
        # compatibility.  Resolve has historically been more reliable with the
        # source URL directly on the asset than with the newer media-rep wrapper.
        ET.SubElement(resources, "asset", {
            "id": rid,
            "name": Path(source).name,
            "start": "0s",
            "duration": _fcpx_frames(asset_frames, fps),
            "hasVideo": "1",
            "hasAudio": "1",
            "format": "r1",
            "audioSources": "1",
            "audioChannels": "2",
            "audioRate": "48000",
            "src": _fcpxml_media_uri(source, path.parent),
        })

    clip_frames: list[tuple[int, int]] = []

    for item in items:
        source = str(Path(item.source_video).resolve())
        in_f = _frames(item.start, fps)
        out_f = max(in_f + 1, _frames(item.end, fps))
        media_frames = source_duration_frames.get(source)

        if media_frames is not None:
            if in_f >= media_frames:
                raise SequenceError(
                    "Resolve export stopped because a clip starts at or beyond "
                    f"the end of its source media.\n\n"
                    f"Clip: {item.title or item.id}\nSource: {source}\n"
                    f"Source in: {item.start:.3f}s"
                )
            out_f = min(out_f, media_frames)
            if out_f <= in_f:
                out_f = min(media_frames, in_f + 1)

        clip_frames.append((in_f, out_f - in_f))

    total_frames = sum(d for _, d in clip_frames)

    library = ET.SubElement(root, "library")
    event = ET.SubElement(library, "event", name="Silence Slicer")
    project = ET.SubElement(event, "project", name=name)
    sequence = ET.SubElement(project, "sequence", {
        "format": "r1",
        "duration": _fcpx_frames(total_frames, fps),
        "tcStart": "0s",
        "tcFormat": "NDF",
        "audioLayout": "stereo",
        "audioRate": "48k",
    })
    spine = ET.SubElement(sequence, "spine")
    cursor_frames = 0

    for n, (item, (in_f, dur_f)) in enumerate(zip(items, clip_frames), 1):
        source = str(Path(item.source_video).resolve())
        attrs = {
            "name": item.title or f"Clip {n}",
            "ref": source_ids[source],
            "format": "r1",
            "offset": _fcpx_frames(cursor_frames, fps),
            "start": _fcpx_frames(in_f, fps),
            "duration": _fcpx_frames(dur_f, fps),
            "audioRole": "dialogue",
        }
        clip = ET.SubElement(spine, "asset-clip", attrs)
        if item.notes or item.chapter or item.category:
            ET.SubElement(clip, "note").text = " | ".join(
                x for x in (item.chapter, item.category, item.notes) if x
            )
        cursor_frames += dur_f

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def _edl_timecode(frame_count: int, fps: Fraction) -> str:
    """CMX3600 non-drop timecode from an absolute frame count."""
    nominal = max(1, int(round(float(fps))))
    frame_count = max(0, int(frame_count))
    ff = frame_count % nominal
    total_seconds = frame_count // nominal
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = total_minutes // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def export_edl(
    path: str | Path,
    items: Sequence[SequenceItem],
    name: str = "Silence Slicer Sequence",
    fps=30,
) -> Path:
    """Write a simple CMX3600 hard-cut EDL for DaVinci Resolve.

    This is intentionally conservative: one video event per sequence item,
    straight cuts only, and clip-name comments containing the real source file.
    It gives Resolve a second interchange format when FCPXML linking is fussy.
    """
    if not items:
        raise SequenceError("The sequence is empty.")

    fps = parse_fps(fps)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"TITLE: {name}",
        "FCM: NON-DROP FRAME",
        "",
    ]

    record_cursor = 0
    for n, item in enumerate(items, 1):
        source_in = _frames(item.start, fps)
        source_out = max(source_in + 1, _frames(item.end, fps))
        dur = source_out - source_in
        record_in = record_cursor
        record_out = record_cursor + dur

        # AX is deliberately used as the reel: Resolve can conform by clip-name
        # comment/path rather than relying on fragile/truncated CMX reel names.
        lines.append(
            f"{n:03d}  AX       V     C        "
            f"{_edl_timecode(source_in, fps)} "
            f"{_edl_timecode(source_out, fps)} "
            f"{_edl_timecode(record_in, fps)} "
            f"{_edl_timecode(record_out, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {Path(item.source_video).name}")
        lines.append(f"* SOURCE FILE: {Path(item.source_video).resolve()}")
        if item.title:
            clean_title = re.sub(r"[\r\n]+", " ", item.title).strip()
            lines.append(f"* COMMENT: {clean_title[:200]}")
        lines.append("")
        record_cursor = record_out

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def probe_geometry(ffprobe: str, source: str | Path) -> tuple[int, int, bool]:
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(source),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
        creationflags=_creationflags(),
    )
    if proc.returncode != 0:
        raise SequenceError(proc.stderr.strip() or f"Could not probe {source}")
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise SequenceError(f"No video stream found in {source}")
    width = int(streams[0].get("width") or 1920)
    height = int(streams[0].get("height") or 1080)

    acmd = [
        ffprobe, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index", "-of", "csv=p=0", str(source),
    ]
    aproc = subprocess.run(
        acmd, capture_output=True, text=True, check=False,
        creationflags=_creationflags(),
    )
    has_audio = aproc.returncode == 0 and bool(aproc.stdout.strip())
    return width, height, has_audio


def render_assembly(
    ffmpeg: str,
    ffprobe: str,
    items: Sequence[SequenceItem],
    output_path: str | Path,
    fps=30,
    output_size: tuple[int, int] | None = None,
    framing: str = "Fill / center crop",
    progress: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Render a rough hard-cut assembly.

    Each source range is normalized to the chosen output geometry, H.264/AAC, 30fps
    by default (NTSC rates like "29.97" are accepted), then concatenated.  Work is disk-backed so memory use stays flat.
    """
    if not items:
        raise SequenceError("The sequence is empty.")
    fps_arg = _ffmpeg_fps(fps)
    ffmpeg = str(ffmpeg)
    ffprobe = str(ffprobe)
    if not Path(ffmpeg).is_file() and shutil.which(ffmpeg) is None:
        raise SequenceError("FFmpeg was not found.")
    if not Path(ffprobe).is_file() and shutil.which(ffprobe) is None:
        raise SequenceError("ffprobe was not found.")

    for item in items:
        if not Path(item.source_video).is_file():
            raise SequenceError(f"Source file is missing:\n{item.source_video}")

    if output_size:
        target_w, target_h = map(int, output_size)
    else:
        target_w, target_h, _ = probe_geometry(ffprobe, items[0].source_video)
    target_w -= target_w % 2
    target_h -= target_h % 2
    if target_w <= 0 or target_h <= 0:
        raise SequenceError("Invalid output dimensions.")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="silence_sequence_") as td:
        td_path = Path(td)
        segment_paths: list[Path] = []
        total = len(items) + 1

        for idx, item in enumerate(items, 1):
            if cancel_check and cancel_check():
                raise SequenceError("Sequence render cancelled.")
            seg = td_path / f"segment_{idx:04d}.mp4"
            segment_paths.append(seg)
            _, _, has_audio = probe_geometry(ffprobe, item.source_video)
            if str(framing).lower().startswith("fit"):
                vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                    f"fps={fps_arg},format=yuv420p"
                )
            else:
                # Fill the target frame and center-crop overflow. This makes
                # 9:16 / 4:5 exports useful for Shorts rather than a tiny
                # landscape image floating inside a vertical canvas.
                vf = (
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h},"
                    f"fps={fps_arg},format=yuv420p"
                )
            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{item.start:.3f}", "-t", f"{item.duration:.3f}",
                "-i", item.source_video,
            ]
            if has_audio:
                cmd += [
                    "-map", "0:v:0", "-map", "0:a:0", "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart", str(seg),
                ]
            else:
                cmd += [
                    "-f", "lavfi", "-t", f"{item.duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo",
                    "-map", "0:v:0", "-map", "1:a:0", "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                    "-c:a", "aac", "-b:a", "192k", "-shortest",
                    "-movflags", "+faststart", str(seg),
                ]
            proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            creationflags=_creationflags(),
        )
            if proc.returncode != 0:
                raise SequenceError(proc.stderr.strip() or f"FFmpeg failed while rendering clip {idx}.")
            if progress:
                progress(idx / total * 100.0, f"Rendered clip {idx} / {len(items)}")

        concat_file = td_path / "concat.txt"
        concat_file.write_text(
            "\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in segment_paths),
            encoding="utf-8",
        )
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c", "copy", "-movflags", "+faststart", str(out),
        ]
        proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False,
                creationflags=_creationflags(),
            )
        if proc.returncode != 0:
            raise SequenceError(proc.stderr.strip() or "FFmpeg failed while joining the sequence.")
        if progress:
            progress(100.0, "Assembly complete")
    return out
