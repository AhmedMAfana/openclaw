"""Voice transcription service — faster-whisper (local, free).

Converts Telegram voice messages (.ogg) to text.
Uses the 'tiny' model (~75MB, ~1 sec per message).
"""
import asyncio
import os

from taghdev.utils.logging import get_logger

log = get_logger()

_model = None


def _get_model():
    """Lazy-load the whisper model (downloads on first use)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("transcription.loading_model", model="tiny")
        _model = WhisperModel("tiny", device="cpu", compute_type="int8")
        log.info("transcription.model_ready")
    return _model


async def transcribe_ogg(ogg_path: str) -> str:
    """Transcribe an .ogg voice file to text.

    1. Convert .ogg → .wav via ffmpeg
    2. Transcribe .wav via faster-whisper
    3. Cleanup temp files
    """
    wav_path = ogg_path.replace(".ogg", ".wav")

    try:
        # Convert ogg to wav (timeout: 15s)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1",
            "-f", "wav", wav_path, "-y",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            log.error("transcription.ffmpeg_timeout")
            return ""

        if proc.returncode != 0:
            log.error("transcription.ffmpeg_failed", returncode=proc.returncode)
            return ""

        # Transcribe in thread pool (faster-whisper is sync, timeout: 30s)
        loop = asyncio.get_event_loop()
        text = await asyncio.wait_for(
            loop.run_in_executor(None, _transcribe_sync, wav_path),
            timeout=30,
        )

        log.info("transcription.done", text_length=len(text))
        return text

    except asyncio.TimeoutError:
        log.error("transcription.timeout", ogg_path=ogg_path)
        return ""
    except Exception as e:
        log.error("transcription.failed", error=str(e))
        return ""
    finally:
        # Cleanup
        for f in [ogg_path, wav_path]:
            try:
                os.unlink(f)
            except OSError:
                pass


def _transcribe_sync(wav_path: str) -> str:
    """Synchronous transcription (runs in thread pool)."""
    model = _get_model()
    segments, info = model.transcribe(wav_path, beam_size=1, language=None)
    text = " ".join(segment.text.strip() for segment in segments)
    return text.strip()
