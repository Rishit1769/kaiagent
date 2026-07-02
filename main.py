# -*- coding: utf-8 -*-
"""
Kai Agent for Windows - Entry Point.
Boots Qt, spawns overlay+panel+tray, starts ambient mic listener, binds hotkey.
"""

import os
import sys
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from companion_manager import CompanionManager
from config import cfg
from hotkey import GlobalHotkeyMonitor, StopHotkey
from ui.overlay import (
    MODE_IDLE,
    MODE_LISTENING,
    MODE_SPEAKING,
    MODE_THINKING,
    CursorOverlay,
)
from ui.panel import AppState, CompanionPanel
from ui.tray import TrayManager


STATE_TO_CURSOR_MODE = {
    AppState.IDLE: MODE_IDLE,
    AppState.LISTENING: MODE_LISTENING,
    AppState.THINKING: MODE_THINKING,
    AppState.SPEAKING: MODE_SPEAKING,
}


def _copilot_login_flow(tray, panel, manager):
    """Run the GitHub device-flow login in a worker thread so the UI stays live."""
    import asyncio
    import threading

    from ai.github_copilot_provider import device_login

    def _on_code(user_code: str, verification_uri: str):
        panel.show_copilot_code(user_code, verification_uri)
        tray.show_notification("GitHub Copilot - enter this code", user_code)

    def _worker():
        try:
            asyncio.run(device_login(on_code=_on_code))
            tray.show_notification(
                "GitHub Copilot",
                "Signed in! Refreshing model list...",
            )
            manager.refresh_copilot_models()
        except Exception as e:
            tray.show_notification("Copilot login failed", str(e))
            panel.show_copilot_error(str(e))

    threading.Thread(target=_worker, daemon=True).start()


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Kai Agent")
    app.setApplicationDisplayName("Kai Agent")

    manager = CompanionManager()
    panel = CompanionPanel()
    overlay = CursorOverlay()
    tray = TrayManager()

    def _on_state(state: AppState):
        panel.set_state(state)
        tray.set_state_icon(state.name.lower())
        overlay.set_mode(STATE_TO_CURSOR_MODE.get(state, MODE_IDLE))

    manager.sig_state_changed.connect(_on_state)
    manager.sig_response_chunk.connect(panel.append_response_chunk)
    manager.sig_audio_level.connect(panel.set_audio_level)
    manager.sig_audio_level.connect(overlay.set_audio_level)
    manager.sig_point_at.connect(overlay.point_at)
    manager.sig_point_hold.connect(overlay.set_point_hold)
    manager.sig_point_release.connect(overlay.release_point)
    manager.sig_arrow.connect(overlay.add_arrow)
    manager.sig_circle.connect(overlay.add_circle)
    manager.sig_underline.connect(overlay.add_underline)
    manager.sig_label.connect(overlay.add_text)
    manager.sig_error.connect(
        lambda e: tray.show_notification("Kai Agent error", str(e))
    )

    panel.on_model_changed.connect(manager.set_model)

    def _on_doc_dropped(path: str):
        ok = manager.attach_document(path)
        tray.show_notification(
            "Document Attached" if ok else "Attach failed",
            f"{path}\nAsk Kai Agent about it now." if ok else "Couldn't read that file.",
        )

    panel.on_document_dropped.connect(_on_doc_dropped)

    tray.on_show_panel.connect(panel.show)
    tray.on_hide_panel.connect(panel.hide)
    tray.on_toggle_search.connect(manager.set_web_search)
    tray.on_toggle_wake_word.connect(manager.set_wake_word)
    tray.on_toggle_slow_mode.connect(manager.set_slow_mode)
    tray.on_toggle_slow_mode.connect(overlay.set_slow_mode)
    tray.on_toggle_quiz_mode.connect(manager.set_quiz_mode)
    tray.on_toggle_privacy.connect(manager.set_privacy_guard)
    tray.on_toggle_code_mode.connect(manager.set_code_mode_auto)
    tray.on_toggle_multilang.connect(manager.set_multilang)
    tray.on_toggle_journal.connect(manager.set_journal)
    tray.on_toggle_ocr.connect(manager.set_ocr_enabled)

    def _record_start():
        out = manager.start_recording()
        tray.show_notification(
            "Lesson Recording",
            f"Recording to:\n{out}"
            if out
            else "Failed - install imageio[ffmpeg]: pip install imageio imageio-ffmpeg",
        )

    def _record_stop():
        out = manager.stop_recording()
        if out:
            tray.show_notification("Lesson saved", out)

    tray.on_record_start.connect(_record_start)
    tray.on_record_stop.connect(_record_stop)
    manager.sig_recording_state.connect(lambda on, _path: tray.set_recording_state(on))

    def _wf_start():
        ok = manager.workflow_start()
        tray.show_notification(
            "Workflow Capture",
            "Recording your clicks + keys. Stop from tray when done."
            if ok
            else "Install pynput: pip install pynput",
        )

    def _wf_stop():
        summary = manager.workflow_stop()
        if summary:
            tray.show_notification(
                "Workflow Captured",
                "Sent to Kai Agent as context. Ask: 'what did I just do?'",
            )
            manager._attached_docs.append(("recorded_workflow.txt", summary))

    tray.on_workflow_start.connect(_wf_start)
    tray.on_workflow_stop.connect(_wf_stop)

    tray.on_collab_start.connect(manager.collab_start_host)

    def _collab_join():
        from PyQt6.QtWidgets import QInputDialog

        code, ok = QInputDialog.getText(
            None, "Join Live Session", "Enter 6-character session code:"
        )
        if ok and code:
            manager.collab_join(code.strip())

    tray.on_collab_join.connect(_collab_join)

    def _open_journal():
        import subprocess

        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "Kai Agent")
        try:
            os.startfile(path)
        except Exception:
            subprocess.Popen(["explorer", path])

    tray.on_journal_open.connect(_open_journal)

    def _attach_doc():
        from PyQt6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            None,
            "Attach a document for Kai Agent",
            "",
            "Documents (*.pdf *.docx *.txt *.md *.csv)",
        )
        if path:
            ok = manager.attach_document(path)
            tray.show_notification(
                "Document Attached",
                f"{path}\nAsk Kai Agent about it now."
                if ok
                else "Couldn't read that file.",
            )

    tray.on_attach_doc.connect(_attach_doc)

    def _switch(name: str):
        manager.set_active_provider(name)
        panel.refresh_for_provider(name)
        tray.rebuild_menu()
        tray.show_notification("Kai Agent", f"Switched to {name}")

    tray.on_switch_provider.connect(_switch)
    tray.on_stop.connect(manager.stop)
    tray.on_copilot_login.connect(lambda: _copilot_login_flow(tray, panel, manager))
    tray.on_copilot_refresh.connect(manager.refresh_copilot_models)

    def _on_copilot_models_done(count: int):
        if cfg.llm_provider() == "copilot":
            panel.refresh_for_provider("copilot")
        tray.show_notification(
            "GitHub Copilot",
            f"Loaded {count} models from your seat. Free models are tagged in the Model dropdown.",
        )

    manager.sig_copilot_models_done.connect(_on_copilot_models_done)

    def _on_models_refreshed(provider: str, count: int):
        if cfg.llm_provider() == provider:
            panel.refresh_for_provider(provider)

    manager.sig_models_refreshed.connect(_on_models_refreshed)

    tray.on_ollama_set_model.connect(manager.set_ollama_model)
    tray.on_ollama_pull.connect(manager.pull_ollama_model)
    tray.on_ollama_refresh.connect(manager.refresh_ollama_models)
    manager.sig_ollama_models.connect(tray.set_ollama_models)

    def _on_ollama_pull_status(name: str, status: str):
        tray.show_notification("Ollama", status)

    manager.sig_ollama_pull_status.connect(_on_ollama_pull_status)

    if cfg.llm_provider() == "ollama":
        manager.refresh_ollama_models()

    def _run_setup_again():
        from ui.setup_wizard import SetupWizard

        wiz = SetupWizard()
        wiz.show()
        _setup_keepalive[0] = wiz

    tray.on_run_setup.connect(_run_setup_again)

    def _save_diagnostics():
        import datetime
        import platform
        import traceback

        from ai import ollama_bootstrap as ob

        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        out = Path(base) / "Kai Agent" / (
            f"diagnostics-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        )
        try:
            providers_d = cfg.describe()
        except Exception:
            providers_d = {}

        report = []
        report.append(
            f"Kai Agent diagnostics - {datetime.datetime.now().isoformat()}"
        )
        report.append(f"Python: {sys.version.split()[0]}")
        report.append(f"Platform: {platform.platform()}")
        report.append(f"Active LLM: {providers_d.get('llm', '?')}")
        report.append(
            f"STT: {providers_d.get('stt', '?')}  TTS: {providers_d.get('tts', '?')}"
        )
        report.append("")
        report.append("--- Ollama ---")
        try:
            report.append(f"Host: {cfg.ollama_host}")
            report.append(f"Text model:   {cfg.ollama_text_model}")
            report.append(f"Vision model: {cfg.ollama_vision_model}")
            report.append(f"Binary on PATH: {ob.is_ollama_installed()}")
            report.append(f"Server reachable: {ob.is_ollama_running()}")
            if ob.is_ollama_running():
                report.append(f"Installed models: {ob.list_installed_models()}")
        except Exception:
            report.append(traceback.format_exc())
        report.append("")
        report.append("--- GitHub Copilot ---")
        try:
            from ai.github_copilot_provider import _token_path, is_authenticated

            report.append(
                f"Token file: {_token_path()}  exists={_token_path().exists()}"
            )
            report.append(f"Authenticated: {is_authenticated()}")
        except Exception:
            report.append(traceback.format_exc())
        try:
            out.write_text("\n".join(report), encoding="utf-8")
            tray.show_notification("Diagnostics saved", str(out))
            try:
                os.startfile(str(out))
            except Exception:
                pass
        except Exception as e:
            tray.show_notification("Diagnostics failed", str(e))

    tray.on_diagnostics.connect(_save_diagnostics)
    tray.on_quit.connect(lambda: (manager.shutdown(), app.quit()))

    hotkey = GlobalHotkeyMonitor(
        on_press=manager.on_hotkey_press,
        on_release=manager.on_hotkey_release,
    )
    hotkey.start()

    stop_key = StopHotkey(on_stop=manager.stop, key="esc")
    stop_key.start()

    overlay.show()
    manager.start()

    providers = cfg.describe()
    tray.show_notification(
        "Kai Agent is running",
        f"Say 'Kai Agent' or hold {cfg.hotkey}  |  LLM: {providers['llm']}",
    )

    try:
        from ui.setup_wizard import SetupWizard, maybe_show_setup_wizard

        if os.environ.get("KAI_AGENT_FORCE_SETUP", "").strip() in ("1", "true", "yes"):
            wiz = SetupWizard()
            wiz.show()
            _setup_keepalive[0] = wiz
        else:
            wiz = maybe_show_setup_wizard()
            if wiz is not None:
                _setup_keepalive[0] = wiz
    except Exception as e:
        print(f"[setup-wizard] skipped: {e}")

    sys.exit(app.exec())


_setup_keepalive: list = [None]


if __name__ == "__main__":
    main()
