import math
import logging

from PyQt6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF, QTimer, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QCursor, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QTextEdit, QWidget

from ui.design import ANIM_FAST_MS, BLUE_GLOW, FONT_RESPONSE, TEXT_PRIMARY, TEXT_SECONDARY


logger = logging.getLogger(__name__)


class TranscriptionOverlay(QWidget):
    """Floating glass transcription capsule shown near the cursor while speaking."""

    CURSOR_OFFSET = QPoint(20, 20)
    MAX_TEXT_WIDTH = 300
    MIN_WIDTH = 220
    MIN_HEIGHT = 74
    HOLD_MS = 1500
    TEXT_REVEAL_MS = 18
    CHAR_FADE_MS = 140

    def __init__(self):
        super().__init__()
        self.partial_text = ""
        self.final_text = ""
        self._display_text = ""
        self._target_text = ""
        self._animated_text = ""
        self._revealed_char_times: list[int] = []
        self._placeholder = "Listening..."
        self._show_placeholder = True
        self._mic_error = False
        self._orb_phase = 0.0
        self._audio_level = 0.0
        self._text_opacity = 1.0
        self._overlay_opacity = 0.0
        self._follow_cursor = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

        self._text_layer = _LiquidGlassTextLayer(self)
        self._text_layer.setGeometry(self._text_rect())
        self._text_layer.raise_()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._animate_tick)
        self._tick.start(16)

        self._typing_timer = QTimer(self)
        self._typing_timer.timeout.connect(self._advance_typing_animation)
        self._typing_timer.setInterval(self.TEXT_REVEAL_MS)

        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self._move_near_cursor)
        self._follow_timer.setInterval(33)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._fade_out)

        self._fade_anim = QPropertyAnimation(self, b"overlayOpacity", self)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._fade_anim.finished.connect(self._on_fade_finished)

        self._text_anim = QPropertyAnimation(self, b"textOpacity", self)
        self._text_anim.setDuration(120)
        self._text_anim.setStartValue(0.6)
        self._text_anim.setEndValue(1.0)
        self._text_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._resize_for_text()

    def begin_capture(self):
        logger.info("Transcription overlay begin_capture")
        self.partial_text = ""
        self.final_text = ""
        self._display_text = ""
        self._target_text = ""
        self._animated_text = ""
        self._revealed_char_times = []
        self._placeholder = "Listening..."
        self._show_placeholder = True
        self._mic_error = False
        self._follow_cursor = True
        self._hide_timer.stop()
        self._move_near_cursor()
        self._resize_for_text()
        self._overlay_opacity = 0.0
        self.show()
        self.raise_()
        self._text_layer.raise_()
        self._follow_timer.start()
        self._fade_to(1.0, ANIM_FAST_MS)
        self._render_visible_text()
        self.update()

    def update_transcription(self, text: str):
        logger.info("Overlay transcription updated: %r", text)
        print("TEXT RECEIVED:", text)
        self.partial_text = text
        self._show_placeholder = not bool(text.strip()) and not self._mic_error
        clean_text = text.strip()
        if clean_text.startswith(self._display_text):
            self._display_text = clean_text
        elif clean_text:
            prefix_len = 0
            for old_char, new_char in zip(self._display_text, clean_text):
                if old_char != new_char:
                    break
                prefix_len += 1
            self._display_text = self._display_text + clean_text[prefix_len:]
        else:
            self._display_text = ""
        self._resize_for_text()
        self._queue_text_render()

    def finalize_transcription(self, text: str):
        logger.info("Transcription overlay finalize: %r", text)
        self.final_text = text.strip()
        if self.final_text:
            self._display_text = self.final_text
            self._show_placeholder = False
        elif self.partial_text.strip():
            self._display_text = self.partial_text.strip()
            self._show_placeholder = False
        else:
            self._display_text = ""
            self._show_placeholder = True
        self._follow_cursor = False
        self._follow_timer.stop()
        self._resize_for_text()
        self._queue_text_render(force_reset=not self.final_text)
        self._hide_timer.start(self.HOLD_MS)
        self.update()

    def show_mic_error(self, message: str = "Mic not available"):
        logger.warning("Transcription overlay mic error: %s", message)
        self.partial_text = ""
        self.final_text = message
        self._display_text = message
        self._show_placeholder = False
        self._mic_error = True
        self._follow_cursor = False
        self._follow_timer.stop()
        self._move_near_cursor()
        self._resize_for_text()
        self.show()
        self.raise_()
        self._text_layer.raise_()
        self._fade_to(1.0, ANIM_FAST_MS)
        self._queue_text_render(force_reset=True)
        self._hide_timer.start(self.HOLD_MS)
        self.update()

    def set_audio_level(self, rms: float):
        self._audio_level = self._audio_level * 0.65 + min(1.0, rms * 3.2) * 0.35

    def _fade_to(self, value: float, duration: int):
        self._fade_anim.stop()
        self._fade_anim.setDuration(duration)
        self._fade_anim.setStartValue(self._overlay_opacity)
        self._fade_anim.setEndValue(value)
        self._fade_anim.start()

    def _fade_out(self):
        logger.info("Transcription overlay fade_out")
        self._fade_anim.stop()
        self._fade_anim.setDuration(180)
        self._fade_anim.setStartValue(self._overlay_opacity)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_fade_finished(self):
        if self._overlay_opacity <= 0.01:
            logger.info("Transcription overlay hidden")
            self.hide()

    def _pulse_text(self):
        self._text_anim.stop()
        self._text_opacity = 0.7
        self._text_anim.start()

    def _queue_text_render(self, force_reset: bool = False):
        target = self._current_text()
        if force_reset or not target.startswith(self._animated_text):
            self._animated_text = ""
            self._revealed_char_times = []
        self._target_text = target
        self._pulse_text()
        self._render_visible_text()
        if self._animated_text != self._target_text:
            self._typing_timer.start()
        else:
            self._typing_timer.stop()
        self.update()

    def _advance_typing_animation(self):
        if self._animated_text == self._target_text:
            self._typing_timer.stop()
            return
        next_index = len(self._animated_text)
        self._animated_text += self._target_text[next_index]
        self._revealed_char_times.append(self._now_ms())
        self._render_visible_text()
        if self._animated_text == self._target_text:
            self._typing_timer.stop()

    def _render_visible_text(self):
        text = self._current_text()
        visible_text = self._animated_text if text == self._target_text else text
        if not visible_text and self._show_placeholder:
            visible_text = self._placeholder
        self._text_layer.set_text(
            visible_text,
            placeholder=self._show_placeholder,
            mic_error=self._mic_error,
            text_opacity=self._text_opacity,
            reveal_times=self._revealed_char_times,
            full_target=self._target_text or text,
        )
        print("UI updated", visible_text)
        print("text rendered", visible_text)
        logger.info("TEXT RENDERED: %r", visible_text)

    def _move_near_cursor(self):
        cursor = QCursor.pos() + self.CURSOR_OFFSET
        screen = QApplication.screenAt(cursor) or QApplication.primaryScreen()
        if screen is None:
            self.move(cursor)
            return
        geo = screen.availableGeometry()
        x = min(cursor.x(), geo.right() - self.width())
        y = min(cursor.y(), geo.bottom() - self.height())
        x = max(geo.left(), x)
        y = max(geo.top(), y)
        self.move(x, y)

    def _current_text(self) -> str:
        return self._display_text if self._display_text else self._placeholder

    def _resize_for_text(self):
        text = self._current_text()
        fm = QFontMetrics(FONT_RESPONSE)
        rect = fm.boundingRect(0, 0, self.MAX_TEXT_WIDTH, 500, Qt.TextFlag.TextWordWrap, text)
        width = max(self.MIN_WIDTH, min(390, rect.width() + 92))
        height = max(self.MIN_HEIGHT, min(150, rect.height() + 36))
        self.resize(width, height)
        self._text_layer.setGeometry(self._text_rect())
        if self.isVisible():
            self._move_near_cursor()

    def _animate_tick(self):
        self._orb_phase += 0.12 + self._audio_level * 0.08
        self._text_layer.update()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._text_layer.setGeometry(self._text_rect())

    def _text_rect(self) -> QRect:
        rect = self.rect().adjusted(76, 22, -28, -22)
        return rect if rect.width() > 0 and rect.height() > 0 else self.rect()

    @staticmethod
    def _now_ms() -> int:
        from time import monotonic_ns
        return monotonic_ns() // 1_000_000

    def get_text_opacity(self) -> float:
        return self._text_opacity

    def set_text_opacity(self, value: float):
        self._text_opacity = value
        self._text_layer.set_text(
            self._text_layer._text,
            placeholder=self._show_placeholder,
            mic_error=self._mic_error,
            text_opacity=self._text_opacity,
            reveal_times=self._revealed_char_times,
            full_target=self._target_text or self._current_text(),
        )
        self.update()

    textOpacity = pyqtProperty(float, fget=get_text_opacity, fset=set_text_opacity)

    def get_overlay_opacity(self) -> float:
        return self._overlay_opacity

    def set_overlay_opacity(self, value: float):
        self._overlay_opacity = max(0.0, min(1.0, float(value)))
        self.update()

    overlayOpacity = pyqtProperty(float, fget=get_overlay_opacity, fset=set_overlay_opacity)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setOpacity(self._overlay_opacity)

            rect = QRectF(self.rect()).adjusted(10.0, 10.0, -10.0, -10.0)
            if rect.width() <= 0 or rect.height() <= 0:
                return

            path = QPainterPath()
            path.addRoundedRect(rect, 15.0, 15.0)

            glow_color = QColor(BLUE_GLOW)
            glow_color.setAlpha(36)
            for inset, alpha in ((18.0, 22), (10.0, 38), (4.0, 56)):
                outer = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
                if outer.width() <= 0 or outer.height() <= 0:
                    continue
                painter.setPen(Qt.PenStyle.NoPen)
                c = QColor(glow_color)
                c.setAlpha(alpha)
                painter.setBrush(c)
                painter.drawRoundedRect(outer, 20.0, 20.0)

            fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            fill.setColorAt(0.0, QColor(34, 38, 54, 158))
            fill.setColorAt(0.25, QColor(26, 28, 42, 130))
            fill.setColorAt(1.0, QColor(20, 20, 30, 102))
            painter.fillPath(path, fill)

            gloss = QLinearGradient(rect.topLeft(), rect.bottomRight())
            gloss.setColorAt(0.0, QColor(255, 255, 255, 44))
            gloss.setColorAt(0.35, QColor(200, 220, 255, 12))
            gloss.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.fillPath(path, gloss)

            painter.setPen(QPen(QColor(140, 180, 255, 78), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

            orb_center = QPoint(40, self.height() // 2)
            orb_center_f = QPointF(orb_center)
            pulse = 1.0 + 0.08 * math.sin(self._orb_phase) + self._audio_level * 0.22
            outer = QRadialGradient(orb_center_f, 26.0 * pulse)
            outer.setColorAt(0.0, QColor(110, 180, 255, 155))
            outer.setColorAt(0.55, QColor(40, 140, 255, 72))
            outer.setColorAt(1.0, QColor(40, 140, 255, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(outer)
            painter.drawEllipse(orb_center_f, 26.0 * pulse, 26.0 * pulse)

            mid = QRadialGradient(orb_center_f, 12.0 * pulse)
            mid.setColorAt(0.0, QColor(220, 245, 255, 255))
            mid.setColorAt(0.45, QColor(90, 180, 255, 235))
            mid.setColorAt(1.0, QColor(36, 115, 255, 210))
            painter.setBrush(mid)
            painter.drawEllipse(orb_center_f, 12.0 * pulse, 12.0 * pulse)

            ring = QColor(190, 225, 255, 120)
            painter.setPen(QPen(ring, 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(orb_center_f, 14.0 * pulse, 14.0 * pulse)

        except Exception:
            logger.exception("Transcription overlay paintEvent failed")
        finally:
            if painter.isActive():
                painter.end()


class _LiquidGlassTextLayer(QTextEdit):
    """Dedicated top text layer so orb/background painting never hides transcription."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._placeholder = True
        self._mic_error = False
        self._text_opacity = 1.0
        self._reveal_times: list[int] = []
        self._full_target = ""
        self.setReadOnly(True)
        self.setFrameStyle(0)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("background: transparent; border: none;")
        self.viewport().setAutoFillBackground(False)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def set_text(
        self,
        text: str,
        *,
        placeholder: bool,
        mic_error: bool,
        text_opacity: float,
        reveal_times: list[int],
        full_target: str,
    ):
        self._text = text
        self._placeholder = placeholder
        self._mic_error = mic_error
        self._text_opacity = text_opacity
        self._reveal_times = list(reveal_times)
        self._full_target = full_target
        self.viewport().update()

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            painter.setFont(FONT_RESPONSE)

            base_color = QColor(TEXT_SECONDARY if self._placeholder else TEXT_PRIMARY)
            if self._mic_error:
                base_color = QColor(255, 210, 210)
            base_color.setAlpha(max(0, min(255, int(255 * self._text_opacity * 0.92))))

            fm = QFontMetrics(FONT_RESPONSE)
            line_height = fm.lineSpacing()
            text_rect = self.viewport().rect()
            x = float(text_rect.left())
            y = float(text_rect.top() + fm.ascent() + 2)
            max_width = max(10.0, float(text_rect.width()))
            now_ms = TranscriptionOverlay._now_ms()

            for index, char in enumerate(self._text):
                if char == "\n":
                    x = float(text_rect.left())
                    y += float(line_height)
                    continue
                advance = float(fm.horizontalAdvance(char))
                if x > text_rect.left() and x + advance > text_rect.left() + max_width:
                    x = float(text_rect.left())
                    y += float(line_height)
                if y > text_rect.bottom() + line_height:
                    break

                progress = 1.0
                if index < len(self._reveal_times):
                    age = max(0, now_ms - self._reveal_times[index])
                    progress = min(1.0, age / TranscriptionOverlay.CHAR_FADE_MS)
                char_alpha = max(0.15, progress) * self._text_opacity
                blur_alpha = max(0.0, 1.0 - progress) * 0.28 * self._text_opacity
                y_offset = (1.0 - progress) * 3.0

                if blur_alpha > 0.01 and not char.isspace():
                    glow = QColor(190, 225, 255, int(255 * blur_alpha))
                    painter.setPen(glow)
                    painter.drawText(QPointF(x, y - y_offset + 1.0), char)

                pen = QColor(base_color)
                pen.setAlpha(max(0, min(255, int(base_color.alpha() * char_alpha))))
                painter.setPen(pen)
                painter.drawText(QPointF(x, y - y_offset), char)
                x += advance
        except Exception:
            logger.exception("Transcription overlay text layer paintEvent failed")
        finally:
            if painter.isActive():
                painter.end()
