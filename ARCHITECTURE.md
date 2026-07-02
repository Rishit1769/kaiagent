# Clicky for Windows — Architecture

**Clicky** is an AI teaching companion that lives next to your cursor on Windows. It's a Python/PyQt6 port of the original macOS/SwiftUI app. It runs as a background process with a transparent cursor overlay ("blue buddy"), a floating companion panel, a system tray menu voice input (push-to-talk + wake word), multi-provider AI, and pixel-precise pointing at UI elements.

---

## 1. Entry Point (`main.py`)

Boots Qt, then spawns four components:

1. **`CompanionManager`** — central async state machine (orchestrates everything)
2. **`CompanionPanel`** — floating chat window (340×480, drag-repositionable, accepts drag-drop PDFs)
3. **`CursorOverlay`** — transparent click-through overlay covering all monitors (blue triangle buddy)
4. **`TrayManager`** — system tray icon with full context menu

Also starts:
- **`GlobalHotkeyMonitor`** (default: `Ctrl+Alt+Space`) — push-to-talk press/release
- **`StopHotkey`** (`Esc`) — cancels LLM stream + TTS mid-word
- **First-run setup wizard** if no Ollama models are installed

All communication between manager and UI goes through **Qt signals** (thread-safe, queued cross-thread).

### Signal Wiring (backbone)

```
sig_state_changed       → Panel + Tray + Overlay    IDLE/LISTENING/THINKING/SPEAKING
sig_response_chunk      → Panel                     streaming LLM response text
sig_audio_level         → Panel (waveform) + Overlay  RMS audio level for visual feedback
sig_point_at/hold/release → Overlay                  pointing flight control
sig_arrow/circle/underline/label → Overlay           whiteboard annotations
sig_error               → Tray                       toast notifications
sig_copilot_models_done → Tray                       model list loaded
sig_ollama_models       → Tray                       installed model list
sig_ollama_pull_status  → Tray                       model download progress
sig_recording_state     → Tray                       recording on/off indicator
on_model_changed        → Panel → Manager            model dropdown selection
on_* (30+ signals)      → Tray → Manager             all toggle/action commands
```

---

## 2. Core Processing Pipeline

The full flow when the user speaks (in `CompanionManager._end_capture_and_process()`):

```
1. PCM audio from AmbientListener.stop_recording()
2. STT.transcribe(pcm) → transcript text
3. Voice command short-circuits (no LLM needed):
   - "stop" → cancel everything
   - "next" → advance lesson step
   - "repeat" → replay last TTS
   - journal queries → local SQLite reply
   - quiz review → spaced repetition from journal
   - Skills system → custom handler
4. Screen capture (skipped for sensitive/identity windows)
5. Parallel tasks:
   - Web search (DuckDuckGo or Tavily)
   - Element pointing (UIA → OCR → vision grid, 3 tiers)
6. Build system prompt with context (window title, OCR text, attached docs, etc.)
7. LLM.stream_response() → streaming chunks parsed for POINT/ARROW/CIRCLE/UNDERLINE/LABEL tags
8. Append to per-app history (capped at 20 messages)
9. Log to knowledge journal (SQLite)
10. TTS.speak() — holds pointing visible on element until speech completes
11. Release point → return to IDLE
```

### State Machine

```
IDLE → LISTENING → THINKING → SPEAKING → IDLE
```

- **IDLE**: ambient mic scanning for wake word or waiting for hotkey
- **LISTENING**: capturing audio (push-to-talk held or wake word triggered)
- **THINKING**: transcribing + screen capture + LLM streaming
- **SPEAKING**: TTS playback (pointing held during)

### Sleep/Wake Watchdog

Background thread checks `time.monotonic()` every 5 seconds. If drift > 15 seconds (system resumed from sleep), restarts the mic stream and asyncio loop.

---

## 3. Configuration (`config.py`)

Singleton `cfg` loads from `.env` → `.env.local` (`.env.local` overrides `.env`).

### Provider Auto-Detection Priority

