from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
WORD_RE = re.compile(r"[a-z0-9']+", re.I)

def _fmt(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"

def _srt_time_to_seconds(s: str) -> float:
    s = s.strip().replace(".", ",")
    hh, mm, rest = s.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")[:3]) / 1000.0

def _parse_srt(path: Path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    rows = []
    for block in blocks:
        lines = [x.rstrip() for x in block.splitlines() if x.strip()]
        if not lines:
            continue
        timing_idx = next((i for i, x in enumerate(lines) if "-->" in x), None)
        if timing_idx is None:
            continue
        timing = lines[timing_idx]
        try:
            a, b = [x.strip() for x in timing.split("-->", 1)]
            start = _srt_time_to_seconds(a)
            end = _srt_time_to_seconds(b.split()[0])
        except Exception:
            continue
        body = " ".join(lines[timing_idx + 1:]).strip()
        if body:
            rows.append({"start": start, "end": end, "text": body})
    return rows

def _normalize(text: str) -> str:
    words = WORD_RE.findall(text.lower())
    return " ".join(words)

def _wordset(text: str):
    return set(WORD_RE.findall(text.lower()))

def _shingles(norm: str, n=3):
    w = norm.split()
    if len(w) < n:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i+n]) for i in range(len(w)-n+1)}

def _similar(a: str, b: str) -> bool:
    if a == b:
        return True
    aw, bw = a.split(), b.split()
    if min(len(aw), len(bw)) < 5:
        return False
    ratio_len = min(len(aw), len(bw)) / max(len(aw), len(bw))
    if ratio_len < 0.68:
        return False
    aset, bset = set(aw), set(bw)
    union = len(aset | bset)
    jac = len(aset & bset) / union if union else 0.0
    if jac < 0.68:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.86

