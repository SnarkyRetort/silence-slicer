#!/usr/bin/env python3
"""Reusable timestamped footage preview window for Silence Cutter.

Keeps Footage Analysis preview behavior aligned with Review & Export:
- embedded video preview
- synchronized audio via hidden ffplay
- scrubber
- ±5 second navigation
- play/pause
- expand/contract preview
- external-player fallback

The window owns its subprocesses and tears them down on close so it cannot leave
orphaned ffmpeg/ffplay jobs behind.
"""

from __future__ import annotations

import base64
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


def _fmt_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class FootagePreviewWindow:
    """A small reusable preview player for a selected footage range."""

    def __init__(
        self,
        master,
        *,
        ffmpeg: Path,
        ffplay: Path | None,
        video: Path,
        start: float,
        end: float,
        video_duration: float,
        creationflags=0,
        title="Silence Slicer — Footage Moment",
        preroll=2.0,
        postroll=2.0,
        colors=None,
    ):
        self.master = master
        self.ffmpeg = Path(ffmpeg)
        self.ffplay = Path(ffplay) if ffplay else None
        self.video = Path(video)
        self.video_duration = max(0.01, float(video_duration or 0.01))
        self.creationflags = creationflags
        self.preroll = float(preroll)
        self.postroll = float(postroll)
        self.range_start = max(0.0, float(start) - self.preroll)
        self.range_end = min(self.video_duration, max(float(end) + self.postroll, self.range_start + 1.0))
        self.position = self.range_start
        self.expanded = False
        self.playing = False
        self.play_started = None
        self.play_origin = self.position
        self.generation = 0
        self.video_proc = None
        self.audio_proc = None
        self.external_proc = None
        self.latest_frame = None
        self.photo = None
        self.closed = False
        self.scrubbing = False

        c = colors or {}
        self.bg = c.get("bg", "#171717")
        self.field = c.get("field", "#2a2a2a")
        self.fg = c.get("fg", "#f2f2f2")
        self.muted = c.get("muted", "#b8b8b8")
        self.accent = c.get("accent", "#7057d9")

        self.win = tk.Toplevel(master)
        self.win.title(title)
        self.win.configure(background=self.bg)
        self.win.geometry("1040x720")
        self.win.minsize(760, 540)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._build_ui()
        self._request_still(self.position)
        self._tick()

    def _preview_width(self):
        return 960 if self.expanded else 640

    def _build_ui(self):
        outer = ttk.Frame(self.win)
        outer.pack(fill="both", expand=True, padx=12, pady=12)

        preview_box = ttk.LabelFrame(outer, text="Video Preview")
        preview_box.pack(fill="both", expand=True)

        scrubber_row = ttk.Frame(preview_box)
        scrubber_row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(scrubber_row, text="Scrubber:").pack(side="left")
        self.scrubber_var = tk.DoubleVar(value=self.position)
        self.scrubber = tk.Scale(
            scrubber_row,
            from_=self.range_start,
            to=self.range_end,
            variable=self.scrubber_var,
            command=self._scrubber_moved,
            orient="horizontal",
            showvalue=False,
            resolution=0.01,
            sliderlength=24,
            width=14,
            bd=0,
            highlightthickness=0,
            background=self.bg,
            foreground=self.fg,
            troughcolor="#5a5a5a",
            activebackground=self.accent,
            sliderrelief="raised",
        )
        self.scrubber.pack(side="left", fill="x", expand=True, padx=(10, 10))
        self.time_var = tk.StringVar()
        ttk.Label(scrubber_row, textvariable=self.time_var).pack(side="right")
        self.scrubber.bind("<ButtonPress-1>", self._scrubber_pressed)
        self.scrubber.bind("<ButtonRelease-1>", self._scrubber_released)

        self.preview_label = tk.Label(
            preview_box,
            text="Loading preview…",
            background="#050505",
            foreground=self.muted,
            anchor="center",
        )
        self.preview_label.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        controls = ttk.Frame(preview_box)
        controls.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(controls, text="◀ 5 sec", command=lambda: self._nudge(-5.0)).pack(side="left")
        self.play_btn = ttk.Button(controls, text="▶ Play", command=self._toggle_play)
        self.play_btn.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="5 sec ▶", command=lambda: self._nudge(5.0)).pack(side="left", padx=(8, 0))
        self.expand_btn = ttk.Button(controls, text="Expand Preview", command=self._toggle_expand)
        self.expand_btn.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Open External Player", command=self._open_external).pack(side="left", padx=(8, 0))
        self.selected_var = tk.StringVar()
        ttk.Label(controls, textvariable=self.selected_var).pack(side="right")

        ttk.Label(
            outer,
            text="Footage Analysis preview • same transport controls as Review & Export",
        ).pack(anchor="w", pady=(8, 0))

        self._update_labels()

    def update_range(self, *, video: Path, start: float, end: float, video_duration: float, title=None):
        self._stop_playback(update_position=False)
        self.video = Path(video)
        self.video_duration = max(0.01, float(video_duration or 0.01))
        self.range_start = max(0.0, float(start) - self.preroll)
        self.range_end = min(self.video_duration, max(float(end) + self.postroll, self.range_start + 1.0))
        self.position = self.range_start
        self.scrubber.configure(from_=self.range_start, to=self.range_end)
        self.scrubber_var.set(self.position)
        if title:
            self.win.title(title)
        self._update_labels()
        self._request_still(self.position)
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _update_labels(self):
        self.time_var.set(f"{_fmt_time(self.position)} / {_fmt_time(self.video_duration)}")
        self.selected_var.set(f"Selected: {_fmt_time(self.position)}")

    def _scrubber_pressed(self, _event=None):
        self.scrubbing = True
        if self.playing:
            self._stop_playback(update_position=True)

    def _scrubber_moved(self, value):
        try:
            self.position = float(value)
        except Exception:
            return
        self._update_labels()

    def _scrubber_released(self, _event=None):
        self.scrubbing = False
        self.position = float(self.scrubber_var.get())
        self._update_labels()
        self._request_still(self.position)

    def _nudge(self, delta):
        if self.playing:
            self._stop_playback(update_position=True)
        self.position = min(self.range_end, max(self.range_start, self.position + float(delta)))
        self.scrubber_var.set(self.position)
        self._update_labels()
        self._request_still(self.position)

    def _toggle_expand(self):
        was_playing = self.playing
        if was_playing:
            self._stop_playback(update_position=True)
        self.expanded = not self.expanded
        self.expand_btn.configure(text="Contract Preview" if self.expanded else "Expand Preview")
        self.win.geometry("1420x930" if self.expanded else "1040x720")
        self._request_still(self.position)
        if was_playing:
            self._start_playback()

    def _request_still(self, timestamp):
        if self.closed or not self.video.is_file():
            return
        self.generation += 1
        generation = self.generation
        width = self._preview_width()
        self.preview_label.configure(text="Loading preview…", image="")
        self.photo = None

        def worker():
            cmd = [
                str(self.ffmpeg), "-hide_banner", "-loglevel", "error",
                "-ss", f"{timestamp:.3f}", "-i", str(self.video),
                "-frames:v", "1", "-vf", f"scale={width}:-2",
                "-f", "image2pipe", "-vcodec", "png", "pipe:1",
            ]
            try:
                cp = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=self.creationflags,
                )
                if cp.returncode != 0 or not cp.stdout:
                    return
                data = base64.b64encode(cp.stdout).decode("ascii")
                self.win.after(0, lambda: self._show_png(generation, data, timestamp))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_png(self, generation, data, timestamp):
        if self.closed or generation != self.generation:
            return
        try:
            self.photo = tk.PhotoImage(data=data)
            self.preview_label.configure(image=self.photo, text="")
            self.position = float(timestamp)
            self.scrubber_var.set(self.position)
            self._update_labels()
        except Exception:
            self.preview_label.configure(text="Preview frame unavailable", image="")

    @staticmethod
    def _read_ppm_frame(stream):
        magic = stream.readline()
        if not magic or magic.strip() != b"P6":
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
        buf = bytearray()
        while len(buf) < need:
            chunk = stream.read(need - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        header = f"P6\n{width} {height}\n255\n".encode("ascii")
        return header + bytes(buf)

    def _toggle_play(self):
        if self.playing:
            self._stop_playback(update_position=True)
            self._request_still(self.position)
        else:
            self._start_playback()

    def _start_playback(self):
        if self.closed or not self.video.is_file():
            return
        self._stop_playback(update_position=False)
        self.generation += 1
        generation = self.generation
        start = float(self.position)
        duration = max(0.1, self.range_end - start)
        self.play_origin = start
        self.play_started = time.monotonic()
        self.playing = True
        self.play_btn.configure(text="⏸ Pause")

        video_cmd = [
            str(self.ffmpeg), "-hide_banner", "-loglevel", "error",
            "-re", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(self.video), "-an",
            "-vf", f"scale={self._preview_width()}:-2:force_original_aspect_ratio=decrease,fps=15",
            "-f", "image2pipe", "-vcodec", "ppm", "pipe:1",
        ]
        try:
            self.video_proc = subprocess.Popen(
                video_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=self.creationflags, bufsize=1024 * 1024,
            )
        except Exception as exc:
            self.playing = False
            self.play_btn.configure(text="▶ Play")
            messagebox.showerror("Silence Cutter", f"Could not start footage preview:\n{exc}", parent=self.win)
            return

        if self.ffplay and self.ffplay.is_file():
            try:
                self.audio_proc = subprocess.Popen(
                    [
                        str(self.ffplay), "-nodisp", "-autoexit", "-hide_banner", "-loglevel", "quiet",
                        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(self.video),
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=self.creationflags,
                )
            except Exception:
                self.audio_proc = None

        def worker():
            proc = self.video_proc
            if not proc or not proc.stdout:
                return
            try:
                while not self.closed and self.playing and generation == self.generation:
                    frame = self._read_ppm_frame(proc.stdout)
                    if frame is None:
                        break
                    elapsed = max(0.0, time.monotonic() - (self.play_started or time.monotonic()))
                    pos = min(self.range_end, self.play_origin + elapsed)
                    data = base64.b64encode(frame).decode("ascii")
                    self.win.after(0, lambda d=data, p=pos, g=generation: self._show_ppm(g, d, p))
            finally:
                if not self.closed:
                    self.win.after(0, lambda: self._playback_finished(generation))

        threading.Thread(target=worker, daemon=True).start()

    def _show_ppm(self, generation, data, position):
        if self.closed or generation != self.generation or not self.playing:
            return
        try:
            self.photo = tk.PhotoImage(data=data, format="PPM")
            self.preview_label.configure(image=self.photo, text="")
            self.position = float(position)
            if not self.scrubbing:
                self.scrubber_var.set(self.position)
            self._update_labels()
        except Exception:
            pass

    def _playback_finished(self, generation):
        if self.closed or generation != self.generation:
            return
        self.position = min(self.range_end, self.position)
        self._stop_playback(update_position=False)
        self._request_still(self.position)

    def _stop_playback(self, update_position=True):
        was_playing = self.playing
        if update_position and was_playing and self.play_started is not None:
            self.position = min(
                self.range_end,
                max(self.range_start, self.play_origin + max(0.0, time.monotonic() - self.play_started)),
            )
        self.generation += 1
        self.playing = False
        self.play_started = None
        for proc_name in ("video_proc", "audio_proc"):
            proc = getattr(self, proc_name, None)
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            setattr(self, proc_name, None)
        if hasattr(self, "play_btn"):
            self.play_btn.configure(text="▶ Play")
        if hasattr(self, "scrubber_var"):
            self.scrubber_var.set(self.position)
        self._update_labels()

    def _open_external(self):
        if not self.ffplay or not self.ffplay.is_file():
            messagebox.showinfo(
                "Silence Cutter",
                "ffplay.exe was not found beside FFmpeg.",
                parent=self.win,
            )
            return
        try:
            if self.external_proc and self.external_proc.poll() is None:
                self.external_proc.terminate()
        except Exception:
            pass
        try:
            duration = max(0.1, self.range_end - self.position)
            self.external_proc = subprocess.Popen(
                [
                    str(self.ffplay), "-hide_banner", "-loglevel", "warning",
                    "-ss", f"{self.position:.3f}", "-t", f"{duration:.3f}",
                    "-autoexit", "-window_title", "Silence Slicer — Footage Moment",
                    str(self.video),
                ],
                creationflags=self.creationflags,
            )
        except Exception as exc:
            messagebox.showerror("Silence Cutter", f"Could not start external player:\n{exc}", parent=self.win)

    def _tick(self):
        if self.closed:
            return
        if self.playing and self.play_started is not None:
            pos = min(self.range_end, self.play_origin + max(0.0, time.monotonic() - self.play_started))
            self.position = pos
            if not self.scrubbing:
                self.scrubber_var.set(pos)
            self._update_labels()
        self.win.after(100, self._tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._stop_playback(update_position=False)
        try:
            if self.external_proc and self.external_proc.poll() is None:
                self.external_proc.terminate()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