| Service | Priority Chain |
|---------|---------------|
| **LLM** | `CLICKY_ACTIVE_LLM` env → Claude → OpenAI → Copilot → Gemini → Ollama |
| **STT** | `CLICKY_STT` env → Deepgram → OpenAI Whisper → whisper.cpp → Faster-Whisper |
| **TTS** | ElevenLabs → OpenAI TTS → Edge TTS (free, no key needed) |
| **Search** | Tavily (if key set) → DuckDuckGo (free) |

### Ollama Dual Model Slots

- `ollama_vision_model` — for screen-aware queries (pointing, "what's on screen?")
- `ollama_text_model` — for Code Mode, journal Q&A (no screenshot needed)
- Runtime switch via `set_ollama_model(kind, name)` — persists to env vars for the session

### Provider Persistence

`set_active_llm(name)` writes `CLICKY_ACTIVE_LLM=name` to `.env` so the choice survives restarts.

---

## 4. AI Module (`ai/`)

### LLM Providers

| Provider | File | Client | Default Model | Vision |
|----------|------|--------|---------------|--------|
| Claude | `claude_provider.py` | `anthropic.AsyncAnthropic` | `claude-sonnet-4-6` | ✅ multimodal |
| OpenAI | `openai_provider.py` | `openai.AsyncOpenAI` | `gpt-4o` | ✅ image_url |
| Gemini | `gemini_provider.py` | `httpx` (REST SSE) | `gemini-2.5-flash` | ✅ inline_data |
| Ollama | `ollama_provider.py` | `httpx` | Per-slot config | ✅ images field |
| Copilot | `github_copilot_provider.py` | `httpx` | `gpt-4o-mini` (free) | ✅ image_url |

### GitHub Copilot Provider (`github_copilot_provider.py`)

- **Device-flow OAuth** — same flow as VS Code. Uses public VS Code client ID: `Iv1.b507a08c87ecfe98`.
- Token cached to `%LOCALAPPDATA%\Clicky\github_token.json`
- **Short-lived Copilot token**: 25-minute TTL, auto-refreshed before expiry
- **Live model discovery** via `GET /models` → cached 6 hours
- Models have a `multiplier` field: `0` = free tier, `≥1` = premium quota usage
- UI displays "(free)" / "(N×)" so the user knows what burns quota

### Pointing System (3 Tiers) — `hybrid_pointer.py`

| Tier | Technique | Speed | Accuracy | Dependency |
|------|-----------|-------|----------|------------|
| 1 | **Windows UI Automation** (UIA) | ~5 ms | Pixel-perfect | `uiautomation` |
| 2 | **Offline OCR** (RapidOCR/ONNX) | ~300 ms | Text-perfect | `rapidocr-onnxruntime` |
| 3 | **Vision LLM grid** | ~1-3 s | ~25-50 px | Any vision LLM |

Tries tiers in order, returns first match with confidence ≥ 0.5. Falls through to the vision grid only when UIA and OCR both whiff.

#### Element Locator (`element_locator.py`) — Claude Computer Use path

- Only works with `ANTHROPIC_API_KEY`
- Uses `computer_use_20251124` beta tool
- Picks best resolution (1024/1280/1366) matching aspect ratio
- Returns coordinates in **logical screen space** with DPI/origin adjustment

#### Universal Locator (`universal_locator.py`) — Works with any vision LLM

- Stage 1: 12×8 grid overlay on screenshot, LLM picks cell (1-96)
- Stage 2: 3×3 cell zoom, 6×6 sub-grid, LLM picks sub-cell (1-36)
- Coordinate transform: inference px → JPEG px → physical px → logical px

### Web Search (`web_search.py`)

- Free: DuckDuckGo (via `ddgs` library) + concurrent page fetch (top 3 pages, ~1400 chars each)
- Premium: Tavily API (if key set)
- Query expansion: adds current year for recency-sensitive questions
- Citations in format `[1]`, `[2]` — injected into system prompt

### Model Registry (`model_registry.py`)

