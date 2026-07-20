"""Resolve app directories for development and frozen (PyInstaller) builds."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """
    Writable directory next to the executable (frozen) or project root (dev).
    Used for history.json, voices_cache.json, output/.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """
    Directory for bundled read-only resources (e.g. app.ico).
    PyInstaller one-file extracts to sys._MEIPASS.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS"))
    return app_dir()


def resource_path(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)


def app_path(*parts: str) -> Path:
    return app_dir().joinpath(*parts)
