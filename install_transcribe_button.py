#!/usr/bin/env python3
from __future__ import annotations

import py_compile
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

GUI_NAME = "silence_cutter_gui.py"
PATCH_MARKER = "# --- STANDALONE SRT TRANSCRIPTION PATCH ---"

METHODS = r'''
    # --- STANDALONE SRT TRANSCRIPTION PATCH ---

    def _transcribe_srt_clicked(self):
        # Transcribe current video without re-running the cutter.
        candidates = [
            self.output_var.get().strip(),
            self.input_var.get().strip(),
        ]

        video_path = None
        for candidate in candidates:
            if candidate:
                path = Path(candidate)
                if path.exists() and path.is_file():
                    video_path = path
                    break

        if video_path is None:
            picked = filedialog.askopenfilename(
                title="Choose video to transcribe",
                filetypes=[
                    ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"),
                    ("All files", "*.*"),
                ],
            )
            if not picked:
                return
            video_path = Path(picked)

        helper_path = Path(__file__).with_name("srt_transcriber.py")
        if not helper_path.exists():
            messagebox.showerror(
                APP_TITLE,
                "srt_transcriber.py is missing.\\n\\n"
                "Put it beside silence_cutter_gui.py.",
            )
            return

        if self.project:
            transcript_dir = self.project.folder("transcript")
            transcript_dir.mkdir(parents=True, exist_ok=True)
            srt_path = transcript_dir / f"{video_path.stem}.srt"
        else:
            srt_path = video_path.with_suffix(".srt")

        if srt_path.exists():
            replace = messagebox.askyesno(
                APP_TITLE,
                f"This SRT already exists:\\n\\n{srt_path}\\n\\nReplace it?",
            )
            if not replace:
                return

        self.transcribe_btn.config(state="disabled")
        self.status_var.set(f"Transcribing {video_path.name} to SRT...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)
        self.progress_text_var.set("SRT...")

        cmd = [
            sys.executable,
            str(helper_path),
            "--input",
            str(video_path),
            "--output",
            str(srt_path),
            "--model",
            "medium",
            "--device",
            "auto",
        ]

        def worker():
            try:
                creationflags = 0
                if os.name == "nt":
                    creationflags = getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
                output_text, _ = proc.communicate()

                if proc.returncode != 0:
                    tail = "\\n".join(
                        output_text.strip().splitlines()[-25:]
                    )
                    raise RuntimeError(
                        "Transcription subprocess failed.\\n\\n"
                        + (tail or f"Exit code: {proc.returncode}")
                    )

                self.after(
                    0,
                    lambda: self._transcribe_srt_finished(
                        video_path,
                        srt_path,
                        output_text,
                    ),
                )

            except Exception as exc:
                self.after(
                    0,
                    lambda e=exc: self._transcribe_srt_failed(e),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _transcribe_srt_finished(
        self,
        video_path,
        srt_path,
        helper_output="",
    ):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 100
        self.progress_text_var.set("100%")
        self.transcribe_btn.config(state="normal")

        registered_path = srt_path

        if self.project:
            try:
                active = (
                    self.footage_version_var.get()
                    or self.project.data.get("active_processed")
                    or "SOURCE"
                )
                registered_path = self.project.import_transcript(
                    srt_path,
                    variant=active,
                    copy_into_project=False,
                )
                self._refresh_project_display()
                self._refresh_processed_versions()
            except Exception as exc:
                self.project_status_var.set(
                    "SRT created, but project registration warning: "
                    f"{exc}"
                )

        self.footage_srt_var.set(str(registered_path))
        self._save_preferences()

        language = ""
        block_count = ""
        for line in helper_output.splitlines():
            if line.startswith("DONE|"):
                parts = line.split("|", 3)
                if len(parts) >= 3:
                    block_count = parts[2]
                if len(parts) >= 4:
                    language = parts[3]

        detail = ""
        if block_count:
            detail += f" {block_count} subtitle blocks."
        if language:
            detail += f" Language: {language}."

        self.status_var.set(
            f"SRT complete: {Path(registered_path).name}.{detail}"
        )

        messagebox.showinfo(
            APP_TITLE,
            "SRT transcription complete.\\n\\n"
            f"{registered_path}",
        )

    def _transcribe_srt_failed(self, exc):
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0
        self.progress_text_var.set("0%")
        self.transcribe_btn.config(state="normal")
        self.status_var.set("SRT transcription failed.")

        messagebox.showerror(
            APP_TITLE,
            "SRT transcription failed.\\n\\n"
            f"{exc}\\n\\n"
            "The cutter and project were left intact.",
        )

'''


def patch_gui(gui_path: Path):
    original = gui_path.read_text(encoding="utf-8")

    if PATCH_MARKER in original:
        return None, False

    backup = gui_path.with_name(
        f"{gui_path.stem}.backup_{datetime.now():%Y%m%d_%H%M%S}{gui_path.suffix}"
    )
    shutil.copy2(gui_path, backup)

    text = original

    button_pattern = re.compile(
        r'(?P<indent>[ \t]*)self\.process_btn\.pack'
        r'\(side="left", padx=\(8, 0\)\)\s*\n'
    )
    match = button_pattern.search(text)
    if not match:
        raise RuntimeError('Could not find the "Process Video" button block.')

    indent = match.group("indent")
    button_code = (
        match.group(0)
        + "\n"
        + indent
        + "self.transcribe_btn = ttk.Button(\n"
        + indent
        + '    action_row, text="Transcribe SRT", command=self._transcribe_srt_clicked\n'
        + indent
        + ")\n"
        + indent
        + 'self.transcribe_btn.pack(side="left", padx=(8, 0))\n'
    )

    text = text[:match.start()] + button_code + text[match.end():]

    anchor = "    def _project_dir(self, name, fallback=None):"
    pos = text.find(anchor)
    if pos < 0:
        raise RuntimeError("Could not find _project_dir() insertion point.")

    text = text[:pos] + METHODS + text[pos:]
    gui_path.write_text(text, encoding="utf-8")

    try:
        py_compile.compile(str(gui_path), doraise=True)
    except Exception:
        shutil.copy2(backup, gui_path)
        raise

    return backup, True


def main():
    base = Path(__file__).resolve().parent
    gui_path = base / GUI_NAME

    if len(sys.argv) > 1:
        gui_path = Path(sys.argv[1]).expanduser().resolve()

    if not gui_path.exists():
        print(f"ERROR: Could not find {gui_path}")
        print("Put this file beside silence_cutter_gui.py and run it again.")
        return 1

    helper = gui_path.with_name("srt_transcriber.py")
    if not helper.exists():
        print("ERROR: srt_transcriber.py is missing.")
        print("Put srt_transcriber.py beside silence_cutter_gui.py first.")
        return 1

    py_compile.compile(str(helper), doraise=True)
    backup, changed = patch_gui(gui_path)

    print()
    if changed:
        print("PATCH COMPLETE")
        print(f"Patched: {gui_path}")
        print(f"Backup : {backup}")
    else:
        print("Already patched.")
        print(f"GUI: {gui_path}")

    print()
    print('Launch Silence Slicer and use the new "Transcribe SRT" button.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
