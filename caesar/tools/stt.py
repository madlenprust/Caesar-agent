"""Speech-to-Text: транскрипция голосовых сообщений.

Использует faster-whisper (порт OpenAI Whisper на CTranslate2).
Работает локально, бесплатно, без cloud API.
Модель скачивается один раз при первом использовании.
Работает на CPU (GPU не требуется, не используется).

Модели:
  tiny   - 39 MB, fastest,    качество ниже
  base   - 74 MB, fast,       отличное для русского
  small  - 244 MB, medium,    хорошее качество
  medium - 769 MB, slow,      отличное качество
  large  - 1550 MB, slowest,  лучшее качество

По умолчанию: base (быстро + хороший русский).
"""

import asyncio
import os
import subprocess
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any

# Подавляем предупреждения torch/CUDA — мы работаем на CPU.
# Скрываем GPU от torch чтобы не было warning о старых драйверах.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
warnings.filterwarnings("ignore", message=".*CUDA.*")
warnings.filterwarnings("ignore", message=".*NVIDIA.*")
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=UserWarning, module="ctranslate2")

from caesar.tools.base import Tool, ToolResult
from caesar.logging_setup import get_logger


logger = get_logger("stt")

# Lazy-loaded singleton
_whisper_model = None
_whisper_model_name: str | None = None


def _get_whisper_model(model_name: str, language: str | None = None):
    """Получить singleton faster-whisper модели (lazy load)."""
    global _whisper_model, _whisper_model_name
    
    if _whisper_model is not None and _whisper_model_name == model_name:
        return _whisper_model
    
    logger.info(f"Loading faster-whisper model '{model_name}' (first use, downloads ~150MB)...")
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "faster-whisper не установлен. Установите: pip install faster-whisper"
        ) from e
    
    # compute_type="int8" — оптимизация для CPU (быстро, мало памяти)
    # Если есть GPU — можно "float16" или "int8_float16"
    # На CPU без AVX2 — "int8" единственный вариант
    try:
        _whisper_model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
        )
        _whisper_model_name = model_name
        logger.info(f"Whisper model '{model_name}' loaded (CPU, int8)")
    except Exception as e:
        logger.error(f"Failed to load whisper model '{model_name}': {e}")
        raise
    
    return _whisper_model


