#!/usr/bin/env python3
from pathlib import Path
import shutil
from datetime import datetime
import py_compile

gui = Path(__file__).resolve().parent / "silence_cutter_gui.py"
if not gui.exists():
    raise SystemExit("silence_cutter_gui.py not found beside this fixer.")

text = gui.read_text(encoding="utf-8")

old = '''        candidates = [
            self.output_var.get().strip(),
            self.input_var.get().strip(),
        ]
'''

new = '''        # Always prefer the untouched source recording for the master transcript.
        # A processed render may be incomplete/corrupt if a prior cut failed.
        candidates = [
            self.input_var.get().strip(),
            self.output_var.get().strip(),
        ]
'''

if new in text:
    print("Already fixed: Transcribe SRT prefers the source video.")
    raise SystemExit(0)

if old not in text:
    raise SystemExit("Expected transcription candidate block was not found; no changes made.")

backup = gui.with_name(
    f"{gui.stem}.backup_source_srt_{datetime.now():%Y%m%d_%H%M%S}{gui.suffix}"
)
shutil.copy2(gui, backup)

text = text.replace(old, new, 1)
gui.write_text(text, encoding="utf-8")

try:
    py_compile.compile(str(gui), doraise=True)
except Exception:
    shutil.copy2(backup, gui)
    raise

print("FIX COMPLETE")
print(f"Patched: {gui}")
print(f"Backup : {backup}")
print("Transcribe SRT now prefers the original source recording.")