def detect_repeat_groups(rows):
    """Fast-ish near-duplicate grouping for subtitle lines."""
    norms = [_normalize(r["text"]) for r in rows]
    parent = list(range(len(rows)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    exact = {}
    for i, n in enumerate(norms):
        if len(n.split()) < 5:
            continue
        if n in exact:
            union(i, exact[n])
        else:
            exact[n] = i

    inverted = defaultdict(list)
    checked = set()
    for i, n in enumerate(norms):
        if len(n.split()) < 5:
            continue
        candidates = set()
        for sh in _shingles(n, 3):
            for j in inverted.get(sh, ()):
                candidates.add(j)
        # Compare only plausible prior candidates. This keeps long SRTs responsive.
        for j in candidates:
            key = (j, i)
            if key in checked:
                continue
            checked.add(key)
            if _similar(n, norms[j]):
                union(i, j)
        for sh in _shingles(n, 3):
            inverted[sh].append(i)

    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)

    result = []
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        takes = [dict(rows[i], index=i) for i in sorted(idxs, key=lambda x: rows[x]["start"])]
        result.append({
            "id": f"repeat-{takes[0]['start']:.3f}-{len(takes)}",
            "text": takes[0]["text"],
            "takes": takes,
        })
    result.sort(key=lambda g: g["takes"][0]["start"])
    return result

def _walk(widget):
    yield widget
    try:
        for child in widget.winfo_children():
            yield from _walk(child)
    except Exception:
        return

def _widget_text(widget):
    try:
        return str(widget.cget("text"))
    except Exception:
        return ""

class RepeatGroupsWindow(tk.Toplevel):
    def __init__(self, app, srt_path: Path, video_path: Path | None):
        super().__init__(app)
        self.app = app
        self.srt_path = Path(srt_path)
        self.video_path = Path(video_path) if video_path else None
        self.rows = _parse_srt(self.srt_path)
        self.groups = detect_repeat_groups(self.rows)
        self.choice_path = self.srt_path.with_name(self.srt_path.stem + "_repeat_choices.json")
        self.choices = self._load_choices()

        self.title(f"Repeat Groups — {self.srt_path.name}")
        self.geometry("1120x700")
        try:
            self.configure(bg=getattr(app, "bg", "#151515"))
        except Exception:
            pass

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(
            top,
            text=f"{len(self.groups)} repeat group(s) detected from the SRT. No speaker index required.",
        ).pack(side="left")
        ttk.Button(top, text="Reload", command=self._reload).pack(side="right")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        cols = ("time", "takes", "choice", "text")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Group / Take")
        self.tree.heading("time", text="Time")
        self.tree.heading("takes", text="Takes")
        self.tree.heading("choice", text="Choice")
        self.tree.heading("text", text="Transcript")
        self.tree.column("#0", width=125, stretch=False)
        self.tree.column("time", width=110, stretch=False)
        self.tree.column("takes", width=65, stretch=False, anchor="center")
        self.tree.column("choice", width=120, stretch=False)
        self.tree.column("text", width=650, stretch=True)
        self.tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda _e: self.preview_selected())

        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Preview", command=self.preview_selected).pack(side="left")
        ttk.Button(buttons, text="Keep This", command=self.keep_this).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Keep All", command=self.keep_all).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Treat Separately", command=self.treat_separately).pack(side="left", padx=(8, 0))
        ttk.Label(
            buttons,
            text="Choices are saved beside the SRT and survive reopening.",
        ).pack(side="right")

        self._populate()

    def _load_choices(self):
        try:
            data = json.loads(self.choice_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_choices(self):
        payload = {
            "schema_version": 1,
            "source_srt": str(self.srt_path),
            "groups": self.choices,
        }
        tmp = self.choice_path.with_suffix(self.choice_path.suffix + ".part")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.choice_path)

    def _choice_label(self, group):
        c = self.choices.get(group["id"], {})
        mode = c.get("mode")
        if mode == "one":
            return f"KEEP take {int(c.get('take', 0)) + 1}"
        if mode == "all":
            return "KEEP ALL"
        if mode == "separate":
            return "SEPARATE"
        return "Unreviewed"

    def _populate(self):
        self.tree.delete(*self.tree.get_children())
        for gi, g in enumerate(self.groups):
            gid = f"g{gi}"
            first = g["takes"][0]["start"]
            self.tree.insert(
                "", "end", iid=gid, text=f"REPEAT ×{len(g['takes'])}",
                values=(_fmt(first), len(g["takes"]), self._choice_label(g), g["text"])
            )
            for ti, t in enumerate(g["takes"]):
                self.tree.insert(
                    gid, "end", iid=f"{gid}:t{ti}", text=f"Take {ti+1}",
                    values=(_fmt(t["start"]), "", "", t["text"])
                )

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            return None, None
        iid = sel[0]
        m = re.match(r"g(\d+)(?::t(\d+))?$", iid)
        if not m:
            return None, None
        gi = int(m.group(1))
        ti = int(m.group(2)) if m.group(2) is not None else None
        if not (0 <= gi < len(self.groups)):
            return None, None
        return gi, ti

    def preview_selected(self):
        gi, ti = self._selected()
        if gi is None:
            return
        if ti is None:
            ti = 0

        take = self.groups[gi]["takes"][ti]
        start = float(take["start"])
        end = max(start + 0.25, float(take.get("end", start + 4.0)))

        # Repeat Groups needs an actual moving, audible preview. Use ffplay
        # directly here rather than the embedded Footage Analysis window, which
        # initially displays a still frame and requires a separate Play action.
        ffplay = getattr(self.app, "ffplay", None)
        if not ffplay:
            ffplay = shutil.which("ffplay")
        if not ffplay and getattr(self.app, "ffmpeg", None):
            candidate = Path(str(self.app.ffmpeg)).with_name("ffplay.exe" if os.name == "nt" else "ffplay")
            if candidate.is_file():
                ffplay = str(candidate)

        if self.video_path and self.video_path.exists() and ffplay:
            try:
                sample_start = max(0.0, start - 1.0)
                sample_duration = max(2.0, (end - start) + 2.0)

                old_proc = getattr(self, "_repeat_preview_proc", None)
                if old_proc and old_proc.poll() is None:
                    try:
                        old_proc.terminate()
                    except Exception:
                        pass

                self._repeat_preview_proc = subprocess.Popen(
                    [
                        str(ffplay),
                        "-hide_banner", "-loglevel", "error",
                        "-autoexit",
                        "-window_title", f"Repeat Take — {_fmt(start)}",
                        "-ss", f"{sample_start:.3f}",
                        "-t", f"{sample_duration:.3f}",
                        str(self.video_path),
                    ],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                )
                return
            except Exception as exc:
                messagebox.showerror("Silence Slicer", f"Could not preview this take:\n{exc}")
                return

        # If ffplay is unavailable, fall back to the app's embedded range viewer.
        try:
            play_range = getattr(self.app, "_play_footage_range", None)
            if callable(play_range):
                if self.video_path and self.video_path.exists():
                    var = getattr(self.app, "footage_video_var", None)
                    if hasattr(var, "set"):
                        var.set(str(self.video_path))
                play_range(start, end, preroll=1.0, postroll=1.0)
                return
        except Exception as exc:
            messagebox.showerror("Silence Slicer", f"Could not open preview:\n{exc}")
            return

        messagebox.showinfo("Silence Slicer", f"Take starts at {_fmt(start)}")

    def keep_this(self):
        gi, ti = self._selected()
        if gi is None:
            return
        if ti is None:
            messagebox.showinfo("Silence Slicer", "Select one of the Take rows first.")
            return
        g = self.groups[gi]
        self.choices[g["id"]] = {"mode": "one", "take": ti}
        self._save_choices()
        self._populate()
        self.tree.selection_set(f"g{gi}:t{ti}")

    def keep_all(self):
        gi, _ = self._selected()
        if gi is None:
            return
        g = self.groups[gi]
        self.choices[g["id"]] = {"mode": "all"}
        self._save_choices()
        self._populate()

    def treat_separately(self):
        gi, _ = self._selected()
        if gi is None:
            return
        g = self.groups[gi]
        self.choices[g["id"]] = {"mode": "separate"}
        self._save_choices()
        self._populate()

    def _reload(self):
        self.rows = _parse_srt(self.srt_path)
        self.groups = detect_repeat_groups(self.rows)
        self._populate()

