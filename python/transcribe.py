"""Transcribe meeting audio with parakeet-mlx and emit JSON.

Usage:
  uv run transcribe.py <audio> [--mic <mic_path>] [--mix <other>] [--model <hf-id>]
  uv run transcribe.py --download     # fetch the model once, before first use

The app runs this with Hugging Face network access switched off, so the model
must already be in the local cache. `--download` puts it there.

Speaker attribution (`--mic`, the app's default):
  Pass the system-output track as <audio> and the microphone track via --mic.
  When both tracks carry real audio, each is transcribed SEPARATELY and the
  segments are merged into one speaker-labeled transcript — microphone → "You"
  (the user), system output → "Remote" (every other participant). That label is
  the signal `extract_todos.py` uses to tell `mine` from `waiting_on` reliably.
  A `<stem>.mixed.wav` is still written for playback. If a track is silent or
  missing, we fall back to a single unlabeled transcript (legacy behavior), so a
  muted mic can never mislabel every utterance as "Remote".

  --mix keeps the old behavior explicitly: mix the two tracks to mono, transcribe
  once, emit an unlabeled transcript.

Output (stdout): JSON
  {"text": str,
   "segments": [{"start": float, "end": float, "text": str, "speaker": str|null}],
   "diarized": bool}
Progress + errors go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from parakeet_mlx import from_pretrained


DEFAULT_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"

SPEAKER_LABELS = {"you": "You", "remote": "Remote"}
# A track whose peak amplitude is below this never held real speech (a muted or
# unrouted input floats around the noise floor, ~1e-4; speech peaks near 0.1–1.0).
_SILENCE_PEAK = 0.01


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), always_2d=True)
    mono = data.mean(axis=1).astype(np.float32)
    return mono, sr


def _peak(sig: np.ndarray) -> float:
    return float(np.max(np.abs(sig))) if len(sig) else 0.0


def _strip_track_suffix(path: Path) -> str:
    """`rec-…​.system.wav` / `rec-….mic.wav` → `rec-…` (else the plain stem)."""
    name = path.name
    for tag in (".system.wav", ".mic.wav"):
        if name.endswith(tag):
            return name[: -len(tag)]
    return path.stem


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def mix_tracks(a: Path, b: Path, out: Path) -> Path:
    """Mix two audio files into one mono WAV at the higher sample rate.

    Tracks of different lengths get zero-padded to the max length. Silent
    tracks (RMS == 0) are skipped so the other track isn't attenuated by the
    normalization step.
    """
    sig_a, sr_a = _read_mono(a)
    sig_b, sr_b = _read_mono(b)
    target_sr = max(sr_a, sr_b)

    def maybe_resample(sig: np.ndarray, sr: int) -> np.ndarray:
        if sr == target_sr:
            return sig
        # Cheap linear resample — good enough for ASR.
        n_out = int(round(len(sig) * target_sr / sr))
        x_old = np.linspace(0.0, 1.0, num=len(sig), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, sig).astype(np.float32)

    sig_a = maybe_resample(sig_a, sr_a)
    sig_b = maybe_resample(sig_b, sr_b)
    n = max(len(sig_a), len(sig_b))
    if len(sig_a) < n: sig_a = np.pad(sig_a, (0, n - len(sig_a)))
    if len(sig_b) < n: sig_b = np.pad(sig_b, (0, n - len(sig_b)))

    parts = []
    for sig in (sig_a, sig_b):
        if float(np.sqrt(np.mean(sig.astype(np.float64) ** 2))) > 1e-6:
            parts.append(sig)
    if not parts:
        mixed = sig_a  # all silent — emit one of them
    else:
        mixed = np.sum(parts, axis=0)
        peak = float(np.max(np.abs(mixed)))
        if peak > 1.0:
            mixed = mixed / peak * 0.98

    sf.write(str(out), mixed.astype(np.float32), target_sr, subtype="PCM_16")
    print(f"mixed → {out}  ({len(mixed)/target_sr:.2f}s @ {target_sr} Hz)", file=sys.stderr, flush=True)
    return out


def _write_mono(sig: np.ndarray, sr: int, dst: Path) -> Path:
    """Write a mono WAV for the ASR model to read."""
    sf.write(str(dst), sig, sr, subtype="PCM_16")
    return dst


def _rms(sig: np.ndarray) -> float:
    return float(np.sqrt(np.mean(sig.astype(np.float64) ** 2))) if len(sig) else 0.0


def _window_rms(sig: np.ndarray, sr: int, start: float, end: float) -> float:
    i0 = max(0, int(start * sr))
    i1 = min(len(sig), int(end * sr))
    return _rms(sig[i0:i1]) if i1 > i0 else 0.0


# How much louder a segment must be on its OWN track than on the other track to
# be kept. Remote (system-output) audio plays out the speakers and bleeds into
# the mic as an attenuated echo, so the same utterance is transcribed on BOTH
# tracks; the echo copy is quieter on the mic than the original is on the system
# track, so "own track must be at least this many× louder" drops it. The bleed
# is one-directional (the mic is never played back out), so this only prunes
# mic-side echoes of remote speech. >1.0 biases toward removing echo; genuine
# simultaneous double-talk on the quieter track can be dropped.
_ECHO_MARGIN = float(os.getenv("RECORDER_ECHO_MARGIN", "1.3"))


def suppress_cross_talk(
    you_sig: np.ndarray, you_sr: int,
    remote_sig: np.ndarray, remote_sr: int,
    you_segs: list[dict], remote_segs: list[dict],
    margin: float = _ECHO_MARGIN,
) -> tuple[list[dict], list[dict]]:
    """Drop segments that are really the other track's audio bleeding through.

    A segment is kept only when its own track is clearly louder than the other
    during that time window. Both tracks are (near) time-aligned, so we compare
    each segment's window energy across tracks."""
    kept_you = [
        s for s in you_segs
        if _window_rms(you_sig, you_sr, s["start"], s["end"])
        >= _window_rms(remote_sig, remote_sr, s["start"], s["end"]) * margin
    ]
    kept_remote = [
        s for s in remote_segs
        if _window_rms(remote_sig, remote_sr, s["start"], s["end"])
        >= _window_rms(you_sig, you_sr, s["start"], s["end"]) * margin
    ]
    return kept_you, kept_remote


