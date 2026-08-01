"""Voice service — Faster-Whisper STT and Kokoro TTS.

Public API:
- STT: ``STTService``, ``STTSession``, ``EnergyVAD``, ``build_vad``,
  ``FasterWhisperTranscriber``, ``AudioDecoder``, ``STTError``.
- TTS: ``TTSService``, ``TTSStreamBuffer``, ``KokoroSynthesizer``,
  ``split_sentences``, ``TTSError``.
"""
from voice.stt import (
    AudioDecoder,
    EnergyVAD,
    FasterWhisperTranscriber,
    STTError,
    STTService,
    STTSession,
    TranscriptCallback,
    TranscriberProtocol,
    VADProtocol,
    WebRTCVAD,
    build_vad,
)
from voice.tts import (
    AudioFrameCallback,
    KokoroSynthesizer,
    SynthesizerProtocol,
    TTSError,
    TTSService,
    TTSStreamBuffer,
    split_sentences,
)

__all__ = [
    # STT
    "AudioDecoder",
    "EnergyVAD",
    "FasterWhisperTranscriber",
    "STTError",
    "STTService",
    "STTSession",
    "TranscriptCallback",
    "TranscriberProtocol",
    "VADProtocol",
    "WebRTCVAD",
    "build_vad",
    # TTS
    "AudioFrameCallback",
    "KokoroSynthesizer",
    "SynthesizerProtocol",
    "TTSError",
    "TTSService",
    "TTSStreamBuffer",
    "split_sentences",
]
