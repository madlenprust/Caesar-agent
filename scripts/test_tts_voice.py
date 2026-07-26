"""Тесты H2 — TTS voice replies.

Покрытие:
- synthesize: import-guard (edge-tts не установлен → None).
- synthesize: mock edge-tts + no-ffmpeg → mp3 (audio/mpeg).
- synthesize: mock edge-tts + mock ffmpeg → ogg (audio/ogg).
- _send_voice: mock _api_call → sendVoice с files, файл удалён после.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from caesar.tools.tts import synthesize


# --- synthesize ---

async def test_synthesize_no_edge_tts_returns_none():
    with patch("caesar.tools.tts._get_edge_tts", return_value=None):
        result = await synthesize("Hello world")
    assert result is None


async def test_synthesize_empty_text_returns_none():
    mock_etts = MagicMock()
    with patch("caesar.tools.tts._get_edge_tts", return_value=mock_etts):
        assert await synthesize("") is None
        assert await synthesize("   ") is None


async def test_synthesize_no_ffmpeg_returns_mp3():
    """edge-tts → mp3; нет ffmpeg → (mp3_path, 'audio/mpeg')."""
    mock_etts = MagicMock()
    communicate = MagicMock()
    communicate.save = AsyncMock(side_effect=lambda p: Path(p).write_bytes(b"fake mp3"))
    mock_etts.Communicate = MagicMock(return_value=communicate)

    with patch("caesar.tools.tts._get_edge_tts", return_value=mock_etts), \
         patch("shutil.which", return_value=None):
        result = await synthesize("Hello world test")

    assert result is not None
    path, ct = result
    assert ct == "audio/mpeg"
    assert path.exists()
    assert path.suffix == ".mp3"
    path.unlink(missing_ok=True)


async def test_synthesize_with_ffmpeg_returns_ogg():
    """edge-tts → mp3 → ffmpeg → ogg; есть ffmpeg → (ogg_path, 'audio/ogg')."""
    mock_etts = MagicMock()
    communicate = MagicMock()
    communicate.save = AsyncMock(side_effect=lambda p: Path(p).write_bytes(b"fake mp3"))
    mock_etts.Communicate = MagicMock(return_value=communicate)

    def fake_ffmpeg(*args, **kw):
        # subprocess.run(cmd_list, ...) → args[0] = ["ffmpeg", ..., ogg_path]
        ogg_path = args[0][-1]
        Path(ogg_path).write_bytes(b"fake ogg opus")
        return MagicMock(returncode=0)

    with patch("caesar.tools.tts._get_edge_tts", return_value=mock_etts), \
         patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=fake_ffmpeg):
        result = await synthesize("Hello world test")

    assert result is not None
    path, ct = result
    assert ct == "audio/ogg"
    assert path.exists()
    assert path.suffix == ".ogg"
    path.unlink(missing_ok=True)


# --- _send_voice ---

async def test_send_voice_calls_sendvoice_and_cleans_up():
    """_send_voice: ogg → sendVoice API с files, файл удалён после."""
    from caesar.channels.telegram_adapter import TelegramAdapter

    tmp = Path(tempfile.mkstemp(suffix=".ogg", prefix="test_voice_")[1])
    tmp.write_bytes(b"fake ogg")

    stub = SimpleNamespace(
        _api_call=AsyncMock(return_value={"message_id": 42}),
        log=MagicMock(),
    )
    result = await TelegramAdapter._send_voice(stub, chat_id=123, audio_path=tmp, content_type="audio/ogg")

    assert result == {"message_id": 42}
    stub._api_call.assert_called_once()
    call_args = stub._api_call.call_args
    assert call_args.args[0] == "sendVoice"
    assert call_args.args[1]["chat_id"] == "123"  # data — positional arg[1]
    assert "voice" in call_args.kwargs["files"]
    assert not tmp.exists()  # файл удалён


async def test_send_voice_mp3_uses_sendaudio():
    """mp3 → sendAudio (не sendVoice)."""
    from caesar.channels.telegram_adapter import TelegramAdapter

    tmp = Path(tempfile.mkstemp(suffix=".mp3", prefix="test_audio_")[1])
    tmp.write_bytes(b"fake mp3")

    stub = SimpleNamespace(
        _api_call=AsyncMock(return_value={"message_id": 99}),
        log=MagicMock(),
    )
    result = await TelegramAdapter._send_voice(stub, chat_id=456, audio_path=tmp, content_type="audio/mpeg")

    assert result == {"message_id": 99}
    assert stub._api_call.call_args.args[0] == "sendAudio"
    assert "audio" in stub._api_call.call_args.kwargs["files"]
    assert not tmp.exists()
