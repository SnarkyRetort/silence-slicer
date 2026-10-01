#!/usr/bin/env python3
from pathlib import Path
import shutil
from datetime import datetime
import py_compile

gui = Path(__file__).resolve().parent / "silence_cutter_gui.py"
if not gui.exists():
    raise SystemExit("silence_cutter_gui.py not found beside this fixer.")

text = gui.read_text(encoding="utf-8")

old = '''            "--device",
            "auto",
'''

new = '''            "--device",
            "cpu",
'''

if new in text:
    print("Already fixed: standalone SRT transcription is forced to CPU.")
    raise SystemExit(0)

if old not in text:
    raise SystemExit('Could not find the "--device", "auto" block. No changes made.')

backup = gui.with_name(
    f"{gui.stem}.backup_cpu_srt_{datetime.now():%Y%m%d_%H%M%S}{gui.suffix}"
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
print("Standalone SRT transcription now uses CPU/int8 and will not require CUDA cuBLAS.")
