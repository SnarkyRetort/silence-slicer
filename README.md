# Silence Slicer

Silence Slicer is a Windows desktop video workflow tool for turning long recordings into usable edits. It can remove dead air, create and route subtitles, analyze transcripts for notable moments, review clips as KEEP / MAYBE / TRASH, assemble sequences, and export editable timelines for DaVinci Resolve.

## Highlights

- -50 / -40 / -30 / -27 Conversation / -25 dB silence-cut presets
- FFmpeg-based video processing and preview
- Processed-video transcription: cut RAW footage first, then transcribe only the cleaned working video
- Optional local `faster-whisper` transcription when no source SRT is available
- Ranked footage analysis with KEEP / MAYBE / TRASH review
- Sequence Builder for assembling selected moments
- FCPXML / OTIO-oriented Resolve handoff workflow
- Story cross-reference tools for transcripts, moments, and diary-style source material
- Optional PocketTTS narration workflow

## Requirements

- Windows 10/11
- Python 3.10+ recommended
- FFmpeg, ffprobe, and preferably ffplay
- Optional: `faster-whisper` for automatic local transcription
- Optional: a compatible local PocketTTS endpoint for narration features

Silence Slicer checks common Windows FFmpeg locations and your system `PATH`. A typical install is `C:\ffmpeg\bin`, but FFmpeg does not need to live there if it is already on `PATH`.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python silence_cutter_gui.py
```

If you do not want automatic transcription, `faster-whisper` is optional; the rest of the application uses Python's standard library.

## FFmpeg

FFmpeg binaries are **not bundled in this repository**. Install FFmpeg separately from a trusted distribution. Silence Slicer needs `ffmpeg` and `ffprobe`; `ffplay` is recommended for preview playback.

Silence Slicer can find FFmpeg in several ways:

1. **Add FFmpeg to Windows `PATH`** — recommended for a normal system-wide install. For example, if FFmpeg is installed at `D:\\ffmpeg`, add `D:\\ffmpeg\\bin` to your Windows `PATH`. The drive letter does not matter.
2. **Portable/local install** — create a `bin` folder beside `silence_cutter_gui.py` and place `ffmpeg.exe`, `ffprobe.exe`, and `ffplay.exe` inside it:

```text
Silence-Slicer/
├─ silence_cutter_gui.py
├─ footage_analysis.py
├─ sequence_builder.py
├─ ...
└─ bin/
   ├─ ffmpeg.exe
   ├─ ffprobe.exe
   └─ ffplay.exe
```

3. **Common Windows location** — the application also checks `C:\\ffmpeg\\bin` and `C:\\ffmpeg`.

If FFmpeg is installed somewhere else, adding its `bin` directory to `PATH` is the simplest option.

### Adding FFmpeg to Windows PATH

1. Locate the folder containing `ffmpeg.exe`, `ffprobe.exe`, and `ffplay.exe`. Example: `D:\\ffmpeg\\bin`.
2. Open **Start** and search for **Edit the system environment variables**.
3. Open **Environment Variables**.
4. Under **User variables**, select **Path** and choose **Edit**.
5. Choose **New**, paste the FFmpeg `bin` folder path, and click **OK** through the dialogs.
6. Open a new Command Prompt and run:

```bat
ffmpeg -version
ffprobe -version
```

If both commands print version information, Silence Slicer should be able to find them.

## Quick Start on Another Windows PC

1. Install **Python 3.10 or newer**.
2. Extract or clone Silence Slicer anywhere you want. It does not need to be on the same drive as FFmpeg.
3. Install FFmpeg using one of the methods above: add its `bin` folder to `PATH`, or place the three executables in `Silence-Slicer\bin`.
4. Open Command Prompt or PowerShell in the Silence Slicer folder.
5. Create and activate a virtual environment, then install the Python requirements:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

6. Launch Silence Slicer:

```bat
python silence_cutter_gui.py
```

Once Python, the requirements, and FFmpeg are available, the program can be moved to another folder or drive and used normally. Your videos and project folders can live on any drive with enough free space.

## Project workflow

Silence Slicer's project pipeline is intentionally one-way so video and subtitle timing cannot be mixed between stages:

```text
RAW recording
    -> silence-cut processed video (working / "meat" source)
    -> matching SRT transcribed from that processed video
    -> Footage Analysis using that exact video/SRT pair
    -> Top-N category folders (Funniest, Emotional, Arguments, Character-Defining, etc.)
       containing matching MP4 + clip-retimed SRT pairs
```

The normal project workflow does not transcribe RAW footage. Footage Analysis accepts registered processed project versions and their exact registered SRTs; category exports derive from the currently loaded processed pair.

## Project files

The main modules are:

- `silence_cutter_gui.py` — desktop UI and workflow orchestration
- `silence_cutter.py` — command-line silence cutting
- `footage_analysis.py` — transcript / moment analysis
- `footage_preview.py` — video preview and transport
- `sequence_builder.py` — sequence assembly and timeline export
- `project_manager.py` — project workspace and asset routing
- `story_crossrefs.py` — story / transcript cross-reference tools
- `unbound_utils.py` — shared helpers

Historical development notes are preserved under `docs/`.

## Privacy / local data

The public repository intentionally excludes local media, subtitles, transcripts, generated CSV files, cached Python files, shortcuts, FFmpeg binaries, and local Git history. Do not commit API keys, tokens, private transcripts, or personal project media.

## License

Silence Slicer is released under the MIT License; see `LICENSE`. FFmpeg is a separate project and has its own licensing terms.
