"""Audio playback with pause/resume/stop.

Primary backend on Windows: built-in winmm MCI (no extra packages).
Uses blocking "play ... wait" so completion is accurate (no false early end).
Optional fallback: pygame if installed (non-Windows).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Callable, Optional


class AudioPlayer:
    """Play / pause / resume / stop audio files."""

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

    def _detect_backend(self) -> str:
        if sys.platform == "win32":
            return "mci"
        try:
            import pygame  # noqa: F401

            return "pygame"
        except ImportError:
            return "mci" if sys.platform == "win32" else "none"

    # ── public API ────────────────────────────────────────────────

    def play(
        self,
        path: str,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)

        with self._lock:
            # Stop previous playback without treating as natural complete
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
            elif self._backend == "pygame":
                self._pygame_play(path)
                self._play_thread = threading.Thread(
                    target=self._pygame_watch_thread,
                    args=(gen,),
                    daemon=True,
                )
                self._play_thread.start()
            else:
                self._playing = False
                raise RuntimeError(
                    "Không có backend phát audio. Trên Windows dùng MCI; "
                    "trên hệ khác hãy cài pygame."
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
                # After pause, resume continues; if wait thread is blocked on play wait, it stays blocked.
                err = self._mci(f"resume {self._alias}")
                if err != 0:
                    # Some devices need play again without re-open
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
            # stop/close unblocks any "play ... wait"
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

    # ── MCI (Windows) ─────────────────────────────────────────────

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
        # mpegvideo is the reliable type for MP3 on modern Windows
        err = self._mci(f'open "{path}" type mpegvideo alias {self._alias}')
        if err != 0:
            err = self._mci(f'open "{path}" alias {self._alias}')
        if err != 0:
            raise RuntimeError(f"MCI open failed (code {err}): {path}")

    def _mci_play_wait_thread(self, path: str, gen: int) -> None:
        """
        Open + blocking play wait. Returns only when audio finishes or device is closed/stopped.
        """
        cb: Optional[Callable[[], None]] = None
        try:
            with self._lock:
                if gen != self._generation:
                    return
                try:
                    self._mci_open(path)
                except Exception:
                    # Failed to open — complete immediately so pipeline can continue
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

            # Blocking call — do NOT hold lock
            # "wait" makes mciSendString return only after playback ends or stop/close.
            err = self._mci(f"play {self._alias} wait")

            with self._lock:
                # Stale generation → this play was superseded (stop / new play)
                if gen != self._generation:
                    return
                # Natural finish (or error after start). Notify pipeline only if still current.
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

    # ── pygame fallback ───────────────────────────────────────────

    def _pygame_play(self, path: str) -> None:
        import pygame

        if not self._pygame_ready:
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
            self._pygame_ready = True
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()

    def _pygame_watch_thread(self, gen: int) -> None:
        import pygame

        # Small grace period so get_busy() is true after play()
        time.sleep(0.15)
        while True:
            with self._lock:
                if gen != self._generation or not self._playing:
                    return
                if self._paused:
                    time.sleep(0.05)
                    continue
                busy = bool(pygame.mixer.music.get_busy())
            if not busy:
                # Confirm still stopped (avoid transient false negatives)
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
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                return
            time.sleep(0.05)
