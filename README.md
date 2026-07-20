# Edge TTS — Chuyển văn bản thành giọng nói

Ứng dụng desktop Python dùng thư viện **edge-tts** để chuyển văn bản thành giọng nói, với hai chế độ:

1. **Đọc trực tiếp** — tách câu, gửi **tối đa 3 câu / request** edge-tts, phát ngay; highlight batch đang đọc; bôi đen để tua.
2. **Xuất MP3** — cùng pipeline theo batch 3 câu, ghép thành một file MP3.


## Tính năng

- Danh sách giọng đa ngôn ngữ (edge-tts / Microsoft neural voices)
- Điều chỉnh **rate** (tốc độ) và **pitch** (cao độ)
- Play / Pause / Resume / Stop
- Highlight câu đang được đọc
- Bôi đen đoạn văn → **Tua tới đoạn chọn** (hoặc chuột phải)
- Lịch sử phiên (lưu local `history.json`)
- Giao diện CustomTkinter (sáng/tối theo hệ thống)

## Yêu cầu

- Python 3.10+ (đã kiểm tra hướng hỗ trợ Python 3.14)
- Kết nối Internet (edge-tts gọi dịch vụ Microsoft)
- **pygame-ce**: phát audio LIVE trực tiếp từ RAM (`BytesIO`), không ghi file temp khi đọc
- (Khuyến nghị) **ffmpeg** trong PATH để ghép MP3 chất lượng tốt qua `pydub`. Nếu không có ffmpeg, app vẫn thử ghép binary MP3.


### Cài ffmpeg (Windows)

1. Tải từ https://www.gyan.dev/ffmpeg/builds/ hoặc `winget install ffmpeg`
2. Thêm thư mục `bin` của ffmpeg vào biến môi trường PATH
3. Mở lại terminal và kiểm tra: `ffmpeg -version`

## Cài đặt

```bash
cd e:\TTS
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> **Lưu ý Python 3.13/3.14:** package `audioop-lts` được cài tự động để `pydub` hoạt động. Cần `pygame-ce` để đọc trực tiếp từ RAM.



## Chạy

```bash
python main.py
```

## Đóng gói EXE (Windows)

```bash
build_exe.bat
```

Hoặc:

```bash
python -m PyInstaller --noconfirm --clean --onefile --windowed --name TTS --icon app.ico --add-data "app.ico;." --collect-all customtkinter --collect-all edge_tts main.py
```

Kết quả: `dist\TTS.exe` (icon `app.ico`, tên cửa sổ **TTS**).  
File `history.json`, `voices_cache.json`, `output/` được tạo **cạnh file exe**.


## Cách dùng

1. Dán văn bản vào ô soạn thảo.
2. Chọn **ngôn ngữ** / **giọng**, chỉnh rate & pitch nếu cần.
3. Chọn chế độ:
   - **Đọc trực tiếp** → bấm **Bắt đầu**. Câu đang đọc được tô vàng.
   - **Xuất MP3** → chọn đường dẫn file → **Bắt đầu**.
4. **Tạm dừng / Tiếp tục / Dừng** khi cần.
5. Muốn tua: bôi đen câu (hoặc vị trí trong câu) → **Tua tới đoạn chọn**.
6. **Lịch sử**: mở lại text + cài đặt phiên trước (double-click).

## Cấu trúc project

```
TTS/
├── main.py
├── requirements.txt
├── README.md
├── app/
│   ├── gui.py
│   ├── tts_engine.py
│   ├── audio_player.py
│   ├── sentence_splitter.py
│   ├── voices.py
│   └── history.py
└── output/
```

## Lưu ý

- App gửi **tối đa 3 câu / request** tới edge-tts (batch) để giảm số round-trip; highlight live theo cả batch đang phát.
- **Đọc trực tiếp**: audio giữ trong RAM, phát bằng pygame-ce (`BytesIO`) — không ghi file temp khi đọc.
- **Xuất MP3**: file tạm batch nằm trong `output/temp` và được dọn khi thoát / sau khi ghép.
- Lịch sử tối đa 100 mục (`history.json`).



