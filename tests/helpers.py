import io
import struct
import wave
from pathlib import Path


def wav_bytes(seconds: float = 0.1, rate: int = 22050) -> bytes:
    frames = int(rate * seconds)
    payload = b"".join(struct.pack("<h", int(8000 * ((index % 100) - 50) / 50)) for index in range(frames))
    stream = io.BytesIO()
    with wave.open(stream, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)
    return stream.getvalue()


def make_voice(settings, voice_id: str = "v_test", content: bytes = b"RIFFfake") -> Path:
    path = settings.voices_dir / voice_id / "ref.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
