#!/usr/bin/env python3
"""
Silence Cutter — The Unbound Edition

Self-contained dark-mode GUI for trimming low-audio sections from video.

Expected tools:
    C:\ffmpeg\bin\ffmpeg.exe
    C:\ffmpeg\bin\ffprobe.exe

Features:
- Low / Medium / High / Conversation cut presets
- Advanced controls and custom presets
- NVENC default
- Audio smoothing at cut boundaries without A/V drift
- Full analysis summary, cut-density warning, size estimates
- Determinate progress, elapsed time, ETA
- Custom test range
- Automatic dated/numbered filenames
- Remembered settings/output folder
- Disk-space preflight and abandoned-temp cleanup
- Pause/resume between render chunks
- Play Output / Open Output Folder
- Visual kept/cut timeline preview with click-to-see video frames
- Embedded Play/Pause preview with synchronized audio, plus ±5 second navigation
- Optional external player fallback
- Scene and Bridge markers with notes
- Exportable clip list CSV with source/output timecodes
- Footage Analysis page for SRT-driven highlight discovery
- Ranked Top 10/20/30 moments plus ALL full-dialogue mode, transcript search, KEEP/MAYBE/TRASH triage
- Timestamped footage index sidecars and CSV/TXT/JSON exports
- Sequence Builder for ordered rough cuts, smash-cut assemblies, and Resolve FCPXML export
- Project workspaces that keep source, processed media, transcripts, analysis, sequences, and exports together
"""

import base64
import hashlib
import csv
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
import urllib.error
import urllib.request
import wave
from tkinter import filedialog, messagebox, simpledialog, ttk

import footage_analysis as fa
import sequence_builder as sb
import project_manager as pm
import story_crossrefs as sx
import footage_preview as fp
import unbound_utils

APP_TITLE = "Silence Cutter"
CONFIG_PATH = Path.home() / ".silence_cutter_gui.json"
TEMP_PREFIX = ".silencecut_"

AUDIO_SMOOTHING = {
    "Off": 0.0,
    "Light": 0.04,
    "Medium": 0.07,
    "Strong": 0.12,
}

CUT_PRESETS = {
    "Low": {
        "threshold": -35.0,
        "min_silence": 1.8,
        "padding": 0.30,
        "min_clip": 0.10,
        "audio_smoothing": "Light",
    },
    "Medium": {
        "threshold": -30.0,
        "min_silence": 1.5,
        "padding": 0.25,
        "min_clip": 0.10,
        "audio_smoothing": "Medium",
    },
    "High": {
        "threshold": -25.0,
        "min_silence": 1.0,
        "padding": 0.18,
        "min_clip": 0.10,
        "audio_smoothing": "Medium",
    },
    "Conversation": {
        "threshold": -27.0,
        "min_silence": 1.25,
        "padding": 0.25,
        "min_clip": 0.12,
        "audio_smoothing": "Medium",
    },
}


def fmt_time(seconds):
    if seconds is None:
        return "—"
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def fmt_clock(seconds):
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(nbytes):
    if nbytes is None:
        return "—"
    n = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def parse_ffmpeg_time(value):
    try:
        h, m, s = value.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return 0.0


def safe_slug(value):
    value = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return value.strip("_") or "cut"


def resolve_tools():
    here = Path(__file__).resolve().parent
    dirs = [
        Path(r"C:\ffmpeg\bin"),
        here / "bin",
        here,
        Path(r"C:\ffmpeg"),
    ]
    exe = ".exe" if os.name == "nt" else ""
    for d in dirs:
        ffmpeg = d / f"ffmpeg{exe}"
        ffprobe = d / f"ffprobe{exe}"
        ffplay = d / f"ffplay{exe}"
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg, ffprobe, (ffplay if ffplay.is_file() else None)

    f1 = shutil.which("ffmpeg")
    f2 = shutil.which("ffprobe")
    f3 = shutil.which("ffplay")
    if f1 and f2:
        return Path(f1), Path(f2), (Path(f3) if f3 else None)
    return None, None, None


def load_config():
    try:
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_config(data):
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def build_keep_segments(silences, start, end, padding, min_keep):
    """Invert silence spans into absolute source-time keep spans within [start,end]."""
    edge = 0.001
    adjusted = []
    for s, e in silences:
        s = max(start, s)
        e = min(end, e)
        if e <= s:
            continue
        s2 = s if s <= start + edge else s + padding
        e2 = e if e >= end - edge else e - padding
        if e2 > s2:
            adjusted.append((s2, e2))

    keeps = []
    cursor = start
    for s, e in adjusted:
        if s > cursor:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if end > cursor:
        keeps.append((cursor, end))
    return [(a, b) for a, b in keeps if b - a >= min_keep]


def build_filtergraph(keeps, offset=0.0, audio_fade=0.0, fade_first=False, fade_last=False):
    """
    Video uses hard cuts.
    Audio uses tiny fade-in/out ramps at cut boundaries.
    No overlap is introduced, so total A/V duration stays matched.
    """
    segs = [(max(0.0, a - offset), max(0.0, b - offset)) for a, b in keeps]
    n = len(segs)
    if not n:
        raise ValueError("No clips to render.")

    lines = []

    def audio_line(i, a, b, src, dst):
        dur = max(0.0, b - a)
        filters = [f"atrim=start={a:.4f}:end={b:.4f}", "asetpts=PTS-STARTPTS"]
        if audio_fade > 0 and dur > 0:
            fade = min(audio_fade, dur / 2.0)
            if ((i > 0) or fade_first) and fade > 0:
                filters.append(f"afade=t=in:st=0:d={fade:.4f}")
            if ((i < n - 1) or fade_last) and fade > 0:
                filters.append(f"afade=t=out:st={max(0.0, dur-fade):.4f}:d={fade:.4f}")
        lines.append(f"{src}{','.join(filters)}{dst}")

    if n == 1:
        a, b = segs[0]
        lines.append(f"[0:v:0]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[outv];")
        audio_line(0, a, b, "[0:a:0]", "[outa]")
        return "\n".join(lines)

    vs = "".join(f"[vs{i}]" for i in range(n))
    aus = "".join(f"[as{i}]" for i in range(n))
    lines.append(f"[0:v:0]split={n}{vs};")
    lines.append(f"[0:a:0]asplit={n}{aus};")

    for i, (a, b) in enumerate(segs):
        lines.append(f"[vs{i}]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[v{i}];")
        audio_line(i, a, b, f"[as{i}]", f"[a{i}];")

    joined = "".join(f"[v{i}][a{i}]" for i in range(n))
    lines.append(f"{joined}concat=n={n}:v=1:a=1[outv][outa]")
    return "\n".join(lines)


class FFmpegRunError(RuntimeError):
    """FFmpeg process failure with stderr preserved for retry decisions."""

    def __init__(self, phase, returncode, stderr_text):
        self.phase = phase
        self.returncode = returncode
        self.stderr_text = stderr_text or ""
        tail = self.stderr_text[-2200:]
        super().__init__(f"FFmpeg failed during {phase}:\n{tail}")


class SilenceCutterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x720")
        self.minsize(1000, 650)

        self.ffmpeg, self.ffprobe, self.ffplay = resolve_tools()
        self.events = queue.Queue()
        self.proc = None
        self.running = False
        self.cancel_event = threading.Event()
        self.pause_event = threading.Event()
        self.job_start = None
        self.current_analysis = None
        self.analysis_signature = None
        self.advanced_visible = False
        self.last_output = None
        self.input_duration = None
        self.input_bytes = None
        self.timeline_selected_time = None
        self.markers = []
        self.preview_photo = None
        self.preview_request_id = 0
        self.preview_temp = None
        self.preview_expanded = False
        self.ffplay_proc = None
        self.playback_video_proc = None
        self.playback_audio_proc = None
        self.playback_active = False
        self.playback_generation = 0
        self.playback_latest_frame = None
        self.playback_start_time = 0.0
        self.playback_started_monotonic = None

        cfg = load_config()
        self.marker_store = cfg.get("markers_by_input", {}) if isinstance(cfg.get("markers_by_input", {}), dict) else {}

        # Project workspace: one manifest owns the paths for a complete source ->
        # processed -> transcript -> analysis -> sequence -> export workflow.
        self.project = None
        self.project_path_var = tk.StringVar(value=cfg.get("current_project", ""))
        self.project_name_var = tk.StringVar(value="No project open")
        self.project_source_var = tk.StringVar(value="—")
        self.project_processed_var = tk.StringVar(value="—")
        self.project_srt_display_var = tk.StringVar(value="—")
        self.project_index_var = tk.StringVar(value="—")
        self.project_sequence_var = tk.StringVar(value="—")
        self.project_status_var = tk.StringVar(value="Create or open a project to keep every related file together.")

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=cfg.get("output_dir", str(Path.home() / "Videos")))
        self.cut_strength = tk.StringVar(value=cfg.get("cut_strength", "High"))

        self.threshold_var = tk.StringVar(value=str(cfg.get("threshold", -25.0)))
        self.min_silence_var = tk.StringVar(value=str(cfg.get("min_silence", 1.0)))
        self.padding_var = tk.StringVar(value=str(cfg.get("padding", 0.18)))
        self.min_clip_var = tk.StringVar(value=str(cfg.get("min_clip", 0.10)))

        self.encoder_var = tk.StringVar(value=cfg.get("encoder", "nvenc"))
        self.crf_var = tk.StringVar(value=str(cfg.get("crf", 18)))
        self.cpu_preset_var = tk.StringVar(value=cfg.get("cpu_preset", "medium"))
        self.audio_var = tk.StringVar(value=cfg.get("audio_bitrate", "192k"))
        self.audio_smoothing_var = tk.StringVar(value=cfg.get("audio_smoothing", "Medium"))
        self.batch_var = tk.StringVar(value=str(cfg.get("batch_size", 40)))

        self.skip_intro_var = tk.StringVar(value=str(cfg.get("skip_intro", 0.0)))
        self.skip_outro_var = tk.StringVar(value=str(cfg.get("skip_outro", 0.0)))
        self.test_start_var = tk.StringVar(value=str(cfg.get("test_start_min", 0.0)))
        self.test_duration_var = tk.StringVar(value=str(cfg.get("test_duration_min", 10.0)))

        self.auto_name_var = tk.BooleanVar(value=cfg.get("auto_name", True))
        self.auto_srt_var = tk.BooleanVar(value=cfg.get("auto_srt", True))
        self.overwrite_var = tk.BooleanVar(value=False)

        self.custom_presets = cfg.get("custom_presets", {}) if isinstance(cfg.get("custom_presets", {}), dict) else {}
        self.custom_preset_var = tk.StringVar(value="")

        self.raw_duration_var = tk.StringVar(value="—")
        self.raw_size_var = tk.StringVar(value="—")
        self.after_var = tk.StringVar(value="—")
        self.estimated_size_var = tk.StringVar(value="—")
        self.actual_size_var = tk.StringVar(value="—")
        self.removed_var = tk.StringVar(value="—")
        self.kept_var = tk.StringVar(value="—")
        self.cuts_var = tk.StringVar(value="—")
        self.chunks_var = tk.StringVar(value="—")
        self.cut_result_var = tk.StringVar(value="—")
        self.free_space_var = tk.StringVar(value="—")
        self.temp_need_var = tk.StringVar(value="—")
        self.status_var = tk.StringVar(value="Ready.")
        self.progress_text_var = tk.StringVar(value="0%")
        self.selected_time_var = tk.StringVar(value="Selected: —")

        self.tts_endpoint_var = tk.StringVar(value=cfg.get("tts_endpoint", "http://127.0.0.1:8086"))
        self.tts_model_var = tk.StringVar(value=cfg.get("tts_model", "pocket-tts"))
        self.tts_voice_var = tk.StringVar(value=cfg.get("tts_voice", "TheNarrator"))
        self.tts_output_dir_var = tk.StringVar(value=cfg.get("tts_output_dir", str(Path.home() / "Music" / "Unbound Narration")))
        self.narration_name_var = tk.StringVar(value=cfg.get("narration_name", "The Unbound Project"))
        self.narration_status_var = tk.StringVar(value="Narration tab ready.")
        self.narration_progress_text_var = tk.StringVar(value="0%")
        self.last_narration_output = None
        self.last_narration_assets = {}
        self.last_narration_manifest = None
        self.narration_busy = False

        # Footage Analysis stays deliberately lightweight: paths, subtitle text,
        # and compact JSON metadata only. Video frames are never retained in RAM.
        self.footage_video_var = tk.StringVar(value=cfg.get("footage_video", ""))
        self.footage_version_var = tk.StringVar(value="")
        self.footage_srt_var = tk.StringVar(value=cfg.get("footage_srt", ""))
        self.footage_mode_var = tk.StringVar(value=cfg.get("footage_mode", "Best Overall"))
        self.footage_top_var = tk.StringVar(value=str(cfg.get("footage_top", 20)))
        self.footage_character_var = tk.StringVar(value=cfg.get("footage_character", ""))
        self.footage_query_var = tk.StringVar(value="")
        self.footage_search_var = tk.StringVar(value="")
        self.footage_status_var = tk.StringVar(value="Load a cleaned video and its matching SRT.")
        self.footage_entries = []
        self.footage_moments = []
        self.footage_video_duration = None
        self.footage_index_path = None
        self.footage_preview_proc = None
        self.footage_preview_window = None

        # Sequence Builder is deliberately a separate pre-editor module. It holds
        # only clip references/timestamps in memory; media stays on disk.
        self.sequence_name_var = tk.StringVar(value=cfg.get("sequence_name", "The Unbound Rough Cut"))
        self.sequence_fps_var = tk.StringVar(value=str(cfg.get("sequence_fps", 30)))
        self.sequence_format_var = tk.StringVar(value=cfg.get("sequence_format", "16:9 — 1920x1080"))
        self.sequence_framing_var = tk.StringVar(value=cfg.get("sequence_framing", "Fill / center crop"))
        self.sequence_status_var = tk.StringVar(value="Build a rough sequence from curated footage moments.")
        self.sequence_progress_var = tk.StringVar(value="0%")
        self.sequence_items = []
        self.sequence_project_path = None
        self.sequence_preview_proc = None
        self.sequence_render_busy = False
        self.sequence_cancel = threading.Event()

        # Story Crossrefs links diary-roll memories to footage moments and later callbacks.
        self.crossref_diary_var = tk.StringVar(value=cfg.get("crossref_diary", ""))
        self.crossref_query_var = tk.StringVar(value="")
        self.crossref_threshold_var = tk.StringVar(value=str(cfg.get("crossref_threshold", 0.18)))
        self.crossref_status_var = tk.StringVar(value="Load a diary roll and one or more footage indexes.")
        self.crossref_diaries = []
        self.crossref_moments = []
        self.crossref_refs = []
        self.crossref_footage_sources = []
        self.crossref_preview_proc = None

        self._apply_theme()
        self._build_ui()
        # --- REPEAT GROUPS + SRT SAFETY UPGRADE ---
        try:
            from silence_slicer_repeat_srt_tools import install_into_app
            self.after(0, lambda: install_into_app(self))
        except Exception as _repeat_srt_upgrade_error:
            print(f"Repeat/SRT upgrade load warning: {_repeat_srt_upgrade_error}", file=sys.stderr)
        self._apply_builtin_preset(self.cut_strength.get(), update_output=False)
        saved_project = self.project_path_var.get().strip()
        if saved_project:
            try:
                self._activate_project(pm.open_project(saved_project), quiet=True)
            except Exception:
                self.project_path_var.set("")
        self.after(100, self._poll_events)

        if not self.ffmpeg or not self.ffprobe:
            self.after(250, lambda: messagebox.showwarning(
                APP_TITLE,
                "FFmpeg/ffprobe were not found. Expected C:\\ffmpeg\\bin or PATH."
            ))

    # ---------- theme ----------
    def _apply_theme(self):
        self.bg = "#151515"
        self.panel = "#202020"
        self.field = "#2c2c2c"
        self.fg = "#eeeeee"
        self.muted = "#b8b8b8"
        self.accent = "#7c5cff"
        self.configure(bg=self.bg)

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # High-contrast global styling.  Explicit widget styles are important on
        # Windows because native/theme defaults can otherwise produce pale text
        # on pale table rows and notebook tabs.
        ui_font = ("Segoe UI", 10)
        ui_font_bold = ("Segoe UI", 10, "bold")
        style.configure(".", background=self.bg, foreground=self.fg, font=ui_font)
        style.configure("TFrame", background=self.bg)
        style.configure("TLabelframe", background=self.bg, foreground=self.fg)
        style.configure("TLabelframe.Label", background=self.bg, foreground=self.fg, font=ui_font_bold)
        style.configure("TLabel", background=self.bg, foreground=self.fg, font=ui_font)
        style.configure("Big.TLabel", background=self.bg, foreground=self.fg, font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", background=self.bg, foreground="#d0d0d0", font=ui_font)

        style.configure("TButton", background=self.panel, foreground=self.fg, padding=7, font=ui_font)
        style.map("TButton",
                  background=[("active", "#353535"), ("pressed", "#444444")],
                  foreground=[("disabled", "#9a9a9a"), ("!disabled", self.fg)])
        style.configure("TRadiobutton", background=self.bg, foreground=self.fg, font=ui_font)
        style.map("TRadiobutton", background=[("active", self.bg)], foreground=[("disabled", "#999999"), ("!disabled", self.fg)])
        style.configure("TCheckbutton", background=self.bg, foreground=self.fg, font=ui_font)

        style.configure("TEntry", fieldbackground=self.field, foreground=self.fg, insertcolor=self.fg, font=ui_font)
        style.map("TEntry",
                  fieldbackground=[("disabled", "#222222"), ("readonly", self.field)],
                  foreground=[("disabled", "#999999"), ("readonly", self.fg), ("!disabled", self.fg)])
        style.configure("TCombobox", fieldbackground=self.field, background=self.field, foreground=self.fg, arrowcolor=self.fg, font=ui_font)
        style.map("TCombobox",
                  fieldbackground=[("readonly", self.field), ("disabled", "#222222")],
                  foreground=[("readonly", self.fg), ("disabled", "#999999"), ("!disabled", self.fg)],
                  selectbackground=[("readonly", "#4b3c86")],
                  selectforeground=[("readonly", "#ffffff")])

        # Readable tables on every page.  This fixes the white/gray-on-white
        # Treeview rows seen on some Windows theme/DPI combinations.
        style.configure("Treeview",
                        background="#242424", fieldbackground="#242424",
                        foreground="#f4f4f4", rowheight=28, font=ui_font,
                        borderwidth=0)
        style.configure("Treeview.Heading",
                        background="#333333", foreground="#ffffff",
                        font=ui_font_bold, relief="flat", padding=(6, 5))
        style.map("Treeview",
                  background=[("selected", "#6248c7")],
                  foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading",
                  background=[("active", "#444444")],
                  foreground=[("active", "#ffffff")])

        # Keep every notebook tab dark and legible, selected or not.
        style.configure("TNotebook", background=self.bg, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background="#2a2a2a", foreground="#f0f0f0",
                        font=ui_font_bold, padding=(10, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", "#4a3a75"), ("active", "#383838")],
                  foreground=[("selected", "#ffffff"), ("active", "#ffffff"), ("!selected", "#e8e8e8")])

        style.configure("Horizontal.TProgressbar", troughcolor=self.field, background=self.accent)
        self.option_add("*TCombobox*Listbox.background", self.field)
        self.option_add("*TCombobox*Listbox.foreground", self.fg)
        self.option_add("*TCombobox*Listbox.selectBackground", "#484848")
        self.option_add("*TCombobox*Listbox.selectForeground", self.fg)

    # ---------- UI ----------
    def _build_ui(self):
        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True, padx=10, pady=10)

        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)

        project_tab = ttk.Frame(self.notebook)
        cut_tab = ttk.Frame(self.notebook)
        narration_tab = ttk.Frame(self.notebook)
        footage_tab = ttk.Frame(self.notebook)
        crossref_tab = ttk.Frame(self.notebook)
        sequence_tab = ttk.Frame(self.notebook)
        review_tab = ttk.Frame(self.notebook)

        self.notebook.add(project_tab, text="Project")
        self.notebook.add(cut_tab, text="Cut & Analyze")
        self.notebook.add(narration_tab, text="Narration / TTS")
        self.notebook.add(footage_tab, text="Footage Analysis")
        self.notebook.add(crossref_tab, text="Story Crossrefs")
        self.notebook.add(sequence_tab, text="Sequence Builder")
        self.notebook.add(review_tab, text="Review & Export")

        outer = ttk.Frame(cut_tab)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        files = ttk.LabelFrame(outer, text="Files")
        files.pack(fill="x")
        ttk.Label(files, text="Input video:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(files, textvariable=self.input_var).grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(files, text="Browse…", command=self._browse_input).grid(row=0, column=2, padx=8, pady=6)

        ttk.Label(files, text="Output file:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(files, textvariable=self.output_var).grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(files, text="Browse…", command=self._browse_output).grid(row=1, column=2, padx=8, pady=6)
        files.columnconfigure(1, weight=1)

        strength = ttk.LabelFrame(outer, text="Cut Strength")
        strength.pack(fill="x", pady=(10, 0))
        ttk.Label(strength, text="How ruthless should the cutter be?", style="Big.TLabel").pack(side="left", padx=(10, 22), pady=8)
        for name in ("Low", "Medium", "High", "Conversation"):
            ttk.Radiobutton(
                strength, text=name, value=name, variable=self.cut_strength,
                command=lambda n=name: self._apply_builtin_preset(n)
            ).pack(side="left", padx=10)

        adv_header = ttk.Frame(outer)
        adv_header.pack(fill="x", pady=(10, 0))
        self.advanced_btn = ttk.Button(adv_header, text="Show Advanced ▾", command=self._toggle_advanced)
        self.advanced_btn.pack(side="left")

        self.advanced = ttk.LabelFrame(outer, text="Advanced Settings")
        self._build_advanced()

        self.analysis_frame = ttk.LabelFrame(outer, text="Analysis")
        self.analysis_frame.pack(fill="x", pady=(10, 0))
        analysis = self.analysis_frame

        labels = [
            ("Raw duration:", self.raw_duration_var, 0, 0),
            ("Raw size:", self.raw_size_var, 0, 2),
            ("After cut:", self.after_var, 1, 0),
            ("Estimated size:", self.estimated_size_var, 1, 2),
            ("Removed:", self.removed_var, 2, 0),
            ("Actual size:", self.actual_size_var, 2, 2),
            ("Kept clips:", self.kept_var, 3, 0),
            ("Cuts:", self.cuts_var, 3, 2),
            ("Chunks:", self.chunks_var, 4, 0),
            ("Cut result:", self.cut_result_var, 4, 2),
            ("Free space:", self.free_space_var, 5, 0),
            ("Temp need:", self.temp_need_var, 5, 2),
        ]
        for title, var, row, col in labels:
            ttk.Label(analysis, text=title).grid(row=row, column=col, sticky="w", padx=10, pady=4)
            ttk.Label(analysis, textvariable=var, style="Big.TLabel").grid(row=row, column=col+1, sticky="w", padx=10, pady=4)

        actions = ttk.LabelFrame(outer, text="Run Cutter")
        actions.pack(fill="x", pady=(10, 6))

        action_row = ttk.Frame(actions)
        action_row.pack(fill="x", padx=8, pady=8)
        self.analyze_btn = ttk.Button(action_row, text="Analyze", command=self._analyze_clicked)
        self.analyze_btn.pack(side="left")

        self.test_btn = ttk.Button(action_row, text="Test Range", command=self._test_clicked)
        self.test_btn.pack(side="left", padx=(8, 0))

        self.process_btn = ttk.Button(action_row, text="Process Video", command=self._process_clicked)
        self.process_btn.pack(side="left", padx=(8, 0))

        self.pause_btn = ttk.Button(action_row, text="Pause", command=self._pause_resume, state="disabled")
        self.pause_btn.pack(side="left", padx=(8, 0))

        self.cancel_btn = ttk.Button(action_row, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        ttk.Checkbutton(
            action_row, text="Auto SRT → Footage Analysis", variable=self.auto_srt_var
        ).pack(side="left", padx=(18, 0))

        ttk.Label(actions, text="Page 3 now holds preview, timeline, markers, log output, and export tools.", style="Muted.TLabel").pack(anchor="w", padx=10, pady=(0, 8))

        prog = ttk.Frame(outer)
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(prog, textvariable=self.progress_text_var).pack(side="right", padx=(10, 0))

        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

        self._build_project_tab(project_tab)
        self._build_narration_tab(narration_tab)
        self._build_footage_tab(footage_tab)
        self._build_crossref_tab(crossref_tab)
        self._build_sequence_tab(sequence_tab)

        review_outer = ttk.Frame(review_tab)
        review_outer.pack(fill="both", expand=True, padx=12, pady=12)

        preview_box = ttk.LabelFrame(review_outer, text="Video Preview")
        preview_box.pack(fill="x", pady=(0, 0))

        scrubber_row = ttk.Frame(preview_box)
        scrubber_row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(scrubber_row, text="Scrubber:", style="Big.TLabel").pack(side="left")
        self.scrubber_var = tk.DoubleVar(value=0.0)
        self.scrubber = tk.Scale(
            scrubber_row,
            from_=0.0, to=1.0, variable=self.scrubber_var,
            command=self._scrubber_moved, state=tk.DISABLED,
            orient="horizontal", showvalue=False, resolution=0.01,
            sliderlength=24, width=14, bd=0, highlightthickness=0,
            background=self.bg, foreground=self.fg,
            troughcolor="#5a5a5a", activebackground=self.accent,
            sliderrelief="raised",
        )
        self.scrubber.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self.scrubber_time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(scrubber_row, textvariable=self.scrubber_time_var).pack(side="right")
        self.scrubber.bind("<ButtonPress-1>", self._scrubber_pressed)
        self.scrubber.bind("<ButtonRelease-1>", self._scrubber_released)

        self.preview_label = tk.Label(
            preview_box,
            text="Choose a video to load a preview frame",
            background="#050505",
            foreground=self.muted,
            anchor="center",
        )
        self.preview_label.pack(fill="x", padx=8, pady=(8, 4))

        preview_controls = ttk.Frame(preview_box)
        preview_controls.pack(fill="x", padx=8, pady=(2, 8))

        self.preview_back_btn = ttk.Button(preview_controls, text="◀ 5 sec", command=lambda: self._nudge_preview(-5.0), state="disabled")
        self.preview_back_btn.pack(side="left")
        self.play_here_btn = ttk.Button(preview_controls, text="▶ Play", command=self._toggle_embedded_playback, state="disabled")
        self.play_here_btn.pack(side="left", padx=(8, 0))
        self.preview_forward_btn = ttk.Button(preview_controls, text="5 sec ▶", command=lambda: self._nudge_preview(5.0), state="disabled")
        self.preview_forward_btn.pack(side="left", padx=(8, 0))
        self.expand_preview_btn = ttk.Button(preview_controls, text="Expand Preview", command=self._toggle_preview_size, state="disabled")
        self.expand_preview_btn.pack(side="left", padx=(8, 0))
        self.external_player_btn = ttk.Button(preview_controls, text="Open External Player", command=self._play_external, state="disabled")
        self.external_player_btn.pack(side="left", padx=(8, 0))
        ttk.Label(preview_controls, textvariable=self.selected_time_var, style="Big.TLabel").pack(side="right")

        timeline_box = ttk.LabelFrame(review_outer, text="Timeline / Scene Markers")
        timeline_box.pack(fill="x", pady=(10, 0))

        self.timeline_canvas = tk.Canvas(timeline_box, height=76, background=self.field, highlightthickness=0, bd=0)
        self.timeline_canvas.pack(fill="x", padx=8, pady=(8, 4))
        self.timeline_canvas.bind("<Configure>", lambda _e: self._draw_timeline())
        self.timeline_canvas.bind("<Button-1>", self._timeline_click)

        timeline_controls = ttk.Frame(timeline_box)
        timeline_controls.pack(fill="x", padx=8, pady=(2, 6))
        self.scene_btn = ttk.Button(timeline_controls, text="Add Scene Marker", command=lambda: self._add_marker("Scene"), state="disabled")
        self.scene_btn.pack(side="left", padx=(14, 0))
        self.bridge_btn = ttk.Button(timeline_controls, text="Add Bridge Marker", command=lambda: self._add_marker("Bridge"), state="disabled")
        self.bridge_btn.pack(side="left", padx=(8, 0))
        self.delete_marker_btn = ttk.Button(timeline_controls, text="Delete Marker", command=self._delete_marker, state="disabled")
        self.delete_marker_btn.pack(side="left", padx=(8, 0))
        self.export_btn = ttk.Button(timeline_controls, text="Export Clip List CSV", command=self._export_clip_list, state="disabled")
        self.export_btn.pack(side="right")

        marker_frame = ttk.Frame(timeline_box)
        marker_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.marker_tree = ttk.Treeview(marker_frame, columns=("type", "time", "note"), show="headings", height=4)
        self.marker_tree.heading("type", text="Type")
        self.marker_tree.heading("time", text="Time")
        self.marker_tree.heading("note", text="Note")
        self.marker_tree.column("type", width=90, stretch=False)
        self.marker_tree.column("time", width=110, stretch=False)
        self.marker_tree.column("note", width=620, stretch=True)
        self.marker_tree.pack(side="left", fill="x", expand=True)
        self.marker_tree.bind("<<TreeviewSelect>>", lambda _e: self._marker_selection_changed())
        marker_scroll = ttk.Scrollbar(marker_frame, orient="vertical", command=self.marker_tree.yview)
        marker_scroll.pack(side="right", fill="y")
        self.marker_tree.configure(yscrollcommand=marker_scroll.set)

        review_actions = ttk.Frame(review_outer)
        review_actions.pack(fill="x", pady=(10, 6))
        self.play_btn = ttk.Button(review_actions, text="Play Output", command=self._play_output, state="disabled")
        self.play_btn.pack(side="right")
        self.folder_btn = ttk.Button(review_actions, text="Open Output Folder", command=self._open_output_folder)
        self.folder_btn.pack(side="right", padx=(0, 8))
        ttk.Label(review_actions, text="This page is for scrubbing, marking, reviewing, and exporting notes.", style="Muted.TLabel").pack(side="left")

        logbox = ttk.LabelFrame(review_outer, text="Output")
        logbox.pack(fill="both", expand=True, pady=(8, 0))
        self.log = tk.Text(
            logbox, wrap="word",
            background=self.field, foreground=self.fg, insertbackground=self.fg,
            selectbackground="#4a4a4a", selectforeground=self.fg, relief="flat"
        )
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)

    # ---------- Project workspace ----------
    def _build_project_tab(self, tab):
        outer = ttk.Frame(tab)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        head = ttk.LabelFrame(outer, text="Silence Slicer Project")
        head.pack(fill="x")
        ttk.Label(head, text="Project:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(head, textvariable=self.project_path_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8, pady=7)
        ttk.Button(head, text="Create Project…", command=self._create_project_clicked).grid(row=0, column=2, padx=5, pady=7)
        ttk.Button(head, text="Open Project…", command=self._open_project_clicked).grid(row=0, column=3, padx=5, pady=7)
        ttk.Button(head, text="Open Folder", command=self._open_project_folder).grid(row=0, column=4, padx=8, pady=7)
        head.columnconfigure(1, weight=1)

        ttk.Label(outer, textvariable=self.project_name_var, style="Big.TLabel").pack(anchor="w", pady=(12, 4))
        ttk.Label(
            outer,
            text="One project owns the whole chain: source → processed video → transcript → footage analysis → sequences → exports.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        source = ttk.LabelFrame(outer, text="Source Recording")
        source.pack(fill="x")
        ttk.Label(source, text="Current source:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(source, textvariable=self.project_source_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8, pady=7)
        ttk.Button(source, text="Link Existing…", command=lambda: self._project_import_source(False)).grid(row=0, column=2, padx=5, pady=7)
        ttk.Button(source, text="Copy Into Project…", command=lambda: self._project_import_source(True)).grid(row=0, column=3, padx=8, pady=7)
        source.columnconfigure(1, weight=1)

        transcript = ttk.LabelFrame(outer, text="Transcript / SRT")
        transcript.pack(fill="x", pady=(10, 0))
        ttk.Label(transcript, text="Matching SRT:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(transcript, textvariable=self.project_srt_display_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=8, pady=7)
        ttk.Button(transcript, text="Import SRT…", command=self._project_import_srt).grid(row=0, column=2, padx=8, pady=7)
        transcript.columnconfigure(1, weight=1)
        ttk.Label(
            transcript,
            text="Gameplay SRT must come from speech transcription. Narration/TTS captions are generated automatically from the narration script.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        assets = ttk.LabelFrame(outer, text="Project Assets")
        assets.pack(fill="x", pady=(10, 0))
        rows = [
            ("Processed video", self.project_processed_var),
            ("Footage index", self.project_index_var),
            ("Last sequence", self.project_sequence_var),
        ]
        for r, (label, var) in enumerate(rows):
            ttk.Label(assets, text=label + ":").grid(row=r, column=0, sticky="w", padx=8, pady=6)
            ttk.Entry(assets, textvariable=var, state="readonly").grid(row=r, column=1, sticky="ew", padx=8, pady=6)
        assets.columnconfigure(1, weight=1)

        ttk.Label(outer, textvariable=self.project_status_var, style="Muted.TLabel").pack(anchor="w", pady=(10, 0))

    def _require_project(self):
        if not self.project:
            raise RuntimeError("Create or open a project first.")
        return self.project

    def _create_project_clicked(self):
        parent = filedialog.askdirectory(title="Choose where to create the project folder")
        if not parent:
            return
        name = simpledialog.askstring(APP_TITLE, "Project name:", initialvalue="The Unbound Project")
        if not name:
            return
        try:
            chosen = Path(parent)
            slug = pm.safe_slug(name)
            # If the user already selected a folder named for the project, use it as
            # the project root instead of creating ProjectName/ProjectName.
            root = chosen if chosen.name.casefold() == slug.casefold() else chosen / slug
            self._activate_project(pm.create_project(root, name))
            self.project_status_var.set("Project created. Link or copy the original recording next.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _open_project_clicked(self):
        path = filedialog.askopenfilename(
            title="Open Silence Slicer project",
            filetypes=[("Silence Slicer project", "project.json"), ("JSON", "*.json")],
        )
        if not path:
            return
        try:
            self._activate_project(pm.open_project(path))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _activate_project(self, workspace, quiet=False):
        self.project = workspace
        self.project_path_var.set(str(workspace.manifest_path))
        self.project_name_var.set(workspace.name)
        self.output_dir_var.set(str(workspace.folder("processed")))
        self.tts_output_dir_var.set(str(workspace.folder("narration")))
        self.narration_name_var.set(workspace.name)
        self._refresh_project_display()
        self._refresh_processed_versions()

        source = workspace.absolute_path("source")
        processed = workspace.absolute_path("processed")
        # Pair the reopened video with ITS OWN transcript, not whichever SRT
        # happened to be imported last.
        srt = None
        if processed:
            for label, path in workspace.processed_variants().items():
                try:
                    if Path(path).resolve() == Path(processed).resolve():
                        srt = workspace.srt_for(label)
                        break
                except OSError:
                    pass
        index = workspace.absolute_path("footage_index")
        sequence = workspace.absolute_path("sequence")
        if source and source.exists():
            self.input_var.set(str(source))
            if not processed or not processed.exists():
                self.output_var.set(str(workspace.default_processed_path(source, self.cut_strength.get())))
        if processed and processed.exists():
            self.last_output = processed
            self.footage_video_var.set(str(processed))
        self.footage_srt_var.set(str(srt) if srt and srt.exists() else "")
        if index:
            self.footage_index_path = index
        if sequence:
            self.sequence_project_path = str(sequence)
        self._save_preferences()
        if not quiet:
            self.project_status_var.set(f"Project open: {workspace.name}")

    def _refresh_project_display(self):
        if not self.project:
            self.project_name_var.set("No project open")
            return
        info = self.project.summary()
        mode = info.get("source_mode") or ""
        src = info.get("source") or "—"
        self.project_source_var.set((f"[{mode}] " if mode else "") + src if src != "—" else src)
        self.project_processed_var.set(info.get("processed") or "—")
        self.project_srt_display_var.set(info.get("srt") or "—")
        self.project_index_var.set(info.get("footage_index") or "—")
        self.project_sequence_var.set(info.get("sequence") or "—")

    def _project_import_source(self, copy_into_project=False):
        try:
            project = self._require_project()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc)); return
        path = filedialog.askopenfilename(
            title="Choose original recording",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            actual = project.attach_source(path, copy_into_project=copy_into_project)
            self.input_var.set(str(actual))
            self.output_dir_var.set(str(project.folder("processed")))
            self.output_var.set(str(project.default_processed_path(actual, self.cut_strength.get())))
            self.current_analysis = None
            self.analysis_signature = None
            self._refresh_project_display()
            self._save_preferences()
            verb = "Copied" if copy_into_project else "Linked"
            self.project_status_var.set(f"{verb} source recording. Cut & Analyze is ready to use it.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _project_import_srt(self):
        try:
            project = self._require_project()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc)); return
        path = filedialog.askopenfilename(title="Import matching SRT", filetypes=[("SubRip subtitles", "*.srt")])
        if not path:
            return
        try:
            active = self.footage_version_var.get() or project.data.get("active_processed")
            variant = active or "SOURCE"
            actual = project.import_transcript(path, variant=variant, copy_into_project=True)
            self.footage_srt_var.set(str(actual))
            self._refresh_project_display()
            self._save_preferences()
            self.project_status_var.set("SRT imported into the project transcript folder.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _open_project_folder(self):
        try:
            project = self._require_project()
            folder = project.root
            if os.name == "nt": os.startfile(str(folder))
            elif sys.platform == "darwin": subprocess.Popen(["open", str(folder)])
            else: subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _project_register_processed(self, path, label=None):
        if not self.project:
            return
        try:
            label = pm._norm_label(label or self.cut_strength.get() or "Processed")
            self.project.register_processed(label, Path(path))
            self.footage_version_var.set(label.upper())
            self.footage_video_var.set(str(Path(path)))
            self.footage_index_path = self.project.footage_index_path(path)
            self._refresh_project_display()
            self._refresh_processed_versions()
        except Exception as exc:
            self.project_status_var.set(f"Project update warning: {exc}")

    def _project_register_index(self, path):
        if self.project and path:
            try:
                self.project.set_asset("footage_index", Path(path))
                self._refresh_project_display()
            except Exception:
                pass

    def _project_register_sequence(self, path):
        if self.project and path:
            try:
                self.project.set_asset("sequence", Path(path))
                self._refresh_project_display()
            except Exception:
                pass

    def _project_dir(self, name, fallback=None):
        if self.project:
            return self.project.folder(name)
        return Path(fallback or Path.home())

    # ---------- Footage Analysis ----------
    def _build_footage_tab(self, parent):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        source = ttk.LabelFrame(outer, text="Footage Source")
        source.pack(fill="x")
        ttk.Label(source, text="Project version:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.footage_version_combo = ttk.Combobox(source, textvariable=self.footage_version_var, values=(), state="readonly")
        self.footage_version_combo.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        self.footage_version_combo.bind("<<ComboboxSelected>>", lambda _e: self._select_footage_version())
        ttk.Label(source, text="Cleaned video:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(source, textvariable=self.footage_video_var).grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(source, text="Browse…", command=self._browse_footage_video).grid(row=1, column=2, padx=8, pady=6)
        ttk.Label(source, text="Matching SRT:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(source, textvariable=self.footage_srt_var).grid(row=2, column=1, sticky="ew", padx=8, pady=6)
        ttk.Button(source, text="Browse…", command=self._browse_footage_srt).grid(row=2, column=2, padx=8, pady=6)
        self.footage_load_btn = ttk.Button(source, text="Load Footage Map", command=self._load_footage_map)
        self.footage_load_btn.grid(row=0, column=3, rowspan=3, padx=8, pady=6, sticky="ns")
        source.columnconfigure(1, weight=1)

        controls = ttk.LabelFrame(outer, text="Find Best Moments")
        controls.pack(fill="x", pady=(10, 0))
        ttk.Label(controls, text="Mode:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        self.footage_mode_combo = ttk.Combobox(
            controls, textvariable=self.footage_mode_var, values=fa.MODE_LABELS,
            state="readonly", width=27,
        )
        self.footage_mode_combo.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=7)
        self.footage_mode_combo.bind("<<ComboboxSelected>>", lambda _e: self._footage_mode_changed())
        ttk.Label(controls, text="Top:").grid(row=0, column=2, sticky="w", padx=(4, 4), pady=7)
        self.footage_top_combo = ttk.Combobox(
            controls, textvariable=self.footage_top_var,
            values=("10", "20", "30", "ALL"), width=6, state="normal",
        )
        self.footage_top_combo.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=7)
        ttk.Label(controls, text="Character:").grid(row=0, column=4, sticky="w", padx=(4, 4), pady=7)
        self.footage_character_combo = ttk.Combobox(
            controls, textvariable=self.footage_character_var, values=("",),
            width=19, state="normal",
        )
        self.footage_character_combo.grid(row=0, column=5, sticky="ew", padx=(0, 8), pady=7)
        ttk.Label(controls, text="Must contain:").grid(row=0, column=6, sticky="w", padx=(4, 4), pady=7)
        ttk.Entry(controls, textvariable=self.footage_query_var, width=18).grid(row=0, column=7, sticky="ew", padx=(0, 8), pady=7)
        self.footage_analyze_btn = ttk.Button(controls, text="Analyze Moments", command=self._analyze_footage_moments, state="disabled")
        self.footage_analyze_btn.grid(row=0, column=8, padx=(8, 4), pady=7)
        self.footage_reset_btn = ttk.Button(controls, text="Clear Ranked", command=self._clear_footage_results)
        self.footage_reset_btn.grid(row=0, column=9, padx=(4, 8), pady=7)
        controls.columnconfigure(5, weight=1)
        controls.columnconfigure(7, weight=1)

        ttk.Label(
            controls,
            text="Ranking is local and deterministic: it surfaces likely moments for review; KEEP/MAYBE/TRASH remains your decision.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=10, sticky="w", padx=8, pady=(0, 7))

        panes = ttk.Panedwindow(outer, orient="vertical")
        panes.pack(fill="both", expand=True, pady=(10, 0))

        transcript_box = ttk.LabelFrame(panes, text="Transcript")
        results_box = ttk.LabelFrame(panes, text="Ranked Moments")
        panes.add(transcript_box, weight=1)
        panes.add(results_box, weight=2)

        search_row = ttk.Frame(transcript_box)
        search_row.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(search_row, text="Search:").pack(side="left")
        footage_search = ttk.Entry(search_row, textvariable=self.footage_search_var)
        footage_search.pack(side="left", fill="x", expand=True, padx=(8, 8))
        footage_search.bind("<Return>", lambda _e: self._search_footage_transcript())
        ttk.Button(search_row, text="Find", command=self._search_footage_transcript).pack(side="left")
        ttk.Button(search_row, text="Show All", command=lambda: self._refresh_footage_transcript()).pack(side="left", padx=(8, 0))

        transcript_frame = ttk.Frame(transcript_box)
        transcript_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.footage_transcript_tree = ttk.Treeview(
            transcript_frame, columns=("start", "end", "speaker", "text"),
            show="headings", height=7,
        )
        for col, label, width, stretch in (
            ("start", "Start", 90, False),
            ("end", "End", 90, False),
            ("speaker", "Speaker", 130, False),
            ("text", "Transcript", 760, True),
        ):
            self.footage_transcript_tree.heading(col, text=label)
            self.footage_transcript_tree.column(col, width=width, stretch=stretch)
        self.footage_transcript_tree.pack(side="left", fill="both", expand=True)
        self.footage_transcript_tree.bind("<Double-1>", lambda _e: self._preview_transcript_selection())
        tr_scroll = ttk.Scrollbar(transcript_frame, orient="vertical", command=self.footage_transcript_tree.yview)
        tr_scroll.pack(side="right", fill="y")
        self.footage_transcript_tree.configure(yscrollcommand=tr_scroll.set)

        result_frame = ttk.Frame(results_box)
        result_frame.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        self.footage_result_tree = ttk.Treeview(
            result_frame,
            columns=("rank", "start", "end", "duration", "status", "characters", "title"),
            show="headings", height=10, selectmode="extended",
        )
        specs = (
            ("rank", "#", 42, False),
            ("start", "Start", 82, False),
            ("end", "End", 82, False),
            ("duration", "Dur", 60, False),
            ("status", "Status", 84, False),
            ("characters", "Characters", 170, False),
            ("title", "Moment", 620, True),
        )
        for col, label, width, stretch in specs:
            self.footage_result_tree.heading(col, text=label)
            self.footage_result_tree.column(col, width=width, stretch=stretch)
        self.footage_result_tree.pack(side="left", fill="both", expand=True)
        self.footage_result_tree.bind("<<TreeviewSelect>>", lambda _e: self._footage_result_selected())
        self.footage_result_tree.bind("<Double-1>", lambda _e: self._preview_selected_moment())
        self.footage_result_tree.bind("<Key-k>", lambda _e: self._set_moment_status("KEEP"))
        self.footage_result_tree.bind("<Key-K>", lambda _e: self._set_moment_status("KEEP"))
        self.footage_result_tree.bind("<Key-m>", lambda _e: self._set_moment_status("MAYBE"))
        self.footage_result_tree.bind("<Key-M>", lambda _e: self._set_moment_status("MAYBE"))
        self.footage_result_tree.bind("<Key-t>", lambda _e: self._set_moment_status("TRASH"))
        self.footage_result_tree.bind("<Key-T>", lambda _e: self._set_moment_status("TRASH"))
        res_scroll = ttk.Scrollbar(result_frame, orient="vertical", command=self.footage_result_tree.yview)
        res_scroll.pack(side="right", fill="y")
        self.footage_result_tree.configure(yscrollcommand=res_scroll.set)

        detail = ttk.LabelFrame(results_box, text="Selected Moment")
        detail.pack(fill="x", padx=8, pady=(4, 6))
        self.footage_detail_text = tk.Text(
            detail, height=5, wrap="word",
            background=self.field, foreground=self.fg, insertbackground=self.fg,
            selectbackground="#4a4a4a", selectforeground=self.fg, relief="flat",
        )
        self.footage_detail_text.pack(fill="x", padx=8, pady=8)
        self.footage_detail_text.configure(state="disabled")

        actions = ttk.Frame(results_box)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        self.footage_preview_btn = ttk.Button(actions, text="▶ Preview Moment", command=self._preview_selected_moment, state="disabled")
        self.footage_preview_btn.pack(side="left")
        ttk.Button(actions, text="KEEP (K)", command=lambda: self._set_moment_status("KEEP")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="MAYBE (M)", command=lambda: self._set_moment_status("MAYBE")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="TRASH (T)", command=lambda: self._set_moment_status("TRASH")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Edit Details", command=self._edit_selected_moment).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="+ Selected to Sequence", command=self._sequence_add_selected_moment).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="+ KEEP to Sequence", command=self._sequence_add_keep_moments).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="ALL Dialogue → Sequence", command=self._sequence_add_all_dialogue).pack(side="left", padx=(8, 0))
        ttk.Separator(actions, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(actions, text="Export CSV", command=lambda: self._export_footage_moments("csv")).pack(side="left")
        ttk.Button(actions, text="Export TXT", command=lambda: self._export_footage_moments("txt")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Export JSON", command=lambda: self._export_footage_moments("json")).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open Index Folder", command=self._open_footage_index_folder).pack(side="right")

        bulk = ttk.Frame(results_box)
        bulk.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(bulk, text="Bulk review:", style="Muted.TLabel").pack(side="left")
        ttk.Button(bulk, text="Keep All", command=lambda: self._set_all_moment_status("KEEP")).pack(side="left", padx=(8, 0))
        ttk.Button(bulk, text="Maybe All", command=lambda: self._set_all_moment_status("MAYBE")).pack(side="left", padx=(8, 0))
        ttk.Button(bulk, text="Trash All", command=lambda: self._set_all_moment_status("TRASH")).pack(side="left", padx=(8, 0))
        ttk.Button(bulk, text="Keep Top N", command=self._keep_top_n_moments).pack(side="left", padx=(8, 0))
        ttk.Label(bulk, text="Ctrl/Shift-select rows, or press K / M / T to mark and advance.", style="Muted.TLabel").pack(side="left", padx=(14, 0))

        ttk.Label(outer, textvariable=self.footage_status_var, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

    def _refresh_processed_versions(self):
        if not hasattr(self, "footage_version_combo"):
            return
        values = []
        if self.project:
            values = sorted(self.project.processed_variants().keys())
            if self.project.absolute_path("source"):
                values = ["ORIGINAL"] + values
        self.footage_version_combo["values"] = tuple(values)
        current = self.footage_version_var.get()
        if current not in values:
            active = (self.project.data.get("active_processed") if self.project else "") or ""
            if active in values:
                self.footage_version_var.set(active)
            elif values:
                self.footage_version_var.set(values[-1])
        if self.footage_version_var.get():
            self._select_footage_version(update_status=False)

    def _select_footage_version(self, update_status=True):
        if not self.project:
            return
        label = self.footage_version_var.get().strip().upper()
        if not label:
            return
        if label == "ORIGINAL":
            video = self.project.absolute_path("source")
            srt = self.project.srt_for("SOURCE")
        else:
            video = self.project.processed_variants().get(label)
            srt = self.project.srt_for(label)
        if video:
            self.footage_video_var.set(str(video))
            self.footage_index_path = self.project.footage_index_path(video)
        self.footage_srt_var.set(str(srt) if srt and srt.exists() else "")
        if update_status:
            self.footage_status_var.set(f"Project version selected: {label}")

    def _browse_footage_video(self):
        initial = Path(self.footage_video_var.get()).parent if self.footage_video_var.get() else self._project_dir("processed", Path.home() / "Videos")
        path = filedialog.askopenfilename(
            title="Choose cleaned video",
            initialdir=str(initial if initial.exists() else Path.home()),
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")],
        )
        if path:
            self.footage_video_var.set(path)
            video = Path(path)
            likely = video.with_suffix(".srt")
            alternate = video.with_name(video.stem + "_transcript.srt")
            if not self.footage_srt_var.get():
                if likely.is_file():
                    self.footage_srt_var.set(str(likely))
                elif alternate.is_file():
                    self.footage_srt_var.set(str(alternate))
            self._save_preferences()

    def _browse_footage_srt(self):
        initial = Path(self.footage_srt_var.get()).parent if self.footage_srt_var.get() else self._project_dir("transcript", Path.home())
        path = filedialog.askopenfilename(
            title="Choose matching SRT",
            initialdir=str(initial if initial.exists() else Path.home()),
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")],
        )
        if path:
            self.footage_srt_var.set(path)
            self._save_preferences()

    def _load_footage_map(self):
        try:
            video = Path(self.footage_video_var.get()).expanduser()
            srt = Path(self.footage_srt_var.get()).expanduser()
            if not video.is_file():
                raise RuntimeError("Choose the cleaned video first.")
            if not srt.is_file():
                raise RuntimeError("Choose the matching SRT file first.")

            entries = fa.parse_srt(srt)
            duration, has_video, _has_audio = self._probe(video)
            if not has_video:
                raise RuntimeError("The selected footage file does not contain a video stream.")

            subtitle_end = max((entry.end for entry in entries), default=0.0)
            mismatch = subtitle_end - duration
            if mismatch > 3.0:
                raise RuntimeError(
                    "The SRT runs past the selected video by "
                    f"{mismatch:.1f} seconds. Choose the SRT made for this cleaned video."
                )

            self.footage_entries = entries
            self.footage_video_duration = duration
            self.footage_index_path = self.project.footage_index_path(video) if self.project else fa.sidecar_path_for(video)
            speakers = fa.infer_speakers(entries)
            self.footage_character_combo["values"] = [""] + speakers
            self._refresh_footage_transcript()

            loaded_history = False
            if self.footage_index_path.is_file():
                try:
                    payload = fa.load_index(self.footage_index_path)
                    same_srt = Path(payload.get("source_srt", "")).name == srt.name
                    if same_srt:
                        skipped_moments = []
                        self.footage_moments = fa.moments_from_index(payload, skipped=skipped_moments)
                        if skipped_moments:
                            self.events.put(("log",
                                f"Footage index: skipped {len(skipped_moments)} unreadable moment(s): "
                                + "; ".join(skipped_moments[:5]) + "\n"))
                        analysis = payload.get("analysis", {})
                        if analysis.get("mode") in fa.MODE_LABELS:
                            self.footage_mode_var.set(analysis["mode"])
                        self.footage_character_var.set(analysis.get("character_filter", ""))
                        self.footage_query_var.set(analysis.get("query", ""))
                        self._refresh_footage_results()
                        loaded_history = bool(self.footage_moments)
                except Exception:
                    loaded_history = False

            self.footage_analyze_btn.configure(state="normal")
            self.footage_preview_btn.configure(state="normal")
            msg = (
                f"Loaded {len(entries):,} subtitle lines • video {fa.seconds_to_clock(duration)}"
                + (f" • {len(speakers)} named speaker(s)" if speakers else "")
            )
            if loaded_history:
                msg += f" • restored {len(self.footage_moments)} saved moments"
            self.footage_status_var.set(msg)
            self._save_preferences()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _refresh_footage_transcript(self, entries=None):
        if not hasattr(self, "footage_transcript_tree"):
            return
        tree = self.footage_transcript_tree
        tree.delete(*tree.get_children())
        rows = self.footage_entries if entries is None else entries
        for entry in rows:
            tree.insert(
                "", "end", iid=f"srt-{entry.index}-{int(entry.start*1000)}",
                values=(
                    fa.seconds_to_clock(entry.start),
                    fa.seconds_to_clock(entry.end),
                    entry.speaker or "—",
                    entry.text,
                ),
                tags=(str(entry.index),),
            )

    def _search_footage_transcript(self):
        if not self.footage_entries:
            return
        query = self.footage_search_var.get().strip()
        matches = fa.search_entries(self.footage_entries, query)
        self._refresh_footage_transcript(matches)
        self.footage_status_var.set(
            f"Transcript search: {len(matches):,} match(es)" if query else f"Showing all {len(matches):,} subtitle lines."
        )

    def _entry_for_tree_item(self, item_id):
        if not item_id:
            return None
        values = self.footage_transcript_tree.item(item_id, "values")
        if not values:
            return None
        start_text = values[0]
        for entry in self.footage_entries:
            if fa.seconds_to_clock(entry.start) == start_text and entry.text == values[3]:
                return entry
        return None

    def _preview_transcript_selection(self):
        sel = self.footage_transcript_tree.selection()
        if not sel:
            return
        entry = self._entry_for_tree_item(sel[0])
        if entry:
            self._play_footage_range(entry.start, entry.end)

    def _footage_mode_changed(self):
        full_dialogue = self.footage_mode_var.get() == "Full Dialogue"
        if hasattr(self, "footage_analyze_btn"):
            self.footage_analyze_btn.configure(text="Build Full Dialogue" if full_dialogue else "Analyze Moments")
        if full_dialogue:
            # Full Dialogue is never capped by Top 10/20/30. Make that explicit
            # instead of leaving a misleading numeric limit visible.
            self.footage_top_var.set("ALL")
            self.footage_status_var.set(
                "Full Dialogue: ALL SRT dialogue will be kept in chronological order and added to Sequence Builder."
            )

    def _analyze_footage_moments(self):
        if not self.footage_entries:
            messagebox.showinfo(APP_TITLE, "Load the footage and matching SRT first.")
            return

        # ALL is the no-ranking-cap path. It is equivalent to Full Dialogue:
        # every SRT line is used, chronological order is preserved, and nearby
        # padded dialogue spans are merged by _sequence_add_all_dialogue().
        top_value = self.footage_top_var.get().strip().upper()
        if self.footage_mode_var.get() == "Full Dialogue" or top_value == "ALL":
            self._sequence_add_all_dialogue()
            return

        try:
            top_n = int(top_value)
            if not 1 <= top_n <= 200:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_TITLE, "Top count must be 1–200, or ALL for the full dialogue sequence.")
            return

        old = list(self.footage_moments)
        moments = fa.rank_moments(
            self.footage_entries,
            mode=self.footage_mode_var.get(),
            top_n=top_n,
            character=self.footage_character_var.get(),
            query=self.footage_query_var.get(),
        )

        # Preserve human decisions/notes when a re-analysis finds essentially the
        # same time span. Machine ranking may change; human curation does not.
        # "Same span" is measured against the LONGER clip, so a 6-second smash cut
        # inside a 90-second KEEP scene does not inherit that scene's status or ID.
        # Each earlier moment can be claimed by at most one new moment.
        claimed = set()
        for moment in moments:
            best = None
            best_overlap = 0.0
            for previous in old:
                if id(previous) in claimed:
                    continue
                overlap = max(0.0, min(moment.end, previous.end) - max(moment.start, previous.start))
                denom = max(0.1, max(moment.duration, previous.duration))
                ratio = overlap / denom
                if ratio > best_overlap:
                    best, best_overlap = previous, ratio
            if best is not None and best_overlap >= 0.72:
                claimed.add(id(best))
                # Keep the old ID so sequence items linked via source_moment_id still match.
                moment.id = best.id
                moment.status = best.status
                moment.notes = best.notes
                moment.chapter = best.chapter
                moment.tags = list(best.tags)
                if best.title and best.title != best.text[:82]:
                    moment.title = best.title

        # Never drop human work: reviewed or annotated moments that the new
        # ranking didn't reproduce are carried over instead of being erased
        # from the saved index.
        kept = [
            prev for prev in old
            if id(prev) not in claimed
            and (prev.status != "UNREVIEWED" or prev.notes or prev.chapter or prev.tags)
        ]
        for idx, prev in enumerate(kept, len(moments) + 1):
            prev.rank = idx
        moments = moments + kept

        self.footage_moments = moments
        self._refresh_footage_results()
        self._save_footage_index()
        self.footage_status_var.set(
            f"Found {len(moments) - len(kept)} ranked moment(s) • {self.footage_mode_var.get()}"
            + (f" • kept {len(kept)} earlier reviewed moment(s)" if kept else "")
            + " • index saved"
        )

    def _clear_footage_results(self):
        """Clear ranked-moment results without unloading the video or SRT."""
        self.footage_moments = []
        self._refresh_footage_results()
        self._set_footage_detail("")
        self._save_footage_index()
        self.footage_status_var.set(
            "Ranked Moments cleared. Video and SRT are still loaded; choose a new mode/top count and Analyze Moments."
        )

    def _refresh_footage_results(self, select_id=None):
        if not hasattr(self, "footage_result_tree"):
            return
        tree = self.footage_result_tree
        tree.delete(*tree.get_children())
        for idx, moment in enumerate(self.footage_moments):
            iid = f"moment-row-{idx}"
            tree.insert(
                "", "end", iid=iid,
                values=(
                    moment.rank or idx + 1,
                    fa.seconds_to_clock(moment.start),
                    fa.seconds_to_clock(moment.end),
                    f"{moment.duration:.1f}s",
                    {"KEEP": "KEEP ✓", "MAYBE": "MAYBE ?", "TRASH": "TRASH ×"}.get(moment.status, moment.status),
                    ", ".join(moment.speakers) if moment.speakers else "—",
                    moment.title,
                ),
            )
            if select_id == moment.id:
                tree.selection_set(iid)
                tree.see(iid)
        if not self.footage_moments:
            self._set_footage_detail("")

    def _selected_footage_moment(self):
        sel = self.footage_result_tree.selection() if hasattr(self, "footage_result_tree") else ()
        if not sel:
            return None
        try:
            idx = int(sel[0].rsplit("-", 1)[-1])
            return self.footage_moments[idx]
        except (ValueError, IndexError):
            return None

    def _footage_result_selected(self):
        moment = self._selected_footage_moment()
        if not moment:
            self._set_footage_detail("")
            return
        detail = (
            f"#{moment.rank}  {fa.seconds_to_clock(moment.start)}–{fa.seconds_to_clock(moment.end)}  "
            f"{moment.duration:.1f}s  [{moment.status}]\n"
            f"Why: {moment.reason}\n"
            f"Chapter: {moment.chapter or '—'}    Tags: {', '.join(moment.tags) if moment.tags else '—'}\n"
            f"{moment.text}"
        )
        self._set_footage_detail(detail)

    def _set_footage_detail(self, text):
        if not hasattr(self, "footage_detail_text"):
            return
        self.footage_detail_text.configure(state="normal")
        self.footage_detail_text.delete("1.0", "end")
        if text:
            self.footage_detail_text.insert("1.0", text)
        self.footage_detail_text.configure(state="disabled")

    def _play_footage_range(self, start, end, preroll=2.0, postroll=2.0):
        """Open/reuse the dedicated Footage Analysis preview window.

        The preview intentionally mirrors Review & Export: embedded picture,
        hidden synchronized audio, scrubber, ±5 second transport, play/pause,
        expand/contract, and an external-player option.
        """
        try:
            video = Path(self.footage_video_var.get()).expanduser()
            if not video.is_file():
                raise RuntimeError("The footage video is missing.")
            if not self.ffmpeg:
                raise RuntimeError("FFmpeg is required for footage preview.")
            duration = float(self.footage_video_duration or 0.0)
            if duration <= 0:
                duration, has_video, _ = self._probe(video)
                if not has_video:
                    raise RuntimeError("The selected footage does not contain a video stream.")
                self.footage_video_duration = duration

            colors = {
                "bg": self.bg, "field": self.field, "fg": self.fg,
                "muted": self.muted, "accent": self.accent,
            }
            if self.footage_preview_window is not None and not self.footage_preview_window.closed:
                self.footage_preview_window.update_range(
                    video=video, start=float(start), end=float(end),
                    video_duration=duration,
                    title="Silence Slicer — Footage Moment",
                )
            else:
                self.footage_preview_window = fp.FootagePreviewWindow(
                    self, ffmpeg=self.ffmpeg, ffplay=self.ffplay, video=video,
                    start=float(start), end=float(end), video_duration=duration,
                    creationflags=self._creationflags(),
                    title="Silence Slicer — Footage Moment",
                    preroll=preroll, postroll=postroll, colors=colors,
                )
            self.footage_status_var.set(
                f"Previewing {fa.seconds_to_clock(float(start))}–{fa.seconds_to_clock(float(end))}."
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _preview_selected_moment(self):
        moment = self._selected_footage_moment()
        if not moment:
            messagebox.showinfo(APP_TITLE, "Select a ranked moment first.")
            return
        self._play_footage_range(moment.start, moment.end)

    def _selected_footage_moments(self):
        if not hasattr(self, "footage_result_tree"):
            return []
        selected = set(self.footage_result_tree.selection())
        result = []
        for iid in self.footage_result_tree.get_children():
            if iid not in selected:
                continue
            try:
                idx = int(iid.rsplit("-", 1)[-1])
                result.append(self.footage_moments[idx])
            except (ValueError, IndexError):
                pass
        return result

    def _set_moment_status(self, status):
        if status not in fa.STATUS_VALUES or not hasattr(self, "footage_result_tree"):
            return
        tree = self.footage_result_tree
        selected_iids = list(tree.selection())
        moments = self._selected_footage_moments()
        if not moments:
            return

        rows_before = list(tree.get_children())
        first_pos = rows_before.index(selected_iids[0]) if selected_iids and selected_iids[0] in rows_before else 0
        single = len(moments) == 1
        for moment in moments:
            moment.status = status

        self._refresh_footage_results()
        self._save_footage_index()

        rows = list(tree.get_children())
        if rows:
            next_pos = min(first_pos + (1 if single else len(moments)), len(rows) - 1)
            iid = rows[next_pos]
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
            tree.focus_set()
            self._footage_result_selected()

        if single:
            moment = moments[0]
            self.footage_status_var.set(
                f"{status}: {fa.seconds_to_clock(moment.start)}–{fa.seconds_to_clock(moment.end)} • saved • next selected"
            )
        else:
            self.footage_status_var.set(f"{status}: marked {len(moments)} selected moments • saved")

    def _set_all_moment_status(self, status):
        if status not in fa.STATUS_VALUES or not self.footage_moments:
            return
        for moment in self.footage_moments:
            moment.status = status
        self._refresh_footage_results()
        self._save_footage_index()
        self.footage_status_var.set(f"{status}: marked all {len(self.footage_moments)} moments • saved")

    def _keep_top_n_moments(self):
        if not self.footage_moments:
            return
        top_value = self.footage_top_var.get().strip().upper()
        if top_value == "ALL":
            top_n = len(self.footage_moments)
        else:
            try:
                top_n = max(1, min(int(top_value), len(self.footage_moments)))
            except ValueError:
                top_n = min(20, len(self.footage_moments))
        for idx, moment in enumerate(self.footage_moments):
            moment.status = "KEEP" if idx < top_n else moment.status
        self._refresh_footage_results()
        self._save_footage_index()
        label = "ALL" if top_value == "ALL" else str(top_n)
        self.footage_status_var.set(f"KEEP: marked {label} ranked moments • saved")

    def _edit_selected_moment(self):
        moment = self._selected_footage_moment()
        if not moment:
            messagebox.showinfo(APP_TITLE, "Select a ranked moment first.")
            return

        win = tk.Toplevel(self)
        win.title("Edit Footage Moment")
        win.transient(self)
        win.grab_set()
        win.geometry("720x480")
        win.configure(bg=self.bg)

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=12, pady=12)
        title_var = tk.StringVar(value=moment.title)
        chapter_var = tk.StringVar(value=moment.chapter)
        tags_var = tk.StringVar(value=", ".join(moment.tags))

        ttk.Label(frame, text=f"{fa.seconds_to_clock(moment.start)}–{fa.seconds_to_clock(moment.end)}", style="Big.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="Title:").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=title_var).grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Documentary chapter/use:").grid(row=2, column=0, sticky="nw", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=chapter_var).grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Tags (comma separated):").grid(row=3, column=0, sticky="nw", padx=(0, 8), pady=5)
        ttk.Entry(frame, textvariable=tags_var).grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Label(frame, text="Notes:").grid(row=4, column=0, sticky="nw", padx=(0, 8), pady=5)
        notes = tk.Text(
            frame, height=10, wrap="word", background=self.field, foreground=self.fg,
            insertbackground=self.fg, selectbackground="#4a4a4a", relief="flat",
        )
        notes.grid(row=4, column=1, sticky="nsew", pady=5)
        notes.insert("1.0", moment.notes)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(4, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=1, sticky="e", pady=(10, 0))

        def save_and_close():
            moment.title = title_var.get().strip() or moment.title
            moment.chapter = chapter_var.get().strip()
            moment.tags = [t.strip() for t in tags_var.get().split(",") if t.strip()]
            moment.notes = notes.get("1.0", "end-1c").strip()
            moment_id = moment.id
            self._refresh_footage_results(select_id=moment_id)
            self._footage_result_selected()
            self._save_footage_index()
            win.destroy()

        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", command=save_and_close).pack(side="right", padx=(0, 8))

    def _footage_payload(self):
        return fa.make_index_payload(
            self.footage_video_var.get(),
            self.footage_srt_var.get(),
            self.footage_entries,
            self.footage_moments,
            video_duration=self.footage_video_duration,
            mode=self.footage_mode_var.get(),
            character_filter=self.footage_character_var.get(),
            query=self.footage_query_var.get(),
        )

    def _save_footage_index(self):
        if not self.footage_entries or not self.footage_video_var.get():
            return None
        try:
            target = self.footage_index_path or fa.sidecar_path_for(self.footage_video_var.get())
            self.footage_index_path = fa.save_index(target, self._footage_payload())
            self._project_register_index(self.footage_index_path)
            return self.footage_index_path
        except Exception as exc:
            self.footage_status_var.set(f"Could not save footage index: {exc}")
            return None

    def _export_footage_moments(self, kind):
        if not self.footage_moments:
            messagebox.showinfo(APP_TITLE, "Analyze moments first.")
            return
        video = Path(self.footage_video_var.get())
        suffix = {"csv": ".csv", "txt": ".txt", "json": ".json"}[kind]
        initial_name = video.stem + "_moments" + suffix
        if self.project:
            target = self.project.analysis_path(video.stem + "_moments", suffix)
        else:
            target = filedialog.asksaveasfilename(
                title=f"Export {kind.upper()} moment list", initialdir=str(video.parent), initialfile=initial_name,
                defaultextension=suffix, filetypes=[(kind.upper(), f"*{suffix}"), ("All files", "*.*")],
            )
            if not target: return
        try:
            if kind == "csv": fa.export_moments_csv(target, self.footage_moments, str(video))
            elif kind == "txt": fa.export_moments_txt(target, self.footage_moments, str(video))
            else: fa.export_moments_json(target, self._footage_payload())
            self.footage_status_var.set(f"Exported {kind.upper()}: {Path(target).name}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _open_footage_index_folder(self):
        try:
            folder = self._project_dir("analysis", Path(self.footage_video_var.get()).expanduser().parent)
            if not folder.exists():
                raise RuntimeError("Load a footage video first.")
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))


    # ---------- Story Crossrefs ----------
    def _build_crossref_tab(self, parent):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        diary = ttk.LabelFrame(outer, text="Diary Roll")
        diary.pack(fill="x")
        ttk.Label(diary, text="Diary CSV:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(diary, textvariable=self.crossref_diary_var).grid(row=0, column=1, sticky="ew", padx=8, pady=7)
        ttk.Button(diary, text="Import Diary Roll…", command=self._crossref_import_diary).grid(row=0, column=2, padx=8, pady=7)
        diary.columnconfigure(1, weight=1)

        sources = ttk.LabelFrame(outer, text="Footage Moment Sources")
        sources.pack(fill="x", pady=(10, 0))
        ttk.Button(sources, text="Add Footage Index(es)…", command=self._crossref_add_indexes).pack(side="left", padx=8, pady=7)
        ttk.Button(sources, text="Use Current Footage Analysis", command=self._crossref_use_current).pack(side="left", padx=(0, 8), pady=7)
        ttk.Button(sources, text="Scan Project Analysis Folder", command=self._crossref_scan_project).pack(side="left", padx=(0, 8), pady=7)
        self.crossref_source_label = ttk.Label(sources, text="0 footage moments loaded", style="Muted.TLabel")
        self.crossref_source_label.pack(side="right", padx=8, pady=7)

        controls = ttk.LabelFrame(outer, text="Find Memory / Callback Links")
        controls.pack(fill="x", pady=(10, 0))
        ttk.Label(controls, text="Filter diary for:").grid(row=0, column=0, sticky="w", padx=8, pady=7)
        ttk.Entry(controls, textvariable=self.crossref_query_var, width=24).grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=7)
        ttk.Label(controls, text="Minimum score:").grid(row=0, column=2, sticky="w", padx=(6, 4), pady=7)
        ttk.Entry(controls, textvariable=self.crossref_threshold_var, width=8).grid(row=0, column=3, sticky="w", padx=(0, 8), pady=7)
        ttk.Button(controls, text="Cross-Reference", command=self._crossref_run).grid(row=0, column=4, padx=8, pady=7)
        ttk.Button(controls, text="Export CSV", command=lambda: self._crossref_export("csv")).grid(row=0, column=5, padx=(0, 8), pady=7)
        ttk.Button(controls, text="Export JSON", command=lambda: self._crossref_export("json")).grid(row=0, column=6, padx=(0, 8), pady=7)
        controls.columnconfigure(1, weight=1)

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True, pady=(10, 0))
        self.crossref_tree = ttk.Treeview(
            body,
            columns=("score","type","diary","dtime","moment","mtime","shared"),
            show="headings", selectmode="browse",
        )
        for col,label,width,stretch in (
            ("score","Score",65,False),("type","Relationship",165,False),("diary","Diary memory",280,True),("dtime","Diary @",90,False),
            ("moment","Related footage moment",320,True),("mtime","Footage @",90,False),("shared","Why",280,True),
        ):
            self.crossref_tree.heading(col,text=label); self.crossref_tree.column(col,width=width,stretch=stretch)
        self.crossref_tree.pack(side="left", fill="both", expand=True)
        scroll=ttk.Scrollbar(body,orient="vertical",command=self.crossref_tree.yview); scroll.pack(side="right",fill="y")
        self.crossref_tree.configure(yscrollcommand=scroll.set)
        self.crossref_tree.bind("<Double-1>", lambda _e: self._crossref_preview_moment())

        actions = ttk.Frame(outer); actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="▶ Preview Related Footage", command=self._crossref_preview_moment).pack(side="left")
        ttk.Button(actions, text="▶ Preview Diary Roll", command=self._crossref_preview_diary).pack(side="left", padx=(8,0))
        ttk.Button(actions, text="Send Related Moment to Sequence", command=self._crossref_send_sequence).pack(side="left", padx=(8,0))
        ttk.Label(actions, textvariable=self.crossref_status_var, style="Muted.TLabel").pack(side="right")

    def _crossref_import_diary(self):
        initial = self._project_dir("analysis", Path.home())
        path = filedialog.askopenfilename(title="Choose diary roll CSV", initialdir=str(initial if initial.exists() else Path.home()), filetypes=[("CSV files","*.csv"),("All files","*.*")])
        if not path: return
        try:
            self.crossref_diaries = sx.load_diary_csv(path)
            if not self.crossref_diaries: raise RuntimeError("No diary entries were found in that CSV.")
            self.crossref_diary_var.set(path)
            self.crossref_status_var.set(f"Loaded {len(self.crossref_diaries)} diary entries.")
            self._save_preferences()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _crossref_add_indexes(self):
        initial = self._project_dir("analysis", Path.home())
        paths = filedialog.askopenfilenames(title="Choose footage index JSON or moment CSV files", initialdir=str(initial if initial.exists() else Path.home()), filetypes=[("Indexes / CSV","*.json *.csv"),("All files","*.*")])
        if not paths: return
        try:
            added=0
            for raw in paths:
                p=Path(raw)
                items = sx.load_footage_index(p) if p.suffix.lower()==".json" else sx.moments_from_csv(p)
                self.crossref_moments.extend(items); added += len(items)
                if str(p) not in self.crossref_footage_sources: self.crossref_footage_sources.append(str(p))
            self.crossref_source_label.configure(text=f"{len(self.crossref_moments)} footage moments loaded")
            self.crossref_status_var.set(f"Added {added} footage moments.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _crossref_use_current(self):
        if not self.footage_moments:
            messagebox.showinfo(APP_TITLE, "Load/analyze footage in Footage Analysis first."); return
        video=self.footage_video_var.get().strip()
        existing={(m.source_video,m.id) for m in self.crossref_moments}
        added=0
        for m in self.footage_moments:
            key=(video,m.id)
            if key in existing: continue
            self.crossref_moments.append(sx.FootageMoment(id=m.id,start=m.start,end=m.end,title=m.title or m.text[:90],text=m.text,characters=", ".join(m.speakers),source_video=video,source_index=str(self.footage_index_path or ""),status=m.status,category=m.category,tags=", ".join(m.tags)))
            added+=1
        self.crossref_source_label.configure(text=f"{len(self.crossref_moments)} footage moments loaded")
        self.crossref_status_var.set(f"Added {added} current footage moments.")

    def _crossref_scan_project(self):
        if not self.project:
            messagebox.showinfo(APP_TITLE, "Open a project first."); return
        folder=self.project.folder("analysis")
        paths=sorted(folder.glob("*_footage_index.json"))
        if not paths:
            messagebox.showinfo(APP_TITLE, "No footage indexes were found in this project's analysis folder."); return
        self.crossref_moments=[]; self.crossref_footage_sources=[]
        try:
            for p in paths:
                self.crossref_moments.extend(sx.load_footage_index(p)); self.crossref_footage_sources.append(str(p))
            self.crossref_source_label.configure(text=f"{len(self.crossref_moments)} footage moments loaded")
            self.crossref_status_var.set(f"Loaded {len(paths)} project footage index(es).")
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

    def _crossref_run(self):
        try:
            if not self.crossref_diaries:
                diary=Path(self.crossref_diary_var.get()).expanduser()
                if diary.is_file(): self.crossref_diaries=sx.load_diary_csv(diary)
            if not self.crossref_diaries: raise RuntimeError("Import the diary roll CSV first.")
            if not self.crossref_moments: raise RuntimeError("Add footage indexes or use the current Footage Analysis moments first.")
            threshold=float(self.crossref_threshold_var.get() or 0.18)
            self.crossref_refs=sx.build_crossrefs(self.crossref_diaries,self.crossref_moments,min_score=threshold,max_per_diary=5,query=self.crossref_query_var.get())
            self.crossref_tree.delete(*self.crossref_tree.get_children())
            for i,r in enumerate(self.crossref_refs):
                self.crossref_tree.insert("","end",iid=f"xref-{i}",values=(f"{r.score:.2f}",r.relation_type,r.diary_title[:90],sx.clock(r.diary_start),r.moment_title[:95],sx.clock(r.moment_start),r.reason[:120]))
            self.crossref_status_var.set(f"Found {len(self.crossref_refs)} suggested story link(s).")
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

    def _selected_crossref(self):
        sel=self.crossref_tree.selection()
        if not sel: return None
        try: return self.crossref_refs[int(sel[0].split("-")[-1])]
        except Exception: return None

    def _crossref_play(self, video, start, end, title):
        try:
            p=Path(video).expanduser()
            if not p.is_file(): raise RuntimeError(f"Source video is missing:\n{p}")
            if self.crossref_preview_proc and self.crossref_preview_proc.poll() is None:
                try: self.crossref_preview_proc.terminate()
                except Exception: pass
            if self.ffplay:
                ps=max(0,float(start)-2.0); dur=max(1.0,float(end)-float(start)+4.0)
                self.crossref_preview_proc=subprocess.Popen([str(self.ffplay),"-hide_banner","-loglevel","warning","-ss",f"{ps:.3f}","-t",f"{dur:.3f}","-autoexit","-window_title",title,str(p)],creationflags=self._creationflags())
            elif os.name=="nt": os.startfile(str(p))
            else: raise RuntimeError("ffplay is required for timestamped preview.")
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

    def _crossref_preview_moment(self):
        r=self._selected_crossref()
        if not r: return
        self._crossref_play(r.moment_video,r.moment_start,r.moment_end,"Silence Slicer — Related Footage")

    def _crossref_preview_diary(self):
        r=self._selected_crossref()
        if not r: return
        self._crossref_play(r.diary_video,r.diary_start,r.diary_end,"Silence Slicer — Diary Roll")

    def _crossref_send_sequence(self):
        r = self._selected_crossref()
        if not r:
            messagebox.showinfo(APP_TITLE, "Select a story link first.")
            return
        for m in self.crossref_moments:
            if m.id == r.moment_id and m.source_video == r.moment_video:
                try:
                    chars = [x.strip() for x in m.characters.split(",") if x.strip()]
                    item = sb.make_item(
                        m.source_video, m.start, m.end, title=m.title, characters=chars,
                        category="Story Crossref", notes=f"Diary link: {r.diary_title} | {r.reason}",
                        source_moment_id=m.id, source_index=m.source_index,
                    )
                except Exception as exc:
                    messagebox.showerror(APP_TITLE, f"Could not add that moment to the sequence:\n{exc}")
                    return
                existing = next((i for i, it in enumerate(self.sequence_items) if it.id == item.id), None)
                if existing is not None:
                    self._sequence_refresh(select_index=existing)
                    self.crossref_status_var.set(f"That moment is already in the sequence (clip {existing + 1}).")
                    return
                self.sequence_items.append(item)
                self._sequence_refresh(select_index=len(self.sequence_items) - 1)
                msg = f"Added related footage moment to Sequence Builder (clip {len(self.sequence_items)})."
                self.sequence_status_var.set(msg)
                self.crossref_status_var.set(msg)
                return
        messagebox.showwarning(APP_TITLE, "The footage moment for that link is no longer loaded. Re-add its footage index and run Cross-Reference again.")

    def _crossref_export(self, kind):
        if not self.crossref_refs:
            messagebox.showinfo(APP_TITLE,"Run Cross-Reference first."); return
        base=self._project_dir("analysis", Path.home())
        ext=".csv" if kind=="csv" else ".json"
        if self.project:
            path=self.project.analysis_path("story_crossrefs", ext)
        else:
            path=filedialog.asksaveasfilename(title=f"Export story crossrefs {kind.upper()}",initialdir=str(base),initialfile="story_crossrefs"+ext,defaultextension=ext)
            if not path: return
        try:
            if kind=="csv": sx.save_crossrefs_csv(path,self.crossref_refs)
            else: sx.save_crossrefs_json(path,self.crossref_refs,[self.crossref_diary_var.get()],self.crossref_footage_sources)
            self.crossref_status_var.set(f"Exported {len(self.crossref_refs)} story links.")
        except Exception as exc: messagebox.showerror(APP_TITLE,str(exc))

    # ---------- Sequence Builder ----------
    def _build_sequence_tab(self, parent):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        setup = ttk.LabelFrame(outer, text="Rough Sequence")
        setup.pack(fill="x")
        ttk.Label(setup, text="Sequence name:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(setup, textvariable=self.sequence_name_var).grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(setup, text="Timeline FPS:").grid(row=0, column=2, sticky="w", padx=(8, 4), pady=6)
        ttk.Combobox(setup, textvariable=self.sequence_fps_var, values=tuple(sb.FPS_CHOICES), state="readonly", width=7).grid(row=0, column=3, sticky="w", padx=(0, 8), pady=6)
        ttk.Button(setup, text="Import Footage Index…", command=self._sequence_import_index).grid(row=0, column=4, padx=8, pady=6)
        ttk.Button(setup, text="Load Sequence…", command=self._sequence_load_project).grid(row=0, column=5, padx=8, pady=6)
        ttk.Button(setup, text="Save Sequence…", command=self._sequence_save_project).grid(row=0, column=6, padx=8, pady=6)

        ttk.Label(setup, text="Output format:").grid(row=1, column=0, sticky="w", padx=8, pady=(0, 6))
        ttk.Combobox(
            setup,
            textvariable=self.sequence_format_var,
            values=(
                "16:9 — 1920x1080",
                "9:16 — 1080x1920",
                "4:5 — 1080x1350",
                "1:1 — 1080x1080",
            ),
            state="readonly",
            width=22,
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(0, 6))

        ttk.Label(setup, text="Framing:").grid(row=1, column=2, sticky="w", padx=(8, 4), pady=(0, 6))
        ttk.Combobox(
            setup,
            textvariable=self.sequence_framing_var,
            values=("Fill / center crop", "Fit / letterbox"),
            state="readonly",
            width=18,
        ).grid(row=1, column=3, columnspan=2, sticky="w", padx=(0, 8), pady=(0, 6))
        setup.columnconfigure(1, weight=1)

        ttk.Label(
            outer,
            text="Pre-editor only: arrange chosen moments here, then export a rough assembly or Resolve timeline. Finishing stays in DaVinci.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(8, 2))

        table_box = ttk.LabelFrame(outer, text="Sequence Order")
        table_box.pack(fill="both", expand=True, pady=(8, 0))
        table_frame = ttk.Frame(table_box)
        table_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.sequence_tree = ttk.Treeview(
            table_frame,
            columns=("order", "timeline", "source", "duration", "characters", "title"),
            show="headings", selectmode="browse", height=16,
        )
        specs = (
            ("order", "#", 42, False),
            ("timeline", "Timeline", 95, False),
            ("source", "Source In", 90, False),
            ("duration", "Dur", 60, False),
            ("characters", "Characters", 170, False),
            ("title", "Moment", 600, True),
        )
        for col, label, width, stretch in specs:
            self.sequence_tree.heading(col, text=label)
            self.sequence_tree.column(col, width=width, stretch=stretch)
        self.sequence_tree.pack(side="left", fill="both", expand=True)
        self.sequence_tree.bind("<Double-1>", lambda _e: self._sequence_preview_selected())
        seq_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.sequence_tree.yview)
        seq_scroll.pack(side="right", fill="y")
        self.sequence_tree.configure(yscrollcommand=seq_scroll.set)

        edit_row = ttk.Frame(outer)
        edit_row.pack(fill="x", pady=(8, 0))
        ttk.Button(edit_row, text="▲ Move Up", command=lambda: self._sequence_move(-1)).pack(side="left")
        ttk.Button(edit_row, text="▼ Move Down", command=lambda: self._sequence_move(1)).pack(side="left", padx=(8, 0))
        ttk.Button(edit_row, text="▶ Preview Selected", command=self._sequence_preview_selected).pack(side="left", padx=(8, 0))
        ttk.Button(edit_row, text="▶ Play Assembly", command=self._sequence_play_assembly).pack(side="left", padx=(8, 0))
        ttk.Button(edit_row, text="Remove", command=self._sequence_remove_selected).pack(side="left", padx=(8, 0))
        ttk.Button(edit_row, text="Clear", command=self._sequence_clear).pack(side="left", padx=(8, 0))
        self.sequence_summary_var = tk.StringVar(value="0 clips • 00:00")
        ttk.Label(edit_row, textvariable=self.sequence_summary_var, style="Big.TLabel").pack(side="right")

        exports = ttk.LabelFrame(outer, text="Send It Downstream")
        exports.pack(fill="x", pady=(10, 0))
        row = ttk.Frame(exports)
        row.pack(fill="x", padx=8, pady=8)
        ttk.Button(row, text="Export Resolve Package…", command=self._sequence_export_fcpxml).pack(side="left")
        ttk.Button(row, text="Export Edit CSV…", command=lambda: self._sequence_export_list("csv")).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Export Edit TXT…", command=lambda: self._sequence_export_list("txt")).pack(side="left", padx=(8, 0))
        self.sequence_render_btn = ttk.Button(row, text="Render Rough Assembly…", command=self._sequence_render_clicked)
        self.sequence_render_btn.pack(side="left", padx=(16, 0))
        self.sequence_cancel_btn = ttk.Button(row, text="Cancel Render", command=self._sequence_cancel_render, state="disabled")
        self.sequence_cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Open Last Folder", command=self._sequence_open_last_folder).pack(side="right")

        self.sequence_progress = ttk.Progressbar(exports, mode="determinate", maximum=100)
        self.sequence_progress.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(exports, textvariable=self.sequence_progress_var).pack(anchor="e", padx=8)
        ttk.Label(exports, textvariable=self.sequence_status_var, style="Muted.TLabel").pack(anchor="w", padx=8, pady=(2, 8))
        self.sequence_last_output = None
        self.sequence_last_assembly = None

    def _sequence_item_from_moment(self, moment):
        if not self.footage_video_var.get():
            raise RuntimeError("Load the source footage before adding moments to the sequence.")
        return sb.make_item(
            self.footage_video_var.get(), moment.start, moment.end,
            title=moment.title, characters=list(moment.speakers), category=moment.category,
            chapter=moment.chapter, notes=moment.notes, tags=list(moment.tags),
            source_index=str(self.footage_index_path or ""), source_moment_id=moment.id,
        )

    def _sequence_add_selected_moment(self):
        moment = self._selected_footage_moment()
        if not moment:
            messagebox.showinfo(APP_TITLE, "Select a ranked moment first.")
            return
        try:
            self.sequence_items.append(self._sequence_item_from_moment(moment))
            self._sequence_refresh(select_index=len(self.sequence_items) - 1)
            self.sequence_status_var.set(f"Added: {moment.title}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_add_all_dialogue(self):
        """Build a chronological sequence with one editable clip per SRT line.

        Each subtitle line becomes its own SequenceItem with 0.50 s of preroll
        and 0.75 s of postroll. Nothing is merged. This is intentionally verbose
        so Resolve receives separate editable cuts instead of giant combined spans.
        """
        if not self.footage_entries:
            messagebox.showinfo(APP_TITLE, "Load the footage and matching SRT first.")
            return
        if not self.footage_video_var.get().strip():
            messagebox.showinfo(APP_TITLE, "Load the footage video first.")
            return

        video = Path(self.footage_video_var.get().strip())
        duration = float(self.footage_video_duration or 0.0)
        pre_pad = 0.50
        post_pad = 0.75

        entries = sorted(self.footage_entries, key=lambda e: (e.start, e.end, e.index))

        # "ALL Dialogue" is a build operation, not an append operation.
        # Starting from a clean sequence prevents old Top-10/20/30 ranked
        # 90-second moment windows from contaminating the dialogue cut.
        self.sequence_items.clear()
        existing = set()

        added = 0
        skipped = 0

        try:
            for number, entry in enumerate(entries, 1):
                start = max(0.0, float(entry.start) - pre_pad)
                end = float(entry.end) + post_pad
                if duration > 0:
                    end = min(duration, end)
                if end <= start:
                    skipped += 1
                    continue

                key = (video, round(start, 3), round(end, 3))
                if key in existing:
                    skipped += 1
                    continue

                speaker = (entry.speaker or "").strip()
                text = (entry.text or "").strip()
                title = text[:82] if text else f"Dialogue {number}"
                if len(text) > 82:
                    title = title.rstrip() + "…"

                self.sequence_items.append(
                    sb.make_item(
                        str(video), start, end,
                        title=title,
                        characters=([speaker] if speaker else []),
                        category="Full Dialogue",
                        chapter="",
                        notes="One SRT line per editable Resolve clip",
                        tags=["dialogue", "full-dialogue"],
                        source_index=str(self.footage_index_path or ""),
                        source_moment_id="",
                    )
                )
                existing.add(key)
                added += 1

            self._sequence_refresh(
                select_index=(len(self.sequence_items) - 1 if added else None)
            )
            total = sum(item.duration for item in self.sequence_items)

            self.sequence_status_var.set(
                f"Added {added} dialogue clip(s) from {len(entries)} SRT line(s)"
                + (f" • skipped {skipped}" if skipped else "")
                + f" • sequence {fa.seconds_to_clock(total)}."
            )
            self.footage_status_var.set(
                f"Full Dialogue REPLACED the sequence with separate editable cuts: "
                f"{added} clip(s) added from {len(entries)} SRT line(s)."
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_add_keep_moments(self):
        keep = [m for m in self.footage_moments if m.status == "KEEP"]
        if not keep:
            messagebox.showinfo(APP_TITLE, "No moments are marked KEEP in the current footage map.")
            return
        existing = {(Path(i.source_video), round(i.start, 3), round(i.end, 3)) for i in self.sequence_items}
        added = 0
        try:
            for moment in sorted(keep, key=lambda m: m.start):
                key = (Path(self.footage_video_var.get()), round(moment.start, 3), round(moment.end, 3))
                if key in existing:
                    continue
                self.sequence_items.append(self._sequence_item_from_moment(moment))
                existing.add(key)
                added += 1
            self._sequence_refresh()
            self.sequence_status_var.set(f"Added {added} KEEP moment(s) from the current footage.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_import_index(self):
        paths = filedialog.askopenfilenames(
            title="Import footage index(es)",
            initialdir=str(self._project_dir("analysis", Path.home())),
            filetypes=[("Footage index JSON", "*_footage_index.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not paths:
            return
        added = 0
        errors = []
        skipped = []
        for path in paths:
            try:
                imported = sb.items_from_footage_index(path, statuses=("KEEP",), skipped=skipped)
                self.sequence_items.extend(imported)
                added += len(imported)
            except Exception as exc:
                errors.append(f"{Path(path).name}: {exc}")
        self._sequence_refresh()
        msg = f"Imported {added} KEEP clip(s) from {len(paths)} footage index file(s)."
        if added == 0:
            counts = {}
            for path in paths:
                try:
                    for raw in fa.load_index(path).get("moments", []):
                        if isinstance(raw, dict):
                            st = str(raw.get("status", "UNREVIEWED")).upper()
                            counts[st] = counts.get(st, 0) + 1
                except Exception:
                    pass
            if counts:
                found = ", ".join(f"{n} {st}" for st, n in sorted(counts.items()))
                messagebox.showinfo(
                    APP_TITLE,
                    "No moments marked KEEP were found.\n\n"
                    f"The selected index contains: {found}.\n\n"
                    "Mark moments KEEP on the Footage Analysis tab (they save automatically), "
                    "then import the index again.",
                )
        if skipped:
            msg += f" Skipped {len(skipped)} invalid clip(s)."
        self.sequence_status_var.set(msg)
        if skipped:
            errors.extend(skipped[:15] + ([f"…and {len(skipped) - 15} more"] if len(skipped) > 15 else []))
        if errors:
            messagebox.showwarning(APP_TITLE, "Some indexes could not be imported:\n\n" + "\n".join(errors))

    def _sequence_refresh(self, select_index=None):
        if not hasattr(self, "sequence_tree"):
            return
        tree = self.sequence_tree
        tree.delete(*tree.get_children())
        cursor = 0.0
        for idx, item in enumerate(self.sequence_items):
            iid = f"sequence-row-{idx}"
            tree.insert("", "end", iid=iid, values=(
                idx + 1,
                sb.seconds_to_clock(cursor),
                sb.seconds_to_clock(item.start),
                f"{item.duration:.1f}s",
                ", ".join(item.characters) if item.characters else "—",
                item.title or Path(item.source_video).name,
            ))
            cursor += item.duration
            if select_index == idx:
                tree.selection_set(iid)
                tree.see(iid)
        self.sequence_summary_var.set(f"{len(self.sequence_items)} clips • {sb.seconds_to_clock(cursor)}")

    def _sequence_selected_index(self):
        if not hasattr(self, "sequence_tree"):
            return None
        sel = self.sequence_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0].rsplit("-", 1)[-1])
        except Exception:
            return None

    def _sequence_move(self, delta):
        idx = self._sequence_selected_index()
        if idx is None:
            return
        new = idx + int(delta)
        if not 0 <= new < len(self.sequence_items):
            return
        self.sequence_items[idx], self.sequence_items[new] = self.sequence_items[new], self.sequence_items[idx]
        self._sequence_refresh(select_index=new)

    def _sequence_remove_selected(self):
        idx = self._sequence_selected_index()
        if idx is None:
            return
        self.sequence_items.pop(idx)
        self._sequence_refresh(select_index=min(idx, len(self.sequence_items) - 1) if self.sequence_items else None)

    def _sequence_clear(self):
        if self.sequence_items and not messagebox.askyesno(APP_TITLE, "Clear the entire rough sequence?"):
            return
        self.sequence_items.clear()
        self._sequence_refresh()
        self.sequence_status_var.set("Sequence cleared.")

    def _sequence_output_geometry(self):
        label = self.sequence_format_var.get().strip()
        presets = {
            "16:9 — 1920x1080": (1920, 1080),
            "9:16 — 1080x1920": (1080, 1920),
            "4:5 — 1080x1350": (1080, 1350),
            "1:1 — 1080x1080": (1080, 1080),
        }
        return presets.get(label, (1920, 1080))

    def _sequence_preview_selected(self):
        idx = self._sequence_selected_index()
        if idx is None:
            return
        item = self.sequence_items[idx]
        try:
            source = Path(item.source_video)
            if not source.is_file():
                raise RuntimeError(f"Source file is missing:\n{source}")
            if self.sequence_preview_proc and self.sequence_preview_proc.poll() is None:
                self.sequence_preview_proc.terminate()
            ffplay = shutil.which("ffplay")
            if not ffplay and self.ffmpeg:
                candidate = Path(self.ffmpeg).with_name("ffplay.exe" if os.name == "nt" else "ffplay")
                if candidate.is_file():
                    ffplay = str(candidate)
            if not ffplay:
                raise RuntimeError("ffplay was not found beside FFmpeg or on PATH.")
            start = max(0.0, item.start - 1.0)
            duration = item.duration + 2.0
            self.sequence_preview_proc = subprocess.Popen([
                ffplay, "-hide_banner", "-loglevel", "error", "-autoexit",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", str(source),
            ])
            self.sequence_status_var.set(f"Previewing #{idx+1}: {item.title}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_play_assembly(self):
        """Play the most recently rendered whole rough assembly."""
        path = self.sequence_last_assembly
        if not path or not Path(path).is_file():
            messagebox.showinfo(
                APP_TITLE,
                "No rough assembly has been rendered for this sequence yet.\n\n"
                "Click Render Rough Assembly first, then Play Assembly will play "
                "the entire sequence from beginning to end."
            )
            return
        try:
            path = str(Path(path))
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_save_project(self):
        if not self.sequence_items:
            messagebox.showinfo(APP_TITLE, "The sequence is empty.")
            return
        if self.project:
            path = self.project.sequence_path(self.sequence_name_var.get())
        else:
            initial = Path(self.sequence_project_path).parent if self.sequence_project_path else Path.home() / "Documents"
            path = filedialog.asksaveasfilename(title="Save Silence Slicer sequence", initialdir=str(initial if initial.exists() else Path.home()), initialfile=re.sub(r"[^A-Za-z0-9_-]+", "_", self.sequence_name_var.get()).strip("_") + "_sequence.json", defaultextension=".json", filetypes=[("Silence Slicer sequence", "*.json")])
            if not path: return
        try:
            sb.save_sequence(path, self.sequence_items, self.sequence_name_var.get())
            self.sequence_project_path = str(path)
            self._project_register_sequence(path)
            self.sequence_status_var.set(f"Saved sequence: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_load_project(self):
        path = filedialog.askopenfilename(title="Load Silence Slicer sequence", initialdir=str(self._project_dir("sequences", Path.home())), filetypes=[("Sequence JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            skipped = []
            name, items = sb.load_sequence(path, skipped=skipped)
            self.sequence_name_var.set(name)
            self.sequence_items = items
            self.sequence_project_path = path
            self._sequence_refresh()
            msg = f"Loaded {len(items)} clip(s) from {Path(path).name}."
            if skipped:
                msg += f" Skipped {len(skipped)} unreadable item(s)."
                messagebox.showwarning(APP_TITLE, "Some sequence items could not be loaded:\n\n" + "\n".join(skipped[:15]))
            self.sequence_status_var.set(msg)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_export_list(self, kind):
        if not self.sequence_items:
            messagebox.showinfo(APP_TITLE, "The sequence is empty.")
            return
        ext = ".csv" if kind == "csv" else ".txt"
        if self.project:
            path = self.project.export_path(self.sequence_name_var.get() + "_edit_list", ext)
        else:
            path = filedialog.asksaveasfilename(title="Export edit list", initialdir=str(Path.home()), defaultextension=ext, filetypes=[(kind.upper(), "*" + ext)])
            if not path: return
        try:
            if kind == "csv":
                sb.export_csv(path, self.sequence_items)
            else:
                sb.export_txt(path, self.sequence_items, self.sequence_name_var.get())
            self.sequence_last_output = Path(path)
            self.sequence_status_var.set(f"Exported edit list: {Path(path).name}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_export_fcpxml(self):
        if not self.sequence_items:
            messagebox.showinfo(APP_TITLE, "The sequence is empty.")
            return

        if self.project:
            path = Path(self.project.export_path(self.sequence_name_var.get(), ".fcpxml"))
        else:
            picked = filedialog.asksaveasfilename(
                title="Export DaVinci Resolve timeline",
                initialdir=str(Path.home()),
                defaultextension=".fcpxml",
                initialfile=re.sub(r"[^A-Za-z0-9_-]+", "_", self.sequence_name_var.get()).strip("_") + ".fcpxml",
                filetypes=[("Final Cut Pro XML", "*.fcpxml"), ("XML", "*.xml")],
            )
            if not picked:
                return
            path = Path(picked)

        try:
            # Validate every source before writing anything Resolve has to parse.
            missing = sorted({
                str(Path(item.source_video))
                for item in self.sequence_items
                if not Path(item.source_video).is_file()
            })
            if missing:
                raise RuntimeError(
                    "Resolve export stopped because source media is missing:\n\n"
                    + "\n".join(missing[:10])
                )

            width, height = self._sequence_output_geometry()
            out = sb.export_fcpxml(
                path,
                self.sequence_items,
                self.sequence_name_var.get(),
                fps=self.sequence_fps_var.get(),
                width=width,
                height=height,
                ffprobe=str(self.ffprobe) if self.ffprobe else None,
            )

            # Parse our own XML back immediately. A malformed file should never
            # be presented to Resolve as a successful export.
            import xml.etree.ElementTree as _ET
            _ET.parse(out)
            if not Path(out).is_file() or Path(out).stat().st_size < 200:
                raise RuntimeError("FCPXML export produced an empty or incomplete file.")

            # Always create a CMX3600 EDL beside the XML as a conservative
            # Resolve fallback. Same sequence, same source ranges.
            edl_path = path.with_suffix(".edl")
            sb.export_edl(
                edl_path,
                self.sequence_items,
                self.sequence_name_var.get(),
                fps=self.sequence_fps_var.get(),
            )
            if not edl_path.is_file() or edl_path.stat().st_size < 100:
                raise RuntimeError("EDL fallback export produced an empty file.")

            self.sequence_last_output = Path(out)
            self.sequence_status_var.set(
                f"Resolve package exported: {Path(out).name} + {edl_path.name}"
            )
            messagebox.showinfo(
                APP_TITLE,
                "Resolve export complete.\n\n"
                f"FCPXML:\n{Path(out)}\n\n"
                f"EDL fallback:\n{edl_path}\n\n"
                "Try the FCPXML first with File → Import → Timeline. "
                "If Resolve rejects or crashes on it, import the EDL instead. "
                "Keep the source video in its current location."
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _sequence_render_clicked(self):
        if not self.sequence_items:
            messagebox.showinfo(APP_TITLE, "The sequence is empty.")
            return
        if self.project:
            path = self.project.export_path(self.sequence_name_var.get() + "_rough", ".mp4")
        else:
            path = filedialog.asksaveasfilename(title="Render rough hard-cut assembly", initialdir=str(Path.home()), defaultextension=".mp4", initialfile=re.sub(r"[^A-Za-z0-9_-]+", "_", self.sequence_name_var.get()).strip("_") + "_rough.mp4", filetypes=[("MP4 video", "*.mp4")])
            if not path: return
        if not self.ffmpeg or not self.ffprobe:
            messagebox.showerror(APP_TITLE, "FFmpeg/ffprobe are required to render an assembly.")
            return
        self.sequence_cancel.clear()
        self.sequence_render_busy = True
        self.sequence_render_btn.configure(state="disabled")
        self.sequence_cancel_btn.configure(state="normal")
        self.sequence_progress["value"] = 0
        self.sequence_progress_var.set("0%")
        self.sequence_status_var.set("Rendering rough assembly…")
        snapshot = [sb.SequenceItem(**vars(item)) for item in self.sequence_items]
        # Read Tk variables here on the main thread; the worker must not touch Tk.
        fps = self.sequence_fps_var.get()
        output_size = self._sequence_output_geometry()
        framing = self.sequence_framing_var.get()
        threading.Thread(
            target=self._sequence_render_worker,
            args=(snapshot, path, fps, output_size, framing),
            daemon=True
        ).start()

    def _sequence_render_worker(self, items, path, fps, output_size, framing):
        try:
            def progress(pct, label):
                self.events.put(("sequence_progress", pct, label))
            out = sb.render_assembly(
                self.ffmpeg, self.ffprobe, items, path,
                fps=fps,
                output_size=output_size,
                framing=framing,
                progress=progress,
                cancel_check=self.sequence_cancel.is_set,
            )
            self.events.put(("sequence_done", str(out)))
        except Exception as exc:
            self.events.put(("sequence_error", str(exc)))

    def _sequence_cancel_render(self):
        self.sequence_cancel.set()
        self.sequence_status_var.set("Cancelling after the current clip finishes…")

    def _sequence_open_last_folder(self):
        target = self.sequence_last_output
        if not target and self.sequence_project_path:
            target = Path(self.sequence_project_path)
        if not target:
            messagebox.showinfo(APP_TITLE, "No sequence output has been saved yet.")
            return
        folder = Path(target).parent
        try:
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _build_narration_tab(self, parent):
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        settings = ttk.LabelFrame(outer, text="Narration Setup")
        settings.pack(fill="x")
        ttk.Label(settings, text="Project name:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(settings, textvariable=self.narration_name_var).grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(settings, text="Voice:").grid(row=0, column=2, sticky="w", padx=8, pady=6)
        self.tts_voice_combo = ttk.Combobox(settings, textvariable=self.tts_voice_var, values=(), width=22, state="normal")
        self.tts_voice_combo.grid(row=0, column=3, sticky="ew", padx=8, pady=6)
        self.refresh_voices_btn = ttk.Button(settings, text="Refresh Voices", command=self._refresh_tts_voices)
        self.refresh_voices_btn.grid(row=0, column=4, padx=8, pady=6)

        ttk.Label(settings, text="PocketTTS endpoint:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(settings, textvariable=self.tts_endpoint_var).grid(row=1, column=1, sticky="ew", padx=8, pady=6)
        ttk.Label(settings, text="Output folder:").grid(row=1, column=2, sticky="w", padx=8, pady=6)
        ttk.Entry(settings, textvariable=self.tts_output_dir_var).grid(row=1, column=3, sticky="ew", padx=8, pady=6)
        ttk.Button(settings, text="Browse…", command=self._browse_tts_output_dir).grid(row=1, column=4, padx=8, pady=6)
        ttk.Label(settings, text="Model:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(settings, textvariable=self.tts_model_var, width=22).grid(row=2, column=1, sticky="w", padx=8, pady=6)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        script_box = ttk.LabelFrame(outer, text="Script")
        script_box.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(script_box, text="Use headings like [INTRO] or [SOUL TEAR]. They will become named audio sections.", style="Muted.TLabel").pack(anchor="w", padx=8, pady=(8, 4))
        script_frame = ttk.Frame(script_box)
        script_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.narration_text = tk.Text(
            script_frame, wrap="word", height=18,
            background=self.field, foreground=self.fg, insertbackground=self.fg,
            selectbackground="#4a4a4a", selectforeground=self.fg, relief="flat"
        )
        self.narration_text.pack(side="left", fill="both", expand=True)
        nsb = ttk.Scrollbar(script_frame, orient="vertical", command=self.narration_text.yview)
        nsb.pack(side="right", fill="y")
        self.narration_text.configure(yscrollcommand=nsb.set)

        script_buttons = ttk.Frame(outer)
        script_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(script_buttons, text="Load Text File…", command=self._load_narration_script).pack(side="left")
        ttk.Button(script_buttons, text="Save Script Copy…", command=self._save_narration_script).pack(side="left", padx=(8, 0))
        ttk.Button(script_buttons, text="Insert Example Markers", command=self._insert_narration_example).pack(side="left", padx=(8, 0))
        ttk.Button(script_buttons, text="Clear", command=lambda: self.narration_text.delete("1.0", "end")).pack(side="left", padx=(8, 0))

        render = ttk.LabelFrame(outer, text="Render")
        render.pack(fill="x", pady=(10, 0))
        buttons = ttk.Frame(render)
        buttons.pack(fill="x", padx=8, pady=8)
        self.test_voice_btn = ttk.Button(buttons, text="Test Voice", command=self._test_narration_voice)
        self.test_voice_btn.pack(side="left")
        self.render_narration_btn = ttk.Button(buttons, text="Render Narration", command=self._render_narration_clicked)
        self.render_narration_btn.pack(side="left", padx=(8, 0))
        self.subtitles_btn = ttk.Button(buttons, text="Build Transcript / SRT / VTT", command=self._build_subtitles_clicked, state="disabled")
        self.subtitles_btn.pack(side="left", padx=(8, 0))
        self.play_narration_btn = ttk.Button(buttons, text="Play Narration", command=self._play_last_narration, state="disabled")
        self.play_narration_btn.pack(side="right")
        self.open_narration_folder_btn = ttk.Button(buttons, text="Open Narration Folder", command=self._open_narration_folder)
        self.open_narration_folder_btn.pack(side="right", padx=(0, 8))

        self.narration_progress = ttk.Progressbar(render, mode="determinate", maximum=100)
        self.narration_progress.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(render, textvariable=self.narration_progress_text_var).pack(anchor="e", padx=8)
        ttk.Label(render, textvariable=self.narration_status_var, style="Muted.TLabel").pack(anchor="w", padx=8, pady=(2, 4))
        ttk.Label(render, text="Full renders automatically create a .txt transcript plus .srt and .vtt subtitle files from the exact TTS script.", style="Muted.TLabel").pack(anchor="w", padx=8, pady=(0, 8))

        nlogbox = ttk.LabelFrame(outer, text="Narration Output")
        nlogbox.pack(fill="both", expand=True, pady=(10, 0))
        self.narration_log = tk.Text(
            nlogbox, wrap="word", height=9,
            background=self.field, foreground=self.fg, insertbackground=self.fg,
            selectbackground="#4a4a4a", selectforeground=self.fg, relief="flat"
        )
        self.narration_log.pack(side="left", fill="both", expand=True)
        nsb2 = ttk.Scrollbar(nlogbox, orient="vertical", command=self.narration_log.yview)
        nsb2.pack(side="right", fill="y")
        self.narration_log.configure(yscrollcommand=nsb2.set)

    def _build_advanced(self):
        def field(label, var, row, col, width=15):
            ttk.Label(self.advanced, text=label).grid(row=row, column=col, sticky="w", padx=8, pady=5)
            ttk.Entry(self.advanced, textvariable=var, width=width).grid(row=row, column=col+1, sticky="w", padx=8, pady=5)

        field("Threshold (dB):", self.threshold_var, 0, 0)
        field("Min silence (sec):", self.min_silence_var, 0, 2)
        field("Padding (sec):", self.padding_var, 1, 0)
        field("Min kept clip (sec):", self.min_clip_var, 1, 2)

        ttk.Label(self.advanced, text="Encoder:").grid(row=2, column=0, sticky="w", padx=8, pady=5)
        ttk.Combobox(self.advanced, textvariable=self.encoder_var, values=["nvenc", "cpu", "qsv", "videotoolbox"], state="readonly", width=13).grid(row=2, column=1, sticky="w", padx=8, pady=5)
        field("Quality / CRF-CQ:", self.crf_var, 2, 2)
        field("CPU preset:", self.cpu_preset_var, 3, 0)
        field("Audio bitrate:", self.audio_var, 3, 2)
        field("Batch size:", self.batch_var, 4, 0)

        ttk.Label(self.advanced, text="Audio smoothing:").grid(row=4, column=2, sticky="w", padx=8, pady=5)
        ttk.Combobox(self.advanced, textvariable=self.audio_smoothing_var, values=list(AUDIO_SMOOTHING), state="readonly", width=13).grid(row=4, column=3, sticky="w", padx=8, pady=5)

        field("Skip intro (sec):", self.skip_intro_var, 5, 0)
        field("Skip outro (sec):", self.skip_outro_var, 5, 2)
        field("Test start (min):", self.test_start_var, 6, 0)
        field("Test duration (min):", self.test_duration_var, 6, 2)

        ttk.Checkbutton(self.advanced, text="Automatic dated/numbered filename", variable=self.auto_name_var, command=self._auto_name_output).grid(row=7, column=0, columnspan=2, sticky="w", padx=8, pady=5)
        ttk.Checkbutton(self.advanced, text="Overwrite existing output", variable=self.overwrite_var).grid(row=7, column=2, columnspan=2, sticky="w", padx=8, pady=5)

        ttk.Label(self.advanced, text="Custom preset:").grid(row=8, column=0, sticky="w", padx=8, pady=5)
        self.custom_combo = ttk.Combobox(self.advanced, textvariable=self.custom_preset_var, values=sorted(self.custom_presets), state="readonly", width=18)
        self.custom_combo.grid(row=8, column=1, sticky="w", padx=8, pady=5)
        ttk.Button(self.advanced, text="Load", command=self._load_custom_preset).grid(row=8, column=2, sticky="w", padx=8, pady=5)
        ttk.Button(self.advanced, text="Save Current…", command=self._save_custom_preset).grid(row=8, column=3, sticky="w", padx=8, pady=5)

    def _browse_tts_output_dir(self):
        p = filedialog.askdirectory(title="Choose narration output folder")
        if p:
            self.tts_output_dir_var.set(p)
            self._save_preferences()

    def _load_narration_script(self):
        p = filedialog.askopenfilename(title="Open narration text", filetypes=[("Text files", "*.txt *.md"), ("All files", "*.*")])
        if not p:
            return
        try:
            text = Path(p).read_text(encoding="utf-8")
            self.narration_text.delete("1.0", "end")
            self.narration_text.insert("1.0", text)
            self.narration_status_var.set(f"Loaded script: {Path(p).name}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _save_narration_script(self):
        if self.project:
            p = self.project.narration_path("narration_script", ".txt")
        else:
            p = filedialog.asksaveasfilename(title="Save narration text", defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("Markdown", "*.md"), ("All files", "*.*")])
            if not p: return
        try:
            Path(p).write_text(self.narration_text.get("1.0", "end-1c"), encoding="utf-8")
            self.narration_status_var.set(f"Saved script copy: {Path(p).name}")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _insert_narration_example(self):
        block = """[INTRO]
There was a point somewhere in this project where I realized I was not just building smarter Skyrim followers anymore.

[THE NAME]
Even the name The Unbound was not something I picked by myself. The original crew voted on it.

[SOUL TEAR]
Which brings me to Kaelen and something called Soul Tear.
"""
        if self.narration_text.get("1.0", "end-1c").strip():
            self.narration_text.insert("end", "\n\n" + block)
        else:
            self.narration_text.insert("1.0", block)

    def _set_narration_busy(self, busy):
        self.narration_busy = busy
        state = "disabled" if busy else "normal"
        self.render_narration_btn.configure(state=state)
        self.test_voice_btn.configure(state=state)

    def _test_narration_voice(self):
        if self.narration_busy:
            return
        script = self.narration_text.get("sel.first", "sel.last") if self.narration_text.tag_ranges("sel") else self.narration_text.get("1.0", "end-1c")
        sample = re.sub(r"\s+", " ", script).strip()
        if not sample:
            sample = "This is a voice test for The Unbound narration tab."
        sample = sample[:300]
        self.narration_log.delete("1.0", "end")
        self.narration_status_var.set("Rendering voice test…")
        self.narration_progress["value"] = 0
        self.narration_progress_text_var.set("0%")
        self._set_narration_busy(True)
        threading.Thread(target=self._render_narration_worker, args=(sample, self._narration_settings(), True), daemon=True).start()

    def _render_narration_clicked(self):
        if self.narration_busy:
            return
        script = self.narration_text.get("1.0", "end-1c").strip()
        if not script:
            messagebox.showerror(APP_TITLE, "Paste or load a script on the Narration / TTS page first.")
            return
        self.narration_log.delete("1.0", "end")
        self.narration_status_var.set("Preparing narration render…")
        self.narration_progress["value"] = 0
        self.narration_progress_text_var.set("0%")
        self._set_narration_busy(True)
        threading.Thread(target=self._render_narration_worker, args=(script, self._narration_settings(), False), daemon=True).start()

    def _narration_settings(self):
        """Snapshot narration settings on the main thread; workers must not read Tk variables."""
        return {
            "voice": self.tts_voice_var.get().strip() or "TheNarrator",
            "output_dir": self.tts_output_dir_var.get(),
            "name": self.narration_name_var.get().strip() or "unbound_narration",
            "endpoint": self.tts_endpoint_var.get().strip().rstrip("/"),
            "model": self.tts_model_var.get().strip() or "pocket-tts",
        }

    def _split_long_piece(self, text, limit=2200):
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) <= limit:
            return [text] if text else []
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks, cur = [], ""
        for s in sentences:
            if not s:
                continue
            proposed = (cur + " " + s).strip() if cur else s
            if len(proposed) <= limit:
                cur = proposed
            else:
                if cur:
                    chunks.append(cur)
                    cur = s
                else:
                    for i in range(0, len(s), limit):
                        chunks.append(s[i:i+limit])
                    cur = ""
        if cur:
            chunks.append(cur)
        return chunks

    def _parse_narration_sections(self, script):
        sections = []
        current_name = "Narration"
        current_lines = []
        for raw in script.splitlines():
            line = raw.rstrip()
            m = re.match(r"^\[(.+?)\]\s*$", line)
            if m:
                body = "\n".join(current_lines).strip()
                if body:
                    sections.append((current_name, body))
                current_name = m.group(1).strip() or "Section"
                current_lines = []
            else:
                current_lines.append(raw)
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_name, body))
        if not sections and script.strip():
            sections.append(("Narration", script.strip()))

        final = []
        for sec_name, sec_text in sections:
            paragraphs = [p.strip() for p in re.split(r"\n\s*\n", sec_text) if p.strip()]
            assembled = []
            cur = ""
            for para in paragraphs:
                proposed = (cur + "\n\n" + para).strip() if cur else para
                if len(proposed) <= 2200:
                    cur = proposed
                else:
                    if cur:
                        assembled.extend(self._split_long_piece(cur))
                    if len(para) <= 2200:
                        cur = para
                    else:
                        assembled.extend(self._split_long_piece(para))
                        cur = ""
            if cur:
                assembled.extend(self._split_long_piece(cur))
            if not assembled:
                assembled = self._split_long_piece(sec_text)
            for idx, chunk in enumerate(assembled, 1):
                final.append({"section": sec_name, "index": idx, "text": chunk})
        return final

    def _refresh_tts_voices(self):
        endpoint = self.tts_endpoint_var.get().strip().rstrip("/")
        if not endpoint:
            messagebox.showerror(APP_TITLE, "PocketTTS endpoint is blank.")
            return
        self.refresh_voices_btn.configure(state="disabled")
        self.narration_status_var.set("Looking for PocketTTS voices…")
        threading.Thread(target=self._refresh_tts_voices_worker, args=(endpoint,), daemon=True).start()

    def _extract_voice_names(self, obj):
        names = []
        def add(value):
            if isinstance(value, str) and value.strip() and value.strip() not in names:
                names.append(value.strip())

        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, str):
                    add(item)
                elif isinstance(item, dict):
                    for key in ("id", "name", "voice", "voice_id", "speaker", "speaker_id"):
                        if key in item:
                            add(item[key])
                            break
        elif isinstance(obj, dict):
            for key in ("voices", "data", "speakers", "items", "results"):
                if key in obj:
                    names.extend([n for n in self._extract_voice_names(obj[key]) if n not in names])
            if not names:
                # Some servers return a mapping of voice-name -> metadata.
                for key, value in obj.items():
                    if isinstance(value, (dict, str, int, float, type(None))):
                        if key not in ("object", "status", "message", "error"):
                            add(key)
        return names

    def _refresh_tts_voices_worker(self, endpoint):
        # First try HTTP discovery in case this PocketTTS build exposes a voice list.
        paths = ("/v1/audio/voices", "/v1/voices", "/voices")
        errors = []
        for path in paths:
            url = endpoint + path
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
                with urllib.request.urlopen(req, timeout=15) as resp:
                    body = resp.read()
                obj = json.loads(body.decode("utf-8", errors="replace"))
                names = self._extract_voice_names(obj)
                if names:
                    self.events.put(("tts_voices", names, path))
                    return
                errors.append(f"{path}: no voice names in response")
            except urllib.error.HTTPError as exc:
                errors.append(f"{path}: HTTP {exc.code}")
            except Exception as exc:
                errors.append(f"{path}: {exc}")

        # DwemerAI PocketTTS commonly stores local speaker assets under
        # /home/dwemer/audio.cpp/speakers. If the API has no list endpoint,
        # ask WSL directly and use the file/folder names as the dropdown.
        if os.name == "nt":
            try:
                cmd = [
                    "wsl.exe", "-d", "DwemerAI4Skyrim3", "-u", "dwemer", "--",
                    "bash", "-lc",
                    "find /home/dwemer/audio.cpp/speakers -maxdepth 2 -type f \
                     \\( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.flac' -o -iname '*.ogg' -o -iname '*.pt' -o -iname '*.bin' \\) \
                     -printf '%f\n' 2>/dev/null | sed 's/\\.[^.]*$//' | sort -fu"
                ]
                cp = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                names = []
                for line in cp.stdout.splitlines():
                    name = line.strip()
                    if name and name not in names:
                        names.append(name)
                if names:
                    self.events.put(("tts_voices", names, "WSL speakers folder"))
                    return
                errors.append("WSL speakers folder: no speaker files found")
            except Exception as exc:
                errors.append(f"WSL speakers folder: {exc}")

        self.events.put(("tts_voices_error", " | ".join(errors)))

    def _tts_request(self, text, voice, endpoint, model="pocket-tts"):
        endpoint = (endpoint or "").strip().rstrip("/")
        if not endpoint:
            raise RuntimeError("PocketTTS endpoint is blank.")
        url = endpoint + "/v1/audio/speech"
        model = (model or "").strip() or "pocket-tts"
        payloads = [
            {"model": model, "input": text, "voice": voice, "response_format": "wav"},
            {"model": model, "input": text, "voice": voice, "format": "wav"},
            {"model": model, "text": text, "voice": voice, "response_format": "wav"},
            {"model": model, "input": text, "voiceid": voice, "response_format": "wav"},
        ]
        last_error = None
        for payload in payloads:
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=180) as resp:
                    body = resp.read()
                    ctype = resp.headers.get("Content-Type", "")
                if body[:4] == b"RIFF":
                    return body
                if "json" in ctype.lower() or body.startswith(b"{"):
                    obj = json.loads(body.decode("utf-8", errors="ignore"))
                    for key in ("audio", "data"):
                        val = obj.get(key) if isinstance(obj, dict) else None
                        if isinstance(val, str) and val:
                            try:
                                decoded = base64.b64decode(val)
                                if decoded[:4] == b"RIFF":
                                    return decoded
                            except Exception:
                                pass
                    raise RuntimeError(obj.get("error", obj) if isinstance(obj, dict) else "PocketTTS returned JSON instead of WAV audio.")
                last_error = f"PocketTTS returned unexpected data ({ctype or 'unknown type'})."
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", errors="ignore")
                except Exception:
                    detail = str(exc)
                last_error = f"HTTP {exc.code}: {detail}"
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(last_error or "PocketTTS did not return usable audio.")

    def _concat_wavs(self, inputs, output_path):
        if not inputs:
            raise RuntimeError("No WAV chunks were generated.")
        output_path = Path(output_path)
        params = None
        with wave.open(str(output_path), "wb") as outwav:
            for i, wav_path in enumerate(inputs):
                with wave.open(str(wav_path), "rb") as src:
                    if i == 0:
                        params = src.getparams()
                        outwav.setparams(params)
                    else:
                        cur = src.getparams()
                        if cur[:3] != params[:3]:
                            raise RuntimeError("Narration chunks came back with mismatched WAV formats.")
                    outwav.writeframes(src.readframes(src.getnframes()))
        return output_path

    def _render_narration_worker(self, script, settings, test_only=False):
        try:
            voice = settings["voice"]
            base_dir = Path(settings["output_dir"]).expanduser()
            base_dir.mkdir(parents=True, exist_ok=True)
            date = datetime.now().strftime("%Y-%m-%d")
            slug = safe_slug(settings["name"])
            session_dir = base_dir / "render"
            chunks_dir = session_dir / "chunks"
            sections_dir = session_dir / "sections"
            chunks_dir.mkdir(parents=True, exist_ok=True)
            sections_dir.mkdir(parents=True, exist_ok=True)

            if test_only:
                chunks = [{"section": "Voice Test", "index": 1, "text": script.strip()}]
            else:
                chunks = self._parse_narration_sections(script)
            if not chunks:
                raise RuntimeError("There was no usable narration text to render.")

            total = len(chunks)
            self.events.put(("narration_log", f"Rendering {total} chunk(s) with voice '{voice}'.\n"))
            rendered = []
            section_map = {}
            for idx, chunk in enumerate(chunks, 1):
                section_slug = safe_slug(chunk["section"]) or "section"
                fname = f"{idx:03d}_{section_slug}_part{chunk['index']:02d}.wav"
                out_path = chunks_dir / fname
                self.events.put(("narration_status", f"Rendering chunk {idx} / {total}: {chunk['section']}"))
                self.events.put(("narration_progress", (idx - 1) / total * 100.0, f"Chunk {idx} / {total}"))
                audio = self._tts_request(chunk["text"], voice, settings["endpoint"], settings["model"])
                out_path.write_bytes(audio)
                rendered.append((chunk, out_path))
                section_map.setdefault(chunk["section"], []).append(out_path)
                self.events.put(("narration_log", f"✓ {fname}\n"))

            section_outputs = []
            for sec_idx, (section_name, files) in enumerate(section_map.items(), 1):
                sec_slug = safe_slug(section_name) or f"section_{sec_idx:02d}"
                sec_out = sections_dir / f"{sec_idx:02d}_{sec_slug}.wav"
                self._concat_wavs(files, sec_out)
                section_outputs.append(sec_out)

            if test_only:
                final_out = session_dir / "voice_test.wav"
                self._concat_wavs([p for _, p in rendered], final_out)
                self.events.put(("narration_progress", 100.0, "Voice test complete"))
                self.events.put(("narration_done", str(final_out), str(session_dir), "test"))
            else:
                final_out = session_dir / f"{date}_{slug}_full_narration.wav"
                self._concat_wavs(section_outputs, final_out)
                assets = self._make_narration_assets(rendered, session_dir, slug, date)
                manifest_payload = {
                    "audio": str(final_out),
                    "assets": {k: str(v) for k, v in assets.items() if k != "manifest"},
                }
                assets["manifest"].write_text(json.dumps(manifest_payload, indent=2, ensure_ascii=False), encoding="utf-8")
                self.events.put(("narration_progress", 100.0, "Narration + subtitles complete"))
                self.events.put(("narration_done", str(final_out), str(session_dir), "full", {k: str(v) for k, v in assets.items()}))
        except Exception as exc:
            self.events.put(("narration_error", str(exc)))

    def _wav_duration(self, wav_path):
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            frames = wf.getnframes()
            return (frames / float(rate)) if rate else 0.0

    def _subtitle_time_srt(self, seconds):
        ms = max(0, int(round(seconds * 1000)))
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _subtitle_time_vtt(self, seconds):
        return self._subtitle_time_srt(seconds).replace(",", ".")

    def _caption_segments(self, text, max_chars=84):
        clean = re.sub(r"\s+", " ", text).strip()
        if not clean:
            return []
        sentences = re.split(r"(?<=[.!?])\s+", clean)
        segments = []
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                segments.append(sentence)
                continue
            words = sentence.split()
            cur = []
            for word in words:
                candidate = " ".join(cur + [word])
                if cur and len(candidate) > max_chars:
                    segments.append(" ".join(cur))
                    cur = [word]
                else:
                    cur.append(word)
            if cur:
                segments.append(" ".join(cur))
        return segments

    def _make_narration_assets(self, rendered, session_dir, slug, date):
        # rendered is [(chunk_dict, wav_path), ...] in final playback order.
        timeline = []
        cursor = 0.0
        transcript_sections = []
        last_section = None

        for chunk, wav_path in rendered:
            duration = max(0.05, self._wav_duration(wav_path))
            section = chunk.get("section", "Narration")
            chunk_text = re.sub(r"\s+", " ", chunk.get("text", "")).strip()
            if section != last_section:
                transcript_sections.append(f"[{section}]")
                last_section = section
            if chunk_text:
                transcript_sections.append(chunk_text)
                transcript_sections.append("")

            captions = self._caption_segments(chunk_text)
            if not captions:
                cursor += duration
                continue

            weights = [max(1, len(re.findall(r"\b\w+[\w'-]*\b", c))) for c in captions]
            total_weight = sum(weights) or len(captions)
            local = cursor
            for i, (caption, weight) in enumerate(zip(captions, weights)):
                if i == len(captions) - 1:
                    end = cursor + duration
                else:
                    end = local + duration * (weight / total_weight)
                timeline.append({
                    "start": local,
                    "end": max(local + 0.05, end),
                    "text": caption,
                    "section": section,
                })
                local = end
            cursor += duration

        transcript_path = session_dir / f"{date}_{slug}_transcript.txt"
        srt_path = session_dir / f"{date}_{slug}.srt"
        vtt_path = session_dir / f"{date}_{slug}.vtt"
        manifest_path = session_dir / f"{date}_{slug}_caption_manifest.json"

        transcript_path.write_text("\n".join(transcript_sections).strip() + "\n", encoding="utf-8")

        srt_lines = []
        for idx, item in enumerate(timeline, 1):
            srt_lines.extend([
                str(idx),
                f"{self._subtitle_time_srt(item['start'])} --> {self._subtitle_time_srt(item['end'])}",
                item["text"],
                "",
            ])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")

        vtt_lines = ["WEBVTT", ""]
        for item in timeline:
            vtt_lines.extend([
                f"{self._subtitle_time_vtt(item['start'])} --> {self._subtitle_time_vtt(item['end'])}",
                item["text"],
                "",
            ])
        vtt_path.write_text("\n".join(vtt_lines), encoding="utf-8")

        manifest = {
            "duration_seconds": cursor,
            "captions": timeline,
            "transcript": str(transcript_path),
            "srt": str(srt_path),
            "vtt": str(vtt_path),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return {
            "transcript": transcript_path,
            "srt": srt_path,
            "vtt": vtt_path,
            "manifest": manifest_path,
        }

    def _build_subtitles_clicked(self):
        try:
            if not self.last_narration_manifest or not Path(self.last_narration_manifest).is_file():
                raise RuntimeError("Render a full narration first. The subtitle builder uses the saved TTS chunk timing so it can match the narration without re-transcribing it.")
            manifest = json.loads(Path(self.last_narration_manifest).read_text(encoding="utf-8"))
            assets = {k: Path(v) for k, v in manifest.get("assets", {}).items() if v}
            missing = [k for k in ("transcript", "srt", "vtt") if k not in assets or not assets[k].exists()]
            if missing:
                raise RuntimeError("The saved caption files are missing. Re-render the narration to rebuild them.")
            self.last_narration_assets = assets
            self.narration_log.insert("end", "\nTranscript/subtitle files are ready:\n")
            for key in ("transcript", "srt", "vtt"):
                self.narration_log.insert("end", f"  {key.upper()}: {assets[key]}\n")
            self.narration_log.see("end")
            self.narration_status_var.set("Transcript, SRT, and VTT are ready.")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _play_last_narration(self):
        try:
            if not self.last_narration_output or not Path(self.last_narration_output).is_file():
                raise RuntimeError("No rendered narration WAV is available yet.")
            path = str(self.last_narration_output)
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _open_narration_folder(self):
        try:
            folder = Path(self.last_narration_output).parent if self.last_narration_output else Path(self.tts_output_dir_var.get()).expanduser()
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    # ---------- settings / persistence ----------
    def _apply_builtin_preset(self, name, update_output=True):
        p = CUT_PRESETS.get(name)
        if not p:
            return
        self.threshold_var.set(str(p["threshold"]))
        self.min_silence_var.set(str(p["min_silence"]))
        self.padding_var.set(str(p["padding"]))
        self.min_clip_var.set(str(p["min_clip"]))
        self.audio_smoothing_var.set(p["audio_smoothing"])
        self._invalidate_analysis()
        if update_output:
            self._auto_name_output()

    def _save_custom_preset(self):
        name = simpledialog.askstring(APP_TITLE, "Name this preset:")
        if not name:
            return
        try:
            s = self._settings()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.custom_presets[name] = {
            "threshold": s["threshold"],
            "min_silence": s["min_silence"],
            "padding": s["padding"],
            "min_clip": s["min_clip"],
            "encoder": s["encoder"],
            "crf": s["crf"],
            "audio_smoothing": self.audio_smoothing_var.get(),
            "batch": s["batch"],
        }
        self.custom_combo["values"] = sorted(self.custom_presets)
        self.custom_preset_var.set(name)
        self._save_preferences()

    def _load_custom_preset(self):
        name = self.custom_preset_var.get()
        p = self.custom_presets.get(name)
        if not p:
            return
        self.threshold_var.set(str(p.get("threshold", -30)))
        self.min_silence_var.set(str(p.get("min_silence", 1.5)))
        self.padding_var.set(str(p.get("padding", .25)))
        self.min_clip_var.set(str(p.get("min_clip", .1)))
        self.encoder_var.set(p.get("encoder", "nvenc"))
        self.crf_var.set(str(p.get("crf", 18)))
        self.audio_smoothing_var.set(p.get("audio_smoothing", "Medium"))
        self.batch_var.set(str(p.get("batch", 40)))
        self._invalidate_analysis()
        self._auto_name_output()

    def _save_preferences(self):
        data = {
            "output_dir": self.output_dir_var.get(),
            "cut_strength": self.cut_strength.get(),
            "threshold": self.threshold_var.get(),
            "min_silence": self.min_silence_var.get(),
            "padding": self.padding_var.get(),
            "min_clip": self.min_clip_var.get(),
            "encoder": self.encoder_var.get(),
            "crf": self.crf_var.get(),
            "cpu_preset": self.cpu_preset_var.get(),
            "audio_bitrate": self.audio_var.get(),
            "audio_smoothing": self.audio_smoothing_var.get(),
            "batch_size": self.batch_var.get(),
            "skip_intro": self.skip_intro_var.get(),
            "skip_outro": self.skip_outro_var.get(),
            "test_start_min": self.test_start_var.get(),
            "test_duration_min": self.test_duration_var.get(),
            "auto_name": self.auto_name_var.get(),
            "auto_srt": self.auto_srt_var.get(),
            "custom_presets": self.custom_presets,
            "markers_by_input": self.marker_store,
            "tts_endpoint": self.tts_endpoint_var.get(),
            "tts_model": self.tts_model_var.get(),
            "tts_voice": self.tts_voice_var.get(),
            "tts_output_dir": self.tts_output_dir_var.get(),
            "current_project": str(self.project.manifest_path) if self.project else self.project_path_var.get(),
            "narration_name": self.narration_name_var.get(),
            "footage_video": self.footage_video_var.get(),
            "footage_srt": self.footage_srt_var.get(),
            "footage_mode": self.footage_mode_var.get(),
            "footage_top": self.footage_top_var.get(),
            "footage_character": self.footage_character_var.get(),
            "sequence_name": self.sequence_name_var.get(),
            "sequence_fps": self.sequence_fps_var.get(),
            "sequence_format": self.sequence_format_var.get(),
            "sequence_framing": self.sequence_framing_var.get(),
        }
        save_config(data)

    # ---------- file naming ----------
    def _next_auto_output(self):
        if self.project and self.input_var.get().strip():
            return self.project.default_processed_path(self.input_var.get().strip(), self.cut_strength.get())
        out_dir = Path(self.output_dir_var.get()).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y-%m-%d")
        label = safe_slug(self.cut_strength.get().lower())
        for n in range(1, 10000):
            p = out_dir / f"{date}_Unbound_{n:03d}_{label}.mp4"
            if not p.exists():
                return p
        raise RuntimeError("Could not find an available output number.")

    def _auto_name_output(self):
        if self.auto_name_var.get() and self.input_var.get():
            try:
                self.output_var.set(str(self._next_auto_output()))
                self._refresh_disk_info()
            except Exception:
                pass

    def _browse_input(self):
        p = filedialog.askopenfilename(
            title="Select video",
            initialdir=str(self._project_dir("source", Path.home() / "Videos")),
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All files", "*.*")]
        )
        if not p:
            return
        self.input_var.set(p)
        self._load_markers_for_input(Path(p))
        self._invalidate_analysis(clear_input=True)
        self._refresh_marker_tree()
        self._auto_name_output()
        self._probe_async(Path(p))
        self._save_preferences()

    def _browse_output(self):
        current = self.output_var.get().strip()
        initial = Path(current) if current else self._next_auto_output()
        p = filedialog.asksaveasfilename(
            title="Save processed video as",
            initialdir=str(initial.parent),
            initialfile=initial.name,
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4"), ("MKV video", "*.mkv"), ("All files", "*.*")]
        )
        if p:
            self.output_var.set(p)
            self.output_dir_var.set(str(Path(p).parent))
            self.auto_name_var.set(False)
            self._refresh_disk_info()
            self._save_preferences()

    # ---------- timeline / markers / export ----------
    def _marker_store_key(self, path=None):
        try:
            p = Path(path or self.input_var.get()).expanduser().resolve()
            return str(p)
        except Exception:
            return str(path or self.input_var.get())

    def _load_markers_for_input(self, path):
        key = self._marker_store_key(path)
        raw = self.marker_store.get(key, [])
        self.markers = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                try:
                    self.markers.append({
                        "type": str(item.get("type", "Scene")),
                        "time": float(item.get("time", 0.0)),
                        "note": str(item.get("note", "")),
                    })
                except Exception:
                    pass
        self.markers.sort(key=lambda m: m["time"])
        self.timeline_selected_time = None
        self.selected_time_var.set("Selected: —")

    def _persist_markers(self):
        key = self._marker_store_key()
        if key:
            self.marker_store[key] = list(self.markers)
        self._save_preferences()

    def _set_selected_time(self, t, request_preview=False):
        r = self.current_analysis
        if r:
            lo, hi = r["start"], r["end"]
        else:
            lo, hi = 0.0, float(self.input_duration or 0.0)
        if hi <= lo:
            return
        t = min(hi, max(lo, float(t)))
        self.timeline_selected_time = t
        self.selected_time_var.set(f"Selected: {fmt_time(t)}")
        if hasattr(self, "scrubber"):
            try:
                self.scrubber_var.set(t)
                self.scrubber_time_var.set(f"{fmt_time(t)} / {fmt_time(hi)}")
            except Exception:
                pass
        self._enable_preview_controls(True)
        self._draw_timeline()
        if request_preview:
            self._request_preview(t)

    def _configure_scrubber(self, start=0.0, end=None, select=None):
        if not hasattr(self, "scrubber"):
            return
        end = float(self.input_duration or 0.0) if end is None else float(end)
        start = float(start)
        if end <= start:
            self.scrubber.configure(from_=0.0, to=1.0, state=tk.DISABLED)
            self.scrubber_var.set(0.0)
            self.scrubber_time_var.set("00:00 / 00:00")
            return
        self.scrubber.configure(from_=start, to=end, state=tk.NORMAL)
        t = start if select is None else min(end, max(start, float(select)))
        self.scrubber_var.set(t)
        self.scrubber_time_var.set(f"{fmt_time(t)} / {fmt_time(end)}")

    def _scrubber_moved(self, value):
        try:
            t = float(value)
        except Exception:
            return
        self.timeline_selected_time = t
        self.selected_time_var.set(f"Selected: {fmt_time(t)}")
        hi = self.current_analysis["end"] if self.current_analysis else float(self.input_duration or 0.0)
        self.scrubber_time_var.set(f"{fmt_time(t)} / {fmt_time(hi)}")
        self._draw_timeline()

    def _scrubber_pressed(self, _event=None):
        if self.playback_active:
            self._stop_embedded_playback(update_position=True, request_preview=False)

    def _scrubber_released(self, _event=None):
        try:
            t = float(self.scrubber_var.get())
        except Exception:
            return
        self._set_selected_time(t, request_preview=True)

    def _timeline_click(self, event):
        if self.playback_active:
            self._stop_embedded_playback(update_position=True, request_preview=False)
        r = self.current_analysis
        if not r:
            return
        width = max(1, self.timeline_canvas.winfo_width())
        pad = 8
        usable = max(1, width - 2 * pad)
        x = min(max(event.x, pad), width - pad)
        frac = (x - pad) / usable
        t = r["start"] + frac * (r["end"] - r["start"])
        self._set_selected_time(t, request_preview=True)

    def _draw_timeline(self):
        if not hasattr(self, "timeline_canvas"):
            return
        c = self.timeline_canvas
        c.delete("all")
        width = max(1, c.winfo_width())
        height = max(1, c.winfo_height())
        pad = 8
        top = 14
        bottom = height - 20
        usable = max(1, width - 2 * pad)

        r = self.current_analysis
        if not r:
            c.create_text(width/2, height/2, text="Analyze a video to see the cut timeline", fill=self.muted)
            return

        start, end = r["start"], r["end"]
        span = max(0.001, end - start)

        # Full analyzed range.
        c.create_rectangle(pad, top, width-pad, bottom, fill="#3a3a3a", outline="")

        # Kept segments.
        for a, b in r["keeps"]:
            x1 = pad + ((a - start) / span) * usable
            x2 = pad + ((b - start) / span) * usable
            if x2 - x1 < 1:
                x2 = x1 + 1
            c.create_rectangle(x1, top, x2, bottom, fill=self.accent, outline="")

        # Markers.
        for idx, marker in enumerate(self.markers):
            t = marker["time"]
            if t < start or t > end:
                continue
            x = pad + ((t - start) / span) * usable
            color = "#f2c14e" if marker["type"] == "Scene" else "#46c2ff"
            c.create_line(x, 4, x, bottom+6, fill=color, width=2)
            c.create_text(x+3, 5, text=str(idx+1), fill=color, anchor="nw", font=("Segoe UI", 8, "bold"))

        if self.timeline_selected_time is not None and start <= self.timeline_selected_time <= end:
            x = pad + ((self.timeline_selected_time - start) / span) * usable
            c.create_line(x, 2, x, height-3, fill="#ffffff", dash=(3, 2))

        c.create_text(pad, height-5, text=fmt_time(start), fill=self.muted, anchor="sw")
        c.create_text(width-pad, height-5, text=fmt_time(end), fill=self.muted, anchor="se")

    def _enable_preview_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        if hasattr(self, "preview_back_btn"):
            self.preview_back_btn.configure(state=state)
            self.preview_forward_btn.configure(state=state)
            self.play_here_btn.configure(state=state)
            if hasattr(self, "expand_preview_btn"):
                self.expand_preview_btn.configure(state=state)
            if hasattr(self, "external_player_btn"):
                self.external_player_btn.configure(state=state)

    def _nudge_preview(self, delta):
        if self.playback_active:
            self._stop_embedded_playback(update_position=True, request_preview=False)
        r = self.current_analysis
        if r:
            lo, hi = r["start"], r["end"]
        else:
            lo, hi = 0.0, float(self.input_duration or 0.0)
        if hi <= lo:
            return
        t = self.timeline_selected_time
        if t is None:
            t = lo
        t = min(hi, max(lo, t + delta))
        self._set_selected_time(t, request_preview=True)

    def _preview_width(self):
        return 960 if self.preview_expanded else 640

    def _toggle_preview_size(self):
        # Expanding changes only the in-app viewer; the rest of the interface
        # remains the same and the main window can still be scrolled.
        if self.playback_active:
            self._stop_embedded_playback(update_position=True, request_preview=False)
        self.preview_expanded = not self.preview_expanded
        if hasattr(self, "expand_preview_btn"):
            self.expand_preview_btn.configure(
                text="Contract Preview" if self.preview_expanded else "Expand Preview"
            )
        if self.timeline_selected_time is not None:
            self._request_preview(self.timeline_selected_time)

    def _request_preview(self, timestamp):
        if not self.input_var.get() or not self.ffmpeg:
            return
        inp = Path(self.input_var.get())
        if not inp.is_file():
            return

        self.preview_request_id += 1
        request_id = self.preview_request_id
        self.preview_label.configure(text="Loading preview…", image="")
        self.preview_photo = None

        threading.Thread(
            target=self._preview_worker,
            args=(inp, float(timestamp), request_id, self._preview_width()),
            daemon=True,
        ).start()

    def _preview_worker(self, inp, timestamp, request_id, preview_width):
        try:
            preview_dir = Path(tempfile.gettempdir()) / "silence_cutter_preview"
            preview_dir.mkdir(parents=True, exist_ok=True)
            out = preview_dir / f"preview_{request_id}.png"

            cmd = [
                str(self.ffmpeg),
                "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.3f}",
                "-i", str(inp),
                "-frames:v", "1",
                "-vf", f"scale={int(preview_width)}:-2",
                str(out),
            ]
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=self._creationflags(),
            )
            if cp.returncode != 0 or not out.is_file():
                raise RuntimeError(cp.stderr.strip() or "Could not generate preview frame.")

            self.events.put(("preview_ready", request_id, str(out), timestamp))
        except Exception as exc:
            self.events.put(("preview_error", request_id, str(exc)))

    def _read_ppm_frame(self, stream):
        """Read one binary P6 PPM frame from an ffmpeg image2pipe stream."""
        magic = stream.readline()
        if not magic:
            return None
        if magic.strip() != b"P6":
            return None

        line = stream.readline()
        while line.startswith(b"#"):
            line = stream.readline()
        try:
            width, height = map(int, line.split())
            maxval = int(stream.readline().strip())
        except Exception:
            return None
        if maxval != 255 or width <= 0 or height <= 0:
            return None

        need = width * height * 3
        chunks = bytearray()
        while len(chunks) < need:
            part = stream.read(need - len(chunks))
            if not part:
                return None
            chunks.extend(part)
        header = f"P6\n{width} {height}\n255\n".encode("ascii")
        return header + bytes(chunks)

    def _toggle_embedded_playback(self):
        if self.playback_active:
            self._stop_embedded_playback(update_position=True, request_preview=True)
        else:
            self._start_embedded_playback()

    def _start_embedded_playback(self):
        if self.timeline_selected_time is None or not self.ffmpeg:
            return
        inp = Path(self.input_var.get())
        if not inp.is_file():
            return

        self._stop_embedded_playback(update_position=False, request_preview=False)
        self.playback_generation += 1
        generation = self.playback_generation
        start_time = float(self.timeline_selected_time)
        self.playback_start_time = start_time
        self.playback_started_monotonic = time.monotonic()
        self.playback_active = True
        self.play_here_btn.configure(text="⏸ Pause")
        self.status_var.set(f"Playing in preview from {fmt_time(start_time)}")

        video_cmd = [
            str(self.ffmpeg),
            "-hide_banner", "-loglevel", "error",
            "-re", "-ss", f"{start_time:.3f}",
            "-i", str(inp),
            "-an",
            "-vf", f"scale={self._preview_width()}:-2:force_original_aspect_ratio=decrease,fps=15",
            "-f", "image2pipe", "-vcodec", "ppm", "pipe:1",
        ]

        try:
            self.playback_video_proc = subprocess.Popen(
                video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=self._creationflags(), bufsize=1024 * 1024,
            )
        except Exception as exc:
            self.playback_active = False
            self.play_here_btn.configure(text="▶ Play")
            messagebox.showerror(APP_TITLE, f"Could not start embedded video preview:\n{exc}")
            return

        # ffplay is used only as a hidden audio engine. No pop-out video window.
        if self.ffplay:
            try:
                self.playback_audio_proc = subprocess.Popen(
                    [
                        str(self.ffplay), "-nodisp", "-autoexit",
                        "-hide_banner", "-loglevel", "quiet",
                        "-ss", f"{start_time:.3f}", "-i", str(inp),
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=self._creationflags(),
                )
            except Exception:
                self.playback_audio_proc = None

        threading.Thread(
            target=self._embedded_playback_worker,
            args=(generation, start_time), daemon=True
        ).start()

    def _embedded_playback_worker(self, generation, start_time):
        proc = self.playback_video_proc
        if not proc or not proc.stdout:
            return
        try:
            while generation == self.playback_generation:
                frame = self._read_ppm_frame(proc.stdout)
                if frame is None:
                    break
                elapsed = max(0.0, time.monotonic() - (self.playback_started_monotonic or time.monotonic()))
                pos = start_time + elapsed
                self.playback_latest_frame = (generation, frame, pos)
        finally:
            if generation == self.playback_generation and self.playback_active:
                self.events.put(("embedded_finished", generation))

    def _stop_embedded_playback(self, update_position=True, request_preview=False):
        was_active = self.playback_active
        if update_position and was_active and self.playback_started_monotonic is not None:
            t = self.playback_start_time + max(0.0, time.monotonic() - self.playback_started_monotonic)
            if self.current_analysis:
                t = min(self.current_analysis["end"], max(self.current_analysis["start"], t))
            elif self.input_duration:
                t = min(float(self.input_duration), max(0.0, t))
            self.timeline_selected_time = t

        self.playback_generation += 1
        self.playback_active = False
        self.playback_started_monotonic = None
        self.playback_latest_frame = None

        for proc_name in ("playback_video_proc", "playback_audio_proc"):
            proc = getattr(self, proc_name, None)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            setattr(self, proc_name, None)

        if hasattr(self, "play_here_btn"):
            self.play_here_btn.configure(text="▶ Play")

        if was_active and self.timeline_selected_time is not None:
            self._set_selected_time(self.timeline_selected_time, request_preview=request_preview)

    def _play_external(self):
        if self.timeline_selected_time is None:
            return
        inp = Path(self.input_var.get())
        if not inp.is_file():
            return
        if not self.ffplay:
            messagebox.showinfo(
                APP_TITLE,
                "ffplay.exe was not found beside FFmpeg.\n\n"
                "Put ffplay.exe in C:\\ffmpeg\\bin to use the external player."
            )
            return
        self._stop_embedded_playback(update_position=True, request_preview=True)
        try:
            if self.ffplay_proc and self.ffplay_proc.poll() is None:
                self.ffplay_proc.terminate()
        except Exception:
            pass
        try:
            self.ffplay_proc = subprocess.Popen(
                [
                    str(self.ffplay), "-hide_banner", "-loglevel", "warning",
                    "-ss", f"{self.timeline_selected_time:.3f}",
                    "-i", str(inp), "-autoexit",
                ],
                creationflags=self._creationflags(),
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Could not start external playback:\n{exc}")

    def _add_marker(self, marker_type):
        if not self.current_analysis:
            return
        t = self.timeline_selected_time
        if t is None:
            t = self.current_analysis["start"]
        note = simpledialog.askstring(
            APP_TITLE,
            f"{marker_type} marker at {fmt_time(t)}\n\nOptional note:",
            parent=self,
        )
        if note is None:
            return
        self.markers.append({"type": marker_type, "time": float(t), "note": note.strip()})
        self.markers.sort(key=lambda m: m["time"])
        self._persist_markers()
        self._refresh_marker_tree()
        self._draw_timeline()

    def _delete_marker(self):
        sel = self.marker_tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except Exception:
            return
        if 0 <= idx < len(self.markers):
            del self.markers[idx]
            self._persist_markers()
            self._refresh_marker_tree()
            self._draw_timeline()

    def _marker_selection_changed(self):
        sel = self.marker_tree.selection()
        if not sel:
            self.delete_marker_btn.configure(state="disabled")
            return
        self.delete_marker_btn.configure(state="normal")
        try:
            idx = int(sel[0])
            marker = self.markers[idx]
            self._set_selected_time(marker["time"], request_preview=True)
        except Exception:
            pass

    def _refresh_marker_tree(self):
        if not hasattr(self, "marker_tree"):
            return
        for item in self.marker_tree.get_children():
            self.marker_tree.delete(item)
        for i, marker in enumerate(self.markers):
            self.marker_tree.insert(
                "", "end", iid=str(i),
                values=(marker["type"], fmt_time(marker["time"]), marker["note"])
            )
        self.delete_marker_btn.configure(state="disabled")

    def _export_clip_list(self):
        r = self.current_analysis
        if not r:
            messagebox.showinfo(APP_TITLE, "Analyze the video first.")
            return

        out = Path(self.output_var.get()) if self.output_var.get() else Path(self.input_var.get())
        default = out.with_name(out.stem + "_clips.csv")
        if self.project:
            target = self.project.analysis_path(out.stem + "_clips", ".csv")
        else:
            target = filedialog.asksaveasfilename(title="Export clip list", initialdir=str(default.parent), initialfile=default.name, defaultextension=".csv", filetypes=[("CSV file", "*.csv")])
            if not target: return

        cumulative = 0.0
        rows = []
        for i, (a, b) in enumerate(r["keeps"], 1):
            dur = b - a
            matching = [m for m in self.markers if a <= m["time"] <= b]
            rows.append({
                "clip_number": i,
                "source_start": fmt_time(a),
                "source_end": fmt_time(b),
                "duration_sec": f"{dur:.3f}",
                "output_start": fmt_time(cumulative),
                "output_end": fmt_time(cumulative + dur),
                "marker_types": " | ".join(m["type"] for m in matching),
                "marker_notes": " | ".join(m["note"] for m in matching if m["note"]),
            })
            cumulative += dur

        fieldnames = [
            "clip_number", "source_start", "source_end", "duration_sec",
            "output_start", "output_end", "marker_types", "marker_notes"
        ]
        with open(target, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        self.status_var.set(f"Clip list exported: {Path(target).name}")
        messagebox.showinfo(APP_TITLE, f"Exported {len(rows)} clips to:\n{target}")

    # ---------- probing ----------
    def _creationflags(self):
        return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def _probe(self, path):
        if not self.ffprobe:
            raise RuntimeError("ffprobe not found. Put it in C:\\ffmpeg\\bin.")
        cp = subprocess.run(
            [str(self.ffprobe), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, creationflags=self._creationflags()
        )
        if cp.returncode != 0:
            raise RuntimeError(f"ffprobe could not read the video:\n{cp.stderr.strip()}")
        info = json.loads(cp.stdout)
        streams = info.get("streams", [])
        has_video = any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1 for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        duration = float(info["format"]["duration"])
        return duration, has_video, has_audio

    def _probe_async(self, path):
        try:
            self.input_bytes = path.stat().st_size
            self.raw_size_var.set(fmt_size(self.input_bytes))
        except Exception:
            self.input_bytes = None

        def worker():
            try:
                duration, has_video, has_audio = self._probe(path)
                self.events.put(("probe", duration, has_video, has_audio))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- validation ----------
    def _settings(self):
        try:
            s = {
                "threshold": float(self.threshold_var.get()),
                "min_silence": float(self.min_silence_var.get()),
                "padding": float(self.padding_var.get()),
                "min_clip": float(self.min_clip_var.get()),
                "encoder": self.encoder_var.get(),
                "crf": int(self.crf_var.get()),
                "preset": self.cpu_preset_var.get().strip() or "medium",
                "audio": self.audio_var.get().strip() or "192k",
                "audio_smoothing_name": self.audio_smoothing_var.get(),
                "audio_smoothing": AUDIO_SMOOTHING.get(self.audio_smoothing_var.get(), 0.07),
                "batch": int(self.batch_var.get()),
                "skip_intro": float(self.skip_intro_var.get()),
                "skip_outro": float(self.skip_outro_var.get()),
                "test_start": float(self.test_start_var.get()) * 60.0,
                "test_duration": float(self.test_duration_var.get()) * 60.0,
                # Version identity travels with the job from the moment it starts.
                # Nothing downstream may re-read the Cut Strength dropdown.
                "variant": pm._norm_label(self.cut_strength.get() or "PROCESSED"),
                "auto_srt": bool(self.auto_srt_var.get()),
            }
        except ValueError:
            raise ValueError("One of the Advanced settings contains an invalid number.")

        if s["min_silence"] <= 0:
            raise ValueError("Minimum silence must be greater than 0.")
        if s["padding"] < 0 or s["min_clip"] < 0:
            raise ValueError("Padding and minimum clip must be 0 or greater.")
        if s["batch"] < 1:
            raise ValueError("Batch size must be at least 1.")
        if s["skip_intro"] < 0 or s["skip_outro"] < 0:
            raise ValueError("Skip intro/outro cannot be negative.")
        if s["test_start"] < 0 or s["test_duration"] <= 0:
            raise ValueError("Test start must be >= 0 and test duration must be > 0.")
        return s

    def _paths(self, require_output=True):
        inp = Path(self.input_var.get().strip().strip('"'))
        if not inp.is_file():
            raise ValueError("Choose a valid input video.")

        if self.auto_name_var.get():
            if not self.output_var.get():
                self.output_var.set(str(self._next_auto_output()))

        out_text = self.output_var.get().strip().strip('"')
        if not out_text:
            raise ValueError("Choose an output file.")
        out = Path(out_text)
        out.parent.mkdir(parents=True, exist_ok=True)

        if inp.resolve() == out.resolve():
            raise ValueError("Output must be different from input.")
        if require_output and out.exists() and not self.overwrite_var.get():
            raise ValueError("Output already exists. Choose another name or enable Overwrite.")
        if not self.ffmpeg or not self.ffprobe:
            raise ValueError("FFmpeg/ffprobe not found. Expected C:\\ffmpeg\\bin.")
        return inp, out

    def _signature(self, inp, s):
        return (
            str(inp.resolve()), s["threshold"], s["min_silence"], s["padding"],
            s["min_clip"], s["batch"], s["skip_intro"], s["skip_outro"]
        )

    # ---------- analysis ----------
    def _analyze_clicked(self):
        if self.running:
            return
        try:
            inp, _ = self._paths(require_output=False)
            s = self._settings()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self._begin_job("Analyzing…", pause_allowed=False)
        threading.Thread(target=self._analyze_only_job, args=(inp, s), daemon=True).start()

    def _analyze_only_job(self, inp, s):
        result = self._analyze_job(inp, s, test_range=None, publish=True)
        if result and not self.cancel_event.is_set():
            self.events.put(("analysis_complete",))

    def _analyze_job(self, inp, s, test_range=None, publish=True):
        try:
            duration, has_video, has_audio = self._probe(inp)
            if not has_video:
                raise RuntimeError("Input has no video stream.")
            if not has_audio:
                raise RuntimeError("Input has no audio track.")

            if test_range:
                start, end = test_range
            else:
                start = min(duration, s["skip_intro"])
                end = max(start, duration - s["skip_outro"])

            if end <= start:
                raise RuntimeError("Skip/test settings leave no video to process.")

            span = end - start
            self.events.put(("log", f"\nAnalyzing {fmt_time(span)} at {s['threshold']} dB / {s['min_silence']} sec…\n"))
            silences = self._detect_silences(inp, s, start, end)
            if self.cancel_event.is_set():
                raise InterruptedError

            keeps = build_keep_segments(silences, start, end, s["padding"], s["min_clip"])
            if not keeps:
                raise RuntimeError("Everything in this range was detected as silence. Lower the threshold.")

            new_duration = sum(b - a for a, b in keeps)
            removed = span - new_duration
            cuts = max(0, len(keeps) - 1)
            chunks = max(1, (len(keeps) + s["batch"] - 1) // s["batch"])
            input_bytes = inp.stat().st_size
            ratio = new_duration / duration if duration else 0
            estimated_final = int(input_bytes * ratio)
            temp_need = int(max(estimated_final * 3.0, estimated_final + 1024**3))

            cuts_per_min = cuts / max(new_duration / 60.0, 0.01)
            if cuts_per_min >= 10:
                density = "Very choppy"
            elif cuts_per_min >= 6:
                density = "Choppy"
            elif cuts_per_min >= 3:
                density = "Busy"
            else:
                density = "Smooth"

            removed_pct = removed / span * 100.0 if span else 0.0
            if removed_pct >= 65:
                ruth = "Very aggressive"
            elif removed_pct >= 45:
                ruth = "Aggressive"
            elif removed_pct >= 25:
                ruth = "Balanced"
            else:
                ruth = "Conservative"

            result = {
                "source_duration": duration,
                "start": start,
                "end": end,
                "duration": span,
                "silences": silences,
                "keeps": keeps,
                "new_duration": new_duration,
                "removed": removed,
                "removed_pct": removed_pct,
                "cuts": cuts,
                "chunks": chunks,
                "input_bytes": input_bytes,
                "estimated_final": estimated_final,
                "temp_need": temp_need,
                "result_label": f"{ruth} • {density}",
            }

            if publish:
                self.current_analysis = result
                self.analysis_signature = self._signature(inp, s)
                self.events.put(("analysis", result))
            return result
        except InterruptedError:
            self.events.put(("cancelled",))
            return None
        except Exception as exc:
            self.events.put(("error", str(exc)))
            return None

    def _detect_silences(self, inp, s, start, end):
        duration = end - start
        null_target = "NUL" if os.name == "nt" else "/dev/null"
        cmd = [
            str(self.ffmpeg), "-hide_banner", "-nostats",
            "-ss", f"{start:.4f}", "-t", f"{duration:.4f}", "-i", str(inp),
            "-vn", "-map", "0:a:0",
            "-af", f"silencedetect=noise={s['threshold']}dB:d={s['min_silence']}",
            "-progress", "pipe:1",
            "-f", "null", null_target,
        ]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, creationflags=self._creationflags()
        )
        self.proc = proc
        stderr_lines = []
        silences = []
        pending = [None]

        def read_err():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
                if m:
                    pending[0] = max(0.0, float(m.group(1))) + start
                    continue
                m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
                if m and pending[0] is not None:
                    silences.append((pending[0], min(end, float(m.group(1)) + start)))
                    pending[0] = None

        threading.Thread(target=read_err, daemon=True).start()

        for line in proc.stdout:
            if self.cancel_event.is_set():
                proc.terminate()
                break
            if line.startswith("out_time="):
                cur = parse_ffmpeg_time(line.split("=", 1)[1])
                frac = min(0.99, cur / duration if duration else 0)
                self.events.put(("progress", frac * 100.0, "Analyzing"))

        code = proc.wait()
        self.proc = None
        if self.cancel_event.is_set():
            raise InterruptedError
        if code != 0:
            raise RuntimeError("Silence detection failed:\n" + "".join(stderr_lines)[-1800:])
        if pending[0] is not None:
            silences.append((pending[0], end))
        self.events.put(("progress", 100.0, "Analysis complete"))
        return silences

    # ---------- test / process ----------
    def _test_clicked(self):
        if self.running:
            return
        try:
            inp, _ = self._paths(require_output=False)
            s = self._settings()
            duration, _, _ = self._probe(inp)
            start = min(duration, s["test_start"])
            end = min(duration, start + s["test_duration"])
            if end <= start:
                raise ValueError("The test range is outside the video.")
            out_dir = Path(self.output_dir_var.get()).expanduser()
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{datetime.now():%Y-%m-%d}_Unbound_TEST_{int(start//60):03d}m_{safe_slug(self.cut_strength.get().lower())}.mp4"
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        if out.exists():
            try:
                out.unlink()
            except Exception:
                messagebox.showerror(APP_TITLE, f"Cannot replace test file:\n{out}")
                return

        self._begin_job("Testing range…", pause_allowed=True)
        threading.Thread(target=self._test_job, args=(inp, out, s, (start, end)), daemon=True).start()

    def _test_job(self, inp, out, s, test_range):
        result = self._analyze_job(inp, s, test_range=test_range, publish=False)
        if not result or self.cancel_event.is_set():
            return
        self.events.put(("analysis", result))
        if not self._disk_preflight(out.parent, result["temp_need"]):
            return
        self.events.put(("log", f"Test result: {fmt_time(result['duration'])} raw -> {fmt_time(result['new_duration'])} kept ({result['removed_pct']:.0f}% removed)\n"))
        self._render(inp, out, result["keeps"], s, result["new_duration"])

    def _process_clicked(self):
        if self.running:
            return
        try:
            inp, out = self._paths(require_output=True)
            s = self._settings()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self._begin_job("Preparing…", pause_allowed=True)
        threading.Thread(target=self._process_job, args=(inp, out, s), daemon=True).start()

    def _process_job(self, inp, out, s):
        try:
            sig = self._signature(inp, s)
            result = self.current_analysis if self.analysis_signature == sig else None
            if not result:
                result = self._analyze_job(inp, s, test_range=None, publish=True)
                if not result:
                    return

            if not self._disk_preflight(out.parent, result["temp_need"]):
                return

            self._cleanup_temp_dirs(out.parent, older_than_seconds=0)
            self.events.put(("log", f"Audio smoothing: {s['audio_smoothing_name']} ({int(s['audio_smoothing']*1000)} ms)\n"))
            self._render(inp, out, result["keeps"], s, result["new_duration"])
        except Exception as exc:
            self.events.put(("error", str(exc)))

    # ---------- automatic processed-video subtitles ----------
    def _srt_timestamp(self, seconds):
        seconds = max(0.0, float(seconds))
        total_ms = int(round(seconds * 1000.0))
        hours, rem = divmod(total_ms, 3600000)
        minutes, rem = divmod(rem, 60000)
        secs, ms = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

    def _write_srt_entries(self, path, entries):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for idx, (start, end, text) in enumerate(entries, 1):
            if end <= start or not str(text).strip():
                continue
            lines.extend([
                str(idx),
                f"{self._srt_timestamp(start)} --> {self._srt_timestamp(end)}",
                str(text).strip(),
                "",
            ])
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _remap_source_srt_to_cut(self, source_srt, keeps, dest_srt):
        entries = fa.parse_srt(source_srt)
        remapped = []
        output_base = 0.0
        for keep_start, keep_end in keeps:
            keep_start = float(keep_start)
            keep_end = float(keep_end)
            for entry in entries:
                overlap_start = max(float(entry.start), keep_start)
                overlap_end = min(float(entry.end), keep_end)
                if overlap_end <= overlap_start:
                    continue
                new_start = output_base + (overlap_start - keep_start)
                new_end = output_base + (overlap_end - keep_start)
                text = f"{entry.speaker}: {entry.text}" if entry.speaker else entry.text
                remapped.append((new_start, new_end, text))
            output_base += max(0.0, keep_end - keep_start)
        if not remapped:
            raise RuntimeError("The source SRT had no subtitle lines inside the kept clips.")
        return self._write_srt_entries(dest_srt, remapped)

    def _transcribe_with_faster_whisper(self, video_path, dest_srt):
        try:
            from faster_whisper import WhisperModel
        except Exception:
            return None

        self.events.put(("phase", "Transcribing final cut for SRT…"))
        self.events.put(("log", "No SOURCE SRT found; using local faster-whisper to build the processed SRT.\n"))
        # base.en is intentionally modest: useful accuracy without turning an hour-long
        # Skyrim recording into a giant GPU/VRAM job. CPU int8 keeps compatibility broad.
        model = WhisperModel("base.en", device="cpu", compute_type="int8")
        segments, _info = model.transcribe(str(video_path), language="en", vad_filter=True)
        rows = []
        for seg in segments:
            text = str(getattr(seg, "text", "")).strip()
            if text:
                rows.append((float(seg.start), float(seg.end), text))
        if not rows:
            raise RuntimeError("Speech transcription returned no subtitle lines.")
        return self._write_srt_entries(dest_srt, rows)

    @staticmethod
    def _cut_signature(out, keeps):
        """Fingerprint of an exact cut, so an SRT is only reused for the same cut."""
        key = str(Path(out).name) + "|" + ";".join(f"{float(a):.3f}-{float(b):.3f}" for a, b in keeps)
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _auto_build_processed_srt(self, out, keeps, s):
        """Runs on the render thread. Uses only the job snapshot `s`, never Tk variables."""
        if not s.get("auto_srt") or not self.project:
            return None, None

        label = s["variant"]
        dest = self.project.folder("transcript") / f"{safe_slug(self.project.name)}_{label}.srt"
        stamp = dest.with_suffix(".cut.json")
        signature = self._cut_signature(out, keeps)

        def mark_fresh(path):
            try:
                unbound_utils.atomic_write_json(stamp, {"variant": label, "video": str(out), "cut_signature": signature})
            except Exception:
                pass
            return path

        # Best path: remap the SOURCE transcript. It's fast, so always rebuild —
        # a re-render with different settings must never keep stale timing.
        source_srt = self.project.srt_for("SOURCE")
        if source_srt and source_srt.is_file():
            self.events.put(("phase", "Building cut-aware SRT…"))
            self.events.put(("log", f"Remapping SOURCE SRT to {label} cut timing…\n"))
            return mark_fresh(self._remap_source_srt_to_cut(source_srt, keeps, dest)), "remapped"

        # Reuse this variant's own SRT only if it was made for this exact cut.
        existing = self.project.srt_for(label)
        if existing and existing.is_file() and existing.stat().st_size > 0:
            try:
                old = json.loads(stamp.read_text(encoding="utf-8")) if stamp.is_file() else {}
            except Exception:
                old = {}
            if old.get("cut_signature") == signature:
                return existing, "existing"
            self.events.put(("log", f"Existing {label} SRT was made for a different cut; rebuilding it.\n"))

        # No source transcript: use an already-installed local speech-to-text
        # backend. We deliberately do not auto-install packages or fabricate captions.
        made = self._transcribe_with_faster_whisper(out, dest)
        if made:
            return mark_fresh(made), "transcribed"
        return None, "no_backend"

    # ---------- disk ----------
    def _disk_preflight(self, folder, need):
        try:
            free = shutil.disk_usage(folder).free
            self.events.put(("disk", free, need))
            if free < need:
                self.events.put(("error", f"Not enough free disk space.\n\nFree: {fmt_size(free)}\nEstimated temp need: {fmt_size(need)}"))
                return False
            return True
        except Exception as exc:
            self.events.put(("error", f"Could not check free disk space:\n{exc}"))
            return False

    def _refresh_disk_info(self):
        try:
            folder = Path(self.output_var.get()).parent if self.output_var.get() else Path(self.output_dir_var.get())
            free = shutil.disk_usage(folder).free
            self.free_space_var.set(fmt_size(free))
        except Exception:
            self.free_space_var.set("—")

    def _cleanup_temp_dirs(self, folder, older_than_seconds=3600):
        now = time.time()
        try:
            for p in Path(folder).glob(TEMP_PREFIX + "*"):
                if not p.is_dir():
                    continue
                try:
                    if older_than_seconds <= 0 or now - p.stat().st_mtime >= older_than_seconds:
                        shutil.rmtree(p, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    # ---------- rendering ----------
    def _video_args(self, s):
        q = str(s["crf"])
        if s["encoder"] == "nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", q, "-b:v", "0", "-pix_fmt", "yuv420p"]
        if s["encoder"] == "qsv":
            return ["-c:v", "h264_qsv", "-global_quality", q, "-pix_fmt", "nv12"]
        if s["encoder"] == "videotoolbox":
            return ["-c:v", "h264_videotoolbox", "-q:v", "65", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", s["preset"], "-crf", q, "-pix_fmt", "yuv420p"]

    def _render(self, inp, out, keeps, s, total_out_duration):
        try:
            if not keeps:
                raise RuntimeError("No clips to render.")
            if len(keeps) <= s["batch"]:
                self._render_direct(inp, out, keeps, s, total_out_duration)
            else:
                self._render_chunked(inp, out, keeps, s, total_out_duration)

            if self.cancel_event.is_set():
                raise InterruptedError

            actual = out.stat().st_size
            self.last_output = out
            srt_path = None
            srt_mode = None
            try:
                srt_path, srt_mode = self._auto_build_processed_srt(out, keeps, s)
            except Exception as exc:
                self.events.put(("log", f"Automatic SRT warning: {exc}\n"))
                srt_mode = "error"
            self.events.put((
                "finished", out, actual, str(srt_path) if srt_path else "", srt_mode or "",
                s["variant"], s["auto_srt"],
            ))
            self._save_preferences()
        except InterruptedError:
            self.events.put(("cancelled",))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _render_direct(self, inp, out, keeps, s, total_out_duration):
        # Pause before starting if requested.
        self._wait_if_paused("Paused before render")
        filtergraph = build_filtergraph(keeps, audio_fade=s["audio_smoothing"])
        cmd = [
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(inp), "-filter_complex", filtergraph,
            "-map", "[outv]", "-map", "[outa]",
            *self._video_args(s),
            "-c:a", "aac", "-b:a", s["audio"],
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats",
            str(out),
        ]
        try:
            self._run_ffmpeg_progress(cmd, total_out_duration, 0, 100, "Rendering")
        except FFmpegRunError as exc:
            if not self._is_recoverable_decode_error(exc.stderr_text):
                raise
            self.events.put(("log", "Source contains damaged media packets. Retrying render in tolerant decode mode…\n"))
            try:
                if out.exists():
                    out.unlink()
            except Exception:
                pass
            retry_cmd = [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                *self._tolerant_input_args(),
                "-i", str(inp), "-filter_complex", filtergraph,
                "-map", "[outv]", "-map", "[outa]",
                *self._video_args(s),
                "-c:a", "aac", "-b:a", s["audio"],
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                str(out),
            ]
            self._run_ffmpeg_progress(retry_cmd, total_out_duration, 0, 100, "Rendering (repair retry)")
            self.events.put(("log", "Recovered from damaged input packets; render completed in tolerant mode.\n"))

    @staticmethod
    def _is_recoverable_decode_error(stderr_text):
        text = (stderr_text or "").lower()
        signatures = (
            "invalid data found when processing input",
            "error submitting packet to decoder",
            "decode error rate",
            "input buffer exhausted",
            "channel element",
            "corrupt",
            "invalid nal unit",
            "error while decoding",
        )
        return any(sig in text for sig in signatures)

    @staticmethod
    def _tolerant_input_args():
        # These are input-side recovery flags. They tell FFmpeg to discard corrupt
        # packets where possible and continue past decoder errors instead of
        # aborting an otherwise usable long recording.
        return [
            "-fflags", "+discardcorrupt",
            "-err_detect", "ignore_err",
            "-max_error_rate", "1.0",
        ]

    def _render_chunked(self, inp, out, keeps, s, total_out_duration):
        batches = [keeps[i:i+s["batch"]] for i in range(0, len(keeps), s["batch"])]
        out.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=out.parent, prefix=TEMP_PREFIX) as tmpname:
            tmp = Path(tmpname)
            parts = []
            rendered_seconds = 0.0

            for idx, batch in enumerate(batches, 1):
                self._wait_if_paused(f"Paused — {idx-1} of {len(batches)} chunks complete")
                if self.cancel_event.is_set():
                    raise InterruptedError

                window_start = batch[0][0]
                window_end = batch[-1][1]
                batch_out_duration = sum(b-a for a,b in batch)
                filtergraph = build_filtergraph(
                    batch,
                    offset=window_start,
                    audio_fade=s["audio_smoothing"],
                    fade_first=(idx > 1),
                    fade_last=(idx < len(batches)),
                )

                part = tmp / f"part_{idx:04d}.mkv"
                self.events.put(("log", f"Chunk {idx}/{len(batches)}  {fmt_time(window_start)} -> {fmt_time(window_end)}  ({len(batch)} clips)\n"))

                cmd = [
                    str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", f"{window_start:.4f}", "-t", f"{window_end-window_start+0.5:.4f}",
                    "-i", str(inp),
                    "-filter_complex", filtergraph,
                    "-map", "[outv]", "-map", "[outa]",
                    *self._video_args(s),
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    "-progress", "pipe:1", "-nostats",
                    str(part),
                ]

                base_pct = rendered_seconds / total_out_duration * 90.0
                span_pct = batch_out_duration / total_out_duration * 90.0
                phase = f"Chunk {idx}/{len(batches)}"
                try:
                    self._run_ffmpeg_progress(cmd, batch_out_duration, base_pct, base_pct + span_pct, phase)
                except FFmpegRunError as exc:
                    if not self._is_recoverable_decode_error(exc.stderr_text):
                        raise

                    self.events.put((
                        "log",
                        f"{phase} hit damaged media packets. Retrying this chunk in tolerant decode mode…\n",
                    ))
                    try:
                        if part.exists():
                            part.unlink()
                    except Exception:
                        pass

                    retry_cmd = [
                        str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                        *self._tolerant_input_args(),
                        "-ss", f"{window_start:.4f}", "-t", f"{window_end-window_start+0.5:.4f}",
                        "-i", str(inp),
                        "-filter_complex", filtergraph,
                        "-map", "[outv]", "-map", "[outa]",
                        *self._video_args(s),
                        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                        "-progress", "pipe:1", "-nostats",
                        str(part),
                    ]
                    self._run_ffmpeg_progress(
                        retry_cmd, batch_out_duration, base_pct, base_pct + span_pct,
                        f"{phase} (repair retry)",
                    )
                    self.events.put((
                        "log",
                        f"{phase} recovered from damaged input packets and completed.\n",
                    ))

                rendered_seconds += batch_out_duration
                parts.append(part)

            self._wait_if_paused(f"Paused — {len(batches)} chunks complete, ready to join")
            if self.cancel_event.is_set():
                raise InterruptedError

            listing = tmp / "parts.txt"
            with listing.open("w", encoding="utf-8") as f:
                for part in parts:
                    safe = part.resolve().as_posix().replace("'", "'\\''")
                    f.write(f"file '{safe}'\n")

            self.events.put(("log", "Joining chunks and encoding final audio…\n"))
            cmd = [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "concat", "-safe", "0", "-i", str(listing),
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", s["audio"],
                "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                str(out),
            ]
            self._run_ffmpeg_progress(cmd, total_out_duration, 90, 100, "Joining")

    def _run_ffmpeg_progress(self, cmd, duration, pct_start, pct_end, phase):
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, creationflags=self._creationflags()
        )
        self.proc = proc
        stderr_lines = []

        def read_err():
            for line in proc.stderr:
                stderr_lines.append(line)

        threading.Thread(target=read_err, daemon=True).start()

        for line in proc.stdout:
            if self.cancel_event.is_set():
                proc.terminate()
                break
            if line.startswith("out_time="):
                cur = parse_ffmpeg_time(line.split("=",1)[1])
                frac = min(1.0, cur / duration if duration else 0)
                pct = pct_start + (pct_end-pct_start) * frac
                self.events.put(("progress", pct, phase))

        code = proc.wait()
        self.proc = None
        if self.cancel_event.is_set():
            raise InterruptedError
        if code != 0:
            raise FFmpegRunError(phase, code, "".join(stderr_lines))
        self.events.put(("progress", pct_end, phase))

    # ---------- pause / cancel ----------
    def _pause_resume(self):
        if not self.running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text="Pause")
            self.status_var.set("Resuming after current boundary…")
        else:
            self.pause_event.set()
            self.pause_btn.configure(text="Resume")
            self.status_var.set("Pause requested — finishing current chunk first…")

    def _wait_if_paused(self, message):
        announced = False
        while self.pause_event.is_set() and not self.cancel_event.is_set():
            if not announced:
                self.events.put(("paused", message))
                announced = True
            time.sleep(0.2)
        if announced and not self.cancel_event.is_set():
            self.events.put(("phase", "Resuming…"))

    def _cancel(self):
        if not self.running:
            return
        self.cancel_event.set()
        self.pause_event.clear()
        self.status_var.set("Cancelling…")
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass

    # ---------- UI helpers ----------
    def _toggle_advanced(self):
        self.advanced_visible = not self.advanced_visible
        if self.advanced_visible:
            self.advanced.pack(fill="x", pady=(6, 0), before=self.analysis_frame)
            self.advanced_btn.configure(text="Hide Advanced ▴")
        else:
            self.advanced.pack_forget()
            self.advanced_btn.configure(text="Show Advanced ▾")

    def _invalidate_analysis(self, clear_input=False):
        self._stop_embedded_playback(update_position=False, request_preview=False)
        self.current_analysis = None
        self.analysis_signature = None
        self.after_var.set("—")
        self.estimated_size_var.set("—")
        self.actual_size_var.set("—")
        self.removed_var.set("—")
        self.kept_var.set("—")
        self.cuts_var.set("—")
        self.chunks_var.set("—")
        self.cut_result_var.set("—")
        self.temp_need_var.set("—")
        self.timeline_selected_time = None
        self.selected_time_var.set("Selected: —")
        if hasattr(self, "scrubber"):
            self._configure_scrubber()
        self.preview_request_id += 1
        self.preview_photo = None
        if hasattr(self, "preview_label"):
            self.preview_label.configure(
                image="",
                text="Choose a video to load a preview frame"
            )
        self._enable_preview_controls(False)
        if hasattr(self, "scene_btn"):
            self.scene_btn.configure(state="disabled")
            self.bridge_btn.configure(state="disabled")
            self.export_btn.configure(state="disabled")
            self.delete_marker_btn.configure(state="disabled")
        if hasattr(self, "timeline_canvas"):
            self._draw_timeline()
        if clear_input:
            self.raw_duration_var.set("—")
            self.raw_size_var.set("—")

    def _begin_job(self, status, pause_allowed):
        self.running = True
        self.cancel_event.clear()
        self.pause_event.clear()
        self.job_start = time.time()
        self.progress["value"] = 0
        self.progress_text_var.set("0%")
        self.status_var.set(status)
        self.log.delete("1.0", "end")
        self.analyze_btn.configure(state="disabled")
        self.test_btn.configure(state="disabled")
        self.process_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal" if pause_allowed else "disabled", text="Pause")
        self.cancel_btn.configure(state="normal")
        self.play_btn.configure(state="disabled")

    def _end_job(self, status):
        self.running = False
        self.proc = None
        self.pause_event.clear()
        self.analyze_btn.configure(state="normal")
        self.test_btn.configure(state="normal")
        self.process_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="Pause")
        self.cancel_btn.configure(state="disabled")
        self.status_var.set(status)
        if self.last_output and self.last_output.is_file():
            self.play_btn.configure(state="normal")

    def _open_output_folder(self):
        try:
            if self.last_output and self.last_output.exists():
                folder = self.last_output.parent
            elif self.output_var.get():
                folder = Path(self.output_var.get()).parent
            else:
                folder = Path(self.output_dir_var.get())
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _play_output(self):
        try:
            if not self.last_output or not self.last_output.is_file():
                raise RuntimeError("No finished output is available yet.")
            if os.name == "nt":
                os.startfile(str(self.last_output))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.last_output)])
            else:
                subprocess.Popen(["xdg-open", str(self.last_output)])
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    # ---------- event loop ----------
    def _poll_events(self):
        # Embedded playback frames are kept as a single latest-frame slot so the
        # UI never builds a huge queue if decoding briefly outruns Tkinter.
        latest = self.playback_latest_frame
        if latest is not None:
            self.playback_latest_frame = None
            generation, frame_bytes, pos = latest
            if generation == self.playback_generation and self.playback_active:
                try:
                    photo = tk.PhotoImage(data=frame_bytes, format="PPM")
                    self.preview_photo = photo
                    self.preview_label.configure(image=photo, text="")
                    self.timeline_selected_time = pos
                    self.selected_time_var.set(f"Selected: {fmt_time(pos)}")
                    if hasattr(self, "scrubber"):
                        self.scrubber_var.set(pos)
                        hi = self.current_analysis["end"] if self.current_analysis else float(self.input_duration or 0.0)
                        self.scrubber_time_var.set(f"{fmt_time(pos)} / {fmt_time(hi)}")
                    self._draw_timeline()
                except Exception:
                    pass

        try:
            while True:
                e = self.events.get_nowait()
                kind = e[0]

                if kind == "log":
                    self.log.insert("end", e[1])
                    self.log.see("end")

                elif kind == "probe":
                    duration, has_video, has_audio = e[1], e[2], e[3]
                    self.input_duration = duration
                    self.raw_duration_var.set(fmt_time(duration))
                    if self.input_var.get():
                        try:
                            self.input_bytes = Path(self.input_var.get()).stat().st_size
                            self.raw_size_var.set(fmt_size(self.input_bytes))
                        except Exception:
                            pass
                    self._refresh_disk_info()
                    if has_video:
                        self._configure_scrubber(0.0, duration, 0.0)
                        self._set_selected_time(0.0, request_preview=True)
                    if not has_video or not has_audio:
                        messagebox.showwarning(APP_TITLE, "This file is missing a usable video or audio stream.")

                elif kind == "analysis":
                    r = e[1]
                    self.after_var.set(fmt_time(r["new_duration"]))
                    self.estimated_size_var.set("~" + fmt_size(r["estimated_final"]))
                    self.removed_var.set(f"{fmt_time(r['removed'])}  ({r['removed_pct']:.0f}%)")
                    self.kept_var.set(str(len(r["keeps"])))
                    self.cuts_var.set(str(r["cuts"]))
                    self.chunks_var.set(str(r["chunks"]))
                    self.cut_result_var.set(r["result_label"])
                    self.temp_need_var.set("~" + fmt_size(r["temp_need"]))
                    self._refresh_disk_info()
                    self.scene_btn.configure(state="normal")
                    self.bridge_btn.configure(state="normal")
                    self.export_btn.configure(state="normal")
                    self._refresh_marker_tree()
                    self._configure_scrubber(r["start"], r["end"], self.timeline_selected_time if self.timeline_selected_time is not None else r["start"])
                    if self.timeline_selected_time is None:
                        self._set_selected_time(r["start"], request_preview=True)
                    else:
                        self._draw_timeline()

                elif kind == "preview_ready":
                    request_id, preview_path, timestamp = e[1], e[2], e[3]
                    if request_id == self.preview_request_id:
                        try:
                            photo = tk.PhotoImage(file=preview_path)
                            self.preview_photo = photo
                            self.preview_temp = preview_path
                            self.preview_label.configure(image=photo, text="")
                            self.status_var.set(f"Preview: {fmt_time(timestamp)}")
                        except Exception as exc:
                            self.preview_label.configure(image="", text=f"Preview failed: {exc}")

                elif kind == "preview_error":
                    request_id, message = e[1], e[2]
                    if request_id == self.preview_request_id:
                        self.preview_photo = None
                        self.preview_label.configure(image="", text=f"Preview failed:\n{message}")

                elif kind == "embedded_finished":
                    generation = e[1]
                    if generation == self.playback_generation and self.playback_active:
                        self._stop_embedded_playback(update_position=True, request_preview=True)
                        self.status_var.set("Preview playback finished.")

                elif kind == "analysis_complete":
                    self.progress["value"] = 100
                    self.progress_text_var.set("100% • Analysis complete")
                    self._end_job("Analysis complete.")

                elif kind == "disk":
                    free, need = e[1], e[2]
                    self.free_space_var.set(fmt_size(free))
                    self.temp_need_var.set("~" + fmt_size(need))

                elif kind == "progress":
                    pct, phase = max(0.0, min(100.0, e[1])), e[2]
                    self.progress["value"] = pct
                    elapsed = time.time() - self.job_start if self.job_start else 0
                    eta = (elapsed / pct * (100-pct)) if pct > 0.5 else None
                    suffix = f" • Elapsed {fmt_clock(elapsed)}"
                    if eta is not None:
                        suffix += f" • ETA {fmt_clock(eta)}"
                    self.progress_text_var.set(f"{pct:.0f}%{suffix}")
                    self.status_var.set(phase)

                elif kind == "paused":
                    self.status_var.set(e[1])

                elif kind == "phase":
                    self.status_var.set(e[1])

                elif kind == "finished":
                    out, actual = e[1], e[2]
                    srt_path = Path(e[3]) if len(e) > 3 and e[3] else None
                    srt_mode = e[4] if len(e) > 4 else ""
                    job_label = e[5] if len(e) > 5 and e[5] else pm._norm_label(self.cut_strength.get() or "PROCESSED")
                    job_auto_srt = e[6] if len(e) > 6 else self.auto_srt_var.get()
                    self.last_output = out
                    self.actual_size_var.set(fmt_size(actual))
                    self.progress["value"] = 100
                    self.progress_text_var.set("100% • Finished")
                    self.log.insert("end", f"\nDone: {out}\nFinal size: {fmt_size(actual)}\n")
                    self.log.see("end")
                    self._end_job("Finished.")
                    self._project_register_processed(out, job_label)

                    if srt_path and srt_path.is_file() and self.project:
                        try:
                            label = job_label
                            actual_srt = self.project.import_transcript(srt_path, variant=label, copy_into_project=False)
                            self.footage_srt_var.set(str(actual_srt))
                            self.project_srt_display_var.set(str(actual_srt))
                            self._refresh_project_display()
                            mode_text = {
                                "remapped": "cut-aware SOURCE SRT",
                                "transcribed": "local speech transcription",
                                "existing": "existing project SRT",
                            }.get(srt_mode, "automatic SRT")
                            self.log.insert("end", f"SRT ready ({mode_text}): {actual_srt}\n")
                            self.project_status_var.set(f"Processed video + matching SRT ready: {label}")
                            self.footage_status_var.set(f"Matching {label} SRT is ready. Load Footage Map to analyze it.")
                            self._save_preferences()
                        except Exception as exc:
                            self.log.insert("end", f"Project SRT registration warning: {exc}\n")
                    elif job_auto_srt and self.project:
                        if srt_mode == "no_backend":
                            self.log.insert("end", "SRT not created: import a SOURCE SRT once, or install faster-whisper for automatic local transcription.\n")
                            self.project_status_var.set("Video ready. SRT needs a SOURCE transcript or local faster-whisper.")
                        elif srt_mode == "error":
                            self.project_status_var.set("Video ready. Automatic SRT hit an error; see Review & Export log.")

                    if self.auto_name_var.get():
                        self.output_var.set(str(self._next_auto_output()))
                    self._refresh_disk_info()

                elif kind == "cancelled":
                    self._end_job("Cancelled.")

                elif kind == "tts_voices":
                    names, source_path = e[1], e[2]
                    self.tts_voice_combo.configure(values=names)
                    if not self.tts_voice_var.get().strip() or self.tts_voice_var.get().strip() not in names:
                        self.tts_voice_var.set(names[0])
                    self.refresh_voices_btn.configure(state="normal")
                    self.narration_status_var.set(f"Found {len(names)} voice(s) via {source_path}.")
                    self.narration_log.insert("end", "Available voices:\n  " + "\n  ".join(names) + "\n")
                    self.narration_log.see("end")
                    self._save_preferences()

                elif kind == "tts_voices_error":
                    self.refresh_voices_btn.configure(state="normal")
                    self.narration_status_var.set("Could not auto-load voices. You can still type a voice name manually.")
                    self.narration_log.insert("end", "Voice discovery failed: " + e[1] + "\n")
                    self.narration_log.see("end")

                elif kind == "narration_log":
                    self.narration_log.insert("end", e[1])
                    self.narration_log.see("end")

                elif kind == "narration_status":
                    self.narration_status_var.set(e[1])

                elif kind == "narration_progress":
                    pct, label = e[1], e[2]
                    self.narration_progress["value"] = max(0.0, min(100.0, pct))
                    self.narration_progress_text_var.set(f"{pct:.0f}% • {label}")

                elif kind == "narration_done":
                    out, folder, mode = Path(e[1]), Path(e[2]), e[3]
                    assets_payload = e[4] if len(e) > 4 and isinstance(e[4], dict) else {}
                    self.last_narration_output = out
                    self.play_narration_btn.configure(state="normal")
                    self.narration_status_var.set(f"Saved: {out.name}")
                    self.narration_log.insert("end", f"\nDone: {out}\n")
                    if mode == "full" and assets_payload:
                        self.last_narration_assets = {k: Path(v) for k, v in assets_payload.items()}
                        self.last_narration_manifest = self.last_narration_assets.get("manifest")
                        self.subtitles_btn.configure(state="normal")
                        self.narration_log.insert("end", "\nTranscript / subtitle files created automatically:\n")
                        for key in ("transcript", "srt", "vtt"):
                            pth = self.last_narration_assets.get(key)
                            if pth:
                                self.narration_log.insert("end", f"  {key.upper()}: {pth}\n")
                    self.narration_log.see("end")
                    self._set_narration_busy(False)
                    self._save_preferences()
                    if mode == "test":
                        try:
                            if os.name == "nt":
                                os.startfile(str(out))
                        except Exception:
                            pass

                elif kind == "narration_error":
                    self._set_narration_busy(False)
                    self.narration_status_var.set("Narration render failed.")
                    messagebox.showerror(APP_TITLE, e[1])

                elif kind == "sequence_progress":
                    pct, label = max(0.0, min(100.0, e[1])), e[2]
                    self.sequence_progress["value"] = pct
                    self.sequence_progress_var.set(f"{pct:.0f}% • {label}")
                    self.sequence_status_var.set(label)

                elif kind == "sequence_done":
                    out = Path(e[1])
                    self.sequence_last_output = out
                    self.sequence_last_assembly = out
                    self.sequence_render_busy = False
                    self.sequence_render_btn.configure(state="normal")
                    self.sequence_cancel_btn.configure(state="disabled")
                    self.sequence_progress["value"] = 100
                    self.sequence_progress_var.set("100% • Assembly complete")
                    self.sequence_status_var.set(f"Rough assembly saved: {out.name}")

                elif kind == "sequence_error":
                    self.sequence_render_busy = False
                    self.sequence_render_btn.configure(state="normal")
                    self.sequence_cancel_btn.configure(state="disabled")
                    message = e[1]
                    if "cancelled" in message.lower():
                        self.sequence_status_var.set("Sequence render cancelled.")
                    else:
                        self.sequence_status_var.set("Sequence render failed.")
                        messagebox.showerror(APP_TITLE, message)

                elif kind == "error":
                    self._end_job("Error.")
                    messagebox.showerror(APP_TITLE, e[1])

        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def destroy(self):
        self._stop_embedded_playback(update_position=False, request_preview=False)
        try:
            if self.ffplay_proc and self.ffplay_proc.poll() is None:
                self.ffplay_proc.terminate()
        except Exception:
            pass
        try:
            if self.footage_preview_window is not None and not self.footage_preview_window.closed:
                self.footage_preview_window.close()
        except Exception:
            pass
        try:
            if self.footage_preview_proc and self.footage_preview_proc.poll() is None:
                self.footage_preview_proc.terminate()
        except Exception:
            pass
        try:
            self.sequence_cancel.set()
            if self.sequence_preview_proc and self.sequence_preview_proc.poll() is None:
                self.sequence_preview_proc.terminate()
        except Exception:
            pass
        if self.input_var.get():
            key = self._marker_store_key()
            if key:
                self.marker_store[key] = list(self.markers)
        self._save_preferences()
        super().destroy()


if __name__ == "__main__":
    app = SilenceCutterApp()
    app.mainloop()
