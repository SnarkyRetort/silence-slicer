#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


def srt_time(seconds: float) -> str:
    ms = max(0, int(round(float(seconds) * 1000.0)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1_000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _import_whisper():
    try:
        from faster_whisper import WhisperModel
        return WhisperModel
    except Exception as exc:
        raise RuntimeError(
            "faster-whisper is not installed. Run: pip install faster-whisper"
        ) from exc


def _build_model(model_name: str, device: str, compute_type: str):
    WhisperModel = _import_whisper()
    print(f"MODEL_TRY|{device}|{compute_type}", flush=True)
    model = WhisperModel(
        model_name,
        device=device,
        compute_type=compute_type,
    )
    print(f"MODEL_OK|{device}|{compute_type}", flush=True)
    return model


def _write_transcription(model, source: Path, output: Path):
    """
    Run transcription AND consume the segment generator here.
    CUDA failures can happen lazily while iterating segments, so this entire
    operation must live inside the retry boundary.
    """
    print(f"TRANSCRIBE_START|{source}", flush=True)

    temp_output = output.with_suffix(output.suffix + ".part")
    block_count = 0
    info = None

    try:
        segments, info = model.transcribe(
            str(source),
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
        )

        with temp_output.open("w", encoding="utf-8", newline="\n") as handle:
            for segment in segments:
                text = (segment.text or "").strip()
                if not text:
                    continue

                block_count += 1
                handle.write(f"{block_count}\n")
                handle.write(
                    f"{srt_time(segment.start)} --> "
                    f"{srt_time(segment.end)}\n"
                )
                handle.write(text + "\n\n")

                if block_count % 10 == 0:
                    print(
                        f"PROGRESS|{block_count}|{segment.end:.2f}",
                        flush=True,
                    )

        temp_output.replace(output)
        language = getattr(info, "language", "") or ""
        print(f"DONE|{output}|{block_count}|{language}", flush=True)
        return block_count, language

    finally:
        if temp_output.exists():
            try:
                temp_output.unlink()
            except OSError:
                pass


def _looks_like_cuda_runtime_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    cuda_markers = (
        "cublas",
        "cudnn",
        "cuda",
        "libcublas",
        "library cublas",
        "cannot be loaded",
        "dll is not found",
        "dll not found",
    )
    return any(marker in msg for marker in cuda_markers)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="medium")
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cuda", "cpu"),
    )
    args = parser.parse_args()

    source = Path(args.input).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()

    if not source.exists():
        raise FileNotFoundError(f"Video does not exist: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)

    # Reliable behavior:
    #   cpu  -> CPU only
    #   cuda -> CUDA only (surface errors)
    #   auto -> try CUDA first; if CUDA fails at model load OR during the lazy
    #           transcription generator, automatically retry from scratch on CPU.
    if args.device == "cpu":
        model = _build_model(args.model, "cpu", "int8")
        _write_transcription(model, source, output)
        return 0

    if args.device == "cuda":
        model = _build_model(args.model, "cuda", "int8_float16")
        _write_transcription(model, source, output)
        return 0

    # AUTO
    try:
        model = _build_model(args.model, "cuda", "int8_float16")
        _write_transcription(model, source, output)
        return 0
    except Exception as exc:
        # On this machine the common failure is cublas64_12.dll. In auto mode
        # we fall back even if the CUDA failure arrives only when the segment
        # generator starts yielding.
        print(f"GPU_TRANSCRIBE_FAIL|{exc}", flush=True)
        if _looks_like_cuda_runtime_failure(exc):
            print("CPU_FALLBACK|CUDA runtime unavailable; retrying on CPU", flush=True)
        else:
            print("CPU_FALLBACK|GPU attempt failed; retrying on CPU", flush=True)

        # Remove any stale partial/final output from a failed GPU attempt.
        part = output.with_suffix(output.suffix + ".part")
        for p in (part,):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        cpu_model = _build_model(args.model, "cpu", "int8")
        _write_transcription(cpu_model, source, output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
