# gui_main.py
import tkinter as tk
from tkinter import messagebox
import sys
import autoemail
from core.run_controller import RunController
from core.settings_manager import SettingsManager
from core.email_queue import EmailQueue
from gui_app import OverlayApp
from path_manager import ensure_dirs, SETTINGS_JSON, LOCK_FILE
from lock_utils import acquire_lock, release_lock
from main import routine_loop  # 네가 main.py에 함수로 만든 routine_loop 가져옴


def run_gui():
    ensure_dirs()

    settings = SettingsManager(SETTINGS_JSON)

    emailq = EmailQueue()
    emailq.configure({
        "enabled": settings.get("email.enabled", False),
        "provider": settings.get("email.provider", "gmail"),
        "smtp_host": settings.get("email.smtp_host", ""),
        "smtp_port": settings.get("email.smtp_port", 587),
        "use_tls": settings.get("email.use_tls", True),
        "sender": settings.get("email.sender", ""),
        "app_password": settings.get("email.app_password", ""),
        # autoemail는 문자열을 쉼표/개행으로 split 하므로 str 유지
        "recipients": settings.get("email.recipients", ""),
        "subject_tmpl": settings.get("email.subject_tmpl", "[Manager] {event}"),
        "body_tmpl": settings.get("email.body_tmpl", "Event: {event}\nTime: {timestamp}\nHost: {hostname}"),
        "rate_limit_min_interval": settings.get("email.rate_limit_min_interval", 600),
        "rate_limit_burst": settings.get("email.rate_limit_burst", 1),
    })

    # autoemail도 동일 설정으로 초기화(legacy txt 모드 방지)
    autoemail.configure({
        "enabled": settings.get("email.enabled", False),
        "provider": settings.get("email.provider", "gmail"),
        "smtp_host": settings.get("email.smtp_host", ""),
        "smtp_port": settings.get("email.smtp_port", 587),
        "use_tls": settings.get("email.use_tls", True),
        "sender": settings.get("email.sender", ""),
        "app_password": settings.get("email.app_password", ""),
        "recipients": settings.get("email.recipients", ""),
        "subject_tmpl": settings.get("email.subject_tmpl", "메일 제목을 입력해주세요"),
        "body_tmpl": settings.get("email.body_tmpl", "본문 내용을 입력해주세요"),
    })

    controller = RunController(routine_loop)

    app = OverlayApp(settings, controller, emailq)
    try:
        app.mainloop()
    finally:
        release_lock(str(LOCK_FILE))


if __name__ == "__main__":
    run_gui()