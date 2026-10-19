import math
import struct
import wave

import pytest

from media_derivation.local_speech import SpeechRegion, TranscriptSegment, build_transcript, detect_speech_regions


def _wav(path):
    rate = 16_000
    samples = [0] * rate
    samples += [int(5000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate)]
    samples += [0] * rate
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def test_energy_vad_finds_only_voiced_window(tmp_path):
    path = tmp_path / "speech.wav"
    _wav(path)
    regions = detect_speech_regions(path, frame_ms=50, threshold_rms=500, min_speech_ms=200)
    assert len(regions) == 1
    assert 950 <= regions[0].start_ms <= 1050
    assert 1900 <= regions[0].end_ms <= 2050


def test_transcript_uses_local_asr_only_for_vad_regions(tmp_path):
    path = tmp_path / "speech.wav"
    _wav(path)

    class ASR:
        backend_id = "fixture.local"
        def transcribe(self, wav_path, regions):
            assert regions
            return (TranscriptSegment(regions[0].start_ms, regions[0].end_ms, "fixture speech", 0.9),)

    result = build_transcript("asset-1", path, ASR())
    assert result.schema == "p16.transcript.v1"
    assert result.asr_backend == "fixture.local"
    assert result.segments[0].text == "fixture speech"


def test_transcript_rejects_unaligned_asr_output(tmp_path):
    path = tmp_path / "speech.wav"
    _wav(path)

    class ASR:
        def transcribe(self, wav_path, regions):
            return (TranscriptSegment(0, 100, "bad"),)

    with pytest.raises(ValueError, match="ASR_SEGMENT_OUTSIDE_VAD"):
        build_transcript("asset-1", path, ASR())


def test_energy_vad_reads_long_recording_in_bounded_chunks(tmp_path, monkeypatch):
    path = tmp_path / "long.wav"
    rate = 16_000
    silence = struct.pack(f"<{rate * 12}h", *([0] * (rate * 12)))
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(silence)

    real_open = wave.open
    requested_frames = []

    class Reader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def __enter__(self):
            self.wrapped.__enter__()
            return self
        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.wrapped, name)
        def readframes(self, count):
            requested_frames.append(count)
            return self.wrapped.readframes(count)

    monkeypatch.setattr(wave, "open", lambda *args, **kwargs: Reader(real_open(*args, **kwargs)))
    assert detect_speech_regions(path) == ()
    assert len(requested_frames) >= 3
    assert max(requested_frames) <= rate * 5


def test_transcript_rejects_segment_ending_after_vad_region(tmp_path):
    path = tmp_path / "speech.wav"
    _wav(path)

    class ASR:
        def transcribe(self, wav_path, regions):
            return (TranscriptSegment(regions[0].start_ms, regions[0].end_ms + 1, "overrun"),)

    with pytest.raises(ValueError, match="ASR_SEGMENT_OUTSIDE_VAD"):
        build_transcript("asset-1", path, ASR())
