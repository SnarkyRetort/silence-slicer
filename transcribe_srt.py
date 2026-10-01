import os
import sys
import argparse
import tkinter as tk
from tkinter import filedialog, messagebox

def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

def transcribe(video_path: str, model_size: str = "medium", device: str = "auto"):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("faster-whisper is not installed.\nRun: pip install faster-whisper")

    choices = [("cuda", "float16"), ("cpu", "int8")] if device == "auto" else \
              ([("cuda", "float16")] if device == "cuda" else [("cpu", "int8")])

    model = None
    last_error = None
    for dev, compute in choices:
        try:
            print(f"Loading Whisper model '{model_size}' on {dev} ({compute})...")
            model = WhisperModel(model_size, device=dev, compute_type=compute)
            print(f"Using {dev}.")
            break
        except Exception as e:
            last_error = e
            print(f"{dev} initialization failed: {e}")

    if model is None:
        raise RuntimeError(f"Could not initialize Whisper model.\nLast error: {last_error}")

    print(f"\nTranscribing:\n{video_path}\n")
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=True
    )

    out_path = os.path.splitext(video_path)[0] + ".srt"
    index = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            index += 1
            f.write(f"{index}\n")
            f.write(f"{srt_time(seg.start)} --> {srt_time(seg.end)}\n")
            f.write(text + "\n\n")
            print(f"[{srt_time(seg.start)}] {text}")

    print(f"\nDONE: {out_path}")
    print(f"Language: {getattr(info, 'language', 'unknown')}")
    return out_path

def choose_file():
    root = tk.Tk()
    root.withdraw()
    path = filedialog.askopenfilename(
        title="Choose video to transcribe",
        filetypes=[
            ("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return path

def main():
    parser = argparse.ArgumentParser(description="Standalone video-to-SRT transcription")
    parser.add_argument("video", nargs="?", help="Video file to transcribe")
    parser.add_argument("--model", default="medium",
                        choices=["tiny", "base", "small", "medium", "large-v2", "large-v3"])
    parser.add_argument("--device", default="auto",
                        choices=["auto", "cuda", "cpu"])
    args = parser.parse_args()

    video = args.video or choose_file()
    if not video:
        print("No video selected.")
        return

    try:
        out = transcribe(video, args.model, args.device)
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo("SRT complete", f"Created:\n{out}")
        root.destroy()
    except Exception as e:
        print("\nERROR:", e)
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Transcription failed", str(e))
        root.destroy()
        sys.exit(1)

if __name__ == "__main__":
    main()
