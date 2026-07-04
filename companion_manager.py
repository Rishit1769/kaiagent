"""
Central state machine for Kai Agent Windows.

Orchestrates:
  hotkey / wake-word â†’ ambient listener capture â†’ STT â†’ screen capture
  â†’ web search â†’ (optional Claude Computer Use pointing) â†’ LLM â†’ TTS
"""

import asyncio
import concurrent.futures
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, List, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import cfg
from ai.base_provider import BaseLLMProvider, Message
from audio.ambient_listener import AmbientListener
from audio.capture import pcm16_to_wav
from screen.capture import capture_all_screens, capture_primary
from ui.panel import AppState
from tutor import (
    active_window_title, app_key,
    is_locate, is_multistep, is_next, is_stop, is_sensitive_window,
    is_repeat, is_journal_today, is_journal_week, is_quiz_review,
    is_identity_question,
)
from tutor_features import (
    journal, pdf_context, ocr, code_mode, lesson_recorder,
    multilang, workflow_capture, collab,
)
import skills as skills_pkg


logger = logging.getLogger(__name__)
_preview_model_cache = None

PCM_BYTES_PER_SECOND = 16000 * 2
LIVE_TRANSCRIBE_POLL_S = 0.1
LIVE_TRANSCRIBE_CHUNK_S = 0.3
LIVE_TRANSCRIBE_CHUNK_BYTES = int(PCM_BYTES_PER_SECOND * LIVE_TRANSCRIBE_CHUNK_S)
LIVE_TRANSCRIBE_WINDOW_S = 2.0
LIVE_TRANSCRIBE_WINDOW_BYTES = int(PCM_BYTES_PER_SECOND * LIVE_TRANSCRIBE_WINDOW_S)
LIVE_TRANSCRIBE_DECODE_INTERVAL_S = 0.35
LIVE_TRANSCRIBE_FINAL_MIN_S = 0.35
LIVE_TRANSCRIBE_FINAL_MIN_BYTES = int(PCM_BYTES_PER_SECOND * LIVE_TRANSCRIBE_FINAL_MIN_S)

STT_TIMEOUT_S = 45.0
LOCATOR_TIMEOUT_S = 20.0
SEARCH_TIMEOUT_S = 15.0
LLM_FIRST_TOKEN_TIMEOUT_S = 30.0
TTS_TIMEOUT_S = 90.0


class SessionCancelled(RuntimeError):
    """Raised when a request session is superseded, cancelled, or no longer active."""


class RequestTimeout(RuntimeError):
    """Raised when a request phase exceeds its watchdog deadline."""


@dataclass
class RequestSession:
    request_id: int
    origin: str
    started_at: float
    phase: str = "created"
    transcript: str = ""
    screenshot: Any = None
    detected_point: Optional[tuple[int, int, str]] = None
    search_task: Optional[asyncio.Task] = None
    locate_task: Optional[asyncio.Task] = None
    stream_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    cancelled: bool = False
    overlay_engaged: bool = False
    cleanup_done: bool = False
    error: str = ""
    seen_tags: set[str] = field(default_factory=set)


def _get_preview_model():
    global _preview_model_cache
    if _preview_model_cache is None:
        from faster_whisper import WhisperModel

        _preview_model_cache = WhisperModel(
            cfg.whisper_model,
            device="cpu",
            compute_type="int8",
        )
    return _preview_model_cache


def _transcribe_preview_segments(pcm_bytes: bytes) -> list[str]:
    import os
    import tempfile

    if not pcm_bytes:
        return []
    model = _get_preview_model()
    silence_pad = bytes(int(PCM_BYTES_PER_SECOND * 0.2))
    wav_bytes = pcm16_to_wav(silence_pad + pcm_bytes + silence_pad)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        path = f.name
    try:
        segments, _ = model.transcribe(
            path,
            beam_size=1,
            language="en",
            condition_on_previous_text=False,
            vad_filter=True,
            no_speech_threshold=0.2,
            temperature=0.0,
        )
        out: list[str] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            print("TRANSCRIBED:", text)
            logger.info("TRANSCRIBED: %r", text)
            out.append(text)
        return out
    finally:
        os.unlink(path)


def _ensure_ollama_running():
    """Start Ollama if it isn't already running. Waits up to 8 s for it to be ready."""
    import subprocess
    import urllib.request

    url = "http://localhost:11434/api/tags"
    for _ in range(2):
        try:
            urllib.request.urlopen(url, timeout=2)
            return  # already up
        except Exception:
            pass

    # Not responding â€” launch it detached so it survives the Python process
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except FileNotFoundError:
        return  # ollama not installed, provider will fail gracefully

    # Wait up to 8 s for the server to come up
    for _ in range(16):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            pass


def _build_system_prompt(
    window_title: str = "",
    lesson_step: int = 0,
    total_steps: int = 0,
    quiz_mode: bool = False,
    detected_coord: Optional[tuple] = None,
    code_active: bool = False,
    language_code: str = "en",
    extra: str = "",
) -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    ctx_lines = [f"TODAY'S DATE: {today}."]
    if window_title:
        ctx_lines.append(f'ACTIVE WINDOW: "{window_title}"')
    if detected_coord:
        x, y, label = detected_coord
        ctx_lines.append(
            f"DETECTED ELEMENT (pre-computed by the pointing engine â€” use "
            f"this coordinate verbatim): x={x}, y={y}, label='{label}'."
        )
    if total_steps > 1:
        ctx_lines.append(
            f"LESSON PROGRESS: step {lesson_step + 1} of {total_steps}. "
            "Explain ONLY this step, then end with \"Say 'next' when ready.\""
        )

    # â”€â”€ Quiz mode: dominant prompt that completely replaces normal behaviour â”€â”€
    if quiz_mode:
        return f"""You are Kai Agent, an interactive QUIZ TUTOR. The user has
turned on Quiz Mode and wants to be tested, NOT explained to.

{chr(10).join(ctx_lines)}

ABSOLUTE QUIZ RULES (override everything else):
  â€¢ NEVER answer the user's question directly. NEVER point at UI elements.
    NEVER emit [POINT:...] tags. NEVER explain how things work.
  â€¢ If the user is greeting / starting ("hello", "what's on my screen", "begin",
    "quiz me", anything), START the quiz: ask ONE short, specific question
    about what's visible on screen â€” name a button, recognise an icon, predict
    what a click would do, identify the active app, etc.
  â€¢ If the user's last message looks like an ANSWER (a noun, a short phrase, a
    yes/no), evaluate it in â‰¤1 sentence ("Correct!" / "Close â€” actually..."),
    then immediately ask the NEXT question.
  â€¢ Questions should be progressively harder. Vary topic across UI literacy,
    keyboard shortcuts, what's currently visible, predicting outcomes.
  â€¢ Keep it warm and encouraging. Never lecture.
  â€¢ Format every turn as:  <one-line evaluation if applicable>  <one question>

STYLE: short, friendly, never more than 2 sentences. End every turn with a
question mark."""

    return f"""You are Kai Agent, a VISUAL AI tutor running on Windows. You live
next to the user's cursor. Your job is to *show*, not just tell.

{chr(10).join(ctx_lines)}

HARD RULES (never break):
  1. LOCATE QUESTIONS ("where is X", "how do I click Y", "show me X", "find X"):
     â€¢ If a DETECTED ELEMENT coordinate is provided above, emit EXACTLY ONE tag
       [POINT:x,y:label:screen1]  using those coordinates and a 1-3 word label.
       Follow with ONE sentence explaining what it is. Nothing else.
     â€¢ If no coordinate is provided AND you can see the element in the screenshot,
       emit [POINT:x,y:label:screen1] at your best-guess pixel coordinates.
     â€¢ If the element is NOT visible, say plainly: "I don't see X on this page â€”
       you're looking at [describe actual page]. Want me to help you get there?"
       DO NOT invent generic directions like "click the search bar at the top".

  2. MULTI-STEP TASKS (export, install, configure, setup, etc.):
     Describe ONLY the next single step. Point at it. End with "Say 'next' when
     ready." Never dump a numbered list of 5 steps in one response.

  3. VISION: describe only what is ACTUALLY in the screenshot. The user said
     something, but trust your eyes over their words. If they say "YouTube" and
     the screen shows Google, tell them so.

  4. WEB SEARCH: when [Web Search Results] appear in the system prompt, you MUST
     use them as your primary source. Give a DIRECT, SPECIFIC answer â€” never say
     "I don't know" or list vague options if the results contain real names,
     rankings, or facts. Commit to what the search found. Cite like [1], [2].
     Today is {today}. Your training data is stale â€” always prefer search results
     over your own memory for anything recent (news, rankings, current events,
     "who is", "what is the best", "latest", "top", etc.).

  5. PUBLIC figures, celebrities, YouTubers, athletes, politicians, companies,
     products, brands â€” ANSWER FREELY using your training data + search results.
     NEVER refuse with "I can't identify people" / "I can't help with that" /
     "personal or sensitive". The user is asking a tutor question, not running
     facial recognition â€” these are public figures with public Wikipedia pages.
     If asked "who is MrBeast" â€” say "MrBeast (Jimmy Donaldson) is an American
     YouTuber known forâ€¦". Same for any other public person.

  6. ANNOTATE for emphasis: when teaching where multiple things matter, you
     MAY emit annotation tags (in addition to one POINT tag):
       [ARROW:x1,y1->x2,y2]            line with arrowhead
       [CIRCLE:x,y,r:label]            ring around an area
       [UNDERLINE:x,y,width]           underline a word
       [LABEL:x,y:short text]          floating caption
     Use sparingly â€” at most 2 annotations per response.

STYLE: warm, concise, teacher-y. 1-2 sentences per step. No markdown bullets
unless genuinely listing options.{_code_addendum(code_active)}{_lang_addendum(language_code)}{extra}"""


