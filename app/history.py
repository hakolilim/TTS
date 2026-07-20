"""Local JSON history for TTS sessions."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

HISTORY_PATH = Path(__file__).resolve().parent.parent / "history.json"
MAX_ENTRIES = 100


@dataclass
class HistoryEntry:
    id: str
    full_text: str
    text_preview: str
    voice: str
    rate: str
    pitch: str
    mode: str  # "live" | "export"
    created_at: str
    output_path: Optional[str] = None

    @staticmethod
    def create(
        full_text: str,
        voice: str,
        rate: str,
        pitch: str,
        mode: str,
        output_path: Optional[str] = None,
    ) -> "HistoryEntry":
        preview = full_text.strip().replace("\n", " ")
        if len(preview) > 80:
            preview = preview[:77] + "..."
        return HistoryEntry(
            id=str(uuid.uuid4()),
            full_text=full_text,
            text_preview=preview or "(empty)",
            voice=voice,
            rate=rate,
            pitch=pitch,
            mode=mode,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            output_path=output_path,
        )


class HistoryStore:
    def __init__(self, path: Path = HISTORY_PATH) -> None:
        self.path = path
        self._entries: List[HistoryEntry] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._entries = []
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._entries = [HistoryEntry(**item) for item in raw]
        except (json.JSONDecodeError, OSError, TypeError):
            self._entries = []

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(
                    [asdict(e) for e in self._entries],
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass

    def add(self, entry: HistoryEntry) -> None:
        self._entries.insert(0, entry)
        self._entries = self._entries[:MAX_ENTRIES]
        self.save()

    def remove(self, entry_id: str) -> None:
        self._entries = [e for e in self._entries if e.id != entry_id]
        self.save()

    def clear(self) -> None:
        self._entries = []
        self.save()

    def all(self) -> List[HistoryEntry]:
        return list(self._entries)

    def get(self, entry_id: str) -> Optional[HistoryEntry]:
        for e in self._entries:
            if e.id == entry_id:
                return e
        return None
