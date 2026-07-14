"""Точка входа для `python -m caesar.watchdog`.

Без этого файла `python -m caesar.watchdog` падает («No module named
caesar.watchdog.__main__») → systemd crash-loop → watchdog НИКОГДА не работал.
"""
import asyncio
import sys

from caesar.watchdog import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
