# -*- coding: utf-8 -*-
from PyQt6.QtCore import QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

from config import cfg


def _make_tray_icon(color: QColor) -> QIcon:
    """Generate a simple colored circle as the tray icon."""
    px = QPixmap(QSize(22, 22))
    px.fill(Qt.GlobalColor.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(3, 3, 16, 16)
    painter.end()
    return QIcon(px)


class TrayManager(QObject):
    """Windows system tray icon and context menu."""

    on_show_panel = pyqtSignal()
    on_hide_panel = pyqtSignal()
    on_quit = pyqtSignal()
    on_toggle_search = pyqtSignal(bool)
    on_toggle_wake_word = pyqtSignal(bool)
    on_toggle_slow_mode = pyqtSignal(bool)
    on_toggle_quiz_mode = pyqtSignal(bool)
    on_toggle_privacy = pyqtSignal(bool)
    on_switch_provider = pyqtSignal(str)
    on_copilot_login = pyqtSignal()
    on_copilot_refresh = pyqtSignal()
    on_ollama_set_model = pyqtSignal(str, str)
    on_ollama_pull = pyqtSignal(str)
    on_ollama_refresh = pyqtSignal()
    on_stop = pyqtSignal()
    on_toggle_code_mode = pyqtSignal(bool)
    on_toggle_multilang = pyqtSignal(bool)
    on_toggle_journal = pyqtSignal(bool)
    on_toggle_ocr = pyqtSignal(bool)
    on_record_start = pyqtSignal()
    on_record_stop = pyqtSignal()
    on_collab_start = pyqtSignal()
    on_collab_join = pyqtSignal()
    on_workflow_start = pyqtSignal()
    on_workflow_stop = pyqtSignal()
    on_journal_open = pyqtSignal()
    on_attach_doc = pyqtSignal()
    on_run_setup = pyqtSignal()
    on_diagnostics = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icons = {
            "idle": _make_tray_icon(QColor(80, 80, 120)),
            "listening": _make_tray_icon(QColor(50, 200, 100)),
            "thinking": _make_tray_icon(QColor(0, 120, 255)),
            "speaking": _make_tray_icon(QColor(255, 140, 0)),
        }

        self._tray = QSystemTrayIcon()
        self._tray.setIcon(self._icons["idle"])
        self._tray.setToolTip(f"Kai Agent\nHold {cfg.hotkey} to speak")

        self._search_enabled = True
        self._wake_enabled = True
        self._slow_enabled = False
        self._quiz_enabled = False
        self._privacy_enabled = True
        self._code_enabled = True
        self._multilang_enabled = True
        self._journal_enabled = True
        self._ocr_enabled = True
        self._is_recording = False
        self._ollama_installed: dict[str, list[str]] = {"vision": [], "text": []}

        self._build_menu()
        self._tray.activated.connect(self._on_activated)
        self._tray.show()

    def _build_menu(self):
        menu = QMenu()
        menu.setStyleSheet(
            "QMenu { background: rgb(22,22,28); border: 1px solid rgb(55,55,70);"
            "border-radius: 8px; color: rgb(220,220,230); font-size: 13px; }"
            "QMenu::item:selected { background: rgb(0,90,200); border-radius: 4px; }"
            "QMenu::separator { height: 1px; background: rgb(55,55,70); margin: 4px 8px; }"
        )

        providers = cfg.describe()
        info = menu.addAction(
            f"LLM: {providers['llm']}  |  STT: {providers['stt']}  |  TTS: {providers['tts']}"
        )
        info.setEnabled(False)
        menu.addSeparator()

        menu.addAction("Show Panel").triggered.connect(self.on_show_panel)
        menu.addAction("Hide Panel").triggered.connect(self.on_hide_panel)
        menu.addAction("Stop (Esc)").triggered.connect(self.on_stop)
        menu.addSeparator()

        switch_menu = menu.addMenu(f"Model: {providers['llm']}")
        active = providers["llm"]
        for name in cfg.available_llm_providers():
            label = f"* {name}" if name == active else f"  {name}"
            switch_menu.addAction(label).triggered.connect(
                lambda _=False, n=name: self.on_switch_provider.emit(n)
            )
        switch_menu.addSeparator()
        switch_menu.addAction("Sign in to GitHub Copilot...").triggered.connect(
            self.on_copilot_login
        )
        switch_menu.addAction("Refresh Copilot models").triggered.connect(
            self.on_copilot_refresh
        )

        self._build_ollama_submenu(menu, providers)
        menu.addSeparator()

        self._search_action = menu.addAction(
            "Web Search: ON" if self._search_enabled else "Web Search: OFF"
        )
        self._search_action.setCheckable(True)
        self._search_action.setChecked(self._search_enabled)
        self._search_action.triggered.connect(self._toggle_search)

        self._wake_action = menu.addAction(
            "Wake word 'Kai Agent': ON"
            if self._wake_enabled
            else "Wake word 'Kai Agent': OFF"
        )
        self._wake_action.setCheckable(True)
        self._wake_action.setChecked(self._wake_enabled)
        self._wake_action.triggered.connect(self._toggle_wake)

        menu.addSeparator()
        tutor_menu = menu.addMenu("Tutor Mode")

        self._slow_action = tutor_menu.addAction(
            "Slow Mode (teacher pace): ON"
            if self._slow_enabled
            else "Slow Mode (teacher pace): OFF"
        )
        self._slow_action.setCheckable(True)
        self._slow_action.setChecked(self._slow_enabled)
        self._slow_action.triggered.connect(self._toggle_slow)

        self._quiz_action = tutor_menu.addAction(
            "Quiz Mode: ON" if self._quiz_enabled else "Quiz Mode: OFF"
        )
        self._quiz_action.setCheckable(True)
        self._quiz_action.setChecked(self._quiz_enabled)
        self._quiz_action.triggered.connect(self._toggle_quiz)

        self._privacy_action = tutor_menu.addAction(
            "Privacy Guard: ON"
            if self._privacy_enabled
            else "Privacy Guard: OFF"
        )
        self._privacy_action.setCheckable(True)
        self._privacy_action.setChecked(self._privacy_enabled)
        self._privacy_action.triggered.connect(self._toggle_privacy)

        self._code_action = tutor_menu.addAction(
            "Code Mode (auto): ON" if self._code_enabled else "Code Mode (auto): OFF"
        )
        self._code_action.setCheckable(True)
        self._code_action.setChecked(self._code_enabled)
        self._code_action.triggered.connect(self._toggle_code)

        self._ml_action = tutor_menu.addAction(
            "Multilingual: ON" if self._multilang_enabled else "Multilingual: OFF"
        )
        self._ml_action.setCheckable(True)
        self._ml_action.setChecked(self._multilang_enabled)
        self._ml_action.triggered.connect(self._toggle_multilang)

        self._ocr_action = tutor_menu.addAction(
            "OCR Fallback: ON" if self._ocr_enabled else "OCR Fallback: OFF"
        )
        self._ocr_action.setCheckable(True)
        self._ocr_action.setChecked(self._ocr_enabled)
        self._ocr_action.triggered.connect(self._toggle_ocr)

        menu.addSeparator()
        journal_menu = menu.addMenu("Journal")

        self._journal_action = journal_menu.addAction(
            "Logging: ON" if self._journal_enabled else "Logging: OFF"
        )
        self._journal_action.setCheckable(True)
        self._journal_action.setChecked(self._journal_enabled)
        self._journal_action.triggered.connect(self._toggle_journal)

        journal_menu.addAction("Open journal folder").triggered.connect(
            self.on_journal_open
        )
        journal_menu.addAction("Attach document (PDF / TXT / DOCX)...").triggered.connect(
            self.on_attach_doc
        )

        rec_menu = menu.addMenu("Lesson Recording")
        if self._is_recording:
            rec_menu.addAction("\u25cf Stop recording").triggered.connect(
                self.on_record_stop
            )
        else:
            rec_menu.addAction("Start recording").triggered.connect(
                self.on_record_start
            )

        wf_menu = menu.addMenu("Workflow Capture")
        wf_menu.addAction("Start capturing my clicks").triggered.connect(
            self.on_workflow_start
        )
        wf_menu.addAction("Stop + send to Kai Agent").triggered.connect(
            self.on_workflow_stop
        )

        menu.addSeparator()
        setup_menu = menu.addMenu("Setup && Diagnostics")
        setup_menu.addAction("Run setup wizard again...").triggered.connect(
            self.on_run_setup
        )
        setup_menu.addAction("Save diagnostics report...").triggered.connect(
            self.on_diagnostics
        )

        menu.addSeparator()
        menu.addAction("Quit Kai Agent").triggered.connect(self.on_quit)

        self._tray.setContextMenu(menu)
        self._menu = menu

    def _build_ollama_submenu(self, parent_menu: QMenu, providers: dict):
        from ai.ollama_models_registry import RECOMMENDED_TEXT, RECOMMENDED_VISION

        ol_menu = parent_menu.addMenu("Ollama")
        active_vision = providers.get("ollama_vision_model", "")
        active_text = providers.get("ollama_text_model", "")

        v_menu = ol_menu.addMenu(f"Vision model: {active_vision or '(none)'}")
        installed_vision = self._ollama_installed.get("vision", [])
        if installed_vision:
            for name in installed_vision:
                label = f"* {name}" if name == active_vision else f"  {name}"
                v_menu.addAction(label).triggered.connect(
                    lambda _=False, n=name: self.on_ollama_set_model.emit("vision", n)
                )
        else:
            empty = v_menu.addAction("(no vision models installed)")
            empty.setEnabled(False)

        t_menu = ol_menu.addMenu(f"Text model: {active_text or '(none)'}")
        installed_text = self._ollama_installed.get("text", [])
        if installed_text:
            for name in installed_text:
                label = f"* {name}" if name == active_text else f"  {name}"
                t_menu.addAction(label).triggered.connect(
                    lambda _=False, n=name: self.on_ollama_set_model.emit("text", n)
                )
        else:
            empty = t_menu.addAction("(no text models installed)")
            empty.setEnabled(False)

        ol_menu.addSeparator()
        pull_menu = ol_menu.addMenu("Pull recommended...")
        already = set(installed_vision) | set(installed_text)

        def _add_recs(rec_list, header):
            hdr = pull_menu.addAction(header)
            hdr.setEnabled(False)
            for rec in rec_list:
                installed = any(
                    n == rec.name or n.startswith(rec.name.split(":")[0] + ":")
                    for n in already
                )
                tag = "\u2714 " if installed else "  "
                label = f"{tag}{rec.label}  -  {rec.size}  -  {rec.blurb}"
                act = pull_menu.addAction(label)
                if installed:
                    act.setEnabled(False)
                else:
                    act.triggered.connect(
                        lambda _=False, n=rec.name: self.on_ollama_pull.emit(n)
                    )

        _add_recs(RECOMMENDED_VISION, "Vision")
        pull_menu.addSeparator()
        _add_recs(RECOMMENDED_TEXT, "Text")

        ol_menu.addSeparator()
        ol_menu.addAction("Refresh installed models").triggered.connect(
            self.on_ollama_refresh
        )

    def set_ollama_models(self, classified: dict):
        self._ollama_installed = {
            "vision": list(classified.get("vision", [])),
            "text": list(classified.get("text", [])),
        }
        self.rebuild_menu()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.on_show_panel.emit()

    def _toggle_search(self, checked: bool):
        self._search_enabled = checked
        self._search_action.setText("Web Search: ON" if checked else "Web Search: OFF")
        self.on_toggle_search.emit(checked)

    def _toggle_wake(self, checked: bool):
        self._wake_enabled = checked
        self._wake_action.setText(
            "Wake word 'Kai Agent': ON"
            if checked
            else "Wake word 'Kai Agent': OFF"
        )
        self.on_toggle_wake_word.emit(checked)

    def _toggle_slow(self, checked: bool):
        self._slow_enabled = checked
        self._slow_action.setText(
            "Slow Mode (teacher pace): ON"
            if checked
            else "Slow Mode (teacher pace): OFF"
        )
        self.on_toggle_slow_mode.emit(checked)

    def _toggle_quiz(self, checked: bool):
        self._quiz_enabled = checked
        self._quiz_action.setText("Quiz Mode: ON" if checked else "Quiz Mode: OFF")
        self.on_toggle_quiz_mode.emit(checked)

    def _toggle_privacy(self, checked: bool):
        self._privacy_enabled = checked
        self._privacy_action.setText(
            "Privacy Guard: ON" if checked else "Privacy Guard: OFF"
        )
        self.on_toggle_privacy.emit(checked)

    def _toggle_code(self, checked: bool):
        self._code_enabled = checked
        self._code_action.setText(
            "Code Mode (auto): ON" if checked else "Code Mode (auto): OFF"
        )
        self.on_toggle_code_mode.emit(checked)

    def _toggle_multilang(self, checked: bool):
        self._multilang_enabled = checked
        self._ml_action.setText(
            "Multilingual: ON" if checked else "Multilingual: OFF"
        )
        self.on_toggle_multilang.emit(checked)

    def _toggle_ocr(self, checked: bool):
        self._ocr_enabled = checked
        self._ocr_action.setText(
            "OCR Fallback: ON" if checked else "OCR Fallback: OFF"
        )
        self.on_toggle_ocr.emit(checked)

    def _toggle_journal(self, checked: bool):
        self._journal_enabled = checked
        self._journal_action.setText("Logging: ON" if checked else "Logging: OFF")
        self.on_toggle_journal.emit(checked)

    def set_recording_state(self, on: bool):
        self._is_recording = on
        self.rebuild_menu()

    def set_state_icon(self, state: str):
        self._tray.setIcon(self._icons.get(state, self._icons["idle"]))

    def rebuild_menu(self):
        self._build_menu()

    def show_notification(self, title: str, message: str):
        self._tray.showMessage(
            title, message, QSystemTrayIcon.MessageIcon.Information, 3000
        )

    @property
    def search_enabled(self) -> bool:
        return self._search_enabled