def _segments_from_result(result) -> list[dict]:
    segments = []
    for seg in getattr(result, "sentences", None) or []:
        segments.append(
            {
                "start": float(getattr(seg, "start", 0.0) or 0.0),
                "end": float(getattr(seg, "end", 0.0) or 0.0),
                "text": str(getattr(seg, "text", "")).strip(),
            }
        )
    return segments


def _transcribe(model, path: Path, label: str):
    print(f"transcribing {label} ({path.name})…", file=sys.stderr, flush=True)

    def on_chunk(current: float, total: float) -> None:
        pct = (current / total * 100) if total else 0
        # Keep the `chunk N/Ms (P%)` shape the app's progress parser matches.
        print(f"  chunk {current:.0f}/{total:.0f}s ({pct:.0f}%)  [{label}]", file=sys.stderr, flush=True)

    # Chunk long files so we don't blow past the Metal buffer limit (~9.5 GB on
    # M3). 120 s @ 48 kHz keeps the per-chunk allocation safely small; parakeet
    # stitches segments back together using `overlap_duration`.
    return model.transcribe(
        str(path),
        chunk_duration=120.0,
        overlap_duration=15.0,
        chunk_callback=on_chunk,
    )


def merge_labeled_segments(you_segs: list[dict], remote_segs: list[dict]) -> tuple[str, list[dict]]:
    """Tag each segment with its speaker, merge by start time, and render a
    `You:` / `Remote:` labeled transcript. Each track's timeline is zero-based
    from its own first sample, so cross-track ordering is approximate at
    boundaries — fine for owner attribution, which only needs each utterance's
    own speaker."""
    for s in you_segs:
        s["speaker"] = "you"
    for s in remote_segs:
        s["speaker"] = "remote"
    merged = sorted(you_segs + remote_segs, key=lambda s: (s["start"], s["end"]))
    lines = [
        f'{SPEAKER_LABELS[s["speaker"]]}: {s["text"]}'
        for s in merged
        if s["text"]
    ]
    return "\n".join(lines), merged


def _run_diarized(model, system: Path, mic: Path) -> int:
    stem = _strip_track_suffix(system)

    # Still emit the mixed track: it's the playback artifact the app offers, and
    # its `mixed → …` stderr line doubles as a transcribe-progress heartbeat.
    try:
        mix_tracks(system, mic, system.with_name(f"{stem}.mixed.wav"))
    except Exception as e:
        print(f"mix (for playback) failed: {type(e).__name__}: {e}", file=sys.stderr)

    you_sig, you_sr = _read_mono(mic)
    remote_sig, remote_sr = _read_mono(system)
    you_peak, remote_peak = _peak(you_sig), _peak(remote_sig)

    # If one side never held speech (e.g. muted mic), a labeled transcript would
    # mislabel every line. Fall back to a single unlabeled transcript of
    # whichever track has audio.
    if you_peak < _SILENCE_PEAK or remote_peak < _SILENCE_PEAK:
        if you_peak < _SILENCE_PEAK and remote_peak < _SILENCE_PEAK:
            print(
                f"both tracks silent (you_peak={you_peak:.5f}, "
                f"remote_peak={remote_peak:.5f}); empty transcript",
                file=sys.stderr,
            )
            _emit({"text": "", "segments": [], "diarized": False})
            return 0
        from_you = remote_peak < _SILENCE_PEAK
        sig, sr, label = (
            (you_sig, you_sr, "you-only") if from_you else (remote_sig, remote_sr, "remote-only")
        )
        print(
            f"one track silent (you_peak={you_peak:.5f}, remote_peak={remote_peak:.5f}); "
            f"single-track transcript from {label}",
            file=sys.stderr,
        )
        with tempfile.TemporaryDirectory() as td:
            wav = _write_mono(sig, sr, Path(td) / "present.wav")
            result = _transcribe(model, wav, label)
        _emit({
            "text": str(result.text).strip(),
            "segments": _segments_from_result(result),
            "diarized": False,
        })
        return 0

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        you_wav = _write_mono(you_sig, you_sr, tdp / "you.wav")
        remote_wav = _write_mono(remote_sig, remote_sr, tdp / "remote.wav")
        you_segs = _segments_from_result(_transcribe(model, you_wav, "you"))
        remote_segs = _segments_from_result(_transcribe(model, remote_wav, "remote"))

    # Prune segments that are really the other track bleeding through (remote
    # audio echoing into the mic), so an utterance isn't labeled both You and
    # Remote.
    raw_you, raw_remote = len(you_segs), len(remote_segs)
    you_segs, remote_segs = suppress_cross_talk(
        you_sig, you_sr, remote_sig, remote_sr, you_segs, remote_segs
    )
    dropped = (raw_you - len(you_segs)) + (raw_remote - len(remote_segs))
    if dropped:
        print(
            f"cross-talk suppression dropped {dropped} echo segment(s) "
            f"(margin={_ECHO_MARGIN})",
            file=sys.stderr,
        )

    text, merged = merge_labeled_segments(you_segs, remote_segs)
    print(
        f"diarized: {len(you_segs)} You + {len(remote_segs)} Remote segment(s)",
        file=sys.stderr,
    )
    _emit({"text": text, "segments": merged, "diarized": True})
    return 0


def _run_single(model, audio: Path, mix: Path | None) -> int:
    audio_path = audio
    if mix and mix.exists() and mix.stat().st_size >= 1024:
        out = audio.with_name(f"{_strip_track_suffix(audio)}.mixed.wav")
        try:
            audio_path = mix_tracks(audio, mix, out)
        except Exception as e:
            print(f"mix failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 7

    result = _transcribe(model, audio_path, "audio")
    _emit({
        "text": str(result.text).strip(),
        "segments": _segments_from_result(result),
        "diarized": False,
    })
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("audio", type=Path, nargs="?", help="Primary (system-output) audio track.")
    p.add_argument("--mic", type=Path, default=None,
                   help="Microphone track. When it and `audio` both carry speech, "
                        "produces a speaker-labeled (You/Remote) transcript.")
    p.add_argument("--mix", type=Path, default=None,
                   help="Legacy: mix this track with `audio` to mono and transcribe once "
                        "(unlabeled).")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--download", action="store_true",
                   help="Download the model into the local cache and exit.")
    args = p.parse_args()

    if args.download:
        from_pretrained(args.model)
        print(f"model ready: {args.model}", file=sys.stderr)
        return 0
    if args.audio is None:
        p.error("audio is required unless --download is given")

    if not args.audio.exists():
        print(f"audio not found: {args.audio}", file=sys.stderr)
        return 2
    if args.audio.stat().st_size < 1024:
        print(f"audio too small ({args.audio.stat().st_size} bytes): {args.audio}", file=sys.stderr)
        return 4

    print(f"loading model {args.model}…", file=sys.stderr, flush=True)
    try:
        model = from_pretrained(args.model)
    except Exception as e:
        print(f"model load failed: {type(e).__name__}: {e}", file=sys.stderr)
        if os.environ.get("HF_HUB_OFFLINE") == "1":
            print(
                "The model is not in the local cache and the app runs offline. "
                "Run once: cd python && uv run python transcribe.py --download",
                file=sys.stderr,
            )
        return 5

    mic = args.mic
    if mic and mic.exists() and mic.stat().st_size >= 1024:
        try:
            return _run_diarized(model, system=args.audio, mic=mic)
        except Exception as e:
            # Never lose a transcript to the diarized path — fall back to the
            # single-track transcription of the system output.
            print(
                f"diarized transcribe failed: {type(e).__name__}: {e}; "
                f"falling back to single-track",
                file=sys.stderr,
            )

    return _run_single(model, args.audio, args.mix)


if __name__ == "__main__":
    raise SystemExit(main())
