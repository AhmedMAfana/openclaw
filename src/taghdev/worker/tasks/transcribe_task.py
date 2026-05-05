"""Voice transcription — Groq API (~100ms) with local whisper fallback (~10s)."""
import asyncio
import base64
import os
import tempfile

from taghdev.utils.logging import get_logger

log = get_logger()

_model = None


# ── Groq API (fast path) ──

async def _get_groq_key() -> str:
    """Get Groq API key from DB first, then env fallback."""
    try:
        from taghdev.services.config_service import get_config
        config = await get_config("stt", "provider")
        if config and config.get("api_key"):
            return config["api_key"]
    except Exception:
        pass
    from taghdev.settings import settings
    return settings.groq_api_key


async def _transcribe_groq(ogg_bytes: bytes) -> str | None:
    """Transcribe via Groq Whisper API. Returns text or None on failure."""
    api_key = await _get_groq_key()
    if not api_key:
        return None

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("voice.ogg", ogg_bytes, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo", "language": "en", "response_format": "text"},
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                log.info("transcription.groq_ok", text_length=len(text))
                return text
            log.warning("transcription.groq_http_error", status=resp.status_code, body=resp.text[:200])
            return None
    except Exception as e:
        log.warning("transcription.groq_error", error=str(e))
        return None


# ── Local whisper (fallback) ──

def _get_model():
    """Lazy-load the whisper model (downloads on first use, cached after)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("transcription.loading_model", model="tiny")
        _model = WhisperModel("tiny", device="cpu", compute_type="int8")
        log.info("transcription.model_ready")
    return _model


def _transcribe_sync(wav_path: str) -> str:
    model = _get_model()
    segments, _ = model.transcribe(wav_path, beam_size=1, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def _transcribe_local(ogg_bytes: bytes) -> str:
    """Transcribe locally via faster-whisper. Slower but no API needed."""
    tmp_dir = tempfile.mkdtemp()
    ogg_path = os.path.join(tmp_dir, "voice.ogg")
    wav_path = os.path.join(tmp_dir, "voice.wav")

    try:
        with open(ogg_path, "wb") as f:
            f.write(ogg_bytes)

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", ogg_path, "-ar", "16000", "-ac", "1",
            "-f", "wav", wav_path, "-y",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            return ""

        if proc.returncode != 0:
            return ""

        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, _transcribe_sync, wav_path), timeout=120,
        )
    except Exception as e:
        log.error("transcription.local_failed", error=str(e))
        return ""
    finally:
        for f in [ogg_path, wav_path]:
            try:
                os.unlink(f)
            except OSError:
                pass
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


# ── Main entry ──

async def transcribe_voice(ctx: dict, voice_b64: str) -> str:
    """Worker task: Groq API first (~100ms), local whisper fallback (~10s)."""
    voice_bytes = base64.b64decode(voice_b64)
    log.info("transcription.received", size=len(voice_bytes))

    # 1. Groq API (fast, best quality)
    text = await _transcribe_groq(voice_bytes)
    if text:
        return text

    # 2. Local whisper fallback
    log.info("transcription.fallback_local")
    return await _transcribe_local(voice_bytes)