- Live model lists from Claude/OpenAI/Gemini APIs
- 30-day cache to `%LOCALAPPDATA%\Clicky\models_<provider>.json`
- Curated fallback lists for offline use
- `refresh_all_stale()` called on startup

### Ollama Bootstrap (`ollama_bootstrap.py`)

- Detects Ollama installation + server status
- Can download official Ollama installer
- Pulls models with progress callbacks
- CLI mode: `python -m ai.ollama_bootstrap [status|install|pull|diag]`

---

## 5. Audio Module (`audio/`)

### Ambient Listener (`ambient_listener.py`)

- **Always-on** `sounddevice.InputStream` at 16 kHz
- Two modes: `STANDBY` (wake-word scanning) and `RECORDING` (buffering user speech)
- **VAD**: Energy-based (threshold 0.006 RMS)
- **Wake word**: Runs `faster-whisper tiny.en` on speech segments (daemon thread)
  - Wake phrases: 12 variants including "clicky", "clickie", "hey clicky"
  - Pre-roll buffer (540 ms) prevents clipping
- **Push-to-talk**: Hotkey held → RECORDING mode → buffer PCM bytes

### Speech-to-Text Providers

| Provider | File | Method | Key Required |
|----------|------|--------|-------------|
| Deepgram | `deepgram_stt.py` | HTTP upload (WAV) | `DEEPGRAM_API_KEY` |
| OpenAI Whisper | `openai_stt.py` | AsyncOpenAI SDK | `OPENAI_API_KEY` |
| Whisper.cpp | `whisper_cpp_stt.py` | `pywhispercpp` local | None (GPU, 3-5× faster) |
| Faster-Whisper | `faster_whisper_stt.py` | Local CPU model | None |

### Text-to-Speech Providers

| Provider | File | Method | Key Required |
|----------|------|--------|-------------|
| Edge TTS | `edge_tts_provider.py` | Microsoft neural (free) | None (400+ voices) |
| OpenAI TTS | `openai_tts_provider.py` | `tts-1` model | `OPENAI_API_KEY` |
| ElevenLabs | `elevenlabs_provider.py` | `eleven_flash_v2_5` | `ELEVENLABS_API_KEY` |

### Playback (`playback.py`)

- **Cancellable audio** via `threading.Event` (`_stop_event`)
- Decodes MP3 with PyAV, plays in 50 ms chunks polling stop event
- `stop_audio()` — sets stop event + calls `sd.stop()`

---

## 6. Screen Capture (`screen/capture.py`)

### `ScreenShot` dataclass carries all coordinate metadata:

- `index` — monitor number (1-based)
- `width/height` — downscaled JPEG dimensions (960 px max)
- `physical_width/height` — actual GPU pixels
- `physical_left/top` — monitor origin in `mss` virtual screen
- `dpi_scale` — physical/logical ratio (queried via `GetDpiForMonitor`)
- `logical_left/top` — DPI-adjusted origin for Qt cursor space

### Coordinate Pipeline

```
GPU pixels → mss.grab() → PIL downscale to 960px → JPEG quality 50 → base64
                                ↓
Physical coords + DPI + monitor origin carried alongside
                                ↓
Element locator converts: inference space → physical → logical
```

- `capture_all_screens()` — all monitors
- `capture_primary()` — first monitor only

---

## 7. UI Module (`ui/`)

### Cursor Overlay (`overlay.py`) — 594 lines

A transparent click-through Qt window covering all monitors. Draws the "blue buddy" triangle at 60 FPS.

**States (cross-fade in place):**

| State | Visual | Drawing |
|-------|--------|---------|
| `idle` / `speaking` | Blue triangle (16×16, rotated -35°) | `_draw_triangle()` |
| `listening` | 5-bar reactive audio waveform | `_draw_waveform()` |
| `thinking` | Rotating arc spinner (70% arc) | `_draw_spinner()` |

**Pointing Phase Machine:** `follow → flying → dwelling → returning → follow`