def _convert_to_wav(input_path: str, output_path: str) -> bool:
    """Конвертировать любой аудио-формат в wav 16kHz mono (требование whisper).
    
    Использует ffmpeg. Telegram voice — .ogg (Opus), нужно конвертнуть.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i", input_path,
                "-ar", "16000",     # 16 kHz
                "-ac", "1",         # mono
                "-f", "wav",
                "-y",               # overwrite
                output_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.error(f"ffmpeg failed: {result.stderr.decode()[:500]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timeout (30s)")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg not found in PATH")
        return False
    except Exception as e:
        logger.error(f"ffmpeg error: {e}")
        return False


def _transcribe_sync(
    audio_path: str,
    model_name: str,
    language: str | None,
) -> dict[str, Any]:
    """Синхронная транскрипция (вызывается в executor-е)."""
    model = _get_whisper_model(model_name, language)
    
    t_start = time.time()
    
    # language=None → auto-detect
    # beam_size=5 — баланс скорости/качества
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,        # вырезать тишину в начале/конце
        vad_parameters={"min_silence_duration_ms": 500},
    )
    
    # segments — генератор, нужно материализовать
    segments_list = list(segments)
    
    text = " ".join(s.text.strip() for s in segments_list if s.text.strip())
    duration = info.duration if hasattr(info, "duration") else 0.0
    detected_language = info.language if hasattr(info, "language") else (language or "unknown")
    elapsed = time.time() - t_start
    
    return {
        "text": text,
        "language": detected_language,
        "language_probability": float(getattr(info, "language_probability", 0.0)),
        "duration": float(duration),
        "elapsed": float(elapsed),
        "segments_count": len(segments_list),
        "model": model_name,
    }


class TranscribeAudioTool(Tool):
    """Транскрипция аудио в текст через faster-whisper (локально, бесплатно)."""
    
    name = "transcribe_audio"
    description = (
        "Распознать голосовое сообщение (audio файл) в текст. "
        "Поддерживает: ogg, mp3, wav, m4a, flac. "
        "Использует faster-whisper локально (бесплатно, без cloud). "
        "Модель: base (49MB) по умолчанию, отличный русский. "
        "Первый вызов скачивает модель (~150MB)."
    )
    category = "audio"
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Путь к аудио файлу"},
            "language": {
                "type": "string",
                "description": "Код языка (например 'ru', 'en') или null для авто-детекта",
                "default": None,
            },
            "model": {
                "type": "string",
                "enum": ["tiny", "base", "small", "medium", "large"],
                "description": "Модель whisper. По умолчанию 'base'.",
                "default": "base",
            },
        },
        "required": ["file_path"],
    }
    
    # Дефолты из конфига (устанавливаются orchestrator-ом через set_context)
    default_model: str = "base"
    default_language: str | None = None
    
    async def execute(
        self,
        file_path: str,
        language: str | None = None,
        model: str | None = None,
        **_,
    ) -> ToolResult:
        path = Path(file_path)
        if not path.exists():
            return ToolResult(success=False, error=f"File not found: {file_path}")
        if not path.is_file():
            return ToolResult(success=False, error=f"Not a file: {file_path}")
        
        # Размер файла — не больше 25 MB (whisper ограничение)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > 25:
            return ToolResult(
                success=False,
                error=f"File too large: {size_mb:.1f} MB (max 25 MB)",
            )
        
        # Параметры: аргумент > default из конфига
        effective_model = model or self.default_model or "base"
        effective_language = language or self.default_language
        
        # Если уже wav 16kHz mono — не конвертируем
        ext = path.suffix.lower()
        tmp_wav: str | None = None
        
        try:
            if ext == ".wav":
                # Проверим формат — если уже 16kHz mono, оставляем
                audio_to_transcribe = str(path)
            else:
                # Конвертируем в wav
                tmp_fd, tmp_wav = tempfile.mkstemp(suffix=".wav", prefix="caesar_stt_")
                os.close(tmp_fd)
                
                loop = asyncio.get_event_loop()
                converted = await loop.run_in_executor(
                    None,
                    _convert_to_wav,
                    str(path),
                    tmp_wav,
                )
                if not converted:
                    if tmp_wav and os.path.exists(tmp_wav):
                        os.unlink(tmp_wav)
                    return ToolResult(
                        success=False,
                        error="Failed to convert audio to wav (ffmpeg error)",
                    )
                audio_to_transcribe = tmp_wav
            
            # Транскрипция в executor (CPU-bound, не блокирует event loop)
            loop = asyncio.get_event_loop()
            try:
                result = await loop.run_in_executor(
                    None,
                    _transcribe_sync,
                    audio_to_transcribe,
                    effective_model,
                    effective_language,
                )
            except RuntimeError as e:
                return ToolResult(success=False, error=str(e))
            
            text = result["text"].strip()
            if not text:
                return ToolResult(
                    success=True,
                    data={
                        "text": "",
                        "warning": "Распознавание вернуло пустой текст (возможно тишина в аудио)",
                        "language": result["language"],
                        "duration": result["duration"],
                        "elapsed": result["elapsed"],
                        "model": result["model"],
                    },
                )
            
            return ToolResult(
                success=True,
                data={
                    "text": text,
                    "language": result["language"],
                    "language_probability": result["language_probability"],
                    "duration": result["duration"],
                    "elapsed": result["elapsed"],
                    "segments_count": result["segments_count"],
                    "model": result["model"],
                    "audio_file": str(path),
                },
            )
        
        finally:
            # Чистим tmp wav
            if tmp_wav and os.path.exists(tmp_wav):
                try:
                    os.unlink(tmp_wav)
                except Exception:
                    pass


def get_audio_tools() -> list[Tool]:
    return [TranscribeAudioTool()]
