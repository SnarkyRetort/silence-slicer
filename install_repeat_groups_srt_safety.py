#!/usr/bin/env python3
from __future__ import annotations

import py_compile
import shutil
import sys
from datetime import datetime
from pathlib import Path

GUI_NAME = "silence_cutter_gui.py"
HELPER_NAME = "silence_slicer_repeat_srt_tools.py"
MARKER = "# --- REPEAT GROUPS + SRT SAFETY UPGRADE ---"

def fail(msg):
    print(f"\nERROR: {msg}\n")
    raise SystemExit(1)

def main():
    here = Path(__file__).resolve().parent
    gui = here / GUI_NAME
    helper = here / HELPER_NAME

    if not gui.exists():
        fail(f"Could not find {GUI_NAME} beside this installer.")
    if not helper.exists():
        fail(f"Could not find {HELPER_NAME} beside this installer.")

    py_compile.compile(str(helper), doraise=True)

    src = gui.read_text(encoding="utf-8")
    if MARKER in src:
        print("\nAlready installed. No changes made.")
        print(f"GUI: {gui}")
        return

    # Require the modern UI before touching anything. This avoids patching an older copy.
    modern_markers = ("Sequence Builder", "Analyze Moments")
    if not any(m in src for m in modern_markers):
        fail(
            "This does not look like the current Silence Slicer build "
            "(Sequence Builder / Analyze Moments not found). No changes were made."
        )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = gui.with_name(f"{gui.stem}.backup_repeat_srt_{stamp}{gui.suffix}")
    shutil.copy2(gui, backup)

    anchor = "        self._build_ui()\n"
    if anchor not in src:
        anchor = "        self._build_ui()\r\n"
    if anchor not in src:
        fail(f"Could not find self._build_ui() in {GUI_NAME}. Backup: {backup}")

    newline = "\r\n" if "\r\n" in src else "\n"
    inject = (
        anchor
        + f"        {MARKER}{newline}"
        + f"        try:{newline}"
        + f"            from silence_slicer_repeat_srt_tools import install_into_app{newline}"
        + f"            self.after(0, lambda: install_into_app(self)){newline}"
        + f"        except Exception as _repeat_srt_upgrade_error:{newline}"
        + f"            print(f\"Repeat/SRT upgrade load warning: {{_repeat_srt_upgrade_error}}\", file=sys.stderr){newline}"
    )
    src = src.replace(anchor, inject, 1)
    gui.write_text(src, encoding="utf-8")

    try:
        py_compile.compile(str(gui), doraise=True)
    except Exception as exc:
        shutil.copy2(backup, gui)
        fail(
            "Patched GUI did not compile, so the original was restored automatically.\n"
            f"Compiler error: {exc}\nBackup: {backup}"
        )

    print("\nUPGRADE INSTALLED")
    print(f"Patched : {gui}")
    print(f"Backup  : {backup}")
    print(f"Helper  : {helper}")
    print("\nAdded:")
    print("  • Repeat Groups button beside Sequence Builder / Analyze Moments")
    print("  • Exact + near-repeat SRT detection")
    print("  • Preview / Keep This / Keep All / Treat Separately")
    print("  • Saved repeat choices beside the SRT")
    print("  • Retry SRT Only for rendered videos missing subtitle files")
    print("  • SRT retry never rewrites or deletes rendered video")
    print("\nClose Silence Slicer before installing, then relaunch it.")

if __name__ == "__main__":
    main()
