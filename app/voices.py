"""Load and cache edge-tts voices."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CACHE_PATH = Path(__file__).resolve().parent.parent / "voices_cache.json"


async def _fetch_voices() -> List[Dict[str, Any]]:
    import edge_tts

    return await edge_tts.list_voices()


def list_voices(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """Return list of voice dicts from cache or edge-tts API."""
    if not force_refresh and CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except (json.JSONDecodeError, OSError):
            pass

    voices = asyncio.run(_fetch_voices())
    # Normalize useful fields
    normalized = []
    for v in voices:
        normalized.append(
            {
                "Name": v.get("Name", ""),
                "ShortName": v.get("ShortName", ""),
                "Gender": v.get("Gender", ""),
                "Locale": v.get("Locale", ""),
                "FriendlyName": v.get("FriendlyName", v.get("ShortName", "")),
            }
        )
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return normalized


def locales_from_voices(voices: List[Dict[str, Any]]) -> List[str]:
    """Unique sorted locale codes."""
    return sorted({v.get("Locale", "") for v in voices if v.get("Locale")})


def filter_voices(
    voices: List[Dict[str, Any]],
    locale: Optional[str] = None,
    gender: Optional[str] = None,
) -> List[Dict[str, Any]]:
    result = voices
    if locale and locale != "All":
        result = [v for v in result if v.get("Locale") == locale]
    if gender and gender != "All":
        result = [v for v in result if v.get("Gender", "").lower() == gender.lower()]
    return result


def voice_display_name(v: Dict[str, Any]) -> str:
    short = v.get("ShortName", "")
    gender = v.get("Gender", "")
    locale = v.get("Locale", "")
    return f"{short} ({gender}, {locale})"


def default_voice(voices: List[Dict[str, Any]]) -> str:
    """Prefer Vietnamese neural voice, else first available."""
    for preferred in (
        "vi-VN-HoaiMyNeural",
        "vi-VN-NamMinhNeural",
        "en-US-JennyNeural",
        "en-US-AriaNeural",
    ):
        if any(v.get("ShortName") == preferred for v in voices):
            return preferred
    if voices:
        return voices[0].get("ShortName", "en-US-JennyNeural")
    return "en-US-JennyNeural"
