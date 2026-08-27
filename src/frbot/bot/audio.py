"""Pronunciation audio: synthesis, format conversion, and an on-disk cache.

Hearing a word is not a nicety in French — spelling and sound diverge enough
that a silent card teaches the wrong thing. Every distinct phrase is
synthesised once and cached for the whole cohort, so the marginal cost of a
learner tapping 🔊 is usually zero.
"""

import asyncio
import hashlib
import logging
import shutil
import subprocess
import wave
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24_000  # Gemini TTS output: 24 kHz, mono, 16-bit PCM
CHANNELS = 1
SAMPLE_WIDTH = 2
CACHE_MAX_FILES = 2_000


class AudioUnavailable(Exception):
    """Pronunciation could not be produced; the caller degrades gracefully."""


def cache_key(text: str, voice: str) -> str:
    digest = hashlib.sha256(f"{voice}:{text.strip().lower()}".encode()).hexdigest()
    return digest[:32]


def cache_dir(db_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if db_url.startswith(prefix) and ":memory:" not in db_url:
        return Path(db_url.removeprefix(prefix)).parent / "tts"
    return Path("data/tts")


def pcm_to_wav(pcm: bytes) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _encode_opus(wav_bytes: bytes) -> bytes:
    """Telegram voice messages must be OGG/Opus; anything else shows up as a
    file attachment instead of a playable bubble."""
    process = subprocess.run(  # fixed argv, no shell
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
            "-f", "ogg", "pipe:1",
        ],
        input=wav_bytes,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0 or not process.stdout:
        raise AudioUnavailable(
            f"ffmpeg failed: {process.stderr.decode('utf-8', 'replace')[:200]}"
        )
    return process.stdout


async def to_voice_ogg(pcm: bytes) -> bytes:
    if not pcm:
        raise AudioUnavailable("empty audio from the model")
    if not ffmpeg_available():
        raise AudioUnavailable("ffmpeg is not installed")
    return await asyncio.to_thread(_encode_opus, pcm_to_wav(pcm))


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # atomic: a reader never sees a half-written file


class VoiceCache:
    """Disk cache of synthesised phrases, shared by every participant."""

    def __init__(self, directory: Path, max_files: int = CACHE_MAX_FILES) -> None:
        self.directory = directory
        self.max_files = max_files

    def path_for(self, text: str, voice: str) -> Path:
        return self.directory / f"{cache_key(text, voice)}.ogg"

    async def get(self, text: str, voice: str) -> bytes | None:
        return await asyncio.to_thread(_read, self.path_for(text, voice))

    async def put(self, text: str, voice: str, data: bytes) -> None:
        await asyncio.to_thread(_write, self.path_for(text, voice), data)

    def prune(self) -> int:
        """Drop the least recently used files once the cache grows too large."""
        if not self.directory.exists():
            return 0
        files = sorted(self.directory.glob("*.ogg"), key=lambda p: p.stat().st_atime)
        stale = files[: max(0, len(files) - self.max_files)]
        for path in stale:
            path.unlink(missing_ok=True)
        return len(stale)
