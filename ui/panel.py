# -*- coding: utf-8 -*-
import asyncio
import logging
from enum import Enum, auto

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import cfg
from ui.design import (
    FONT_LABEL,
    FONT_RESPONSE,
    FONT_STATUS,
    FONT_TITLE,
    PANEL_HEIGHT,
    PANEL_QSS,
    PANEL_RADIUS,
    PANEL_WIDTH,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_SPEAKING,
    STATE_THINKING,
)


logger = logging.getLogger(__name__)


class AppState(Enum):
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()


STATE_LABELS = {
    AppState.IDLE: "Say 'Kai Agent' or Ctrl+Alt+Space",
    AppState.LISTENING: "Listening...",
    AppState.THINKING: "Thinking...",
    AppState.SPEAKING: "Speaking...",
}

STATE_COLORS = {
    AppState.IDLE: STATE_IDLE,
    AppState.LISTENING: STATE_LISTENING,
    AppState.THINKING: STATE_THINKING,
    AppState.SPEAKING: STATE_SPEAKING,
}


class WaveformWidget(QWidget):
    """Animated waveform bars shown while listening."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self._levels = [0.0] * 12
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._decay)

    def set_level(self, rms: float):
        import random

        peak = min(1.0, rms * 3)
        self._levels = [
            min(1.0, peak * (0.4 + random.random() * 0.6))
            for _ in self._levels
        ]
        self.update()

    def _decay(self):
        self._levels = [max(0.0, v * 0.85) for v in self._levels]
        self.update()

    def start(self):
        self._timer.start(50)

    def stop(self):
        self._timer.stop()
        self._levels = [0.0] * 12
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_w = max(3, (w - 4 * len(self._levels)) // len(self._levels))
        spacing = (w - bar_w * len(self._levels)) // (len(self._levels) + 1)
        for i, level in enumerate(self._levels):
            x = spacing + i * (bar_w + spacing)
            bar_h = max(4, int(level * (h - 8)))
            y = (h - bar_h) // 2
            alpha = max(80, int(level * 255))
            color = QColor(0, 120, 255, alpha)
            painter.setBrush(QBrush(color))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(x, y, bar_w, bar_h, bar_w // 2, bar_w // 2)
        painter.end()


PROVIDER_LABELS = {
    "claude": "Claude",
    "openai": "GPT-4o",
    "gemini": "Gemini",
    "copilot": "Copilot",
    "ollama": f"Ollama ({cfg.ollama_model})",
}


def _copilot_model_choices() -> list[tuple[str, str]]:
    """Return [(model_id, display_label), ...] for the dropdown."""
    try:
        from ai.github_copilot_provider import model_label, sorted_model_ids
    except Exception:
        return [("gpt-4o-mini", "gpt-4o-mini  (free)")]

    out = [(mid, model_label(mid)) for mid in sorted_model_ids()]
    return out or [("gpt-4o-mini", "gpt-4o-mini  (free)")]


class ProviderBadge(QLabel):
    """Small pill showing active provider."""

    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self.set_provider(provider)
        self.setStyleSheet(
            "background: rgba(0,120,255,25); border: 1px solid rgba(0,120,255,100);"
            "border-radius: 8px; color: rgb(140,180,255); font-size: 11px; padding: 2px 8px;"
        )

    def set_provider(self, provider: str):
        self.setText(PROVIDER_LABELS.get(provider, provider))


class CompanionPanel(QWidget):
    """Floating companion control panel."""

    on_push_to_talk_pressed = pyqtSignal()
    on_push_to_talk_released = pyqtSignal()
    on_model_changed = pyqtSignal(str)
    on_document_dropped = pyqtSignal(str)
    _sig_copilot_code = pyqtSignal(str, str)
    _sig_copilot_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._state = AppState.IDLE
        self._response_text = ""
        self._setup_window()
        self._build_ui()
        self._position_bottom_right()
        self._sig_copilot_code.connect(self._on_copilot_code)
        self._sig_copilot_error.connect(self._on_copilot_error)

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setWindowTitle("Kai Agent")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(PANEL_WIDTH, PANEL_HEIGHT)
        self.setObjectName("panel")
        self.setStyleSheet(PANEL_QSS)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self.on_document_dropped.emit(path)
        event.acceptProposedAction()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Kai Agent")
        title.setObjectName("title")
        title.setFont(FONT_TITLE)
        header.addWidget(title)
        header.addStretch()
        self._badge = ProviderBadge(cfg.llm_provider())
        header.addWidget(self._badge)

        self._min_btn = QPushButton("-")
        self._min_btn.setFixedSize(24, 24)
        self._min_btn.setStyleSheet(
            "QPushButton { background: rgba(60,60,75,180); color: rgb(220,220,230);"
            "border: none; border-radius: 12px; font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background: rgba(80,80,95,220); }"
        )
        self._min_btn.setToolTip("Hide panel (use tray to reopen)")
        self._min_btn.clicked.connect(self.hide)
        header.addWidget(self._min_btn)
        root.addLayout(header)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet("color: rgba(60,60,75,180);")
        root.addWidget(div)

        self._status_dot = QLabel("\u25cf")
        self._status_dot.setStyleSheet(
            f"color: rgb({STATE_IDLE.red()},{STATE_IDLE.green()},{STATE_IDLE.blue()}); font-size: 10px;"
        )
        self._status_label = QLabel(STATE_LABELS[AppState.IDLE])
        self._status_label.setObjectName("status")
        self._status_label.setFont(FONT_STATUS)
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_dot)
        status_row.addWidget(self._status_label)
        status_row.addStretch()
        root.addLayout(status_row)

        self._waveform = WaveformWidget()
        self._waveform.setVisible(False)
        root.addWidget(self._waveform)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._response_label = QLabel()
        self._response_label.setObjectName("response")
        self._response_label.setFont(FONT_RESPONSE)
        self._response_label.setWordWrap(True)
        self._response_label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._response_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._response_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        scroll.setWidget(self._response_label)
        root.addWidget(scroll, stretch=1)

        self._ptt_btn = QPushButton("Say 'Kai Agent' or hold Ctrl+Alt+Space")
        self._ptt_btn.setObjectName("hotkey_btn")
        self._ptt_btn.setFont(FONT_LABEL)
        self._ptt_btn.setFixedHeight(44)
        root.addWidget(self._ptt_btn)

        footer = QHBoxLayout()
        lbl = QLabel("Model:")
        lbl.setFont(FONT_LABEL)
        lbl.setStyleSheet("color: rgb(100,100,120); font-size: 11px;")
        self._model_combo = QComboBox()
        self._model_combo.setStyleSheet(
            "background: rgba(40,40,50,200); border: 1px solid rgba(60,60,75,180);"
            "border-radius: 6px; color: rgb(200,200,215); padding: 2px 6px; font-size: 11px;"
        )
        self._populate_models()
        self._model_combo.currentIndexChanged.connect(
            lambda _idx: self.on_model_changed.emit(
                self._model_combo.currentData() or self._model_combo.currentText()
            )
        )
        footer.addWidget(lbl)
        footer.addWidget(self._model_combo, stretch=1)
        root.addLayout(footer)

    def _populate_models(self):
        self._set_models_for(cfg.llm_provider())

    def _set_models_for(self, provider: str):
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        if provider == "copilot":
            for mid, label in _copilot_model_choices():
                self._model_combo.addItem(label, userData=mid)
        elif provider in ("claude", "openai", "gemini"):
            try:
                from ai.model_registry import cached_models

                for m in cached_models(provider):
                    label = m["id"]
                    if not m.get("vision"):
                        label += "  (no vision)"
                    self._model_combo.addItem(label, userData=m["id"])
            except Exception:
                self._model_combo.addItem("default", userData="default")
        else:
            self._model_combo.addItem(cfg.ollama_model, userData=cfg.ollama_model)
        self._model_combo.blockSignals(False)
        if self._model_combo.count():
            self.on_model_changed.emit(
                self._model_combo.currentData() or self._model_combo.currentText()
            )

    def refresh_for_provider(self, provider: str):
        self._badge.set_provider(provider)
        self._set_models_for(provider)

    def _position_bottom_right(self):
        from PyQt6.QtWidgets import QApplication

        screen = QApplication.primaryScreen().geometry()
        x = screen.right() - PANEL_WIDTH - 24
        y = screen.bottom() - PANEL_HEIGHT - 60
        self.move(x, y)

    def set_state(self, state: AppState):
        self._state = state
        color = STATE_COLORS[state]
        self._status_dot.setStyleSheet(
            f"color: rgb({color.red()},{color.green()},{color.blue()}); font-size: 10px;"
        )
        self._status_label.setText(STATE_LABELS[state])
        self._waveform.setVisible(state == AppState.LISTENING)
        if state == AppState.LISTENING:
            self._waveform.start()
        else:
            self._waveform.stop()

    def update_response(self, text: str):
        self._response_text = text
        self._response_label.setText(text)

    def update_transcription(self, text: str):
        self._response_text = text
        self._response_label.setText(text)
        logger.info("UI transcription updated: %r", text)

    def append_response_chunk(self, chunk: str):
        self._response_text += chunk
        self._response_label.setText(self._response_text)

    def set_audio_level(self, rms: float):
        self._waveform.set_level(rms)

    def clear_response(self):
        self._response_text = ""
        self._response_label.setText("")

    def prepare_for_transcription(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.clear_response()

    def show_copilot_code(self, user_code: str, verification_uri: str):
        self._sig_copilot_code.emit(user_code, verification_uri)

    def show_copilot_error(self, error: str):
        self._sig_copilot_error.emit(error)

    def _on_copilot_code(self, user_code: str, verification_uri: str):
        self.show()
        self.raise_()
        self._response_text = (
            "-- GitHub Copilot Sign-In --\n\n"
            f"1.  Open:  {verification_uri}\n\n"
            f"2.  Enter code:\n\n"
            f"        {user_code}\n\n"
            "3.  Click Authorize in GitHub.\n\n"
            "Kai Agent will sign in automatically once you authorize."
        )
        self._response_label.setText(self._response_text)
        self._status_label.setText("Waiting for Copilot authorization...")

    def _on_copilot_error(self, error: str):
        self._response_text = f"Copilot login failed:\n\n{error}"
        self._response_label.setText(self._response_text)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(
            self, "_drag_pos"
        ):
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(18, 18, 22, 235)))
        painter.setPen(QPen(QColor(60, 60, 75, 180), 1))
        painter.drawRoundedRect(self.rect(), PANEL_RADIUS, PANEL_RADIUS)
        painter.end()