def _code_addendum(active: bool) -> str:
    if not active:
        return ""
    from tutor_features.code_mode import code_system_prompt_addendum
    return code_system_prompt_addendum()


def _lang_addendum(code: str) -> str:
    from tutor_features.multilang import language_directive
    return language_directive(code)


def _guess_label(transcript: str) -> str:
    """Extract a 1-3 word label from a locate query for the speech bubble.
       'where is the search bar' â†’ 'search bar' """
    t = transcript.lower().strip().rstrip("?.!")
    for kw in ("where is the ", "where's the ", "show me the ",
              "find the ", "locate the ", "click the ", "click on the ",
              "how do i click ", "how do i find ", "how do i open ",
              "point at the ", "point to the ", "highlight the "):
        if kw in t:
            tail = t.split(kw, 1)[1]
            words = tail.split()
            return " ".join(words[:3]) or "here"
    return "right here!"


def _split_steps(text: str) -> list[str]:
    """Parse a numbered list out of an LLM response. Returns [] if not a list."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    steps = []
    for ln in lines:
        m = re.match(r"^(?:\d+[\).]|[-*])\s+(.+)$", ln)
        if m:
            steps.append(m.group(1).strip())
    return steps


POINT_RE = re.compile(r'\[POINT:(\d+),(\d+):([^:\]]+):screen(\d+)\]')
# A partial "[POINT..." prefix that hasn't closed yet â€” hold it back from display
# until the next chunk so we never leak a half tag.
POINT_PARTIAL_RE = re.compile(r'\[(?:P|PO|POI|POIN|POINT|POINT:[^\]]*)?$')

# Whiteboard annotation tags â€” same idea as POINT, parsed and stripped.
ARROW_RE     = re.compile(r'\[ARROW:(\d+),(\d+)->(\d+),(\d+)\]')
CIRCLE_RE    = re.compile(r'\[CIRCLE:(\d+),(\d+),(\d+):([^\]]+)\]')
UNDERLINE_RE = re.compile(r'\[UNDERLINE:(\d+),(\d+),(\d+)\]')
LABEL_RE     = re.compile(r'\[LABEL:(\d+),(\d+):([^\]]+)\]')
ANY_TAG_RE   = re.compile(
    r'\[(?:POINT|ARROW|CIRCLE|UNDERLINE|LABEL):[^\]]*\]'
)
ANY_PARTIAL_RE = re.compile(r'\[[A-Z]{0,9}(?::[^\]]*)?$')

# Questions that ask Kai Agent to locate / click UI elements â€” triggers the
# Computer Use element locator when Claude is the provider.
POINT_TRIGGER_RE = re.compile(
    r"\b(where\s+(is|do|can)|how\s+do\s+i\s+(click|find|open|access|use)|"
    r"point\s+(at|to)|show\s+me\s+(the|where)|click\s+(the|on)|find\s+the)\b",
    re.IGNORECASE,
)


class CompanionManager(QObject):
    """Thread-safe signals for Qt UI updates from async/audio threads."""

    sig_state_changed       = pyqtSignal(object)          # AppState
    sig_capture_started     = pyqtSignal()
    sig_transcription_text  = pyqtSignal(str)
    sig_transcription_final = pyqtSignal(str)
    sig_transcription_error = pyqtSignal(str)
    sig_response_reset      = pyqtSignal()
    sig_response_chunk      = pyqtSignal(str)
    sig_response_done       = pyqtSignal(str)
    sig_audio_level         = pyqtSignal(float)
    sig_point_at            = pyqtSignal(float, float, str)
    sig_point_hold          = pyqtSignal(bool)            # True â†’ dwell forever until release
    sig_point_release       = pyqtSignal()                # end dwell + fly buddy back
    sig_error               = pyqtSignal(str)
    sig_copilot_models_done = pyqtSignal(int)             # arg = model count
    sig_models_refreshed    = pyqtSignal(str, int)        # (provider, count)
    sig_ollama_models       = pyqtSignal(dict)            # {"vision": [...], "text": [...]}
    sig_ollama_pull_status  = pyqtSignal(str, str)        # (model_name, status_msg)
    sig_arrow               = pyqtSignal(float, float, float, float)
    sig_circle              = pyqtSignal(float, float, float)
    sig_underline           = pyqtSignal(float, float, float)
    sig_label               = pyqtSignal(float, float, str)
    sig_recording_state     = pyqtSignal(bool, str)       # (is_recording, output_dir)
    sig_overlay_begin_request = pyqtSignal()
    sig_overlay_release_request = pyqtSignal()
    sig_overlay_reset = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._state: AppState = AppState.IDLE
        self._history: List[Message] = []
        self._current_model: Optional[str] = None
        self._web_search_enabled = True
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Providers (lazy)
        self._llm: Optional[BaseLLMProvider] = None
        self._stt = None
        self._tts = None

        # Current in-flight generation â€” tracked so Esc / stop can cancel
        self._current_task: Optional[concurrent.futures.Future] = None
        self._request_counter = 0
        self._active_session: Optional[RequestSession] = None
        self._cancel_flag = False  # legacy compatibility for older helper paths
        self._live_transcription_stop = threading.Event()
        self._live_transcription_thread: Optional[threading.Thread] = None
        self._live_transcription_text = ""
        self._live_transcription_stable_text = ""
        self._live_transcription_live_text = ""
        self._live_transcription_last_decode_at = 0.0
        self._live_transcription_processed_bytes = 0

        # Per-app memory: { window_title: [Message, ...] }
        self._app_memory: dict[str, List[Message]] = {}
        # Current lesson: sequence of pending steps for multi-step tutorials
        self._lesson_steps: list[str] = []
        self._lesson_step_idx: int = 0
        # Toggles
        self._slow_mode = False
        self._quiz_mode = False
        self._privacy_guard = True
        self._code_mode_auto = True       # auto-detect IDE windows
        self._multilang = True             # auto-reply in user's language
        self._journal_enabled = True       # log every Q&A to SQLite
        self._ocr_enabled = True           # use Tesseract for fine print
        self._last_response = ""           # for "say it again" voice command
        self._attached_docs: list[tuple[str, str]] = []   # (filename, text)

        # Optional subsystems (lazy-init to keep startup fast)
        self._recorder: Optional[lesson_recorder.LessonRecorder] = None
        self._collab: Optional[collab.CollabSession] = None
        self._workflow: Optional[workflow_capture.WorkflowCapture] = None

        # Load user-created skills from skills/ + ~/.kai_agent/skills/
        try:
            skills_pkg.load_all()
        except Exception:
            pass

        # Always-on ambient listener
        self._listener = AmbientListener(
            on_level=self._handle_level,
            on_wake=self._handle_wake,
        )

        # Background asyncio loop
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    # â”€â”€ Lifecycle â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start(self):
        try:
            self._listener.start()
        except Exception as e:
            self.sig_error.emit(f"Mic error: {e}")
        # Sleep/wake watchdog â€” restarts mic + loop after system resume
        self._start_sleep_watchdog()
        # On startup, refresh any stale model cache in the background.
        # 30-day TTL means this is a once-a-month no-op for most launches.
        self._submit(self._refresh_stale_models())

    async def _refresh_stale_models(self):
        try:
            from ai.model_registry import refresh_all_stale
            results = await refresh_all_stale()
            for prov, count in results.items():
                if count > 0:
                    self.sig_models_refreshed.emit(prov, count)
        except Exception:
            pass   # silent â€” not user-facing on startup

    def shutdown(self):
        # Kill any audio that was playing when the user clicked Quit
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        self._listener.stop()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # â”€â”€ Sleep/wake watchdog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _start_sleep_watchdog(self):
        """Background thread that detects system resume after sleep/hibernate
        and restarts the mic stream + asyncio loop so the panel stays live."""
        def _watch():
            HEARTBEAT = 5.0          # check every 5 s
            DRIFT_THRESHOLD = 15.0   # if we wake and >15 s have passed, resume occurred
            last_tick = time.monotonic()
            while True:
                time.sleep(HEARTBEAT)
                now = time.monotonic()
                drift = now - last_tick - HEARTBEAT
                last_tick = now
                if drift > DRIFT_THRESHOLD:
                    # System was sleeping â€” restart subsystems
                    self._on_system_resume()

        t = threading.Thread(target=_watch, daemon=True)
        t.start()

    def _on_system_resume(self):
        """Called automatically after the laptop wakes from sleep."""
        # 1. Restart the mic stream (sounddevice handles become stale on resume)
        try:
            self._listener.stop()
        except Exception:
            pass
        time.sleep(1.0)   # give Windows audio stack time to reinit
        try:
            self._listener.start()
        except Exception as e:
            self.sig_error.emit(f"Mic restart after sleep failed: {e}")

        # 2. If the asyncio loop thread died, restart it
        if not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

        # 3. Reset state to IDLE so the panel shows the correct status
        if self._state != AppState.IDLE:
            self._emit_state(AppState.IDLE)
        self.sig_overlay_reset.emit()

    def _submit(self, coro: Awaitable[Any]) -> Optional[concurrent.futures.Future]:
        if not self._loop or not self._loop.is_running():
            logger.warning("Refusing to submit coroutine because the asyncio loop is unavailable")
            self.sig_error.emit("Background request loop unavailable. Please try again.")
            try:
                coro.close()
            except Exception:
                pass
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def _log_done(done: concurrent.futures.Future):
            try:
                done.result()
            except concurrent.futures.CancelledError:
                logger.info("Background future cancelled")
            except Exception:
                logger.exception("Background future failed")

        future.add_done_callback(_log_done)
        return future

    def _next_request_id(self) -> int:
        self._request_counter += 1
        return self._request_counter

    def _start_request_session(self, origin: str) -> Optional[RequestSession]:
        if self._active_session and not self._active_session.cleanup_done:
            logger.warning(
                "Rejecting new request while another session is active req=%s phase=%s origin=%s",
                self._active_session.request_id,
                self._active_session.phase,
                origin,
            )
            return None
        session = RequestSession(
            request_id=self._next_request_id(),
            origin=origin,
            started_at=time.monotonic(),
        )
        self._active_session = session
        self._log_session(session, "session started")
        return session

    def _log_session(self, session: RequestSession, message: str, **extra: Any) -> None:
        payload = {
            "req": session.request_id,
            "origin": session.origin,
            "phase": session.phase,
            "overlay": session.overlay_engaged,
            "elapsed_ms": int((time.monotonic() - session.started_at) * 1000),
            "provider": cfg.llm_provider(),
        }
        payload.update(extra)
        detail = " ".join(f"{k}={v}" for k, v in payload.items())
        logger.info("[request] %s %s", message, detail)

    def _transition_session(self, session: RequestSession, phase: str, **extra: Any) -> None:
        session.phase = phase
        self._log_session(session, "phase transition", **extra)

    def _is_active_session(self, session: Optional[RequestSession]) -> bool:
        return bool(
            session
            and self._active_session is session
            and not session.cleanup_done
            and not session.cancelled
        )

    def _assert_active_session(self, session: RequestSession) -> None:
        if not self._is_active_session(session):
            raise SessionCancelled("request session is no longer active")

    def _mark_session_cancelled(self, session: RequestSession, reason: str) -> None:
        if session.cancelled:
            return
        session.cancelled = True
        session.error = reason
        self._transition_session(session, "cancelled", reason=reason)

    async def _await_with_timeout(
        self,
        session: RequestSession,
        awaitable: Awaitable[Any],
        timeout_s: float,
        phase: str,
        timeout_message: str,
    ) -> Any:
        self._assert_active_session(session)
        try:
            result = await asyncio.wait_for(awaitable, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise RequestTimeout(timeout_message) from exc
        self._assert_active_session(session)
        self._transition_session(session, phase)
        return result

    def _cancel_session_tasks(self, session: RequestSession) -> None:
        for task in (session.search_task, session.locate_task, session.stream_task, session.tts_task):
            if task and not task.done():
                task.cancel()

    def _engage_overlay(self, session: RequestSession) -> None:
        if session.overlay_engaged:
            return
        session.overlay_engaged = True
        self.sig_overlay_begin_request.emit()
        self._log_session(session, "overlay engaged")

    def _capture_request_screenshot(self, session: RequestSession) -> tuple[list[Any], list[str]]:
        """Capture a fresh screenshot for this request and clear any stale one."""
        self._assert_active_session(session)
        session.screenshot = None
        shot = capture_primary()
        if not shot:
            return [], []
        session.screenshot = shot
        return [shot], [shot.base64_jpeg]

    def _history_for_request(self, ak: str, screenshots_b64: list[str]) -> list[Message]:
        """Prefer the live screenshot over stale per-app visual context."""
        history = self._app_memory.setdefault(ak, [])
        if screenshots_b64:
            return []
        return history

    def _show_detected_point(self, session: RequestSession, x: float, y: float, label: str) -> None:
        self._assert_active_session(session)
        self._engage_overlay(session)
        session.detected_point = (int(x), int(y), label)
        self.sig_point_hold.emit(True)
        self.sig_point_at.emit(float(x), float(y), label)
        self._transition_session(session, "pointing_ready", label=label, x=int(x), y=int(y))

    def _finish_request_session(
        self,
        session: Optional[RequestSession],
        *,
        final_phase: str,
        error: str = "",
    ) -> None:
        if session is None or session.cleanup_done:
            return
        session.cleanup_done = True
        if error:
            session.error = error
        self._cancel_session_tasks(session)
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        tts = self._tts
        if tts and hasattr(tts, "stop"):
            try:
                tts.stop()
            except Exception:
                pass
        self.sig_overlay_release_request.emit()
        self.sig_overlay_reset.emit()
        self._transition_session(session, final_phase, error=error or "")
        if self._active_session is session:
            self._active_session = None
        self._current_task = None
        self._emit_state(AppState.IDLE)

    # â”€â”€ Provider lazy init â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _get_llm(self) -> BaseLLMProvider:
        if self._llm is None:
            provider = cfg.llm_provider()
            if provider == "claude":
                from ai.claude_provider import ClaudeProvider
                self._llm = ClaudeProvider()
            elif provider == "openai":
                from ai.openai_provider import OpenAIProvider
                self._llm = OpenAIProvider()
            elif provider == "gemini":
                from ai.gemini_provider import GeminiProvider
                self._llm = GeminiProvider()
            elif provider == "copilot":
                from ai.github_copilot_provider import GitHubCopilotProvider
                self._llm = GitHubCopilotProvider()
            else:
                _ensure_ollama_running()
                from ai.ollama_provider import OllamaProvider
                self._llm = OllamaProvider()
        return self._llm

    def _get_stt(self):
        if self._stt is None:
            provider = cfg.stt_provider()
            if provider == "deepgram":
                from audio.stt.deepgram_stt import DeepgramSTT
                self._stt = DeepgramSTT()
            elif provider == "openai":
                from audio.stt.openai_stt import OpenAISTT
                self._stt = OpenAISTT()
            elif provider == "whisper_cpp":
                try:
                    from audio.stt.whisper_cpp_stt import WhisperCppSTT
                    self._stt = WhisperCppSTT()
                except ImportError:
                    # pywhispercpp missing â†’ fall back silently
                    from audio.stt.faster_whisper_stt import FasterWhisperSTT
                    self._stt = FasterWhisperSTT()
            else:
                from audio.stt.faster_whisper_stt import FasterWhisperSTT
                self._stt = FasterWhisperSTT()
        return self._stt

    def _get_tts(self):
        if self._tts is None:
            provider = cfg.tts_provider()
            if provider == "elevenlabs":
                from audio.tts.elevenlabs_provider import ElevenLabsProvider
                self._tts = ElevenLabsProvider()
            elif provider == "openai":
                from audio.tts.openai_tts_provider import OpenAITTSProvider
                self._tts = OpenAITTSProvider()
            else:
                from audio.tts.edge_tts_provider import EdgeTTSProvider
                self._tts = EdgeTTSProvider()
        return self._tts

    # â”€â”€ Input sources â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def on_hotkey_press(self):
        logger.info("Hotkey triggered: press")
        session = self._start_request_session("hotkey")
        if session is None:
            return
        self._begin_capture(session)

    def on_hotkey_release(self):
        session = self._active_session
        if session and self._state == AppState.LISTENING:
            logger.info("Hotkey triggered: release")
            fut = self._submit(self._run_request_session_pipeline(session))
            if fut is not None:
                self._current_task = fut

    def _handle_wake(self):
        """Triggered from ambient listener when wake-word is detected."""
        session = self._start_request_session("wake")
        if session is None:
            return
        self._begin_capture(session)
        fut = self._submit(self._auto_stop_after_pause(session))
        if fut is not None:
            self._current_task = fut

    def _handle_level(self, rms: float):
        try:
            self.sig_audio_level.emit(rms)
        except Exception:
            pass   # never crash the sounddevice audio thread

    def _start_live_transcription(self):
        logger.info("Starting live transcription thread")
        self._live_transcription_stop.set()
        if self._live_transcription_thread and self._live_transcription_thread.is_alive():
            self._live_transcription_thread.join(timeout=0.2)
        self._live_transcription_text = ""
        self._live_transcription_stable_text = ""
        self._live_transcription_live_text = ""
        self._live_transcription_last_decode_at = 0.0
        self._live_transcription_processed_bytes = 0
        self._live_transcription_stop = threading.Event()
        self._live_transcription_thread = threading.Thread(
            target=self._live_transcription_worker,
            name="kai-live-transcription",
            daemon=True,
        )
        self._live_transcription_thread.start()

    def _stop_live_transcription(self, final_pcm: bytes | None = None):
        logger.info("Stopping live transcription thread")
        self._live_transcription_stop.set()
        thread = self._live_transcription_thread
        if thread and thread.is_alive():
            thread.join(timeout=0.5)
        self._live_transcription_thread = None
        if final_pcm and len(final_pcm) >= LIVE_TRANSCRIBE_FINAL_MIN_BYTES:
            try:
                window_pcm = final_pcm[-LIVE_TRANSCRIBE_WINDOW_BYTES:]
                if len(window_pcm) >= LIVE_TRANSCRIBE_FINAL_MIN_BYTES:
                    tail_segments = _transcribe_preview_segments(window_pcm)
                    if tail_segments:
                        stable_candidate = " ".join(tail_segments[:-1]).strip()
                        live_candidate = tail_segments[-1].strip()
                        self._live_transcription_stable_text = self._merge_preview_text(
                            self._live_transcription_stable_text,
                            stable_candidate,
                        )
                        self._live_transcription_live_text = live_candidate
                        self._live_transcription_text = self._compose_preview_text(
                            self._live_transcription_stable_text,
                            self._live_transcription_live_text,
                        )
                final_text = self._live_transcription_text.strip()
                if final_text:
                    logger.info("Final live transcription preview emitted: %r", final_text)
                    print("EMITTING:", final_text)
                    self.sig_transcription_text.emit(final_text)
            except Exception as e:
                logger.debug("Final live transcription preview failed: %s", e)

    def _live_transcription_worker(self):
        consumed_bytes = 0
        rolling_pcm = bytearray()
        while not self._live_transcription_stop.is_set():
            try:
                pcm = self._listener.snapshot_recording()
                if len(pcm) <= consumed_bytes:
                    time.sleep(LIVE_TRANSCRIBE_POLL_S)
                    continue
                new_pcm = pcm[consumed_bytes:]
                consumed_bytes = len(pcm)
                rolling_pcm.extend(new_pcm)
                if len(rolling_pcm) > LIVE_TRANSCRIBE_WINDOW_BYTES:
                    del rolling_pcm[:-LIVE_TRANSCRIBE_WINDOW_BYTES]
                print("chunk captured", len(new_pcm))
                buffer_duration = len(rolling_pcm) / PCM_BYTES_PER_SECOND
                print("buffer duration", round(buffer_duration, 2))
                logger.info(
                    "Audio chunk captured: %s bytes rolling=%s duration=%.2fs",
                    len(new_pcm),
                    len(rolling_pcm),
                    buffer_duration,
                )
                if len(rolling_pcm) < LIVE_TRANSCRIBE_CHUNK_BYTES:
                    time.sleep(LIVE_TRANSCRIBE_POLL_S)
                    continue
                now = time.monotonic()
                if (
                    self._live_transcription_last_decode_at
                    and now - self._live_transcription_last_decode_at < LIVE_TRANSCRIBE_DECODE_INTERVAL_S
                ):
                    time.sleep(LIVE_TRANSCRIBE_POLL_S)
                    continue
                self._live_transcription_last_decode_at = now
                window_pcm = bytes(rolling_pcm)
                logger.info("Processing live transcription window: %s bytes", len(window_pcm))
                segments = _transcribe_preview_segments(window_pcm)
                logger.info("Chunk processed into %s segment(s)", len(segments))
                print("segments count", len(segments))
                self._live_transcription_processed_bytes = consumed_bytes
                if not segments:
                    continue
                stable_candidate = " ".join(segments[:-1]).strip()
                live_candidate = segments[-1].strip()
                print("transcribed chunk", live_candidate)
                merged_stable = self._merge_preview_text(
                    self._live_transcription_stable_text,
                    stable_candidate,
                )
                merged_text = self._compose_preview_text(merged_stable, live_candidate)
                if merged_text and merged_text != self._live_transcription_text:
                    self._live_transcription_stable_text = merged_stable
                    self._live_transcription_live_text = live_candidate
                    self._live_transcription_text = merged_text
                    print("EMITTING:", merged_text)
                    logger.info("Text emitted to UI: %r", merged_text)
                    self.sig_transcription_text.emit(merged_text)
            except Exception as e:
                logger.debug("Live transcription worker error: %s", e)
                time.sleep(LIVE_TRANSCRIBE_POLL_S)
                continue
            time.sleep(LIVE_TRANSCRIBE_POLL_S)

    @staticmethod
    def _merge_preview_text(previous: str, latest: str) -> str:
        previous = previous.strip()
        latest = latest.strip()
        if not previous:
            return latest
        if not latest:
            return previous
        if latest.startswith(previous):
            return latest
        max_overlap = min(len(previous), len(latest))
        for overlap in range(max_overlap, 0, -1):
            if previous.endswith(latest[:overlap]):
                return previous + latest[overlap:]
        return latest if len(latest) >= len(previous) else previous

    @staticmethod
    def _compose_preview_text(stable_text: str, live_text: str) -> str:
        stable_text = stable_text.strip()
        live_text = live_text.strip()
        if stable_text and live_text:
            return f"{stable_text} {live_text}".strip()
        return stable_text or live_text

    # â”€â”€ Capture flow â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _begin_capture(self, session: RequestSession):
        if not self._listener.audio_available:
            logger.warning("Mic start failed: audio backend unavailable")
            self.sig_transcription_error.emit("Mic not available")
            self._finish_request_session(session, final_phase="failed", error="Mic not available")
            return
        self._assert_active_session(session)
        logger.info("Capture begin: showing cursor overlay near cursor")
        self._engage_overlay(session)
        self.sig_capture_started.emit()
        self.sig_transcription_text.emit("")
        self._listener.start_recording()
        print("mic started")
        logger.info("Mic started")
        if os.environ.get("KAI_AGENT_DEBUG_FAKE_TRANSCRIPTION", "").strip().lower() in {"1", "true", "yes"}:
            print("EMITTING:", "hello testing")
            self.sig_transcription_text.emit("hello testing")
        self._start_live_transcription()
        self._emit_state(AppState.LISTENING)
        self._transition_session(session, "capturing")

    async def _auto_stop_after_pause(self, session: RequestSession):
        """When triggered by wake word, wait for user to finish speaking."""
        import time
        max_total_s = 10.0
        start_t = time.monotonic()
        while self._state == AppState.LISTENING and self._is_active_session(session):
            await asyncio.sleep(0.15)
            if time.monotonic() - start_t > max_total_s:
                break
        if self._is_active_session(session):
            await self._run_request_session_pipeline(session)

    async def _end_capture_and_process(self):
        logger.info("Mic stopped")
        pcm = self._listener.stop_recording()
        self._stop_live_transcription(final_pcm=pcm)
        if len(pcm) < 3200:  # < 0.1s of audio â€” ignore
            self.sig_transcription_final.emit("")
            self._emit_state(AppState.IDLE)
            return

        self._emit_state(AppState.THINKING)
        pointing_held = False  # track whether we told overlay to hold dwell

        try:
            # 1. Transcribe
            try:
                transcript = await self._get_stt().transcribe(pcm)
            except Exception as e:
                self.sig_transcription_error.emit(str(e))
                raise
            logger.info("Final transcription received: %r", transcript)
            if transcript.strip():
                print("EMITTING:", transcript)
                self.sig_transcription_text.emit(transcript)
            self.sig_transcription_final.emit(transcript)
            if not transcript.strip():
                self._emit_state(AppState.IDLE)
                return

            # â”€â”€ Voice commands â€” short-circuit before LLM â”€â”€
            if is_stop(transcript):
                self.stop()
                return

            title = active_window_title()
            ak = app_key(title)

            if is_next(transcript) and self._lesson_steps:
                await self._advance_lesson_step(ak)
                return

            # "say it again" â€” replay the last response without a new LLM call
            if is_repeat(transcript) and self._last_response:
                self.sig_response_reset.emit()
                self.sig_response_chunk.emit(self._last_response)
                self.sig_response_done.emit(self._last_response)
                self._emit_state(AppState.SPEAKING)
                try:
                    await self._get_tts().speak(self._last_response)
                except Exception:
                    pass
                self._emit_state(AppState.IDLE)
                return

            # Journal voice queries â€” answered locally, no LLM call needed
            if is_journal_today(transcript):
                msg = journal.summarise(journal.entries_today(),
                                        "Here's what you asked about today:\n")
                await self._reply_local(msg)
                return
            if is_journal_week(transcript):
                msg = journal.summarise(journal.entries_this_week(),
                                        "Here's the past week:\n")
                await self._reply_local(msg)
                return
            if is_quiz_review(transcript):
                await self._spaced_review()
                return

            # User-created skills (run BEFORE the LLM, like built-ins above)
            try:
                skill = skills_pkg.match(transcript)
                if skill:
                    msg = await skill["handler"](self, transcript)
                    if msg:
                        await self._reply_local(msg)
                    return
            except Exception as e:
                self.sig_error.emit(f"Skill error: {e}")

            # 2. Screen capture â€” skipped if sensitive window (password manager etc.)
            #
            # ALSO skipped for "who is X" / "tell me about X" identity questions:
            # OpenAI + Claude refuse to identify people in screenshots even when
            # the answer is in their training data ("Sorry I can't identify the
            # person in images"). Stripping the screenshot lets the LLM answer
            # from training data + web search instead, which is what the user
            # actually wants when they ask "who is MrBeast" while on YouTube.
            sensitive = self._privacy_guard and is_sensitive_window(title)
            identity_q = is_identity_question(transcript)
            if sensitive or identity_q:
                screenshots = []
                images_b64 = []
            else:
                shot = capture_primary()
                screenshots = [shot] if shot else []
                images_b64 = [shot.base64_jpeg] if shot else []

            # 3. Parallel side-work: web search + element locator
            #
            # Pointing now works for EVERY provider:
            #   â€¢ If ANTHROPIC_API_KEY is set â†’ use Claude Computer Use
            #     (~5px accuracy, gold standard).
            #   â€¢ Otherwise â†’ universal grid-based locator with the active
            #     vision LLM (Copilot GPT-4o, OpenAI, Gemini, Ollama llava).
            #     ~25-50px accuracy. Good enough for buttons/menus/icons.
            locate_triggered = is_locate(transcript)
            multistep = is_multistep(transcript)

            search_task = None
            locate_task = None
            if self._web_search_enabled:
                from ai.web_search import search
                search_task = asyncio.create_task(search(transcript))

            if screenshots and locate_triggered:
                shot = screenshots[0]
                # Pointing accuracy upgrade: try the hybrid pointer first.
                # Tier 1 (UIA tree) is ~5ms and pixel-perfect; tier 2 (OCR)
                # handles canvas apps. Falls through to the vision LLM grid
                # below only when both whiff.
                try:
                    from ai.hybrid_pointer import find_target as _hybrid_find
                    target = _hybrid_find(
                        transcript,
                        screenshot=shot,
                        llm_provider=self._get_llm(),
                    )
                except Exception:
                    target = None

                if target is not None and target.source in ("uia", "ocr"):
                    async def _ready(t=target):
                        return (t.x, t.y)
                    locate_task = asyncio.create_task(_ready())
                elif cfg.anthropic_api_key:
                    # Path A â€” Anthropic Computer Use (best accuracy)
                    from ai.element_locator import detect_element
                    locate_task = asyncio.create_task(detect_element(
                        screenshot_jpeg_b64=shot.base64_jpeg,
                        original_width=shot.width,
                        original_height=shot.height,
                        physical_width=shot.physical_width,
                        physical_height=shot.physical_height,
                        physical_left=shot.physical_left,
                        physical_top=shot.physical_top,
                        dpi_scale=shot.dpi_scale,
                        screen_index=shot.index,
                        user_question=transcript,
                    ))
                else:
                    # Path B â€” Universal grid locator (any vision LLM)
                    try:
                        from ai.universal_locator import detect_element_universal
                        llm = self._get_llm()
                        locate_task = asyncio.create_task(detect_element_universal(
                            llm=llm,
                            screenshot_jpeg_b64=shot.base64_jpeg,
                            original_width=shot.width,
                            original_height=shot.height,
                            physical_width=shot.physical_width,
                            physical_height=shot.physical_height,
                            physical_left=shot.physical_left,
                            physical_top=shot.physical_top,
                            dpi_scale=shot.dpi_scale,
                            screen_index=shot.index,
                            user_question=transcript,
                            model=self._current_model,
                        ))
                    except Exception:
                        # Universal locator should never crash the main flow
                        locate_task = None

            search_results = ""
            if search_task:
                try:
                    search_results = await search_task or ""
                except Exception:
                    search_results = ""

            detected = None
            detected_coord = None
            if locate_task:
                try:
                    detected = await locate_task
                except Exception:
                    detected = None
            if detected:
                # Short label guess â€” first noun phrase after "the"/"where"
                label = _guess_label(transcript)
                detected_coord = (int(detected.x), int(detected.y), label)
                # Fire the overlay NOW so the buddy flies over while the LLM
                # still thinks. Hold dwell until TTS completes.
                self.sig_point_hold.emit(True)
                pointing_held = True
                self.sig_point_at.emit(
                    float(detected.x), float(detected.y), label,
                )

            # â”€â”€ Per-turn enrichment: code mode, language, OCR, attached docs â”€â”€
            code_active = self._code_mode_auto and code_mode.is_code_window(title)
            lang_code = (multilang.detect_language(transcript)
                         if self._multilang else "en")

            # OCR fallback for fine print (only if user actually asks to read)
            ocr_extra = ""
            if self._ocr_enabled and screenshots and ocr.needs_ocr(transcript):
                try:
                    import base64
                    jpeg = base64.b64decode(screenshots[0].base64_jpeg)
                    txt = ocr.run_ocr(jpeg)
                    if txt:
                        ocr_extra = ocr.format_for_prompt(txt)
                except Exception:
                    pass

            # Attached documents (drag-dropped PDFs etc.)
            doc_extra = ""
            for fname, text in self._attached_docs:
                doc_extra += pdf_context.format_for_prompt(fname, text)

            # 4. Build system prompt with all context
            system = _build_system_prompt(
                window_title=title,
                lesson_step=self._lesson_step_idx,
                total_steps=len(self._lesson_steps),
                quiz_mode=self._quiz_mode,
                detected_coord=detected_coord,
                code_active=code_active,
                language_code=lang_code,
                extra=ocr_extra + doc_extra,
            )
            if sensitive:
                system += (
                    "\n\nPRIVACY GUARD: the user's active window looks sensitive "
                    "(password manager, banking, login). I did NOT take a "
                    "screenshot. Answer from memory only, and tell the user you "
                    "skipped the screenshot for safety.\n"
                )
            if search_results:
                from ai.web_search import build_search_context
                system += build_search_context(search_results)

            history = self._history_for_request(ak, images_b64)

            # 5. Stream LLM â€” buffer partial [POINT:...] tags so they never leak
            full_response = ""
            display_buf = ""
            self._cancel_flag = False
            self.sig_response_reset.emit()
            async for chunk in self._get_llm().stream_response(
                user_text=transcript,
                screenshots_b64=images_b64,
                history=history,
                system_prompt=system,
                model=self._current_model,
            ):
                if self._cancel_flag:
                    break
                full_response += chunk
                display_buf += chunk
                if self._active_session:
                    self._parse_points(self._active_session, display_buf)
                display_buf = ANY_TAG_RE.sub("", display_buf)
                m = ANY_PARTIAL_RE.search(display_buf)
                if m:
                    flush = display_buf[: m.start()]
                    display_buf = display_buf[m.start():]
                else:
                    flush = display_buf
                    display_buf = ""
                if flush:
                    self.sig_response_chunk.emit(flush)
            if display_buf:
                self.sig_response_chunk.emit(ANY_TAG_RE.sub("", display_buf))

            # 6. Update per-app history
            history.append(Message(role="user", content=transcript))
            history.append(Message(role="assistant", content=full_response))
            self._app_memory[ak] = history[-20:]

            # Multistep: parse numbered steps for later "next" invocations
            if multistep and not self._lesson_steps:
                steps = _split_steps(full_response)
                if len(steps) > 1:
                    self._lesson_steps = steps
                    self._lesson_step_idx = 0

            clean = ANY_TAG_RE.sub("", full_response).strip()
            self.sig_response_done.emit(clean)
            self._last_response = clean   # for "say it again"

            # Log to knowledge journal (skipped in quiz mode â€” those Q&As aren't
            # study material)
            if self._journal_enabled and not self._quiz_mode:
                try:
                    journal.log_qa(
                        question=transcript, answer=clean,
                        app_key=ak, window_title=title,
                        provider=cfg.llm_provider(),
                        model=self._current_model or "",
                    )
                except Exception:
                    pass

            # Lesson recorder gets the Q&A in transcript.md
            if self._recorder and self._recorder.is_recording:
                self._recorder.log_question(transcript)
                self._recorder.log_answer(clean)

            # Live-collab broadcast
            if self._collab and self._collab.code:
                try:
                    await self._collab.send({
                        "type": "qa", "q": transcript, "a": clean,
                    })
                except Exception:
                    pass

            # 7. TTS â€” hold the point visible while we speak. Switch voice
            # to match the user's language for multilingual mode.
            if self._cancel_flag:
                return
            if self._multilang and lang_code != "en":
                try:
                    tts = self._get_tts()
                    if hasattr(tts, "set_voice"):
                        tts.set_voice(multilang.voice_for(lang_code))
                except Exception:
                    pass
            self._emit_state(AppState.SPEAKING)
            try:
                await self._get_tts().speak(clean)
            except asyncio.CancelledError:
                pass

        except Exception as e:
            self.sig_error.emit(str(e))

        finally:
            if pointing_held:
                self.sig_point_release.emit()
            self._emit_state(AppState.IDLE)

    async def _run_request_session_pipeline(self, session: RequestSession):
        logger.info("Mic stopped")
        self._assert_active_session(session)
        pcm = self._listener.stop_recording()
        self._stop_live_transcription(final_pcm=pcm)
        if len(pcm) < 3200:
            self.sig_transcription_final.emit("")
            self._finish_request_session(session, final_phase="cancelled", error="empty audio capture")
            return

        self._emit_state(AppState.THINKING)
        self._transition_session(session, "capture_complete")

        try:
            transcript = await self._await_with_timeout(
                session,
                self._get_stt().transcribe(pcm),
                STT_TIMEOUT_S,
                "transcription_complete",
                "Speech-to-text timed out",
            )
            session.transcript = transcript
            logger.info("Final transcription received: %r", transcript)
            if transcript.strip():
                print("EMITTING:", transcript)
                self.sig_transcription_text.emit(transcript)
            self.sig_transcription_final.emit(transcript)
            if not transcript.strip():
                self._finish_request_session(session, final_phase="cancelled", error="blank transcription")
                return

            if is_stop(transcript):
                self.stop()
                return

            title = active_window_title()
            ak = app_key(title)

            if is_next(transcript) and self._lesson_steps:
                await self._advance_lesson_step(ak)
                self._finish_request_session(session, final_phase="completed")
                return

            if is_repeat(transcript) and self._last_response:
                await self._reply_local(self._last_response)
                self._finish_request_session(session, final_phase="completed")
                return

            if is_journal_today(transcript):
                msg = journal.summarise(journal.entries_today(), "Here's what you asked about today:\n")
                await self._reply_local(msg)
                self._finish_request_session(session, final_phase="completed")
                return
            if is_journal_week(transcript):
                msg = journal.summarise(journal.entries_this_week(), "Here's the past week:\n")
                await self._reply_local(msg)
                self._finish_request_session(session, final_phase="completed")
                return
            if is_quiz_review(transcript):
                await self._spaced_review()
                self._finish_request_session(session, final_phase="completed")
                return

            try:
                skill = skills_pkg.match(transcript)
                if skill:
                    msg = await skill["handler"](self, transcript)
                    if msg:
                        await self._reply_local(msg)
                    self._finish_request_session(session, final_phase="completed")
                    return
            except Exception as e:
                self.sig_error.emit(f"Skill error: {e}")

            sensitive = self._privacy_guard and is_sensitive_window(title)
            identity_q = is_identity_question(transcript)
            if sensitive or identity_q:
                session.screenshot = None
                screenshots = []
                images_b64 = []
            else:
                screenshots, images_b64 = self._capture_request_screenshot(session)

            locate_triggered = is_locate(transcript)
            multistep = is_multistep(transcript)

            search_results = ""
            if self._web_search_enabled:
                from ai.web_search import search
                session.search_task = asyncio.create_task(search(transcript))

            if screenshots and locate_triggered:
                shot = screenshots[0]
                try:
                    from ai.hybrid_pointer import find_target as _hybrid_find
                    target = _hybrid_find(
                        transcript,
                        screenshot=shot,
                        llm_provider=self._get_llm(),
                    )
                except Exception:
                    target = None

                if target is not None and target.source in ("uia", "ocr"):
                    async def _ready(t=target):
                        return t
                    session.locate_task = asyncio.create_task(_ready())
                elif cfg.anthropic_api_key:
                    from ai.element_locator import detect_element
                    session.locate_task = asyncio.create_task(detect_element(
                        screenshot_jpeg_b64=shot.base64_jpeg,
                        original_width=shot.width,
                        original_height=shot.height,
                        physical_width=shot.physical_width,
                        physical_height=shot.physical_height,
                        physical_left=shot.physical_left,
                        physical_top=shot.physical_top,
                        dpi_scale=shot.dpi_scale,
                        screen_index=shot.index,
                        user_question=transcript,
                    ))
                else:
                    try:
                        from ai.universal_locator import detect_element_universal
                        llm = self._get_llm()
                        session.locate_task = asyncio.create_task(detect_element_universal(
                            llm=llm,
                            screenshot_jpeg_b64=shot.base64_jpeg,
                            original_width=shot.width,
                            original_height=shot.height,
                            physical_width=shot.physical_width,
                            physical_height=shot.physical_height,
                            physical_left=shot.physical_left,
                            physical_top=shot.physical_top,
                            dpi_scale=shot.dpi_scale,
                            screen_index=shot.index,
                            user_question=transcript,
                            model=self._current_model,
                        ))
                    except Exception:
                        session.locate_task = None

            if session.search_task:
                try:
                    search_results = await self._await_with_timeout(
                        session,
                        session.search_task,
                        SEARCH_TIMEOUT_S,
                        "context_ready",
                        "Web search timed out",
                    ) or ""
                except Exception:
                    search_results = ""
            else:
                self._transition_session(session, "context_ready")

            detected = None
            detected_coord = None
            if session.locate_task:
                try:
                    detected = await self._await_with_timeout(
                        session,
                        session.locate_task,
                        LOCATOR_TIMEOUT_S,
                        "context_ready",
                        "UI element locator timed out",
                    )
                except Exception:
                    detected = None
            if detected:
                label = _guess_label(transcript)
                detected_coord = (int(detected.x), int(detected.y), label)
                self._show_detected_point(session, float(detected.x), float(detected.y), label)

            code_active = self._code_mode_auto and code_mode.is_code_window(title)
            lang_code = multilang.detect_language(transcript) if self._multilang else "en"

            ocr_extra = ""
            if self._ocr_enabled and screenshots and ocr.needs_ocr(transcript):
                try:
                    import base64
                    jpeg = base64.b64decode(screenshots[0].base64_jpeg)
                    txt = ocr.run_ocr(jpeg)
                    if txt:
                        ocr_extra = ocr.format_for_prompt(txt)
                except Exception:
                    pass

            doc_extra = ""
            for fname, text in self._attached_docs:
                doc_extra += pdf_context.format_for_prompt(fname, text)

            system = _build_system_prompt(
                window_title=title,
                lesson_step=self._lesson_step_idx,
                total_steps=len(self._lesson_steps),
                quiz_mode=self._quiz_mode,
                detected_coord=detected_coord,
                code_active=code_active,
                language_code=lang_code,
                extra=ocr_extra + doc_extra,
            )
            if sensitive:
                system += (
                    "\n\nPRIVACY GUARD: the user's active window looks sensitive "
                    "(password manager, banking, login). I did NOT take a "
                    "screenshot. Answer from memory only, and tell the user you "
                    "skipped the screenshot for safety.\n"
                )
            if search_results:
                from ai.web_search import build_search_context
                system += build_search_context(search_results)

            history = self._history_for_request(ak, images_b64)
            full_response = ""
            display_buf = ""
            self.sig_response_reset.emit()
            stream = self._get_llm().stream_response(
                user_text=transcript,
                screenshots_b64=images_b64,
                history=history,
                system_prompt=system,
                model=self._current_model,
            )
            session.stream_task = asyncio.current_task()
            self._transition_session(session, "llm_streaming")
            first_chunk = await self._await_with_timeout(
                session,
                stream.__anext__(),
                LLM_FIRST_TOKEN_TIMEOUT_S,
                "llm_streaming",
                "Language model timed out before the first token",
            )

            def _consume_chunk(chunk: str):
                nonlocal full_response, display_buf
                self._assert_active_session(session)
                full_response += chunk
                display_buf += chunk
                self._parse_points(session, display_buf)
                display_buf = ANY_TAG_RE.sub("", display_buf)
                m = ANY_PARTIAL_RE.search(display_buf)
                if m:
                    flush = display_buf[: m.start()]
                    display_buf = display_buf[m.start():]
                else:
                    flush = display_buf
                    display_buf = ""
                if flush:
                    self.sig_response_chunk.emit(flush)

            _consume_chunk(first_chunk)
            async for chunk in stream:
                _consume_chunk(chunk)
            if display_buf:
                self.sig_response_chunk.emit(ANY_TAG_RE.sub("", display_buf))

            history.append(Message(role="user", content=transcript))
            history.append(Message(role="assistant", content=full_response))
            self._app_memory[ak] = history[-20:]

            if multistep and not self._lesson_steps:
                steps = _split_steps(full_response)
                if len(steps) > 1:
                    self._lesson_steps = steps
                    self._lesson_step_idx = 0

            clean = ANY_TAG_RE.sub("", full_response).strip()
            self.sig_response_done.emit(clean)
            self._last_response = clean

            if self._journal_enabled and not self._quiz_mode:
                try:
                    journal.log_qa(
                        question=transcript, answer=clean,
                        app_key=ak, window_title=title,
                        provider=cfg.llm_provider(),
                        model=self._current_model or "",
                    )
                except Exception:
                    pass

            if self._recorder and self._recorder.is_recording:
                self._recorder.log_question(transcript)
                self._recorder.log_answer(clean)

            if self._collab and self._collab.code:
                try:
                    await self._collab.send({"type": "qa", "q": transcript, "a": clean})
                except Exception:
                    pass

            if self._multilang and lang_code != "en":
                try:
                    tts = self._get_tts()
                    if hasattr(tts, "set_voice"):
                        tts.set_voice(multilang.voice_for(lang_code))
                except Exception:
                    pass
            self._emit_state(AppState.SPEAKING)
            self._transition_session(session, "tts_speaking")
            session.tts_task = asyncio.create_task(self._get_tts().speak(clean))
            await self._await_with_timeout(
                session,
                session.tts_task,
                TTS_TIMEOUT_S,
                "completed",
                "Text-to-speech timed out",
            )

        except SessionCancelled as e:
            self._finish_request_session(session, final_phase="cancelled", error=str(e))
            return
        except Exception as e:
            self.sig_error.emit(str(e))
            self._finish_request_session(session, final_phase="failed", error=str(e))
            return

        self._finish_request_session(session, final_phase="completed")

    async def _reply_local(self, msg: str):
        """Show + speak a message that doesn't need an LLM round-trip."""
        self.sig_response_reset.emit()
        self.sig_response_chunk.emit(msg)
        self.sig_response_done.emit(msg)
        self._last_response = msg
        self._emit_state(AppState.SPEAKING)
        try:
            await self._get_tts().speak(msg)
        except Exception:
            pass
        self._emit_state(AppState.IDLE)

    async def _spaced_review(self):
        """SR-style review: pick due entries from the journal, ask one back."""
        due = journal.due_for_review(limit=1)
        if not due:
            await self._reply_local(
                "Nothing due for review right now â€” keep learning, I'll quiz "
                "you in a few days."
            )
            return
        entry = due[0]
        msg = f"Review: {entry['question']}"
        # Mark "correct" optimistically â€” a real implementation would wait for
        # the user's answer and grade it. Stubbed: reschedule based on streak.
        try:
            journal.mark_reviewed(int(entry["id"]), correct=True)
        except Exception:
            pass
        await self._reply_local(msg)

    async def _advance_lesson_step(self, ak: str):
        """User said 'next' â€” re-render the stored next lesson step via TTS,
        no new LLM round-trip needed."""
        self._lesson_step_idx += 1
        if self._lesson_step_idx >= len(self._lesson_steps):
            msg = "That's the last step â€” you're done!"
            self._lesson_steps = []
            self._lesson_step_idx = 0
        else:
            step = self._lesson_steps[self._lesson_step_idx]
            total = len(self._lesson_steps)
            msg = f"Step {self._lesson_step_idx + 1} of {total}: {step}"

        self.sig_response_reset.emit()
        self.sig_response_chunk.emit(msg)
        self.sig_response_done.emit(msg)
        self._emit_state(AppState.SPEAKING)
        try:
            await self._get_tts().speak(msg)
        except Exception:
            pass
        self._emit_state(AppState.IDLE)

    def _parse_points(self, session: RequestSession, text: str):
        self._assert_active_session(session)
        for regex, kind in (
            (POINT_RE, "point"),
            (ARROW_RE, "arrow"),
            (CIRCLE_RE, "circle"),
            (UNDERLINE_RE, "underline"),
            (LABEL_RE, "label"),
        ):
            for match in regex.finditer(text):
                raw = match.group(0)
                if raw in session.seen_tags:
                    continue
                session.seen_tags.add(raw)
                if kind == "point":
                    x, y, label, _ = match.groups()
                    self._show_detected_point(session, float(x), float(y), label.strip())
                elif kind == "arrow":
                    x1, y1, x2, y2 = (float(v) for v in match.groups())
                    self.sig_arrow.emit(x1, y1, x2, y2)
                elif kind == "circle":
                    x, y, r, _label = match.groups()
                    self.sig_circle.emit(float(x), float(y), float(r))
                elif kind == "underline":
                    x, y, w = (float(v) for v in match.groups())
                    self.sig_underline.emit(x, y, w)
                else:
                    x, y, txt = match.groups()
                    self.sig_label.emit(float(x), float(y), txt.strip())

    def _emit_state(self, state: AppState):
        self._state = state
        self.sig_state_changed.emit(state)

    # â”€â”€ Settings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def set_model(self, model: str):
        self._current_model = model

    def set_active_provider(self, name: str):
        """Runtime switch between claude / openai / copilot / gemini / ollama."""
        cfg.set_active_llm(name)
        self._llm = None           # force re-init on next query
        self._current_model = None
        # If switching to Copilot and the cached model list is stale (or
        # missing), refresh it in the background so the panel shows the
        # *current* set of models GitHub offers â€” not stale hardcoded ones.
        if name == "copilot":
            try:
                from ai.github_copilot_provider import cache_is_stale
                if cache_is_stale():
                    self._submit(self._refresh_copilot_models())
            except Exception:
                pass
        elif name in ("claude", "openai", "gemini"):
            try:
                from ai.model_registry import cache_is_stale as _stale
                if _stale(name):
                    self._submit(self._refresh_one_model_list(name))
            except Exception:
                pass
        elif name == "ollama":
            # Surface installed models in the tray immediately
            self.refresh_ollama_models()

    async def _refresh_one_model_list(self, provider: str):
        try:
            from ai.model_registry import refresh
            ms = await refresh(provider)
            self.sig_models_refreshed.emit(provider, len(ms))
        except Exception as e:
            self.sig_error.emit(f"{provider} model refresh failed: {e}")

    def refresh_copilot_models(self):
        """Public â€” bound to the tray 'Refresh Copilot models' action."""
        self._submit(self._refresh_copilot_models())

    async def _refresh_copilot_models(self):
        try:
            from ai.github_copilot_provider import refresh_models_to_cache
            models = await refresh_models_to_cache()
            self.sig_copilot_models_done.emit(len(models))
        except Exception as e:
            self.sig_error.emit(f"Copilot model refresh failed: {e}")

    # â”€â”€ Ollama model management â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def refresh_ollama_models(self):
        """Public â€” kick off async poll of /api/tags. Result via sig_ollama_models."""
        self._submit(self._refresh_ollama_models())

    async def _refresh_ollama_models(self):
        try:
            from ai.ollama_provider import OllamaProvider
            classified = await OllamaProvider().list_models_classified()
            self.sig_ollama_models.emit(classified)
        except Exception as e:
            self.sig_error.emit(f"Ollama model list failed: {e}")

    def set_ollama_model(self, kind: str, name: str):
        """Tray callback â€” update the active vision/text model. No restart needed."""
        cfg.set_ollama_model(kind, name)
        # Force the provider instance to re-read cfg on next call
        if cfg.llm_provider() == "ollama":
            self._llm = None

    def pull_ollama_model(self, name: str):
        """Trigger `ollama pull <name>` in the background. Status via sig_ollama_pull_status."""
        self._submit(self._pull_ollama_model(name))

    async def _pull_ollama_model(self, name: str):
        from ai.ollama_models_registry import pull_model
        self.sig_ollama_pull_status.emit(name, f"Pulling {name}â€¦")

        def _progress(msg: str):
            if msg:
                self.sig_ollama_pull_status.emit(name, msg)

        ok = await pull_model(name, cfg.ollama_host, on_progress=_progress)
        if ok:
            self.sig_ollama_pull_status.emit(name, f"âœ“ {name} ready")
            # Refresh the installed list so the tray menu picks it up
            await self._refresh_ollama_models()
        else:
            self.sig_ollama_pull_status.emit(name, f"âœ— Pull failed for {name}")

    def set_web_search(self, enabled: bool):
        self._web_search_enabled = enabled

    def set_wake_word(self, enabled: bool):
        self._listener.set_wake_word_enabled(enabled)

    def set_slow_mode(self, enabled: bool):
        self._slow_mode = enabled

    def set_quiz_mode(self, enabled: bool):
        was = self._quiz_mode
        self._quiz_mode = enabled
        if enabled and not was:
            # Kick off the first question immediately so the user doesn't
            # have to ask "begin quiz". Uses the active screen as context.
            session = self._start_request_session("quiz")
            if session is not None:
                fut = self._submit(self._kickoff_quiz(session))
                if fut is not None:
                    self._current_task = fut

    async def _kickoff_quiz(self, session: RequestSession):
        """Called when quiz mode flips ON â€” generates the first question
        without waiting for a user utterance."""
        try:
            self._engage_overlay(session)
            self._emit_state(AppState.THINKING)
            self._transition_session(session, "context_ready")
            screenshots, images_b64 = self._capture_request_screenshot(session)
            title = active_window_title()
            system = _build_system_prompt(
                window_title=title, quiz_mode=True,
            )
            ak = app_key(title)
            history = self._history_for_request(ak, images_b64)

            full = ""
            stream = self._get_llm().stream_response(
                user_text="(quiz mode just enabled â€” start the quiz now)",
                screenshots_b64=images_b64,
                history=history,
                system_prompt=system,
                model=self._current_model,
            )
            session.stream_task = asyncio.current_task()
            self._transition_session(session, "llm_streaming")
            first_chunk = await self._await_with_timeout(
                session,
                stream.__anext__(),
                LLM_FIRST_TOKEN_TIMEOUT_S,
                "llm_streaming",
                "Quiz start timed out before the first token",
            )
            full += first_chunk
            self.sig_response_chunk.emit(first_chunk)
            async for chunk in stream:
                self._assert_active_session(session)
                full += chunk
                self.sig_response_chunk.emit(chunk)
            self.sig_response_done.emit(full)
            self._emit_state(AppState.SPEAKING)
            self._transition_session(session, "tts_speaking")
            session.tts_task = asyncio.create_task(self._get_tts().speak(full))
            await self._await_with_timeout(
                session,
                session.tts_task,
                TTS_TIMEOUT_S,
                "completed",
                "Quiz speech timed out",
            )
        except SessionCancelled as e:
            self._finish_request_session(session, final_phase="cancelled", error=str(e))
            return
        except Exception as e:
            self.sig_error.emit(f"Quiz start failed: {e}")
            self._finish_request_session(session, final_phase="failed", error=f"Quiz start failed: {e}")
            return
        self._finish_request_session(session, final_phase="completed")

    def set_privacy_guard(self, enabled: bool):
        self._privacy_guard = enabled

    @property
    def slow_mode(self) -> bool:  return self._slow_mode
    @property
    def quiz_mode(self) -> bool:  return self._quiz_mode
    @property
    def privacy_guard(self) -> bool:  return self._privacy_guard

    def clear_history(self):
        self._history = []
        self._app_memory.clear()
        self._lesson_steps = []
        self._lesson_step_idx = 0

    # â”€â”€ Attached documents (drag-drop on panel) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def attach_document(self, path: str) -> bool:
        text = pdf_context.extract_text(path)
        if not text.strip():
            return False
        from pathlib import Path
        self._attached_docs.append((Path(path).name, text))
        # Cap context â€” most recent 3 docs
        self._attached_docs = self._attached_docs[-3:]
        return True

    def clear_attachments(self):
        self._attached_docs = []

    # â”€â”€ Lesson recording â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def start_recording(self) -> Optional[str]:
        if self._recorder is None:
            self._recorder = lesson_recorder.LessonRecorder()
        out = self._recorder.start()
        if out:
            self.sig_recording_state.emit(True, str(out))
            return str(out)
        return None

    def stop_recording(self) -> Optional[str]:
        if not self._recorder or not self._recorder.is_recording:
            return None
        out = self._recorder.stop()
        self.sig_recording_state.emit(False, str(out) if out else "")
        return str(out) if out else None

    @property
    def is_recording(self) -> bool:
        return bool(self._recorder and self._recorder.is_recording)

    # â”€â”€ Workflow capture (record clicks/keystrokes) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def workflow_start(self) -> bool:
        if self._workflow is None:
            self._workflow = workflow_capture.WorkflowCapture()
        return self._workflow.start()

    def workflow_stop(self) -> str:
        if not self._workflow:
            return ""
        events = self._workflow.stop()
        return self._workflow.summarise() if events else ""

    # â”€â”€ Live collaboration â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def collab_start_host(self):
        """Live-session host. Disabled â€” see tutor_features/collab.py."""
        self.sig_error.emit(
            "Live Session: not available in this build. "
            "Requires a WebRTC signalling server (planned for a future release)."
        )

    def collab_join(self, code: str):
        """Live-session join. Disabled â€” see tutor_features/collab.py."""
        self.sig_error.emit(
            "Live Session: not available in this build. "
            "Requires a WebRTC signalling server (planned for a future release)."
        )

    # â”€â”€ Voice picker (ElevenLabs / Edge) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def set_tts_voice(self, voice: str):
        try:
            tts = self._get_tts()
            if hasattr(tts, "set_voice"):
                tts.set_voice(voice)
        except Exception:
            pass

    # â”€â”€ Toggle setters for the rest of the new features â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def set_code_mode_auto(self, enabled: bool):
        self._code_mode_auto = enabled

    def set_multilang(self, enabled: bool):
        self._multilang = enabled

    def set_journal(self, enabled: bool):
        self._journal_enabled = enabled

    def set_ocr_enabled(self, enabled: bool):
        self._ocr_enabled = enabled

    # â”€â”€ Stop / cancel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def stop(self):
        """Cancel the current LLM stream + any in-flight TTS. Bound to Esc."""
        if self._active_session is not None:
            session = self._active_session
            self._mark_session_cancelled(session, "stopped by user")
            if self._state == AppState.LISTENING:
                try:
                    self._listener.stop_recording()
                except Exception:
                    pass
                self._stop_live_transcription()
            if self._current_task and not self._current_task.done():
                self._current_task.cancel()
            self._lesson_steps = []
            self._lesson_step_idx = 0
            self._finish_request_session(session, final_phase="cancelled", error="stopped by user")
            return
        session = self._active_session
        # Kill audio playback immediately â€” flips the global stop event so
        # the chunked PortAudio loop bails out within ~50 ms.
        try:
            from audio.playback import stop_audio
            stop_audio()
        except Exception:
            pass
        # Some TTS providers also have their own cancel hook
        tts = self._tts
        if tts and hasattr(tts, "stop"):
            try:
                tts.stop()
            except Exception:
                pass
        # Clear any stored lesson so "stop" really means "back to zero"
        self._lesson_steps = []
        self._lesson_step_idx = 0
        self._emit_state(AppState.IDLE)