- **Spring-follow**: stiffness=0.28, damping=0.62, offset (+35, +25) from cursor
- **Bezier arc flight**: quadratic Bezier, arc height = 22% of distance, smoothstep easing (`t²(3-2t)`)
- **Duration**: 1.6-2.8 s (scales with distance), 1.7× multiplier in Slow Mode
- **Rotation**: triangle "leans into" flight path (tangent angle + 90°)
- **Dwell**: 4 s default, `float("inf")` when `point_hold` is set (during TTS)
- **Return**: 1.4 s arc back to cursor

**Whiteboard Annotations** (TTL = 8 s, fade in last 25%):

- `[ARROW:x1,y1→x2,y2]` — line with arrowhead
- `[CIRCLE:x,y,r]` — pulsed ring
- `[UNDERLINE:x,y,w]` — line under text
- `[LABEL:x,y:text]` — floating caption

**Highlight ring**: Pulsing blue ring (26 px radius) around detected element during dwell.

### Companion Panel (`panel.py`) — 392 lines

Floating frameless window (340×480), glass dark background, drag-repositionable, accepts drag-drop PDFs/DOCX/TXT/MD. Contains:
- Title + ProviderBadge (pill showing active provider)
- Status dot + label (state changes)
- WaveformWidget (12 animated bars during listening)
- Scrollable response area with selectable text
- Push-to-talk button
- Model dropdown (populated from live model registry)

### Tray Manager (`tray.py`) — 430 lines

Full system tray context menu with dynamic submenus for:
- Provider info + model switcher
- Copilot sign-in / refresh
- Ollama model management (vision/text model pickers + pull recommended)
- Toggle features: Slow, Quiz, Privacy, Code, Multilingual, OCR modes
- Journal submenu (logging toggle, open folder, attach document)
- Lesson Recording controls (start/stop)
- Workflow Capture controls (start/stop)
- Setup & Diagnostics menu
- Colored circle icons: idle=gray, listening=green, thinking=blue, speaking=orange

### Design System (`design.py`)

