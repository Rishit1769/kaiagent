import math
import logging

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRectF, QTimer, Qt, pyqtProperty
from PyQt6.QtGui import QColor, QCursor, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QApplication, QWidget

from ui.design import ANIM_FAST_MS, BLUE_GLOW, FONT_RESPONSE, TEXT_PRIMARY, TEXT_SECONDARY


logger = logging.getLogger(__name__)


class TranscriptionOverlay(QWidget):
    """Floating glass transcription capsule shown near the cursor while speaking."""

    CURSOR_OFFSET = QPoint(20, 20)
    MAX_TEXT_WIDTH = 300
    MIN_WIDTH = 220
    MIN_HEIGHT = 74
    HOLD_MS = 1500

    def __init__(self):
        super().__init__()
        self.partial_text = ""
        self.final_text = ""
        self._display_text = ""
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

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._animate_tick)
        self._tick.start(16)

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
        self._follow_timer.start()
        self._fade_to(1.0, ANIM_FAST_MS)
        self.update()

    def update_transcription(self, text: str):
        logger.info("Overlay transcription updated: %r", text)
        self.partial_text = text
        self._show_placeholder = not bool(text.strip()) and not self._mic_error
        self._display_text = text.strip()
        self._resize_for_text()
        self._pulse_text()
        self.update()

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
        self._pulse_text()
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
        self._fade_to(1.0, ANIM_FAST_MS)
        self._pulse_text()
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
        if self.isVisible():
            self._move_near_cursor()

    def _animate_tick(self):
        self._orb_phase += 0.12 + self._audio_level * 0.08
        self.update()

    def get_text_opacity(self) -> float:
        return self._text_opacity

    def set_text_opacity(self, value: float):
        self._text_opacity = value
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
            pulse = 1.0 + 0.08 * math.sin(self._orb_phase) + self._audio_level * 0.22
            outer = QRadialGradient(orb_center, 26 * pulse)
            outer.setColorAt(0.0, QColor(110, 180, 255, 155))
            outer.setColorAt(0.55, QColor(40, 140, 255, 72))
            outer.setColorAt(1.0, QColor(40, 140, 255, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(outer)
            painter.drawEllipse(orb_center, int(26 * pulse), int(26 * pulse))

            mid = QRadialGradient(orb_center, 12 * pulse)
            mid.setColorAt(0.0, QColor(220, 245, 255, 255))
            mid.setColorAt(0.45, QColor(90, 180, 255, 235))
            mid.setColorAt(1.0, QColor(36, 115, 255, 210))
            painter.setBrush(mid)
            painter.drawEllipse(orb_center, int(12 * pulse), int(12 * pulse))

            ring = QColor(190, 225, 255, 120)
            painter.setPen(QPen(ring, 1.3))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(orb_center, int(14 * pulse), int(14 * pulse))

            text_rect = rect.adjusted(66.0, 14.0, -18.0, -14.0)
            painter.setFont(FONT_RESPONSE)
            text_color = QColor(TEXT_SECONDARY if self._show_placeholder else TEXT_PRIMARY)
            if self._mic_error:
                text_color = QColor(255, 210, 210)
            text_color.setAlpha(int(255 * self._text_opacity))
            painter.setPen(text_color)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                self._current_text(),
            )
        except Exception:
            logger.exception("Transcription overlay paintEvent failed")
        finally:
            if painter.isActive():
                painter.end()
