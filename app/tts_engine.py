"""edge-tts pipeline: batched sentences with prefetch for smooth live playback."""

from __future__ import annotations

import asyncio
import os
import random
import tempfile
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from app.sentence_splitter import Sentence

# Reject empty / truncated edge-tts responses (common cause of stuck live playback).
MIN_AUDIO_BYTES = 128
MAX_SYNTH_RETRIES = 5
RETRY_BASE_DELAY_S = 0.6
# Per-attempt network/stream hang protection — without this, retry never runs.
SYNTH_TIMEOUT_S = 45.0
# Absolute ceiling while waiting for player on_complete (avoids infinite hang).
PLAYBACK_WAIT_MIN_S = 20.0
PLAYBACK_WAIT_SLACK_S = 15.0
# Rough speech MP3 bitrate for wait estimate (bits/s).
PLAYBACK_EST_BITRATE = 24_000


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

    LIVE mode prefetches upcoming *batches* (default 3 sentences per edge-tts
    request) while the current batch is playing, so transitions are nearly seamless.
    Live audio is held entirely in RAM and handed to the player as bytes (pygame-ce).
    """

    def __init__(
        self,
        temp_dir: Optional[Path] = None,
        prefetch_ahead: int = 2,
        sentences_per_request: int = 3,
    ) -> None:
        self.temp_dir = Path(temp_dir or Path(tempfile.gettempdir()) / "edge_tts_app")
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.prefetch_ahead = max(1, prefetch_ahead)
        self.sentences_per_request = max(1, sentences_per_request)

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
        # Live: start/end are sentence indices [start, end) for the current batch.
        # on_sentence_ready receives MP3 bytes (RAM) for pygame-ce playback.
        self.on_sentence_start: Optional[Callable[[int, int], None]] = None
        self.on_sentence_ready: Optional[Callable[[int, int, bytes], None]] = None

        self.on_progress: Optional[Callable[[int, int], None]] = None
        self.on_export_done: Optional[Callable[[str], None]] = None
        self.on_done: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_state_change: Optional[Callable[[str], None]] = None

        # LIVE: wait until player finishes current batch audio
        self._playback_done = threading.Event()
        self._playback_done.set()

        # Adaptive prefetch: shrink when edge-tts rate-limits / fails often
        self._consecutive_failures = 0

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

    def _emit_status(self, msg: str) -> None:
        if self.on_status:
            try:
                self.on_status(msg)
            except Exception:
                pass

    def _emit_error(self, msg: str) -> None:
        if self.on_error:
            try:
                self.on_error(msg)
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
            self._consecutive_failures = 0
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
        """Jump to sentence index (live mode). Playback continues from that sentence in batches."""
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
        """Called by GUI/player when current batch audio finished."""
        self._playback_done.set()

    def _run_worker(self) -> None:
        try:
            if self.mode == PipelineMode.EXPORT:
                asyncio.run(self._export_loop())
            else:
                asyncio.run(self._live_loop())
        except Exception as e:
            self._emit_error(str(e))
        finally:
            self._set_state(PipelineState.IDLE)
            if not self._stop_flag and self.on_done:
                try:
                    self.on_done()
                except Exception:
                    pass

    async def _synthesize_to_bytes(self, text: str) -> bytes:
        """Stream edge-tts audio into memory (no disk I/O)."""
        import edge_tts

        communicate = edge_tts.Communicate(
            text, self.voice, rate=self.rate, pitch=self.pitch
        )
        buf = bytearray()
        async for chunk in communicate.stream():
            if self._stop_flag:
                raise asyncio.CancelledError()
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    async def _synthesize_to_bytes_with_retry(self, text: str, label: str) -> bytes:
        """Synthesize with retries when hang/timeout, empty audio, or network fails."""
        last_err: Optional[BaseException] = None
        for attempt in range(1, MAX_SYNTH_RETRIES + 1):
            if self._stop_flag:
                raise asyncio.CancelledError()
            try:
                if attempt > 1:
                    self._emit_status(
                        f"Đang thử lại {label} (lần {attempt}/{MAX_SYNTH_RETRIES})..."
                    )
                data = await asyncio.wait_for(
                    self._synthesize_to_bytes(text),
                    timeout=SYNTH_TIMEOUT_S,
                )
                if len(data) < MIN_AUDIO_BYTES:
                    raise RuntimeError(
                        f"audio rỗng/quá nhỏ ({len(data)} bytes, min {MIN_AUDIO_BYTES})"
                    )
                self._consecutive_failures = 0
                return data
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as e:
                last_err = TimeoutError(
                    f"timeout sau {SYNTH_TIMEOUT_S:.0f}s (mạng/edge-tts treo)"
                )
                # Treat as retriable failure
                _ = e
            except Exception as e:
                last_err = e

            if attempt < MAX_SYNTH_RETRIES and not self._stop_flag:
                # Exponential backoff + small jitter (helps rate-limit recovery)
                delay = RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                delay = min(delay, 8.0) + random.uniform(0.0, 0.35)
                self._emit_status(
                    f"Lỗi {label}: {last_err}. Chờ {delay:.1f}s rồi thử lại..."
                )
                await asyncio.sleep(delay)

        self._consecutive_failures += 1
        raise RuntimeError(
            f"{label}: thất bại sau {MAX_SYNTH_RETRIES} lần thử — {last_err}"
        )

    async def _synthesize_to_file(
        self, text: str, out_path: str, label: str = "export"
    ) -> None:
        """Write MP3 to disk (export path). Retries empty / failed synth."""
        data = await self._synthesize_to_bytes_with_retry(text, label)
        # Atomic-ish write: temp then replace to avoid leaving 0-byte targets mid-fail
        tmp_path = out_path + ".part"
        try:
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, out_path)
        finally:
            self._safe_unlink(tmp_path)
        if not os.path.isfile(out_path) or os.path.getsize(out_path) < MIN_AUDIO_BYTES:
            self._safe_unlink(out_path)
            raise RuntimeError(f"Ghi file thất bại hoặc file rỗng: {out_path}")

    def _temp_file(self, suffix: str = ".mp3") -> str:
        name = f"tts_{uuid.uuid4().hex}{suffix}"
        return str(self.temp_dir / name)

    # ── Batch helpers ─────────────────────────────────────────────

    def _batch_end(self, start: int, total: Optional[int] = None) -> int:
        """Exclusive end index for the batch beginning at *start*."""
        if total is None:
            total = len(self.sentences)
        return min(start + self.sentences_per_request, total)

    def _batch_text(self, start: int, end: int) -> str:
        parts = [self.sentences[i].text.strip() for i in range(start, end)]
        return " ".join(p for p in parts if p)

    def _batch_label(self, start: int, end: int) -> str:
        if end - start <= 1:
            return f"câu {start + 1}"
        return f"câu {start + 1}–{end}"

    def _effective_prefetch_ahead(self) -> int:
        """Reduce parallel requests after repeated failures (rate-limit friendly)."""
        if self._consecutive_failures >= 3:
            return 1
        if self._consecutive_failures >= 1:
            return max(1, min(self.prefetch_ahead, 1))
        return self.prefetch_ahead

    # ── Prefetch buffer helpers (LIVE: cache holds MP3 bytes in RAM) ──

    def _clear_cache(
        self,
        cache: Dict[int, bytes],
        keep: Optional[Set[int]] = None,
    ) -> None:
        keep = keep or set()
        for idx in list(cache.keys()):
            if idx not in keep:
                cache.pop(idx, None)

    async def _synthesize_batch_or_fallback(self, start: int, end: int, label: str) -> bytes:
        """
        Try whole-batch synth first. On failure (and batch has >1 sentence),
        fall back to per-sentence synth and binary-concatenate MP3 frames.
        """
        text = self._batch_text(start, end)
        if not text:
            raise RuntimeError(f"{label}: không có nội dung")

        try:
            return await self._synthesize_to_bytes_with_retry(text, label)
        except asyncio.CancelledError:
            raise
        except Exception as batch_err:
            if end - start <= 1:
                raise

            self._emit_status(
                f"Batch {label} lỗi ({batch_err}). Đang fallback từng câu..."
            )
            parts: List[bytes] = []
            failed: List[str] = []
            for idx in range(start, end):
                if self._stop_flag:
                    raise asyncio.CancelledError()
                piece = self.sentences[idx].text.strip()
                if not piece:
                    continue
                sent_label = f"câu {idx + 1}"
                try:
                    data = await self._synthesize_to_bytes_with_retry(piece, sent_label)
                    parts.append(data)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    failed.append(f"{sent_label}: {e}")
                    self._emit_error(f"Bỏ qua {sent_label}: {e}")

            if not parts:
                raise RuntimeError(
                    f"{label}: fallback từng câu cũng thất bại — "
                    + ("; ".join(failed) if failed else str(batch_err))
                )

            # Same-encoder MPEG frames usually play fine when concatenated.
            combined = b"".join(parts)
            if len(combined) < MIN_AUDIO_BYTES:
                raise RuntimeError(f"{label}: audio fallback quá nhỏ")
            if failed:
                self._emit_status(
                    f"{label}: đã ghép {len(parts)} câu, bỏ qua {len(failed)} câu lỗi"
                )
            return combined

    async def _ensure_cached(
        self,
        start: int,
        end: int,
        cache: Dict[int, bytes],
        inflight: Dict[int, asyncio.Task],
    ) -> Optional[bytes]:
        """Return MP3 bytes for batch [start, end), synthesizing if needed."""
        if start in cache:
            data = cache[start]
            if len(data) >= MIN_AUDIO_BYTES:
                return data
            cache.pop(start, None)

        existing = inflight.get(start)
        if existing is not None:
            try:
                data = await existing
                if data and len(data) >= MIN_AUDIO_BYTES:
                    return data
            except Exception:
                pass
            # Prefetch failed or returned bad audio — clear only if still same task.
            if inflight.get(start) is existing:
                inflight.pop(start, None)
            # Fall through and re-synth.

        text = self._batch_text(start, end)
        if not text:
            return None
        label = self._batch_label(start, end)

        async def _job() -> bytes:
            data = await self._synthesize_batch_or_fallback(start, end, label)
            cache[start] = data
            return data

        task = asyncio.create_task(_job())
        inflight[start] = task
        try:
            data = await task
            return data
        except Exception as e:
            cache.pop(start, None)
            self._emit_error(f"Lỗi chuyển {label}: {e}")
            return None
        finally:
            if inflight.get(start) is task:
                inflight.pop(start, None)

    def _schedule_prefetch(
        self,
        from_index: int,
        total: int,
        cache: Dict[int, bytes],
        inflight: Dict[int, asyncio.Task],
    ) -> None:
        """Kick off background synth for the next N batches (non-blocking, RAM cache)."""
        batch_size = self.sentences_per_request
        ahead = self._effective_prefetch_ahead()
        for b in range(ahead):
            start = from_index + b * batch_size
            if start >= total:
                break
            if start in cache or start in inflight:
                continue
            end = self._batch_end(start, total)
            text = self._batch_text(start, end)
            if not text:
                continue
            label = self._batch_label(start, end)

            async def _job(
                idx: int = start,
                batch_end: int = end,
                lbl: str = label,
            ) -> bytes:
                data = await self._synthesize_batch_or_fallback(idx, batch_end, lbl)
                cache[idx] = data
                return data

            task = asyncio.create_task(_job())
            inflight[start] = task

            def _done(
                t: asyncio.Task,
                idx: int = start,
                lbl: str = label,
            ) -> None:
                # Only drop if this callback still owns the slot (avoid racing re-synth).
                if inflight.get(idx) is t:
                    inflight.pop(idx, None)
                try:
                    t.result()
                except asyncio.CancelledError:
                    cache.pop(idx, None)
                except Exception as e:
                    cache.pop(idx, None)
                    self._emit_error(f"Lỗi chuyển {lbl}: {e}")

            task.add_done_callback(_done)

    async def _cancel_inflight(self, inflight: Dict[int, asyncio.Task]) -> None:
        tasks = list(inflight.values())
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        inflight.clear()

    def _playback_wait_timeout_s(self, data: bytes) -> float:
        """Upper bound for waiting on player on_complete for this audio blob."""
        # duration ≈ size*8 / bitrate; add slack for decode/start latency
        est = (len(data) * 8.0) / float(PLAYBACK_EST_BITRATE)
        return max(PLAYBACK_WAIT_MIN_S, est + PLAYBACK_WAIT_SLACK_S)

    async def _wait_playback(self, data: bytes) -> bool:
        """
        Wait until playback done, stop, seek, or timeout.
        Returns True if finished normally (or timed out after force-continue).
        Returns False if stop/seek interrupted.
        """
        timeout_s = self._playback_wait_timeout_s(data)
        deadline = time.monotonic() + timeout_s
        while not self._playback_done.is_set():
            if self._stop_flag or self._seek_to is not None:
                return False
            if time.monotonic() >= deadline:
                self._emit_error(
                    f"Phát audio quá lâu (>{timeout_s:.0f}s) — bỏ qua đoạn hiện tại"
                )
                self._playback_done.set()
                return True
            # Keep event loop free so prefetch tasks progress
            await asyncio.sleep(0.03)
        return True

    async def _live_loop(self) -> None:
        """
        Producer-consumer live loop:
        - Prefetch next batches into RAM while current one plays
        - Each batch (default 3 sentences) = one edge-tts request
        - Hand MP3 bytes to player (pygame-ce) — no disk I/O for live audio
        - On batch failure: fallback per-sentence; never hang forever on synth/play
        """
        total = len(self.sentences)
        i = self.current_index
        cache: Dict[int, bytes] = {}
        inflight: Dict[int, asyncio.Task] = {}
        batch_size = self.sentences_per_request
        skipped_batches = 0

        try:
            # Warm up: start synth for first batch + lookahead immediately
            self._schedule_prefetch(i, total, cache, inflight)

            while i < total:
                if self._stop_flag:
                    break

                # Seek: drop buffer, jump to selected sentence (batches from there)
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

                end = self._batch_end(i, total)
                self.current_index = i

                if self.on_sentence_start:
                    try:
                        self.on_sentence_start(i, end)
                    except Exception:
                        pass
                if self.on_progress:
                    try:
                        self.on_progress(end, total)
                    except Exception:
                        pass

                # Ensure this batch audio is ready in RAM (with retry + fallback)
                data = await self._ensure_cached(i, end, cache, inflight)
                if not data:
                    skipped_batches += 1
                    self._emit_status(
                        f"Bỏ qua {self._batch_label(i, end)} sau khi retry/fallback thất bại"
                    )
                    i = end
                    self._schedule_prefetch(i, total, cache, inflight)
                    continue

                if self._stop_flag:
                    break
                if self._seek_to is not None:
                    continue

                # Prefetch further batches while we play this one
                self._schedule_prefetch(i + batch_size, total, cache, inflight)

                # Hand bytes to player (GUI → pygame-ce BytesIO)
                self._playback_done.clear()
                if self.on_sentence_ready:
                    try:
                        self.on_sentence_ready(i, end, data)
                    except Exception as e:
                        self._emit_error(str(e))
                        self._playback_done.set()

                # Wait until playback done, stop, seek, or timeout
                ok = await self._wait_playback(data)

                cache.pop(i, None)

                if self._stop_flag:
                    break
                if self._seek_to is not None:
                    continue
                if not ok:
                    # Interrupted by stop/seek path above; loop handles flags
                    continue

                i = end
                self.current_index = i
                # Drop cache entries far behind (keep only upcoming batch starts)
                keep: Set[int] = set()
                ahead = self._effective_prefetch_ahead()
                for b in range(ahead + 1):
                    s = i + b * batch_size
                    if s < total:
                        keep.add(s)
                self._clear_cache(cache, keep=keep)
                self._schedule_prefetch(i, total, cache, inflight)

            if skipped_batches and not self._stop_flag:
                self._emit_status(
                    f"Hoàn tất với {skipped_batches} batch bị bỏ qua do lỗi mạng/TTS"
                )
        finally:
            await self._cancel_inflight(inflight)
            self._clear_cache(cache)

    async def _export_loop(self) -> None:
        total = len(self.sentences)
        if total == 0:
            self._emit_error("Không có đoạn audio nào được tạo.")
            return

        batch_size = self.sentences_per_request
        # Batch starts: 0, 3, 6, ...
        batch_starts = list(range(0, total, batch_size))
        # Parallel export with limited concurrency for speed
        sem = asyncio.Semaphore(3)
        results: Dict[int, str] = {}
        failures: List[str] = []

        async def synth_batch(start: int) -> None:
            async with sem:
                if self._stop_flag:
                    return
                end = self._batch_end(start, total)
                text = self._batch_text(start, end)
                if not text:
                    return
                out_path = self._temp_file()
                label = self._batch_label(start, end)
                try:
                    # Prefer whole-batch write; on fail fall back sentence-by-sentence
                    try:
                        await self._synthesize_to_file(text, out_path, label=label)
                    except Exception:
                        if end - start <= 1:
                            raise
                        self._emit_status(
                            f"Export {label} lỗi batch — fallback từng câu..."
                        )
                        # Build combined file from per-sentence synth
                        parts: List[bytes] = []
                        for idx in range(start, end):
                            if self._stop_flag:
                                return
                            piece = self.sentences[idx].text.strip()
                            if not piece:
                                continue
                            sent_label = f"câu {idx + 1}"
                            try:
                                data = await self._synthesize_to_bytes_with_retry(
                                    piece, sent_label
                                )
                                parts.append(data)
                            except Exception as e:
                                failures.append(f"{sent_label}: {e}")
                                self._emit_error(f"Bỏ qua {sent_label}: {e}")
                        if not parts:
                            raise RuntimeError(f"{label}: không tạo được audio")
                        combined = b"".join(parts)
                        tmp_path = out_path + ".part"
                        try:
                            with open(tmp_path, "wb") as f:
                                f.write(combined)
                            os.replace(tmp_path, out_path)
                        finally:
                            self._safe_unlink(tmp_path)
                    results[start] = out_path
                except Exception as e:
                    self._safe_unlink(out_path)
                    failures.append(f"{label}: {e}")
                    self._emit_error(f"Lỗi chuyển {label}: {e}")

        tasks = []
        for start in batch_starts:
            if self._stop_flag:
                break
            while not self._pause_event.is_set():
                if self._stop_flag:
                    break
                await asyncio.sleep(0.05)
            if self._stop_flag:
                break

            end = self._batch_end(start, total)
            self.current_index = start
            if self.on_sentence_start:
                try:
                    self.on_sentence_start(start, end)
                except Exception:
                    pass
            if self.on_progress:
                try:
                    self.on_progress(end, total)
                except Exception:
                    pass
            tasks.append(asyncio.create_task(synth_batch(start)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._stop_flag:
            for p in results.values():
                self._safe_unlink(p)
            return

        # Ordered paths by batch start
        paths: List[str] = []
        for start in batch_starts:
            if start in results:
                paths.append(results[start])

        if not paths:
            self._emit_error("Không có đoạn audio nào được tạo.")
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
                self._emit_error(f"Ghép MP3 thất bại: {e} / {e2}")
                return
        finally:
            for p in paths:
                self._safe_unlink(p)

        if failures:
            self._emit_status(
                f"Xuất xong nhưng thiếu {len(failures)} đoạn do lỗi: "
                + "; ".join(failures[:3])
                + ("..." if len(failures) > 3 else "")
            )

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
            for pattern in ("tts_*", "play_*", "*.part"):
                for f in self.temp_dir.glob(pattern):
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass
