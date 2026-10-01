#!/usr/bin/env python3
"""Small shared helpers for Silence Cutter — The Unbound Edition.

Kept dependency-free so every module (and the GUI) can import it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# Curly quotes/apostrophes that Whisper-style transcripts commonly emit.
_QUOTE_MAP = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"',
})


def normalize_quotes(text: str) -> str:
    """Map curly apostrophes/quotes to ASCII so tokenizers see "won't", not "won" + "t"."""
    return str(text or "").translate(_QUOTE_MAP)


def seconds_to_clock(seconds: float, millis: bool = False) -> str:
    """Format seconds as HH:MM:SS or HH:MM:SS.mmm.

    Rounds to whole milliseconds *before* splitting into fields, so a value like
    59.9996 becomes 00:01:00.000 instead of the invalid 00:00:60.000.
    """
    ms_total = int(round(max(0.0, float(seconds or 0.0)) * 1000))
    if not millis:
        # Truncate to whole seconds for the short form, matching the old behaviour.
        ms_total -= ms_total % 1000
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if millis:
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def atomic_write_text(path: str | Path, text: str) -> Path:
    """Write text via temp file + fsync + os.replace so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
    return path


def atomic_write_json(path: str | Path, payload) -> Path:
    return atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
