"""edge-tts pipeline: sentence-by-sentence with prefetch for smooth live playback."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

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

    LIVE mode prefetches upcoming sentences (each still one edge-tts request)
    while the current sentence is playing, so transitions are nearly seamless.
    """

    def __init__(
        self,
        temp_dir: Optional[Path] = None,
        prefetch_ahead: int = 2,
    ) -> None:
        self.temp_dir = Path(temp_dir or Path(tempfile.gettempdir()) / "edge_tts_app")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.prefetch_ahead = max(1, prefetch_ahead)

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

        # Callbacks (worker thread — GUI must marshal to main thread)
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
                    self._thread.join(timeout=5.0)

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
        """Jump to sentence index (live mode)."""
        with self._lock:
            if not self.sentences:
                return
            index = max(0, min(index, len(self.sentences) - 1))
            self._seek_to = index
            self._playback_done.set()
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

    # ── Prefetch buffer helpers ───────────────────────────────────

    def _clear_cache(
        self,
        cache: Dict[int, str],
        keep: Optional[Set[int]] = None,
    ) -> None:
        keep = keep or set()
        for idx in list(cache.keys()):
            if idx not in keep:
                self._safe_unlink(cache.pop(idx, None) or "")

    async def _ensure_cached(
        self,
        index: int,
        cache: Dict[int, str],
        inflight: Dict[int, asyncio.Task],
    ) -> Optional[str]:
        """Return audio path for sentence index, synthesizing if needed."""
        if index in cache:
            return cache[index]

        if index in inflight:
            try:
                path = await inflight[index]
                return path
            except Exception:
                return None

        sentence = self.sentences[index]
        out_path = self._temp_file()

        async def _job() -> str:
            await self._synthesize_to_file(sentence.text, out_path)
            cache[index] = out_path
            return out_path

        task = asyncio.create_task(_job())
        inflight[index] = task
        try:
            path = await task
            return path
        except Exception as e:
            self._safe_unlink(out_path)
            if self.on_error:
                try:
                    self.on_error(f"Lỗi chuyển câu {index + 1}: {e}")
                except Exception:
                    pass
            return None
        finally:
            inflight.pop(index, None)

    def _schedule_prefetch(
        self,
        from_index: int,
        total: int,
        cache: Dict[int, str],
        inflight: Dict[int, asyncio.Task],
    ) -> None:
        """Kick off background synth for the next N sentences (non-blocking)."""
        for j in range(from_index, min(from_index + self.prefetch_ahead, total)):
            if j in cache or j in inflight:
                continue
            sentence = self.sentences[j]
            out_path = self._temp_file()

            async def _job(idx: int = j, path: str = out_path, text: str = sentence.text) -> str:
                await self._synthesize_to_file(text, path)
                cache[idx] = path
                return path

            task = asyncio.create_task(_job())
            inflight[j] = task

            def _done(t: asyncio.Task, idx: int = j, path: str = out_path) -> None:
                inflight.pop(idx, None)
                try:
                    t.result()
                except Exception as e:
                    self._safe_unlink(path)
                    cache.pop(idx, None)
                    if self.on_error:
                        try:
                            self.on_error(f"Lỗi chuyển câu {idx + 1}: {e}")
                        except Exception:
                            pass

            task.add_done_callback(_done)

    async def _cancel_inflight(self, inflight: Dict[int, asyncio.Task]) -> None:
        tasks = list(inflight.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        inflight.clear()

    async def _live_loop(self) -> None:
        """
        Producer-consumer live loop:
        - Prefetch next sentences while current one plays
        - Each sentence = one edge-tts request
        """
        total = len(self.sentences)
        i = self.current_index
        cache: Dict[int, str] = {}
        inflight: Dict[int, asyncio.Task] = {}

        try:
            # Warm up: start synth for first sentence + lookahead immediately
            self._schedule_prefetch(i, total, cache, inflight)

            while i < total:
                if self._stop_flag:
                    break

                # Seek: drop buffer, jump
                if self._seek_to is not None:
                    target = self._seek_to
                    self._seek_to = None
                    await self._cancel_inflight(inflight)
                    self._clear_cache(cache)
                    i = target
                    self.current_index = i
                    self._schedule_prefetch(i, total, cache, inflight)
                    continue

                # Pause gate (prefetch keeps running in background via tasks)
                while not self._pause_event.is_set():
                    if self._stop_flag or self._seek_to is not None:
                        break
                    await asyncio.sleep(0.05)
                if self._stop_flag:
                    break
                if self._seek_to is not None:
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

                # Ensure this sentence audio is ready (await if still synthesizing)
                out_path = await self._ensure_cached(i, cache, inflight)
                if not out_path:
                    i += 1
                    self._schedule_prefetch(i, total, cache, inflight)
                    continue

                if self._stop_flag:
                    break
                if self._seek_to is not None:
                    continue

                # Prefetch further while we play this one
                self._schedule_prefetch(i + 1, total, cache, inflight)

                # Hand off to player
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
                    # Keep event loop free so prefetch tasks progress
                    await asyncio.sleep(0.03)

                # Done with this file (player should have closed handle)
                path = cache.pop(i, None)
                if path:
                    # Small delay helps Windows release MCI file locks
                    await asyncio.sleep(0.02)
                    self._safe_unlink(path)

                if self._stop_flag:
                    break
                if self._seek_to is not None:
                    continue

                i += 1
                self.current_index = i
                # Drop cache entries far behind (keep only upcoming)
                keep = set(range(i, min(i + self.prefetch_ahead + 1, total)))
                self._clear_cache(cache, keep=keep)
                self._schedule_prefetch(i, total, cache, inflight)
        finally:
            await self._cancel_inflight(inflight)
            self._clear_cache(cache)

    async def _export_loop(self) -> None:
        total = len(self.sentences)
        paths: List[str] = []
        # Parallel export with limited concurrency for speed
        sem = asyncio.Semaphore(3)
        results: Dict[int, str] = {}

        async def synth_one(idx: int, sentence: Sentence) -> None:
            async with sem:
                if self._stop_flag:
                    return
                out_path = self._temp_file()
                try:
                    await self._synthesize_to_file(sentence.text, out_path)
                    results[idx] = out_path
                except Exception as e:
                    self._safe_unlink(out_path)
                    if self.on_error:
                        self.on_error(f"Lỗi chuyển câu {idx + 1}: {e}")

        # Progress UI: sequential start notifications + gather
        tasks = []
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
            tasks.append(asyncio.create_task(synth_one(i, sentence)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._stop_flag:
            for p in results.values():
                self._safe_unlink(p)
            return

        # Ordered paths
        for i in range(total):
            if i in results:
                paths.append(results[i])

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
            for f in self.temp_dir.glob("play_*"):
                try:
                    f.unlink()
                except OSError:
                    pass
        except OSError:
            pass
