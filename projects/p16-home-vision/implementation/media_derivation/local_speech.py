from __future__ import annotations

import json
import math
import struct
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
import wave


TRANSCRIPT_SCHEMA = "p16.transcript.v1"


@dataclass(frozen=True)
class SpeechRegion:
    start_ms: int
    end_ms: int
    rms: float


@dataclass(frozen=True)
class TranscriptSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class Transcript:
    schema: str
    asset_id: str
    language: str | None
    vad_backend: str
    asr_backend: str
    segments: tuple[TranscriptSegment, ...]


class LocalASR(Protocol):
    def transcribe(self, wav_path: Path, regions: tuple[SpeechRegion, ...]) -> tuple[TranscriptSegment, ...]: ...


def detect_speech_regions(
    wav_path: Path, *, frame_ms: int = 30, threshold_rms: float = 420.0,
    min_speech_ms: int = 180, merge_silence_ms: int = 240,
) -> tuple[SpeechRegion, ...]:
    """Bounded-memory PCM16 energy VAD for mono signed 16-bit PCM WAV."""
    if frame_ms <= 0 or threshold_rms < 0 or min_speech_ms < 0 or merge_silence_ms < 0:
        raise ValueError("VAD_CONFIGURATION_INVALID")
    try:
        with wave.open(str(wav_path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
                raise ValueError("VAD_REQUIRES_MONO_PCM16_WAV")
            rate = wav.getframerate()
            if rate <= 0:
                raise ValueError("VAD_SAMPLE_RATE_INVALID")
            samples_per_frame = max(1, rate * frame_ms // 1000)
            # Read at most five seconds of PCM at a time; long recordings never
            # become one huge bytes object or Python tuple of all samples.
            batch_frames = max(samples_per_frame, (rate * 5 // samples_per_frame) * samples_per_frame)
            remaining = wav.getnframes()
            sample_offset = 0
            groups: list[SpeechRegion] = []
            active_start: int | None = None
            active_end = 0
            active_rms_sum = 0.0
            active_frames = 0

            def finish_group() -> None:
                nonlocal active_start, active_end, active_rms_sum, active_frames
                if active_start is not None and active_end - active_start >= min_speech_ms:
                    groups.append(SpeechRegion(
                        active_start, active_end,
                        round(active_rms_sum / active_frames, 2),
                    ))
                active_start = None
                active_end = 0
                active_rms_sum = 0.0
                active_frames = 0

            while remaining:
                requested = min(batch_frames, remaining)
                raw = wav.readframes(requested)
                if len(raw) % 2:
                    raise ValueError("VAD_PCM_INVALID")
                batch_samples = len(raw) // 2
                if batch_samples == 0:
                    break
                for offset in range(0, batch_samples, samples_per_frame):
                    count = min(samples_per_frame, batch_samples - offset)
                    sample_bytes = raw[offset * 2:(offset + count) * 2]
                    samples = struct.unpack(f"<{count}h", sample_bytes)
                    rms = math.sqrt(sum(sample * sample for sample in samples) / count)
                    start = round(sample_offset * 1000 / rate)
                    end = round((sample_offset + count) * 1000 / rate)
                    if rms >= threshold_rms:
                        if active_start is None:
                            active_start = start
                            active_rms_sum = rms
                            active_frames = 1
                        elif start - active_end <= merge_silence_ms:
                            active_rms_sum += rms
                            active_frames += 1
                        else:
                            finish_group()
                            active_start = start
                            active_rms_sum = rms
                            active_frames = 1
                        active_end = end
                    elif active_start is not None and start - active_end > merge_silence_ms:
                        finish_group()
                    sample_offset += count
                remaining -= requested
    except (wave.Error, OSError) as exc:
        raise ValueError("VAD_WAV_INVALID") from exc
    if active_start is not None:
        finish_group()
    return tuple(groups)


class JsonCommandASR:
    """Adapter for an explicitly configured local executable; never calls a network."""
    def __init__(self, command: tuple[str, ...], *, backend_id: str, timeout_seconds: int = 300):
        if not command or not backend_id.strip() or timeout_seconds <= 0:
            raise ValueError("ASR_CONFIGURATION_INVALID")
        self.command, self.backend_id, self.timeout_seconds = command, backend_id, timeout_seconds

    def transcribe(self, wav_path: Path, regions: tuple[SpeechRegion, ...]) -> tuple[TranscriptSegment, ...]:
        payload = json.dumps({"wav_path": str(wav_path), "regions": [asdict(r) for r in regions]})
        try:
            result = subprocess.run(
                [*self.command, payload], check=True, capture_output=True, text=True,
                timeout=self.timeout_seconds,
            )
            rows = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise RuntimeError("LOCAL_ASR_FAILED") from exc
        if not isinstance(rows, list):
            raise RuntimeError("LOCAL_ASR_OUTPUT_INVALID")
        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("LOCAL_ASR_OUTPUT_INVALID")
            start, end, txt, confidence = row.get("start_ms"), row.get("end_ms"), row.get("text"), row.get("confidence")
            if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int) or start < 0 or end < start or not isinstance(txt, str):
                raise RuntimeError("LOCAL_ASR_OUTPUT_INVALID")
            if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1):
                raise RuntimeError("LOCAL_ASR_OUTPUT_INVALID")
            parsed.append(TranscriptSegment(start, end, txt, float(confidence) if confidence is not None else None))
        return tuple(parsed)


def build_transcript(asset_id: str, wav_path: Path, asr: LocalASR, *, language: str | None = None, vad_threshold_rms: float = 420.0) -> Transcript:
    if not asset_id:
        raise ValueError("ASSET_ID_REQUIRED")
    regions = detect_speech_regions(wav_path, threshold_rms=vad_threshold_rms)
    segments = asr.transcribe(wav_path, regions) if regions else ()
    for segment in segments:
        if not any(
            region.start_ms <= segment.start_ms <= region.end_ms
            and segment.end_ms <= region.end_ms
            for region in regions
        ):
            raise ValueError("ASR_SEGMENT_OUTSIDE_VAD")
    return Transcript(TRANSCRIPT_SCHEMA, asset_id, language, "p16.energy_vad.v1", str(getattr(asr, "backend_id", type(asr).__name__)), segments)


def write_transcript(path: Path, transcript: Transcript) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**asdict(transcript), "segments": [asdict(row) for row in transcript.segments]}
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