def _candidate_video(app):
    for attr in ("footage_video_var", "output_var", "input_var"):
        v = getattr(app, attr, None)
        try:
            raw = v.get().strip().strip('"')
        except Exception:
            raw = ""
        if raw:
            p = Path(raw)
            if p.exists() and p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                return p
    p = getattr(app, "last_output", None)
    if p:
        p = Path(p)
        if p.exists():
            return p
    return None

def _project_root(app):
    p = getattr(app, "project", None)
    if p is not None:
        for attr in ("root", "path", "project_dir", "base_dir"):
            val = getattr(p, attr, None)
            if val:
                try:
                    return Path(val)
                except Exception:
                    pass
        try:
            folder = p.folder("transcript")
            return Path(folder).parent
        except Exception:
            pass
    v = _candidate_video(app)
    return v.parent if v else None

def _find_srt(app):
    # Known/likely direct variables first.
    for attr in (
        "srt_var", "source_srt_var", "subtitle_var", "subtitles_var",
        "srt_path_var", "current_srt", "source_srt", "last_srt",
    ):
        v = getattr(app, attr, None)
        try:
            raw = v.get() if hasattr(v, "get") else v
        except Exception:
            raw = None
        if raw:
            p = Path(str(raw).strip().strip('"'))
            if p.exists() and p.suffix.lower() == ".srt":
                return p

    video = _candidate_video(app)
    candidates = []
    if video:
        candidates.extend([
            video.with_suffix(".srt"),
            video.parent / (video.stem + ".srt"),
            video.parent.parent / "transcript" / (video.stem + ".srt"),
        ])
    root = _project_root(app)
    if root:
        tdir = root / "transcript"
        if tdir.exists():
            if video:
                candidates.append(tdir / (video.stem + ".srt"))
            candidates.extend(sorted(tdir.glob("*.srt"), key=lambda p: p.stat().st_mtime, reverse=True))

    for p in candidates:
        try:
            if p.exists():
                return p
        except Exception:
            pass
    return None

def open_repeat_groups(app):
    srt = _find_srt(app)
    if srt is None:
        picked = filedialog.askopenfilename(
            title="Choose SRT for Repeat Groups",
            filetypes=[("SubRip subtitles", "*.srt"), ("All files", "*.*")]
        )
        if not picked:
            return
        srt = Path(picked)
    video = _candidate_video(app)
    try:
        win = RepeatGroupsWindow(app, srt, video)
        win.transient(app)
    except Exception as exc:
        messagebox.showerror("Silence Slicer", f"Could not build Repeat Groups:\n{exc}")

def _safe_transcribe_one(app, video: Path, srt: Path):
    helper = Path(sys.modules[app.__class__.__module__].__file__).with_name("srt_transcriber.py")
    if not helper.exists():
        raise RuntimeError("srt_transcriber.py is missing beside silence_cutter_gui.py")

    srt.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(helper),
        "--input", str(video),
        "--output", str(srt),
        "--model", "medium",
        "--device", "auto",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=creationflags
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-30:])
        raise RuntimeError(tail or f"SRT process exited with code {proc.returncode}")
    if not srt.exists() or srt.stat().st_size == 0:
        raise RuntimeError("Transcriber finished but no SRT was produced.")

