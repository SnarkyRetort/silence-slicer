#!/usr/bin/env python3
from pathlib import Path
from datetime import datetime
import py_compile
import shutil

def main():
    here = Path(__file__).resolve().parent
    replacement = here / "srt_transcriber.py"
    target = here / "srt_transcriber.py"

    # This installer is intended to be run from inside the Silence Slicer folder
    # after both files have been copied there.
    if not replacement.exists():
        raise SystemExit("ERROR: srt_transcriber.py is missing beside this installer.")

    # Because the replacement and target share the same name when copied into the
    # app folder, installation is already complete at copy time.
    py_compile.compile(str(target), doraise=True)
    print("")
    print("CPU FALLBACK FIX READY")
    print(f"Using: {target}")
    print("")
    print("Auto mode now tries GPU first and automatically retries on CPU")
    print("if CUDA/cuBLAS/cuDNN fails during transcription.")
    input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