- Colors: SURFACE (#121216), BLUE_GLOW (#0078FF), palette constants
- Fonts: Segoe UI (10-15 px, various weights)
- Panel: 340×480, radius 16 px
- Full QSS stylesheet for dark glass panel

### Setup Wizard (`setup_wizard.py`) — 361 lines

First-run dialog with 3 steps:
1. Install Ollama (download + launch installer)
2. Pull text model (default: `llama3.2:3b`)
3. Pull vision model (default: `qwen2.5vl:3b`)

Persistent marker: `%LOCALAPPDATA%\Clicky\setup_complete.flag`

---

## 8. Query Classifiers (`tutor.py`)

Regex classifiers that run on transcript **before** LLM call, enabling short-circuits:

| Function | Pattern | Purpose |
|----------|---------|---------|
| `is_locate()` | "where is", "how do I click", "find the", "point at" | Triggers element detection |
| `is_multistep()` | "how do I export/install/setup/configure" | Triggers step-by-step lesson |
| `is_next()` | "next", "continue", "go on" | Advances lesson step |
| `is_stop()` | "stop", "quit", "cancel" | Cancels generation |
| `is_repeat()` | "repeat", "say that again" | Replays last TTS |
| `is_journal_today()` | "what did I learn today" | Local SQLite reply |
| `is_journal_week()` | "what did I learn this week" | Local SQLite reply |
| `is_quiz_review()` | "quiz me", "test me" | Spaced repetition pull |
| `is_identity_question()` | "who is X", "tell me about X" | Skips screenshot (avoids refusal) |
| `is_sensitive_window()` | "password", "login", ".env", "banking" | Privacy guard |

### Prompt Builder (`_build_system_prompt()`)

Builds the system prompt with: today's date, active window title, lesson progress, quiz mode override, detected element coordinates, code mode addendum, language directive, OCR text, attached documents, web search context (with `[1]`, `[2]` citations), and privacy guard addendum.

---

## 9. Tutor Features (`tutor_features/`)

### Knowledge Journal (`journal.py`) — SQLite

- Database at `%LOCALAPPDATA%\Clicky\journal.db`
- Schema: `entries(id, created_at, app_key, window_title, question, answer, provider, model, streak, next_review_at, tags)`
- Every Q&A logged automatically
- Voice queries: "what did I learn today?" / "this week?"
- **SM-2 Spaced Repetition**: intervals 1→3→7→14→30→60→120 days
  - Correct → streak +1, push next review
  - Wrong → reset streak to 0, due tomorrow

### PDF Context (`pdf_context.py`)

- Extracts text from: PDF (`pypdf`), DOCX (`python-docx`), TXT/MD/CSV/code files
- Max 60,000 chars per document, up to 3 files simultaneously
- Format: `[USER-ATTACHED DOCUMENT: filename] --- begin/end ---`

### OCR Fallback (`ocr.py`)

- Trigger: query mentions "read", "what does it say", "small text"
- Backend: `pytesseract` (Tesseract binary required)
- Extracts text from screenshot JPEG → injects into system prompt

### Code Mode (`code_mode.py`)

- IDE detection via regex (VS Code, Cursor, IntelliJ, PyCharm, Vim, etc.)
- Injects code-specialist prompt addendum
- Features: language identification, bug detection, anti-pattern spotting

### Lesson Recorder (`lesson_recorder.py`)

- MP4 screen recording at 8 FPS via `imageio-ffmpeg`
- Markdown transcript with timestamps
- Output: `~/Documents/Clicky Lessons/<timestamp>/lesson.mp4 + transcript.md`

### Multilingual (`multilang.py`)

- Language detection: script heuristics (Devanagari, Cyrillic, Han) + `langdetect`
- 15+ languages with Edge TTS voice mapping
- Injects language directive into system prompt (mandatory — reply entirely in detected language)
- `voice_for(code)` → switches TTS voice per language

### Workflow Capture (`workflow_capture.py`)

- Records clicks + keystrokes via `pynput` background listener
- Event format: `{"t": float, "kind": "click"|"key", "data": {...}}`
- `summarise()` → plain-text summary for LLM ingestion
- Replay intentionally stubbed (security concern)

### Live Collaboration (`collab.py`) — Skeleton

- WebRTC data-channel session (not yet shipped)
- 6-character session codes (e.g., "BLU-X4F")
- Needs: signalling server + `aiortc` implementation

---

## 10. Skills System (`skills/`)

User-extensible voice triggers that run **before** LLM call (same priority as built-in "stop"/"next"):

```python
SKILL = {
    "name": "open_calculator",
    "trigger": r"(open|launch|start) calculator",
    "description": "Opens Windows Calculator",
    "handler": open_calc,  # async fn(manager, transcript) -> str
}
```

- Skills loaded from `skills/` (bundled) + `~/.clicky/skills/` (user custom)
- Bundled examples: `example_self_mode.py`, `open_youtube.py`

---

## 11. Hotkey System (`hotkey.py`)

- **Push-to-talk**: `Ctrl+Alt+Space` (configurable via `CLICKY_HOTKEY` env). Uses `keyboard` library for system-wide hooks.
  - `on_press` → start recording (if IDLE)
  - `on_release` → stop recording + begin processing
- **Stop/Esc**: `keyboard.add_hotkey("esc", ...)` with `suppress=False` (other apps still receive Esc)
  - Cancels LLM stream flag + kills TTS playback mid-word

---

## 12. Build & Distribution

### PyInstaller (`clicky.spec`)

- `--onedir` mode (faster launch than `--onefile`)
- Explicitly lists all lazy-imported modules missed by static analysis
- Collates data from: faster-whisper, ctranslate2, edge-tts, all SDKs
- Excludes: matplotlib, scipy, pandas, tkinter (size optimization)
- No console window for release builds

### Build Script (`build.bat`)

1. Checks Python + installs PyInstaller
2. Generates icon if missing (`assets/make_icon.py`)
3. Cleans old `build/` and `dist/`
4. Runs PyInstaller (2-5 min)
5. Copies `.env.example`, `LICENSE`, `README.md` to dist folder
6. Optional: runs Inno Setup to create `Setup-Clicky.exe`

### Inno Setup (`installer.iss`)

- Version 1.1.2
- Optional: download + install Ollama during installation
- Desktop shortcut + startup launch options

---

## 13. Complete Feature List

| # | Feature | Implementation |
|---|---------|---------------|
| 1 | **Push-to-talk** | `AmbientListener` hotkey capture via `sounddevice` |
| 2 | **Wake word activation** | `faster-whisper tiny.en` on VAD segments, 12 phrase variants |
| 3 | **Screen capture** | `mss` multi-monitor + DPI metadata via `GetDpiForMonitor` |
| 4 | **Speech-to-text** (4 providers) | Deepgram, OpenAI Whisper, whisper.cpp, Faster-Whisper |
| 5 | **Text-to-speech** (3 providers) | Edge TTS (free), OpenAI TTS, ElevenLabs |
| 6 | **LLM** (5 providers) | Claude, OpenAI, Gemini, Copilot, Ollama |
| 7 | **Live model lists** | Vendor API + 30-day cache to local JSON files |
| 8 | **Copilot OAuth** | GitHub device flow + token caching + 25-min auto-refresh |
| 9 | **3-tier pointing** | UIA tree (~5ms) → RapidOCR/ONNX (~300ms) → vision grid (~1-3s) |
| 10 | **Web search grounding** | DuckDuckGo free or Tavily premium, page extraction, `[1]` citations |
| 11 | **Per-app conversation memory** | History dict keyed by `app_key` (window title), capped at 20 |
| 12 | **Knowledge journal** | SQLite + SM-2 spaced repetition (1→3→7→14→30→60→120 days) |
| 13 | **Drag-drop document context** | PDF/DOCX/TXT/MD extraction, max 60K chars, up to 3 attachments |
| 14 | **OCR fallback** | `pytesseract` for fine-print queries |
| 15 | **Code mode** | IDE window detection + code-specialist prompt |
| 16 | **Slow mode** | 1.7× longer flight + dwell for students |
| 17 | **Quiz mode** | Clicky asks you questions about what's on screen |
| 18 | **Privacy guard** | Skip screenshot for password/banking/login windows |
| 19 | **Multilingual** | `langdetect` + script heuristics + TTS voice switch, 15+ languages |
| 20 | **Lesson recording** | MP4 (8 FPS) + markdown transcript with timestamps |
| 21 | **Workflow capture** | Click/keystroke recording via `pynput`, summarized for LLM |
| 22 | **Whiteboard annotations** | ARROW, CIRCLE, UNDERLINE, LABEL tags with 8s TTL + fade |
| 23 | **Skills system** | User-defined voice triggers as Python modules |
| 24 | **Sleep/wake watchdog** | Monotonic timer → restart mic + asyncio on resume |
| 25 | **Setup wizard** | First-run Ollama install + model pull (3-step walkthrough) |
| 26 | **Diagnostics** | Config report saved to `%LOCALAPPDATA%\Clicky\diagnostics-*.txt` |
| 27 | **Esc cancel** | Kill LLM stream + TTS mid-word instantly |
| 28 | **Runtime provider switching** | Swap Claude/OpenAI/Gemini/Copilot/Ollama mid-session, ~1s |
| 29 | **TTS voice picker** | Change voice mid-session (Edge TTS / ElevenLabs) |
| 30 | **Ollama pull over HTTP** | Stream model download with progress callbacks |
| 31 | **Copilot billing labels** | Multiplier-based (free) / (N×) labels in model dropdown |
| 32 | **Identity question bypass** | Skip screenshot for "who is X" queries to avoid refusal |
| 33 | **Multi-monitor support** | Per-monitor `ScreenShot` with origin + DPI |
| 34 | **Auto-start Ollama daemon** | Launch `ollama serve` detached, wait up to 8s for readiness |
| 35 | **Live collaboration** | WebRTC data-channel skeleton (not yet shipped) |

---

## 14. Directory Layout

```
clicky-windows/
├── main.py                      # Entry point — boots Qt, wires all signals
├── companion_manager.py         # Async orchestrator + state machine (1177 lines)
├── config.py                    # Singleton Config — env vars, provider detection
├── tutor.py                     # Query classifiers, privacy guard, prompt builder
├── hotkey.py                    # Global hotkeys (Ctrl+Alt+Space + Esc)
│
├── ai/                          # LLM providers, pointing, search, model registry
│   ├── base_provider.py         # ABC for all LLM providers
│   ├── claude_provider.py       # Claude via anthropic SDK
│   ├── openai_provider.py       # OpenAI / GPT-4o
│   ├── gemini_provider.py       # Gemini via REST SSE
│   ├── ollama_provider.py       # Local Ollama via httpx
│   ├── github_copilot_provider.py  # Copilot OAuth + models
│   ├── hybrid_pointer.py        # 3-tier pointing (UIA → OCR → vision)
│   ├── element_locator.py       # Claude Computer Use pointing
│   ├── universal_locator.py     # Grid-based pointing for any LLM
│   ├── web_search.py            # DuckDuckGo + Tavily
│   ├── model_registry.py        # Live model lists + 30-day cache
│   ├── ollama_bootstrap.py      # Ollama install/detect/pull
│   └── ollama_models_registry.py  # Curated Ollama model recommendations
│
├── audio/                       # Capture, playback, STT, TTS
│   ├── ambient_listener.py      # Always-on mic, VAD, wake word
│   ├── capture.py               # Raw PCM capture via sounddevice
│   ├── playback.py              # Cancellable MP3 playback
│   ├── stt/                     # Speech-to-text providers
│   │   ├── deepgram_stt.py      # Deepgram API
│   │   ├── openai_stt.py        # OpenAI Whisper API
│   │   ├── whisper_cpp_stt.py   # Local whisper.cpp (GPU)
│   │   └── faster_whisper_stt.py # Local Faster-Whisper (CPU)
│   └── tts/                     # Text-to-speech providers
│       ├── edge_tts_provider.py  # Microsoft Edge TTS (free)
│       ├── openai_tts_provider.py # OpenAI TTS API
│       └── elevenlabs_provider.py # ElevenLabs API
│
├── screen/
│   └── capture.py               # Multi-monitor screenshot + DPI metadata
│
├── ui/                          # All UI components
│   ├── overlay.py               # Transparent cursor buddy (594 lines)
│   ├── panel.py                 # Floating companion chat panel
│   ├── tray.py                  # System tray menu (430 lines)
│   ├── design.py                # Colors, fonts, QSS variables
│   └── setup_wizard.py          # First-run Ollama setup (361 lines)
│
├── tutor_features/              # Feature modules
│   ├── journal.py               # SQLite knowledge journal + SM-2
│   ├── pdf_context.py           # PDF/DOCX/TXT extraction
│   ├── ocr.py                   # Tesseract OCR fallback
│   ├── code_mode.py             # IDE detection + prompt addendum
│   ├── lesson_recorder.py       # MP4 screen recording
│   ├── multilang.py             # Language detection + TTS voice map
│   ├── workflow_capture.py      # Click/keystroke recording
│   └── collab.py                # Live collaboration (skeleton)
│
├── skills/                      # User-extensible voice triggers
│   ├── example_self_mode.py     # Template for custom skills
│   └── open_youtube.py          # Example: open YouTube
│
├── assets/                      # Icon generator + demo GIF
├── .env / .env.example          # Configuration
├── requirements.txt             # Full dependencies
├── requirements-student.txt     # Free (no API keys) dependencies
├── clicky.spec                  # PyInstaller build spec
├── installer.iss                # Inno Setup installer script
├── build.bat                    # One-click build script
└── README.md / LICENSE / BUILD.md / SETUP.md / TESTING.md
```