def retry_missing_srts(app):
    root = _project_root(app)
    current = _candidate_video(app)
    videos = []
    transcript_dir = None

    if root:
        processed = root / "processed"
        transcript_dir = root / "transcript"
        if processed.exists():
            videos = [
                p for p in sorted(processed.iterdir())
                if p.is_file() and p.suffix.lower() in VIDEO_EXTS
            ]
    if not videos and current:
        videos = [current]
        transcript_dir = current.parent

    if not videos:
        picked = filedialog.askopenfilename(
            title="Choose rendered video for SRT retry",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"), ("All files", "*.*")]
        )
        if not picked:
            return
        current = Path(picked)
        videos = [current]
        transcript_dir = current.parent

    transcript_dir = Path(transcript_dir)
    jobs = []
    for video in videos:
        srt = transcript_dir / f"{video.stem}.srt"
        if not srt.exists() or srt.stat().st_size == 0:
            jobs.append((video, srt))

    if not jobs:
        messagebox.showinfo("Silence Slicer", "All rendered videos already have SRT files.")
        return

    ok = messagebox.askyesno(
        "Silence Slicer",
        f"Retry SRT only for {len(jobs)} rendered video(s)?\n\n"
        "The video files will not be touched."
    )
    if not ok:
        return

    try:
        if hasattr(app, "status_var"):
            app.status_var.set(f"Retrying {len(jobs)} missing SRT(s)…")
    except Exception:
        pass

    def worker():
        failures = []
        completed = 0
        for video, srt in jobs:
            try:
                _safe_transcribe_one(app, video, srt)
                completed += 1
            except Exception as exc:
                failures.append((video.name, str(exc)))
        def finish():
            try:
                if hasattr(app, "status_var"):
                    app.status_var.set(
                        f"SRT retry finished: {completed} succeeded, {len(failures)} failed."
                    )
            except Exception:
                pass
            if failures:
                detail = "\n\n".join(f"{name}\n{err}" for name, err in failures[:6])
                messagebox.showwarning(
                    "Silence Slicer",
                    f"SRT retry finished.\n\nSucceeded: {completed}\nFailed: {len(failures)}\n\n{detail}"
                )
            else:
                messagebox.showinfo(
                    "Silence Slicer",
                    f"SRT retry finished successfully for {completed} video(s).\n\n"
                    "Rendered video files were never touched."
                )
        app.after(0, finish)

    threading.Thread(target=worker, daemon=True).start()

def install_into_app(app):
    """Runtime UI integration. Designed to avoid touching the cutter/render pipeline."""
    if getattr(app, "_repeat_srt_tools_installed", False):
        return
    app._repeat_srt_tools_installed = True

    anchor = None
    for w in _walk(app):
        txt = _widget_text(w).strip().lower()
        if "sequence builder" in txt:
            anchor = w
            break
    if anchor is None:
        for w in _walk(app):
            txt = _widget_text(w).strip().lower()
            if txt == "analyze moments":
                anchor = w
                break

    if anchor is None:
        # Last-resort integration in a small top-level toolbar attached to root.
        bar = ttk.Frame(app)
        bar.pack(fill="x", padx=10, pady=(0, 6))
    else:
        bar = anchor.master

    if not any(_widget_text(w) == "Repeat Groups" for w in _walk(bar)):
        btn = ttk.Button(bar, text="Repeat Groups", command=lambda: open_repeat_groups(app))
        try:
            if anchor is not None and anchor.winfo_manager() == "grid":
                info = anchor.grid_info()
                row = int(info.get("row", 0))
                used = []
                for child in bar.winfo_children():
                    try:
                        gi = child.grid_info()
                        if gi:
                            used.append(int(gi.get("column", 0)))
                    except Exception:
                        pass
                btn.grid(row=row, column=(max(used)+1 if used else int(info.get("column", 0))+1),
                         padx=(8,0), pady=info.get("pady", 0), sticky="w")
            else:
                btn.pack(side="left", padx=(8,0))
        except Exception:
            btn.pack(side="left", padx=(8,0))

    if not any(_widget_text(w) == "Retry SRT Only" for w in _walk(bar)):
        btn2 = ttk.Button(bar, text="Retry SRT Only", command=lambda: retry_missing_srts(app))
        try:
            if anchor is not None and anchor.winfo_manager() == "grid":
                info = anchor.grid_info()
                row = int(info.get("row", 0))
                used = []
                for child in bar.winfo_children():
                    try:
                        gi = child.grid_info()
                        if gi:
                            used.append(int(gi.get("column", 0)))
                    except Exception:
                        pass
                btn2.grid(row=row, column=(max(used)+1 if used else int(info.get("column", 0))+2),
                          padx=(8,0), pady=info.get("pady", 0), sticky="w")
            else:
                btn2.pack(side="left", padx=(8,0))
        except Exception:
            btn2.pack(side="left", padx=(8,0))

def attach_safe_srt_error_policy(app):
    """
    Reserved hook point for the GUI. The upgrade intentionally does not monkey-patch
    the core render function. Rendered video remains independent; failed subtitles
    are recoverable through Retry SRT Only.
    """
    return
