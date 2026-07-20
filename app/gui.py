"""CustomTkinter GUI for edge-tts Text-to-Speech app."""

from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Dict, List, Optional

import customtkinter as ctk

from app.audio_player import AudioPlayer
from app.history import HistoryEntry, HistoryStore
from app.paths import app_path, resource_path
from app.sentence_splitter import Sentence, sentence_index_at_offset, split_sentences
from app.tts_engine import PipelineMode, PipelineState, TTSPipeline
from app.voices import (
    default_voice,
    filter_voices,
    list_voices,
    locales_from_voices,
    voice_display_name,
)

# Appearance
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

APP_TITLE = "TTS"
OUTPUT_DIR = app_path("output")
ICON_PATH = resource_path("app.ico")


# Text widget highlight colors
HIGHLIGHT_BG = "#f59e0b"
HIGHLIGHT_FG = "#000000"
SELECT_HINT_BG = "#3b82f6"


class HistoryWindow(ctk.CTkToplevel):
    def __init__(self, master: "TTSApp", store: HistoryStore) -> None:
        super().__init__(master)
        self.master_app = master
        self.store = store
        self.title("Lịch sử")
        self.geometry("640x420")
        self.minsize(480, 300)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.listbox_frame = ctk.CTkScrollableFrame(self, label_text="Phiên gần đây")
        self.listbox_frame.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.listbox_frame.grid_columnconfigure(0, weight=1)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))
        ctk.CTkButton(btn_row, text="Tải lại", width=100, command=self.refresh).pack(
            side="left", padx=(0, 8)
        )
        ctk.CTkButton(
            btn_row, text="Xóa mục đã chọn", width=140, command=self.delete_selected
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btn_row, text="Xóa tất cả", width=100, fg_color="#b91c1c",
            hover_color="#991b1b", command=self.clear_all
        ).pack(side="left")
        ctk.CTkButton(btn_row, text="Đóng", width=80, command=self.destroy).pack(
            side="right"
        )

        self._item_widgets: List[ctk.CTkFrame] = []
        self._selected_id: Optional[str] = None
        self.refresh()

    def refresh(self) -> None:
        for w in self._item_widgets:
            w.destroy()
        self._item_widgets.clear()
        self._selected_id = None

        entries = self.store.all()
        if not entries:
            lbl = ctk.CTkLabel(self.listbox_frame, text="Chưa có lịch sử.")
            lbl.grid(row=0, column=0, pady=20)
            self._item_widgets.append(lbl)  # type: ignore
            return

        for i, entry in enumerate(entries):
            frame = ctk.CTkFrame(self.listbox_frame, corner_radius=8)
            frame.grid(row=i, column=0, sticky="ew", pady=4, padx=4)
            frame.grid_columnconfigure(0, weight=1)

            title = f"[{entry.created_at}] {entry.mode.upper()} — {entry.voice}"
            meta = f"{entry.rate} | {entry.pitch}  ·  {entry.text_preview}"

            ctk.CTkLabel(frame, text=title, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
                row=0, column=0, sticky="ew", padx=10, pady=(8, 0)
            )
            ctk.CTkLabel(frame, text=meta, anchor="w", text_color="gray").grid(
                row=1, column=0, sticky="ew", padx=10, pady=(0, 8)
            )

            def make_select(eid: str, fr: ctk.CTkFrame):
                def _sel(_event=None):
                    self._selected_id = eid
                    for other in self._item_widgets:
                        if isinstance(other, ctk.CTkFrame):
                            other.configure(border_width=0)
                    fr.configure(border_width=2, border_color="#3b82f6")

                return _sel

            def make_load(eid: str):
                def _load(_event=None):
                    e = self.store.get(eid)
                    if e:
                        self.master_app.load_history_entry(e)
                        self.destroy()

                return _load

            frame.bind("<Button-1>", make_select(entry.id, frame))
            frame.bind("<Double-Button-1>", make_load(entry.id))
            for child in frame.winfo_children():
                child.bind("<Button-1>", make_select(entry.id, frame))
                child.bind("<Double-Button-1>", make_load(entry.id))

            self._item_widgets.append(frame)

    def delete_selected(self) -> None:
        if not self._selected_id:
            messagebox.showinfo("Lịch sử", "Hãy chọn một mục trước.")
            return
        self.store.remove(self._selected_id)
        self.refresh()

    def clear_all(self) -> None:
        if messagebox.askyesno("Xác nhận", "Xóa toàn bộ lịch sử?"):
            self.store.clear()
            self.refresh()


class TTSApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x720")
        self.minsize(800, 560)
        self._set_app_icon()

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


        self.player = AudioPlayer()
        self.pipeline = TTSPipeline(temp_dir=OUTPUT_DIR / "temp")
        self.history = HistoryStore()

        self._voices: List[Dict[str, Any]] = []
        self._voice_map: Dict[str, str] = {}  # display -> ShortName
        self._sentences: List[Sentence] = []
        self._export_path_var = ctk.StringVar(value=str(OUTPUT_DIR / "output.mp3"))
        self._mode_var = ctk.StringVar(value="live")
        self._status_var = ctk.StringVar(value="Sẵn sàng")
        self._progress_var = ctk.StringVar(value="0/0")
        self._current_audio_path: Optional[str] = None
        self._history_win: Optional[HistoryWindow] = None

        self._build_ui()
        self._wire_pipeline()
        self._load_voices_async()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self) -> None:
        """Window / taskbar icon from app.ico (dev + frozen)."""
        icon = ICON_PATH
        if not icon.is_file():
            # Fallback: next to executable
            icon = app_path("app.ico")
        if not icon.is_file():
            return
        try:
            self.iconbitmap(default=str(icon))
        except Exception:
            try:
                self.iconbitmap(str(icon))
            except Exception:
                pass
        # Windows taskbar icon for customtkinter / tk
        try:
            self.wm_iconbitmap(str(icon))
        except Exception:
            pass
        # Optional iconphoto for better multi-size support
        try:
            from PIL import Image, ImageTk

            img = Image.open(icon)
            self._icon_photo = ImageTk.PhotoImage(img)
            self.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    # ── UI construction ──────────────────────────────────────────


    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Top: settings
        top = ctk.CTkFrame(self)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(top, text="Ngôn ngữ:").grid(row=0, column=0, padx=(10, 4), pady=8, sticky="w")
        self.locale_combo = ctk.CTkComboBox(
            top, values=["All"], width=140, command=self._on_locale_change
        )
        self.locale_combo.set("All")
        self.locale_combo.grid(row=0, column=1, padx=4, pady=8, sticky="w")

        ctk.CTkLabel(top, text="Giọng:").grid(row=0, column=2, padx=(16, 4), pady=8, sticky="w")
        self.voice_combo = ctk.CTkComboBox(top, values=["(đang tải...)"], width=320)
        self.voice_combo.grid(row=0, column=3, padx=4, pady=8, sticky="ew")

        ctk.CTkButton(
            top, text="↻", width=36, command=lambda: self._load_voices_async(True)
        ).grid(row=0, column=4, padx=(4, 10), pady=8)

        # Rate / Pitch
        mid = ctk.CTkFrame(self)
        mid.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        mid.grid_columnconfigure(1, weight=1)
        mid.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(mid, text="Tốc độ (rate):").grid(row=0, column=0, padx=(10, 4), pady=6)
        self.rate_slider = ctk.CTkSlider(mid, from_=-50, to=100, number_of_steps=30, command=self._on_rate)
        self.rate_slider.set(0)
        self.rate_slider.grid(row=0, column=1, sticky="ew", padx=4, pady=6)
        self.rate_label = ctk.CTkLabel(mid, text="+0%", width=60)
        self.rate_label.grid(row=0, column=2, padx=4)

        ctk.CTkLabel(mid, text="Cao độ (pitch):").grid(row=0, column=3, padx=(16, 4), pady=6)
        self.pitch_slider = ctk.CTkSlider(mid, from_=-50, to=50, number_of_steps=20, command=self._on_pitch)
        self.pitch_slider.set(0)
        self.pitch_slider.grid(row=0, column=4, sticky="ew", padx=4, pady=6)
        self.pitch_label = ctk.CTkLabel(mid, text="+0Hz", width=60)
        self.pitch_label.grid(row=0, column=5, padx=(4, 10))

        # Mode row
        mode_row = ctk.CTkFrame(mid, fg_color="transparent")
        mode_row.grid(row=1, column=0, columnspan=6, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(mode_row, text="Chế độ:").pack(side="left", padx=(0, 8))
        ctk.CTkRadioButton(
            mode_row, text="Đọc trực tiếp", variable=self._mode_var, value="live",
            command=self._on_mode_change
        ).pack(side="left", padx=6)
        ctk.CTkRadioButton(
            mode_row, text="Xuất MP3", variable=self._mode_var, value="export",
            command=self._on_mode_change
        ).pack(side="left", padx=6)

        self.export_entry = ctk.CTkEntry(mode_row, textvariable=self._export_path_var, width=320)
        self.export_entry.pack(side="left", padx=(16, 4))
        self.browse_btn = ctk.CTkButton(mode_row, text="Chọn file...", width=100, command=self._browse_export)
        self.browse_btn.pack(side="left", padx=4)

        # Text area with native Text for tags

        text_frame = ctk.CTkFrame(self)
        text_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=6)
        text_frame.grid_columnconfigure(0, weight=1)
        text_frame.grid_rowconfigure(1, weight=1)

        hint = (
            "Dán văn bản bên dưới. Ở chế độ đọc trực tiếp: bôi đen một câu rồi bấm "
            "«Tua tới đoạn chọn» (hoặc chuột phải) để nhảy tới câu đó. Câu đang đọc được tô sáng."
        )
        ctk.CTkLabel(text_frame, text=hint, anchor="w", text_color="gray", wraplength=900).grid(
            row=0, column=0, sticky="ew", padx=10, pady=(8, 4)
        )

        # Embed tk.Text inside CTk frame for highlighting
        text_container = ctk.CTkFrame(text_frame, fg_color="transparent")
        text_container.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        text_container.grid_columnconfigure(0, weight=1)
        text_container.grid_rowconfigure(0, weight=1)

        self.text_widget = tk.Text(
            text_container,
            wrap="word",
            font=("Segoe UI", 12),
            undo=True,
            relief="flat",
            padx=10,
            pady=10,
            spacing1=4,
            spacing3=4,
        )
        self.text_widget.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(text_container, command=self.text_widget.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text_widget.configure(yscrollcommand=scroll.set)

        self.text_widget.tag_configure(
            "current", background=HIGHLIGHT_BG, foreground=HIGHLIGHT_FG
        )
        self.text_widget.bind("<Button-3>", self._on_right_click)

        # Controls
        bottom = ctk.CTkFrame(self)
        bottom.grid(row=3, column=0, sticky="ew", padx=12, pady=(6, 12))
        bottom.grid_columnconfigure(6, weight=1)

        self.play_btn = ctk.CTkButton(bottom, text="▶  Bắt đầu", width=120, command=self._on_play)
        self.play_btn.grid(row=0, column=0, padx=(10, 6), pady=10)

        self.pause_btn = ctk.CTkButton(
            bottom, text="⏸  Tạm dừng", width=120, command=self._on_pause, state="disabled"
        )
        self.pause_btn.grid(row=0, column=1, padx=6, pady=10)

        self.stop_btn = ctk.CTkButton(
            bottom, text="⏹  Dừng", width=100, command=self._on_stop, state="disabled",
            fg_color="#b91c1c", hover_color="#991b1b"
        )
        self.stop_btn.grid(row=0, column=2, padx=6, pady=10)

        self.seek_btn = ctk.CTkButton(
            bottom, text="⏭  Tua tới đoạn chọn", width=160, command=self._on_seek_selection
        )
        self.seek_btn.grid(row=0, column=3, padx=6, pady=10)

        ctk.CTkButton(bottom, text="📜  Lịch sử", width=100, command=self._open_history).grid(
            row=0, column=4, padx=6, pady=10
        )

        self.progress_label = ctk.CTkLabel(bottom, textvariable=self._progress_var, width=80)
        self.progress_label.grid(row=0, column=5, padx=8)

        self.status_label = ctk.CTkLabel(
            bottom, textvariable=self._status_var, anchor="e", text_color="gray"
        )
        self.status_label.grid(row=0, column=6, sticky="e", padx=(8, 12))

        # Apply mode-dependent widget states after all controls exist
        self._on_mode_change()

    # ── Voices ────────────────────────────────────────────────────


    def _load_voices_async(self, force: bool = False) -> None:
        self._status_var.set("Đang tải danh sách giọng...")

        def work():
            try:
                voices = list_voices(force_refresh=force)
                self.after(0, lambda: self._apply_voices(voices))
            except Exception as e:
                self.after(0, lambda: self._status_var.set(f"Lỗi tải giọng: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _apply_voices(self, voices: List[Dict[str, Any]]) -> None:
        self._voices = voices
        locales = ["All"] + locales_from_voices(voices)
        self.locale_combo.configure(values=locales)
        # Prefer vi-VN if available
        if "vi-VN" in locales:
            self.locale_combo.set("vi-VN")
        else:
            self.locale_combo.set("All")
        self._refresh_voice_combo()
        self._status_var.set(f"Đã tải {len(voices)} giọng. Sẵn sàng.")

    def _refresh_voice_combo(self) -> None:
        locale = self.locale_combo.get()
        filtered = filter_voices(self._voices, locale=locale)
        if not filtered:
            filtered = self._voices
        displays = [voice_display_name(v) for v in filtered]
        self._voice_map = {voice_display_name(v): v["ShortName"] for v in filtered}
        self.voice_combo.configure(values=displays or ["(không có)"])
        pref = default_voice(filtered or self._voices)
        for disp, short in self._voice_map.items():
            if short == pref:
                self.voice_combo.set(disp)
                return
        if displays:
            self.voice_combo.set(displays[0])

    def _on_locale_change(self, _value: str = "") -> None:
        self._refresh_voice_combo()

    def _selected_voice(self) -> str:
        disp = self.voice_combo.get()
        return self._voice_map.get(disp, disp)

    def _rate_str(self) -> str:
        v = int(self.rate_slider.get())
        return f"{v:+d}%"

    def _pitch_str(self) -> str:
        v = int(self.pitch_slider.get())
        return f"{v:+d}Hz"

    def _on_rate(self, value: float) -> None:
        self.rate_label.configure(text=f"{int(value):+d}%")

    def _on_pitch(self, value: float) -> None:
        self.pitch_label.configure(text=f"{int(value):+d}Hz")

    def _on_mode_change(self) -> None:
        export = self._mode_var.get() == "export"
        state = "normal" if export else "disabled"
        if hasattr(self, "export_entry"):
            self.export_entry.configure(state=state)
        if hasattr(self, "browse_btn"):
            self.browse_btn.configure(state=state)
        # Seek only useful in live
        if hasattr(self, "seek_btn"):
            self.seek_btn.configure(state="normal" if not export else "disabled")


    def _browse_export(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".mp3",
            filetypes=[("MP3", "*.mp3"), ("All", "*.*")],
            initialdir=str(OUTPUT_DIR),
            initialfile=f"tts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3",
        )
        if path:
            self._export_path_var.set(path)

    # ── Pipeline wiring ───────────────────────────────────────────

    def _wire_pipeline(self) -> None:
        # Use default-arg lambdas to capture values (avoid late-binding bugs).
        # Pipeline passes batch range [start, end) (exclusive end).
        self.pipeline.on_sentence_start = lambda start, end: self.after(
            0, lambda start=start, end=end: self._ui_sentence_start(start, end)
        )
        self.pipeline.on_sentence_ready = lambda start, end, data: self.after(
            0,
            lambda start=start, end=end, data=data: self._ui_sentence_ready(
                start, end, data
            ),
        )

        self.pipeline.on_progress = lambda cur, tot: self.after(
            0, lambda cur=cur, tot=tot: self._progress_var.set(f"{cur}/{tot}")
        )
        self.pipeline.on_export_done = lambda path: self.after(
            0, lambda path=path: self._ui_export_done(path)
        )
        self.pipeline.on_done = lambda: self.after(0, self._ui_done)
        self.pipeline.on_error = lambda msg: self.after(
            0, lambda msg=msg: self._ui_error(msg)
        )
        self.pipeline.on_status = lambda msg: self.after(
            0, lambda msg=msg: self._ui_status(msg)
        )
        self.pipeline.on_state_change = lambda st: self.after(
            0, lambda st=st: self._ui_state(st)
        )

    def _batch_status_label(self, start: int, end: int) -> str:
        if end - start <= 1:
            return f"câu {start + 1}"
        return f"câu {start + 1}–{end}"

    def _ui_sentence_start(self, start: int, end: int) -> None:
        self._highlight_batch(start, end)
        label = self._batch_status_label(start, end)
        preview = ""
        if self._sentences and 0 <= start < len(self._sentences):
            preview = self._sentences[start].preview
        self._status_var.set(f"Đang xử lý {label}: {preview}")

    def _ui_sentence_ready(self, start: int, end: int, data: bytes) -> None:
        """Play synthesized audio from RAM (pygame-ce BytesIO)."""
        if self.pipeline.state == PipelineState.STOPPING or self.pipeline._stop_flag:
            self.pipeline.notify_playback_finished()
            return

        self._highlight_batch(start, end)
        label = self._batch_status_label(start, end)
        preview = ""
        if self._sentences and 0 <= start < len(self._sentences):
            preview = self._sentences[start].preview
        self._status_var.set(f"Đang đọc {label}: {preview}")
        self._current_audio_path = None

        def on_complete():
            self.pipeline.notify_playback_finished()

        try:
            self.player.play_bytes(data, on_complete=on_complete)
        except Exception as e:
            self._status_var.set(f"Lỗi phát audio: {e}")
            on_complete()





    def _ui_export_done(self, path: str) -> None:
        self._status_var.set(f"Đã xuất: {path}")
        self._save_history("export", path)
        messagebox.showinfo("Xuất MP3", f"Đã lưu file:\n{path}")

    def _ui_done(self) -> None:
        self._clear_highlight()
        current = self._status_var.get()
        # Keep pipeline notes about retries/skips/partial export if already set
        keep_note = (
            "bỏ qua" in current.lower()
            or "thiếu" in current.lower()
            or current.startswith("Đã xuất")
            or current.startswith("Xuất xong")
            or current.startswith("Hoàn tất với")
        )
        if self._mode_var.get() == "live":
            if not keep_note:
                self._status_var.set("Hoàn tất đọc.")
            self._save_history("live")
        else:
            if not keep_note:
                self._status_var.set("Hoàn tất.")
        self._set_controls_idle()

    def _ui_status(self, msg: str) -> None:
        """Non-fatal pipeline status (retry, fallback, partial skip, etc.)."""
        self._status_var.set(msg)

    def _ui_error(self, msg: str) -> None:
        self._status_var.set(f"Lỗi: {msg}")
        # Don't block with modal on every sentence error; only status is enough.
        # Major errors still shown:
        if "Ghép MP3" in msg or "Không có đoạn" in msg:
            messagebox.showerror("Lỗi", msg)

    def _ui_state(self, state: str) -> None:
        if state == "running":
            self.play_btn.configure(state="disabled")
            self.pause_btn.configure(state="normal", text="⏸  Tạm dừng")
            self.stop_btn.configure(state="normal")
        elif state == "paused":
            self.pause_btn.configure(text="▶  Tiếp tục")
            self.play_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        elif state in ("idle", "stopping"):
            if state == "idle":
                self._set_controls_idle()

    def _set_controls_idle(self) -> None:
        self.play_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="⏸  Tạm dừng")
        self.stop_btn.configure(state="disabled")

    # ── Highlight ─────────────────────────────────────────────────

    def _char_index(self, offset: int) -> str:
        """Convert absolute character offset to tk Text index 'line.char'."""
        return f"1.0+{offset}c"

    def _highlight_sentence(self, sentence: Sentence) -> None:
        self.text_widget.tag_remove("current", "1.0", "end")
        start = self._char_index(sentence.start_char)
        end = self._char_index(sentence.end_char)
        self.text_widget.tag_add("current", start, end)
        self.text_widget.see(start)

    def _highlight_batch(self, start_idx: int, end_idx: int) -> None:
        """Highlight all sentences in batch range [start_idx, end_idx)."""
        if not self._sentences or start_idx >= end_idx:
            return
        start_idx = max(0, start_idx)
        end_idx = min(end_idx, len(self._sentences))
        if start_idx >= end_idx:
            return
        first = self._sentences[start_idx]
        last = self._sentences[end_idx - 1]
        self.text_widget.tag_remove("current", "1.0", "end")
        start = self._char_index(first.start_char)
        end = self._char_index(last.end_char)
        self.text_widget.tag_add("current", start, end)
        self.text_widget.see(start)

    def _clear_highlight(self) -> None:
        self.text_widget.tag_remove("current", "1.0", "end")


    def _selection_char_offset(self) -> Optional[int]:
        try:
            sel_first = self.text_widget.index("sel.first")
        except tk.TclError:
            return None
        # Count characters from 1.0 to sel.first
        return len(self.text_widget.get("1.0", sel_first))

    # ── Actions ───────────────────────────────────────────────────

    def _get_text(self) -> str:
        return self.text_widget.get("1.0", "end-1c")

    def _on_play(self) -> None:
        text = self._get_text()
        if not text.strip():
            messagebox.showwarning("Thiếu nội dung", "Hãy nhập hoặc dán văn bản.")
            return

        self._sentences = split_sentences(text)
        if not self._sentences:
            messagebox.showwarning("Thiếu nội dung", "Không tách được câu nào.")
            return

        voice = self._selected_voice()
        rate = self._rate_str()
        pitch = self._pitch_str()
        mode = (
            PipelineMode.EXPORT
            if self._mode_var.get() == "export"
            else PipelineMode.LIVE
        )
        export_path = self._export_path_var.get().strip() if mode == PipelineMode.EXPORT else None

        if mode == PipelineMode.EXPORT and not export_path:
            messagebox.showwarning("Xuất MP3", "Hãy chọn đường dẫn file MP3.")
            return

        self.player.stop()
        self._clear_highlight()
        self._progress_var.set(f"0/{len(self._sentences)}")
        self._status_var.set(
            f"Bắt đầu ({'xuất MP3' if mode == PipelineMode.EXPORT else 'đọc trực tiếp'}) "
            f"— {len(self._sentences)} câu..."
        )

        self.pipeline.start(
            sentences=self._sentences,
            voice=voice,
            rate=rate,
            pitch=pitch,
            mode=mode,
            export_path=export_path,
            start_index=0,
        )

    def _on_pause(self) -> None:
        state = self.pipeline.state
        if state == PipelineState.RUNNING:
            self.pipeline.pause()
            self.player.pause()
            self._status_var.set("Đã tạm dừng.")
        elif state == PipelineState.PAUSED:
            self.pipeline.resume()
            self.player.resume()
            self._status_var.set("Tiếp tục...")

    def _on_stop(self) -> None:
        self.pipeline.stop()
        self.player.stop()
        self._clear_highlight()
        self._status_var.set("Đã dừng.")
        self._set_controls_idle()

    def _on_seek_selection(self) -> None:
        if self._mode_var.get() != "live":
            return

        offset = self._selection_char_offset()
        if offset is None:
            messagebox.showinfo(
                "Tua",
                "Hãy bôi đen (chọn) đoạn văn bản / câu bạn muốn tua tới.",
            )
            return

        # Ensure sentences match current text
        text = self._get_text()
        sentences = self._sentences or split_sentences(text)
        if not sentences:
            sentences = split_sentences(text)
        self._sentences = sentences

        idx = sentence_index_at_offset(sentences, offset)
        if idx < 0:
            return

        if self.pipeline.state in (PipelineState.RUNNING, PipelineState.PAUSED):
            self.player.stop()
            self.pipeline.seek_to(idx)
            self._status_var.set(f"Tua tới câu {idx + 1}...")
        else:
            # Not running — start from that sentence
            voice = self._selected_voice()
            self.pipeline.start(
                sentences=sentences,
                voice=voice,
                rate=self._rate_str(),
                pitch=self._pitch_str(),
                mode=PipelineMode.LIVE,
                start_index=idx,
            )
            self._status_var.set(f"Bắt đầu từ câu {idx + 1}...")

    def _on_right_click(self, event) -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Tua tới đoạn chọn", command=self._on_seek_selection)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _save_history(self, mode: str, output_path: Optional[str] = None) -> None:
        text = self._get_text()
        if not text.strip():
            return
        entry = HistoryEntry.create(
            full_text=text,
            voice=self._selected_voice(),
            rate=self._rate_str(),
            pitch=self._pitch_str(),
            mode=mode,
            output_path=output_path,
        )
        self.history.add(entry)

    def _open_history(self) -> None:
        if self._history_win is not None and self._history_win.winfo_exists():
            self._history_win.focus()
            self._history_win.refresh()
            return
        self._history_win = HistoryWindow(self, self.history)

    def load_history_entry(self, entry: HistoryEntry) -> None:
        self.text_widget.delete("1.0", "end")
        self.text_widget.insert("1.0", entry.full_text)
        self._clear_highlight()

        # Try restore voice
        for disp, short in self._voice_map.items():
            if short == entry.voice:
                self.voice_combo.set(disp)
                break
        else:
            # Voice may be filtered out — still set raw if possible
            pass

        # Rate / pitch parse
        try:
            rate_val = int(entry.rate.replace("%", "").replace("+", "") or "0")
            # entry.rate is like "+0%" or "-10%"
            rate_val = int(entry.rate.rstrip("%"))
            self.rate_slider.set(rate_val)
            self.rate_label.configure(text=entry.rate if entry.rate.endswith("%") else f"{rate_val:+d}%")
        except ValueError:
            pass
        try:
            pitch_val = int(entry.pitch.replace("Hz", "").replace("+", "") or "0")
            pitch_val = int(entry.pitch.replace("Hz", ""))
            self.pitch_slider.set(pitch_val)
            self.pitch_label.configure(
                text=entry.pitch if entry.pitch.endswith("Hz") else f"{pitch_val:+d}Hz"
            )
        except ValueError:
            pass

        if entry.mode in ("live", "export"):
            self._mode_var.set(entry.mode)
            self._on_mode_change()
        if entry.output_path:
            self._export_path_var.set(entry.output_path)

        self._status_var.set(f"Đã tải lịch sử: {entry.created_at}")

    def _on_close(self) -> None:
        try:
            self.pipeline.stop()
            self.player.shutdown()
            self.pipeline.cleanup_temp()
        except Exception:
            pass
        self.destroy()


def run_app() -> None:
    app = TTSApp()
    app.mainloop()
