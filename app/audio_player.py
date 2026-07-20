"""Audio playback with pause/resume/stop.

Primary backend: pygame-ce (import as pygame) — plays MP3 from RAM via BytesIO.
Optional fallback: Windows MCI for file paths only (no pure in-memory play).
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from typing import Callable, Optional



class AudioPlayer:
    """Play / pause / resume / stop audio from path or in-memory bytes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playing = False
        self._paused = False
        self._current_path: Optional[str] = None
        self._play_thread: Optional[threading.Thread] = None
        self._on_complete: Optional[Callable[[], None]] = None
        self._backend = self._detect_backend()
        self._alias = "edge_tts_alias"
        self._pygame_ready = False
        # Generation id: incremented on each stop/new play to ignore stale threads
        self._generation = 0
        self._user_stopped = False
        # Keep BytesIO alive while pygame.music holds the stream
        self._source_buf: Optional[io.BytesIO] = None

    def _detect_backend(self) -> str:
        try:
            import pygame  # noqa: F401

            return "pygame"
        except ImportError:
            if sys.platform == "win32":
                return "mci"
            return "none"

    def _ensure_pygame(self) -> None:
        import pygame

        if not self._pygame_ready:
            # edge-tts MP3 is typically 24 kHz mono; pygame resamples as needed
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
            self._pygame_ready = True

    # ── public API ────────────────────────────────────────────────

    def play_bytes(
        self,
        data: bytes,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        """Play MP3 (or other mixer-supported) audio from memory."""
        if not data:
            raise OSError("Audio data is empty (0 bytes)")

        if self._backend == "pygame":
            with self._lock:
                self._stop_internal_locked(fire_complete=False)
                self._generation += 1
                gen = self._generation
                self._user_stopped = False
                self._current_path = None
                self._on_complete = on_complete
                self._playing = True
                self._paused = False
                try:
                    self._pygame_play_bytes(data)
                except Exception:
                    self._playing = False
                    self._on_complete = None
                    self._source_buf = None
                    raise
                self._play_thread = threading.Thread(
                    target=self._pygame_watch_thread,
                    args=(gen,),
                    daemon=True,
                )
                self._play_thread.start()
            return

        if self._backend == "mci":
            # MCI cannot play from RAM — write a short-lived temp and play path.
            import tempfile

            fd, path = tempfile.mkstemp(suffix=".mp3", prefix="tts_play_")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise

            def _done_and_cleanup() -> None:
                try:
                    os.remove(path)
                except OSError:
                    pass
                if on_complete:
                    on_complete()

            try:
                self.play(path, on_complete=_done_and_cleanup)
            except Exception:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise
            return

        raise RuntimeError(
            "Không có backend phát audio. Cài pygame-ce: pip install pygame-ce"
        )

    def play(
        self,
        path: str,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        size = os.path.getsize(path)
        if size <= 0:
            raise OSError(f"Audio file is empty (0 bytes): {path}")

        # Prefer pure RAM path when pygame is available
        if self._backend == "pygame":
            with open(path, "rb") as f:
                data = f.read()
            self.play_bytes(data, on_complete=on_complete)
            return

        with self._lock:
            self._stop_internal_locked(fire_complete=False)

            self._generation += 1
            gen = self._generation
            self._user_stopped = False
            self._current_path = path
            self._on_complete = on_complete
            self._playing = True
            self._paused = False

            if self._backend == "mci":
                self._play_thread = threading.Thread(
                    target=self._mci_play_wait_thread,
                    args=(path, gen),
                    daemon=True,
                )
                self._play_thread.start()
            else:
                self._playing = False
                raise RuntimeError(
                    "Không có backend phát audio. Cài pygame-ce: pip install pygame-ce"
                )

    def pause(self) -> None:
        with self._lock:
            if not self._playing or self._paused:
                return
            if self._backend == "mci":
                self._mci(f"pause {self._alias}")
            elif self._backend == "pygame":
                import pygame

                pygame.mixer.music.pause()
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            if not self._playing or not self._paused:
                return
            if self._backend == "mci":
                err = self._mci(f"resume {self._alias}")
                if err != 0:
                    self._mci(f"play {self._alias}")
            elif self._backend == "pygame":
                import pygame

                pygame.mixer.music.unpause()
            self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._stop_internal_locked(fire_complete=False)

    def _stop_internal_locked(self, fire_complete: bool = False) -> None:
        """Caller must hold self._lock."""
        self._user_stopped = not fire_complete
        self._generation += 1  # invalidate in-flight play threads
        self._on_complete = None if not fire_complete else self._on_complete

        if self._backend == "mci":
            self._mci_close()
        elif self._backend == "pygame" and self._pygame_ready:
            try:
                import pygame

                pygame.mixer.music.stop()
                unload = getattr(pygame.mixer.music, "unload", None)
                if callable(unload):
                    unload()
            except Exception:
                pass

        self._playing = False
        self._paused = False
        self._current_path = None
        self._source_buf = None

    @property
    def is_playing(self) -> bool:
        return self._playing and not self._paused

    @property
    def is_paused(self) -> bool:
        return self._playing and self._paused

    @property
    def is_busy(self) -> bool:
        return self._playing

    def shutdown(self) -> None:
        self.stop()
        if self._pygame_ready:
            try:
                import pygame

                pygame.mixer.quit()
            except Exception:
                pass
            self._pygame_ready = False

    # ── MCI (Windows fallback) ────────────────────────────────────

    @staticmethod
    def _mci(command: str) -> int:
        import ctypes

        return int(ctypes.windll.winmm.mciSendStringW(command, None, 0, None))

    def _mci_close(self) -> None:
        try:
            self._mci(f"stop {self._alias}")
        except Exception:
            pass
        try:
            self._mci(f"close {self._alias}")
        except Exception:
            pass

    def _mci_open(self, path: str) -> None:
        path = os.path.abspath(path).replace("/", "\\")
        self._mci_close()
        err = self._mci(f'open "{path}" type mpegvideo alias {self._alias}')
        if err != 0:
            err = self._mci(f'open "{path}" alias {self._alias}')
        if err != 0:
            raise RuntimeError(f"MCI open failed (code {err}): {path}")

    def _mci_play_wait_thread(self, path: str, gen: int) -> None:
        cb: Optional[Callable[[], None]] = None
        try:
            with self._lock:
                if gen != self._generation:
                    return
                try:
                    self._mci_open(path)
                except Exception:
                    if gen == self._generation and self._playing:
                        self._playing = False
                        cb = self._on_complete
                        self._on_complete = None
                    if cb:
                        try:
                            cb()
                        except Exception:
                            pass
                    return

            self._mci(f"play {self._alias} wait")

            with self._lock:
                if gen != self._generation:
                    return
                if self._playing and not self._user_stopped:
                    self._playing = False
                    self._paused = False
                    cb = self._on_complete
                    self._on_complete = None
                try:
                    self._mci_close()
                except Exception:
                    pass
        except Exception:
            with self._lock:
                if gen == self._generation and self._playing:
                    self._playing = False
                    cb = self._on_complete
                    self._on_complete = None
                try:
                    self._mci_close()
                except Exception:
                    pass

        if cb:
            try:
                cb()
            except Exception:
                pass

    # ── pygame-ce ─────────────────────────────────────────────────

    def _pygame_play_bytes(self, data: bytes) -> None:
        """Caller must hold self._lock (or be sole starter)."""
        import pygame

        self._ensure_pygame()
        buf = io.BytesIO(data)
        # Keep reference so the stream is not GC'd while mixer reads it
        self._source_buf = buf
        pygame.mixer.music.load(buf)
        pygame.mixer.music.play()

    def _pygame_watch_thread(self, gen: int) -> None:
        import pygame

        # Grace period so get_busy() is true after play()
        time.sleep(0.12)
        while True:
            with self._lock:
                if gen != self._generation or not self._playing:
                    return
                if self._paused:
                    time.sleep(0.05)
                    continue
                busy = bool(pygame.mixer.music.get_busy())
            if not busy:
                time.sleep(0.08)
                with self._lock:
                    if gen != self._generation or not self._playing:
                        return
                    if self._paused:
                        continue
                    if pygame.mixer.music.get_busy():
                        continue
                    cb = self._on_complete
                    self._on_complete = None
                    self._playing = False
                    self._paused = False
                    self._source_buf = None
                    try:
                        unload = getattr(pygame.mixer.music, "unload", None)
                        if callable(unload):
                            unload()
                    except Exception:
                        pass
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                return
            time.sleep(0.05)
