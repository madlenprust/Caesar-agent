"""TTS — text → voice .ogg (Opus) для Telegram sendVoice.

edge-tts (Microsoft Azure TTS, free, без API key) → mp3 → ffmpeg → ogg/Opus.
Если ffmpeg нет → mp3 (sendAudio fallback). Если edge-tts не установлен → None.

Используется telegram_adapter'ом для voice-out: юзер написал голосом →
ответ тоже голосом (завершение voice-loop: STT в → TTS out).
"""
import shutil
import subprocess
import tempfile
import os
from pathlib import Path

_INSTALL_HINT = "TTS требует edge-tts: pip install edge-tts"


def _get_edge_tts():
    """Импорт edge_tts. None если не установлен."""
    try:
        import edge_tts
        return edge_tts
    except ImportError:
        return None


async def synthesize(text: str, voice: str = "ru-RU-SvetlanaNeural") -> tuple[Path, str] | None:
    """TTS text → (audio_path, content_type) для TG sendVoice/sendAudio.

    Возвращает (path, content_type):
    - ogg/Opus → sendVoice (round voice bubble, если есть ffmpeg).
    - mp3 → sendAudio (fallback, если ffmpeg нет).
    - None → edge-tts не установлен или пустой текст.

    Файл — временный, вызывающий должен его удалить после отправки.
    """
    edge_tts = _get_edge_tts()
    if edge_tts is None:
        return None
    if not text or not text.strip():
        return None

    # Обрезаем длинный текст (edge-tts на 5000+ токенов может тупить)
    if len(text) > 4000:
        text = text[:4000] + "…"

    tmp_fd, mp3_path_str = tempfile.mkstemp(suffix=".mp3", prefix="caesar_tts_")
    os.close(tmp_fd)
    mp3_path = Path(mp3_path_str)

    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(mp3_path))
    except Exception:
        mp3_path.unlink(missing_ok=True)
        return None

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        return None

    # mp3 → ogg/Opus для sendVoice (round voice bubble в TG)
    if shutil.which("ffmpeg"):
        ogg_path = mp3_path.with_suffix(".ogg")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp3_path),
                 "-c:a", "libopus", "-b:a", "32k",
                 str(ogg_path)],
                capture_output=True, timeout=30,
            )
            if ogg_path.exists() and ogg_path.stat().st_size > 0:
                mp3_path.unlink(missing_ok=True)
                return (ogg_path, "audio/ogg")
        except Exception:
            pass  # fallback to mp3

    # Fallback: mp3 (sendAudio)
    return (mp3_path, "audio/mpeg")
