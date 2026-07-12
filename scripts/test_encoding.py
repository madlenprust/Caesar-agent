"""Тест: определение кодировки через chardet."""
import sys
sys.path.insert(0, "/home/z/my-project")

import tempfile
import os
from pathlib import Path

# Тестовые тексты на русском (без em-dash, он ломает некоторые кодировки)
TEXT_RU = "Агрессия - форма поведения, направленная на причинение вреда. Эрик Берн писал про поглаживания."

# Разные кодировки
ENCODINGS = ["utf-8", "cp1251", "koi8-r", "iso-8859-5", "mac_cyrillic", "utf-16", "utf-16-le", "utf-16-be"]

# Импортируем _extract_document_text через mock
from unittest.mock import MagicMock
import asyncio

# Создаём минимальный mock для TelegramAdapter
from caesar.channels.telegram_adapter import TelegramAdapter
from caesar.config import Config

async def main():
    config = Config()
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = config
    adapter.log = MagicMock()
    
    print("=" * 60)
    print("ENCODING DETECTION TEST")
    print("=" * 60)
    
    all_pass = True
    
    for enc in ENCODINGS:
        # Создаём файл в этой кодировке
        tmp = tempfile.mktemp(suffix=".txt")
        try:
            with open(tmp, "wb") as f:
                f.write(TEXT_RU.encode(enc))
            
            # Извлекаем текст
            extracted = await adapter._extract_document_text(tmp, ".txt")
            
            # Сравниваем
            match = extracted.strip() == TEXT_RU.strip()
            status = "✓" if match else "✗"
            if not match:
                all_pass = False
            
            print(f"\n{status} {enc}")
            print(f"   original:  {TEXT_RU[:60]}")
            print(f"   extracted: {extracted[:60]}")
            if not match:
                # Показываем байты
                print(f"   extracted bytes: {extracted[:30].encode('utf-8', errors='replace')}")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    
    # Тест с BOM
    print("\n--- BOM tests ---")
    for bom_name, bom_bytes, enc in [
        ("UTF-8 BOM", b"\xef\xbb\xbf", "utf-8"),
        ("UTF-16 LE BOM", b"\xff\xfe", "utf-16-le"),
        ("UTF-16 BE BOM", b"\xfe\xff", "utf-16-be"),
    ]:
        tmp = tempfile.mktemp(suffix=".txt")
        try:
            with open(tmp, "wb") as f:
                f.write(bom_bytes + TEXT_RU.encode(enc))
            extracted = await adapter._extract_document_text(tmp, ".txt")
            match = extracted.strip() == TEXT_RU.strip()
            status = "✓" if match else "✗"
            if not match:
                all_pass = False
            print(f"  {status} {bom_name}: {extracted[:60]}")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    
    print("\n" + "=" * 60)
    if all_pass:
        print("=== ALL TESTS PASSED ===")
    else:
        print("=== SOME TESTS FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
