"""edge-tts pipeline: synthesize sentence-by-sentence with seek/pause/stop."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional

from app.sentence_splitter import Sentence


class PipelineMode(str, Enum):
    LIVE = "live"
    EXPORT = "export"


class PipelineState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


class TTSPipeline:
    """
    Shared pipeline for live playback and MP3 export.

    For LIVE mode, audio is synthesized per sentence and handed to the player
    via on_sentence_ready. For EXPORT, all sentences are synthesized then merged.
    """

    def __init__(self, temp_dir: Optional[Path] = None) -> None:
        self.temp_dir = Path(temp_dir or Path(tempfile.gettempdir()) / "edge_tts_app")
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self._state = PipelineState.IDLE
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused
        self._stop_flag = False
        self._seek_to: Optional[int] = None

        self.sentences: List[Sentence] = []
        self.current_index: int = 0
        self.voice: str = "en-US-JennyNeural"
        self.rate: str = "+0%"
        self.pitch: str = "+0Hz"
        self.mode: PipelineMode = PipelineMode.LIVE
        self.export_path: Optional[str] = None

        # Callbacks (invoked from worker thread — GUI must marshal to main thread)
        self.on_sentence_start: Optional[Callable[[int, Sentence], None]] = None
        self.on_sentence_ready: Optional[Callable[[int, Sentence, str], None]] = None
        self.on_progress: Optional[Callable[[int, int], None]] = None
        self.on_export_done: Optional[Callable[[str], None]] = None
        self.on_done: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None

        # LIVE: wait until player finishes current sentence
        self._playback_done = threading.Event()
        self._playback_done.set()

    @property
    def state(self) -> PipelineState:
        return self._state

    def _set_state(self, state: PipelineState) -> None:
        self._state = state
        if self.on_state_change:
            try:
                self.on_state_change(state.value)
            except Exception:
                pass

    def start(
        self,
        sentences: List[Sentence],
        voice: str,
        rate: str = "+0%",
        pitch: str = "+0Hz",
        mode: PipelineMode = PipelineMode.LIVE,
        export_path: Optional[str] = None,
        start_index: int = 0,
    ) -> None:
        with self._lock:
            if self._state in (PipelineState.RUNNING, PipelineState.PAUSED):
                self.stop()
                if self._thread and self._thread.is_alive():
                    self._thread.join(timeout=3.0)

            self.sentences = sentences
            self.voice = voice
            self.rate = rate
            self.pitch = pitch
            self.mode = mode
            self.export_path = export_path
            self.current_index = max(0, start_index)
            self._stop_flag = False
            self._seek_to = None
            self._pause_event.set()
            self._playback_done.set()
            self._set_state(PipelineState.RUNNING)

            self._thread = threading.Thread(target=self._run_worker, daemon=True)
            self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self._state == PipelineState.RUNNING:
                self._pause_event.clear()
                self._set_state(PipelineState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._state == PipelineState.PAUSED:
                self._pause_event.set()
                self._set_state(PipelineState.RUNNING)

    def stop(self) -> None:
        with self._lock:
            self._stop_flag = True
            self._pause_event.set()
            self._playback_done.set()
            if self._state in (PipelineState.RUNNING, PipelineState.PAUSED):
                self._set_state(PipelineState.STOPPING)

    def seek_to(self, index: int) -> None:
        """Jump to sentence index (live mode). Takes effect after current synth/play step."""
        with self._lock:
            if not self.sentences:
                return
            index = max(0, min(index, len(self.sentences) - 1))
            self._seek_to = index
            self._playback_done.set()  # unblock wait on current playback
            if self._state == PipelineState.PAUSED:
                self._pause_event.set()
                self._set_state(PipelineState.RUNNING)

    def notify_playback_finished(self) -> None:
        """Called by GUI/player when current sentence audio finished."""
        self._playback_done.set()

    def _run_worker(self) -> None:
        try:
            if self.mode == PipelineMode.EXPORT:
                asyncio.run(self._export_loop())
            else:
                asyncio.run(self._live_loop())
        except Exception as e:
            if self.on_error:
                try:
                    self.on_error(str(e))
                except Exception:
                    pass
        finally:
            self._set_state(PipelineState.IDLE)
            if not self._stop_flag and self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass

    async def _synthesize_to_file(self, text: str, out_path: str) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, pitch=self.pitch
        )
        await communicate.save(out_path)

    def _temp_file(self, suffix: str = ".mp3") -> str:
        name = f"tts_{uuid.uuid4().hex}{suffix}"
        return str(self.temp_dir / name)

    async def _live_loop(self) -> None:
        total = len(self.sentences)
        i = self.current_index

        while i < total:
            if self._stop_flag:
                break

            # Handle seek
            if self._seek_to is not None:
                i = self._seek_to
                self._seek_to = None
                self.current_index = i

            # Pause gate
            while not self._pause_event.is_set():
                if self._stop_flag:
                    break
                await asyncio.sleep(0.05)
            if self._stop_flag:
                break

            # Seek again after pause
            if self._seek_to is not None:
                i = self._seek_to
                self._seek_to = None
                self.current_index = i
                continue

            sentence = self.sentences[i]
            self.current_index = i

            if self.on_sentence_start:
                try:
                    self.on_sentence_start(i, sentence)
                except Exception:
                    pass
            if self.on_progress:
                try:
                    self.on_progress(i + 1, total)
                except Exception:
                    pass

            out_path = self._temp_file()
            try:
                await self._synthesize_to_file(sentence.text, out_path)
            except Exception as e:
                if self.on_error:
                    self.on_error(f"Lỗi chuyển câu {i + 1}: {e}")
                # Skip failed sentence
                i += 1
                continue

            if self._stop_flag:
                self._safe_unlink(out_path)
                break

            if self._seek_to is not None:
                self._safe_unlink(out_path)
                continue

            # Hand off to player and wait
            self._playback_done.clear()
            if self.on_sentence_ready:
                try:
                    self.on_sentence_ready(i, sentence, out_path)
                except Exception as e:
                    if self.on_error:
                        self.on_error(str(e))
                    self._playback_done.set()

            # Wait until playback done, stop, or seek
            while not self._playback_done.is_set():
                if self._stop_flag or self._seek_to is not None:
                    break
                # Respect pause during playback wait (player also pauses)
                await asyncio.sleep(0.05)

            self._safe_unlink(out_path)

            if self._stop_flag:
                break
            if self._seek_to is not None:
                continue

            i += 1
            self.current_index = i

    async def _export_loop(self) -> None:
        total = len(self.sentences)
        paths: List[str] = []

        for i, sentence in enumerate(self.sentences):
            if self._stop_flag:
                break

            while not self._pause_event.is_set():
                if self._stop_flag:
                    break
                await asyncio.sleep(0.05)
            if self._stop_flag:
                break

            self.current_index = i
            if self.on_sentence_start:
                try:
                    self.on_sentence_start(i, sentence)
                except Exception:
                    pass
            if self.on_progress:
                try:
                    self.on_progress(i + 1, total)
                except Exception:
                    pass

            out_path = self._temp_file()
            try:
                await self._synthesize_to_file(sentence.text, out_path)
                paths.append(out_path)
            except Exception as e:
                if self.on_error:
                    self.on_error(f"Lỗi chuyển câu {i + 1}: {e}")
                self._safe_unlink(out_path)

        if self._stop_flag:
            for p in paths:
                self._safe_unlink(p)
            return

        if not paths:
            if self.on_error:
                self.on_error("Không có đoạn audio nào được tạo.")
            return

        export_path = self.export_path or str(
            Path(__file__).resolve().parent.parent / "output" / "export.mp3"
        )
        Path(export_path).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._merge_mp3(paths, export_path)
        except Exception as e:
            # Fallback: simple binary concat (works for many MP3 streams)
            try:
                self._merge_mp3_binary(paths, export_path)
            except Exception as e2:
                if self.on_error:
                    self.on_error(f"Ghép MP3 thất bại: {e} / {e2}")
                return
        finally:
            for p in paths:
                self._safe_unlink(p)

        if self.on_export_done:
            try:
                self.on_export_done(export_path)
            except Exception:
                pass

    @staticmethod
    def _merge_mp3(paths: List[str], export_path: str) -> None:
        from pydub import AudioSegment

        combined = AudioSegment.empty()
        for p in paths:
            combined += AudioSegment.from_file(p)
        combined.export(export_path, format="mp3")

    @staticmethod
    def _merge_mp3_binary(paths: List[str], export_path: str) -> None:
        with open(export_path, "wb") as out:
            for p in paths:
                with open(p, "rb") as f:
                    out.write(f.read())

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def cleanup_temp(self) -> None:
        try:
            for f in self.temp_dir.glob("tts_*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass
