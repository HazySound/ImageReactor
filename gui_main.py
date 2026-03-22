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
from main import routine_loop, reset_runtime_state  # 네가 main.py에 함수로 만든 routine_loop 가져옴


# ---- Settings SSOT 주입/지연 초기화 ----
_SETTINGS_SM = None  # type: Optional[SettingsManager]


def set_settings_manager(sm):
    global _SETTINGS_SM
    _SETTINGS_SM = sm


def SETTINGS() -> SettingsManager:
    """
    SSOT 접근자. GUI가 set_settings_manager로 주입하면 그대로 사용,
    아니면 단독 실행 시 1회만 생성.
    """
    global _SETTINGS_SM
    if _SETTINGS_SM is None:
        from core.settings_manager import SettingsManager  # 상단 import 유지 가능
        _SETTINGS_SM = SettingsManager(SETTINGS_JSON)
    return _SETTINGS_SM


def run_gui():
    ensure_dirs()

    settings = SettingsManager(SETTINGS_JSON)

    emailq = EmailQueue()
    emailq.configure({
        "enabled": SETTINGS().get("email.enabled", False),
        "provider": SETTINGS().get("email.provider", "gmail"),
        "smtp_host": SETTINGS().get("email.smtp_host", ""),
        "smtp_port": SETTINGS().get("email.smtp_port", 587),
        "use_tls": SETTINGS().get("email.use_tls", True),
        "sender": SETTINGS().get("email.sender", ""),
        "app_password": SETTINGS().get("email.app_password", ""),
        # autoemail는 문자열을 쉼표/개행으로 split 하므로 str 유지
        "recipients": SETTINGS().get("email.recipients", ""),
        "subject_tmpl": SETTINGS().get("email.subject_tmpl", "[Manager] {event}"),
        "body_tmpl": SETTINGS().get("email.body_tmpl", "Event: {event}\nTime: {timestamp}\nHost: {hostname}"),
        "rate_limit_min_interval": SETTINGS().get("email.rate_limit_min_interval", 600),
        "rate_limit_burst": SETTINGS().get("email.rate_limit_burst", 1),
    })

    # autoemail도 동일 설정으로 초기화(legacy txt 모드 방지)
    autoemail.configure({
        "enabled": SETTINGS().get("email.enabled", False),
        "provider": SETTINGS().get("email.provider", "gmail"),
        "smtp_host": SETTINGS().get("email.smtp_host", ""),
        "smtp_port": SETTINGS().get("email.smtp_port", 587),
        "use_tls": SETTINGS().get("email.use_tls", True),
        "sender": SETTINGS().get("email.sender", ""),
        "app_password": SETTINGS().get("email.app_password", ""),
        "recipients": SETTINGS().get("email.recipients", ""),
        "subject_tmpl": SETTINGS().get("email.subject_tmpl", "메일 제목을 입력해주세요"),
        "body_tmpl": SETTINGS().get("email.body_tmpl", "본문 내용을 입력해주세요"),
    })

    controller = RunController(worker=routine_loop, reset_fn=reset_runtime_state)

    app = OverlayApp(settings, controller, emailq)
    try:
        app.mainloop()
    finally:
        release_lock(str(LOCK_FILE))


if __name__ == "__main__":
    run_gui()