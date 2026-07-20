"""Audio playback with pause/resume/stop.

Primary backend on Windows: built-in winmm MCI (no extra packages).
Optional fallback: pygame if installed.
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
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_watch = threading.Event()
        self._on_complete: Optional[Callable[[], None]] = None
        self._backend = self._detect_backend()
        self._alias = "edge_tts_alias"
        self._pygame_ready = False

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
            self._stop_internal_locked()
            self._current_path = path
            self._on_complete = on_complete
            self._playing = True
            self._paused = False
            self._stop_watch.clear()

            if self._backend == "mci":
                self._mci_open_and_play(path)
            elif self._backend == "pygame":
                self._pygame_play(path)
            else:
                self._playing = False
                raise RuntimeError(
                    "Không có backend phát audio. Trên Windows dùng MCI; "
                    "trên hệ khác hãy cài pygame."
                )

            self._watch_thread = threading.Thread(
                target=self._watch_end, daemon=True
            )
            self._watch_thread.start()

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
                self._mci(f"resume {self._alias}")
            elif self._backend == "pygame":
                import pygame

                pygame.mixer.music.unpause()
            self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._stop_internal_locked()

    def _stop_internal_locked(self) -> None:
        self._stop_watch.set()
        self._on_complete = None
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

        return ctypes.windll.winmm.mciSendStringW(command, None, 0, None)

    def _mci_close(self) -> None:
        try:
            self._mci(f"stop {self._alias}")
        except Exception:
            pass
        try:
            self._mci(f"close {self._alias}")
        except Exception:
            pass

    def _mci_open_and_play(self, path: str) -> None:
        # Normalize path for MCI
        path = os.path.abspath(path).replace("/", "\\")
        self._mci_close()
        # Prefer mpegvideo for mp3; fallback to mpegvideo automatically
        err = self._mci(f'open "{path}" type mpegvideo alias {self._alias}')
        if err != 0:
            err = self._mci(f'open "{path}" alias {self._alias}')
        if err != 0:
            raise RuntimeError(f"MCI open failed (code {err}): {path}")
        err = self._mci(f"play {self._alias}")
        if err != 0:
            self._mci_close()
            raise RuntimeError(f"MCI play failed (code {err})")

    def _mci_is_playing(self) -> bool:
        import ctypes

        buf = ctypes.create_unicode_buffer(128)
        ctypes.windll.winmm.mciSendStringW(
            f"status {self._alias} mode", buf, 128, None
        )
        mode = (buf.value or "").strip().lower()
        # modes: not ready, stopped, playing, paused, seeking
        return mode == "playing"

    def _mci_mode(self) -> str:
        import ctypes

        buf = ctypes.create_unicode_buffer(128)
        try:
            ctypes.windll.winmm.mciSendStringW(
                f"status {self._alias} mode", buf, 128, None
            )
            return (buf.value or "").strip().lower()
        except Exception:
            return ""

    # ── pygame fallback ───────────────────────────────────────────

    def _pygame_play(self, path: str) -> None:
        import pygame

        if not self._pygame_ready:
            pygame.mixer.init(frequency=24000, size=-16, channels=1, buffer=2048)
            self._pygame_ready = True
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()

    # ── completion watcher ────────────────────────────────────────

    def _watch_end(self) -> None:
        while not self._stop_watch.is_set():
            with self._lock:
                if not self._playing:
                    return
                if self._paused:
                    time.sleep(0.05)
                    continue
                still = False
                if self._backend == "mci":
                    mode = self._mci_mode()
                    # treat empty/stopped/not ready as finished
                    still = mode in ("playing", "seeking")
                elif self._backend == "pygame":
                    import pygame

                    still = bool(pygame.mixer.music.get_busy())
            if not still:
                cb = None
                with self._lock:
                    if self._playing and not self._paused:
                        self._playing = False
                        cb = self._on_complete
                        self._on_complete = None
                        if self._backend == "mci":
                            self._mci_close()
                if cb:
                    try:
                        cb()
                    except Exception:
                        pass
                return
            time.sleep(0.05)
