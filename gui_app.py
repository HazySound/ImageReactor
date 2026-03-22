# gui_app.py
from __future__ import annotations
import atexit, os, sys
import tkinter as tk
from tkinter import messagebox
from lock_utils import remove_lock
import mss
import customtkinter as ctk
import cv2
import numpy as np
import gc
import time
from PIL import Image, ImageEnhance, ImageGrab, ImageTk
import autoemail
from control_bus import start_event, stop_event
from core.goal_provider import GoalConfigProvider
from core.score_calib import load_scaled_rois_for_current_screen  # 기존 로직 유지 (목표달성 토글 검증용)
# Tesseract 경로 자동 탐지/검증/주입
from core.ocr import (
    auto_find_tesseract_path,
    validate_tesseract,
    set_tesseract_path,
    _match_out_of_range_ko as _is_oor_ko,
)
from path_manager import ASSETS_DIR, SETTINGS_JSON, RESOURCES_DIR
import path_manager as pm
from ui.preset_editor import PresetEditorDialog
from ui.settings_dialog import SettingsDialog
from ui.widgets.hold_button import HoldCircleButton
from core.global_hotkeys import GlobalHotkeys
from core.home_anchor import is_home_configured
from core.state_store import get_state_store  # ★ 추가: UI busy 플래그 공유

pm.chdir_to_base()  # ★ 앱 시작 직후 CWD=exe로 고정

THEME = {
    "APP_BG": "#2f3238",
    "PANEL_BG": "#363a40",
    "LOG_BG": "#3d424a",
    "TEXT": "#e7e9ec",
}

# --- OCR 테스트 라벨 색상 ---
_COLOR_OK = "#10b981"  # green-500
_COLOR_WARN = "#f59e0b"  # amber-500
_COLOR_MUTED = "#9ca3af"  # gray-400


# --- lock 파일/러너 정리 공용 헬퍼 ---
def _cleanup_on_exit(root: tk.Misc | None = None):
    """GUI 종료 시 락 및 리소스 정리."""
    try:
        # main.py와 같은 routine.lock 제거
        remove_lock()
    except Exception:
        pass

    # (선택) path_manager에 별도 LOCK_FILE을 쓰는 경우도 함께 제거
    try:
        lf = getattr(pm, "LOCK_FILE", None)
        if lf and os.path.exists(lf):
            os.remove(lf)
    except Exception:
        pass

    # Tk가 살아있으면 안전하게 내려준다
    try:
        if root is not None and root.winfo_exists():
            root.destroy()
    except Exception:
        pass


# 프로세스가 어떤 방식으로 끝나든 마지막에 한 번 더 시도
atexit.register(_cleanup_on_exit)


# ======================================================================
# OverlayApp
# ======================================================================
class OverlayApp(ctk.CTk):
    """메인 오버레이 앱 (CTk 창)"""

    def __init__(self, settings_mgr, controller, email_queue):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")
        self.configure(fg_color=THEME["APP_BG"])

        self.settings = settings_mgr
        # ★ 메인 모듈에 동일 인스턴스 주입(SSOT 통일)
        try:
            import main as _main
            if hasattr(_main, "set_settings_manager"):
                _main.set_settings_manager(self.settings)
        except Exception:
            pass

        self.controller = controller
        self.emailq = email_queue
        self.goal_provider = GoalConfigProvider(self.settings)

        # --- OCR 엔진 경로 보장(자동 탐지 → 주입 → settings 저장) ---
        self._ensure_tesseract_path(silent=True)

        # 윈도우
        self.title("ImageReactor")
        self.attributes("-topmost", True)
        self.geometry("640x400")
        self.minsize(640, 360)
        self.resizable(False, False)

        # ★ 작업표시줄/타이틀바 아이콘 지정 (Windows)
        try:
            ico_path = os.path.join(str(RESOURCES_DIR), "icon", "icon.ico")
            if os.path.exists(ico_path):
                # Tk에게 큰/작은 아이콘 모두 전달 (Windows에서 작업표시줄에도 반영)
                self.iconbitmap(ico_path)
        except Exception:
            pass

        # 알파(포인터 따라 투명도)
        self.opacity = float(self.settings.get("ui.opacity", 1.0))
        self._alpha_state = "manual"
        self._alpha_track_enabled = False  # ← 기본 off
        self._current_opacity = max(0.35, min(1.0, self.opacity))

        # ★ 누락 필드 보강: 트래킹 로직이 참조하는 값
        # ── Event-driven alpha settings ──
        self._ALPHA_ENTER_IMMEDIATE = True  # Enter 즉시 1.0
        self._ALPHA_LEAVE_DELAY_MS = 120  # Leave 후 지연 적용
        self._ALPHA_BORDER_INSET_PX = 6  # 경계 히스테리시스(공간)

        self._alpha_evt_enabled = False  # 이벤트 드리븐 on/off
        self._alpha_inside = False  # 현재 포인터가 창 내부로 판정되었는지
        self._alpha_leave_deadline_ms = 0  # Leave 지연 데드라인(ms)
        self._alpha_leave_after_id = None  # 단일 after 핸들

        self.idle_alpha = float(self._current_opacity)
        # 저사양 모드 ↔ 일반 모드 전환 시 복원용 상태
        self._low_spec_applied: bool = bool(self.settings.get("gui.low_spec", False))
        self._opacity_before_low_spec: float | None = None
        self.hover_alpha = 1.0

        self._wndproc_installed = False  # 현재 WNDPROC 서브클래싱 여부
        self._orig_wndproc = None  # 복원용 원래 WNDPROC 포인터
        self._win32_hook_handle = None  # 이미 있다면 유지
        self._win32_cb = None  # 이미 있다면 유지
        self._win32_hook_last_ts = 0.0  # ← 마지막 설치/해제 시각(초) for 디바운스

        # layered 적용 여부 추적(마지막으로 attributes로 적용한 값)
        self._last_attr_alpha = None
        if self._current_opacity < 0.999:
            try:
                self.attributes("-alpha", float(self._current_opacity))
                self._last_attr_alpha = float(self._current_opacity)  # ★추가
            except Exception:
                pass

        self._alpha_temp_elevated = False  # busy 동안 임시 1.0 승격 여부
        self._alpha_restore_value = None  # 복귀용 값

        self._last_toggle_ms = 0

        self.mode_var = tk.StringVar(value="compact")
        self.state_var = tk.StringVar(value="IDLE")

        self._build_ui()
        self._init_goal_ui_bindings()

        # ── 단일 UI 틱 설정 ──
        self._TICK_MS = int(self.settings.get("gui.tick_ms", 250))  # 250~300ms 권장
        self._ui_tick_guard = False  # 재진입 방지
        # 폴링 예약 핸들(없으면 None)
        self._poll_after_id = None

        # [ADD] 시작 시 마지막 상태 복원에 따른 시각/로그 반영
        self._sync_goal_visual()
        self._log_startup_goal_and_hotkey()

        # main 모듈에 상태 로거를 주입해 예약 종료/상태 메시지가 색상 로그로 찍히게 함
        try:
            import main as _mainmod
            if hasattr(self, "log_status"):
                _mainmod.set_status_logger(self.log_status)  # (text, fg) 시그니처
                _mainmod.log_scheduled_shutdown_state_current()
                _mainmod.log_spam_state()
        except Exception:
            pass

        if bool(self.settings.get("gui.low_spec", False)):
            self._apply_low_spec_profile()

        # 다음 틱 예약(단일 루프)
        self._poll_after_id = self.after(self._TICK_MS, self._poll_controller)
        self._log_gui(f"해상도 : {self.winfo_screenwidth()} X {self.winfo_screenheight()}")

        geo = self.settings.get("gui._last_geometry")
        if geo:
            try:
                self.geometry(geo)
            except Exception:
                pass

        # --- 이메일 설정: 실행 시점에는 settings.json만 반영 ---
        try:
            s_email = self.settings.get("email", {}) or {}
            autoemail.configure(s_email)  # enabled 명시 없으면 False로 폴백
            self.emailq.configure(s_email)
        except Exception as e:
            print(f"[email] 초기 구성 실패: {e}")

        # 3) 큐 시작
        self.emailq.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ★ 추가: GUI 상호작용(드래그/리사이즈) 시 busy 플래그 on
        self._ui_busy_debounce = None
        self.bind("<ButtonPress-1>", lambda e: self._ui_busy_set(True))
        self.bind("<B1-Motion>", lambda e: self._ui_busy_set(True))
        self.bind("<ButtonRelease-1>", lambda e: self._ui_busy_set(False))

        self._gc_was_enabled = True

        # busy OFF 데드라인/체커
        self._ui_busy_deadline_ms = 0
        self._ui_busy_off_checker_id = None

        # 리사이즈/이동은 디바운스된 핸들러로 교체
        self.bind("<Configure>", self._on_root_configure)

        self._cfg_after = None  # 디바운스 핸들러용 타이머 핸들
        # Windows에서 이동/리사이즈 시작/종료를 정확히 잡아내는 훅
        try:
            if not bool(self.settings.get("gui.low_spec", False)):
                self._install_win32_movesize_hooks()
        except Exception:
            pass

        # 전역 핫키 등록: 게임 포커스 상태에서도 F9/F12/ESC가 동작하도록
        try:
            self._hotkeys = GlobalHotkeys(
                # ↘ Tk 메인스레드로 디스패치
                on_start=lambda: self.after(0, self._on_start),
                on_stop=lambda: self.after(0, self._on_stop),
                on_quit=lambda: self.after(0, self._on_close),
                log=self._log_gui,  # 콘솔 대신 GUI 로그창에 찍고 싶으면 이렇게
                on_toggle=lambda: self.after(0, self._toggle_goal_switch)  # ✅ 전역 F8
            )
            self._hotkeys.register()
            self._global_hotkey_ok = True

        except Exception as e:
            print(f"[hotkey] 전역 핫키 등록 실패: {e}")
            self._global_hotkey_ok = False
            # 실패해도 아래 bind_all이 남아있으므로 GUI 포커스에서는 동작함

        self._ocr_auto_overlay_ctx = None
        self._ocr_auto_overlay_last = False

        # 윈도우 핸들 준비
        try:
            self.update_idletasks()
        except Exception:
            pass

        # 시작 시 훅 상태 맞춤: low_spec → 해제, 아니면 설치
        try:
            want_install = not bool(self.settings.get("gui.low_spec", False))
            # mainloop 전이라도 after(0)로 예약하면 안전
            self.after(0, lambda: self._ensure_win32_hooks(want_install))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        padx, pady = 10, 8
        self._expanded = True

        # ── 상단 바 ─────────────────────────────────
        top = ctk.CTkFrame(self, fg_color=THEME["PANEL_BG"])
        top.pack(fill="x", padx=padx, pady=(8, 6))
        bar = ctk.CTkFrame(top, fg_color="transparent")
        bar.pack(fill="x")

        PAD_STATE = 14
        PAD_TINY = 2
        PAD_SMALL = 6
        TOGGLE_WIDTH = 116
        PRESET_WIDTH = 140

        # 상태 점 + 라벨
        self.state_dot = ctk.CTkCanvas(bar, width=16, height=16, highlightthickness=0, bg=THEME["PANEL_BG"])
        self.state_dot.pack(side="left", padx=(0, PAD_TINY))
        self._set_state_color("IDLE")
        ctk.CTkLabel(bar, textvariable=self.state_var, text_color=THEME["TEXT"]).pack(side="left", padx=(0, PAD_STATE))

        # 목표달성 토글 + 콤보 + 편집
        self.lbl_goal = ctk.CTkLabel(bar, text="목표달성 모드", text_color=THEME["TEXT"])
        self.lbl_goal.pack(side="left", padx=(0, PAD_SMALL))

        self.var_goal_enabled = tk.BooleanVar(value=bool(self.settings.get("goal.enabled", False)))
        self.sw_goal = ctk.CTkSwitch(bar, text=None, variable=self.var_goal_enabled, command=self._on_goal_toggle,
                                     width=44)
        self.sw_goal.pack(side="left", padx=(0, PAD_TINY))

        goal_presets = self.settings.get("goal.presets", {}) or {}
        ordered_ids = sorted(goal_presets.keys())
        preset_names = [goal_presets[pid].get("name", pid) for pid in ordered_ids]
        active_id = self.settings.get("goal.active_preset_id", "p1")
        active_name = (goal_presets.get(active_id, {}) or {}).get("name", preset_names[0] if preset_names else "")

        self.combo_goal = ctk.CTkComboBox(
            bar,
            values=preset_names,
            command=self._on_goal_preset_changed,
            width=PRESET_WIDTH,
            state="readonly" if preset_names else "disabled",
        )
        if preset_names:
            self.combo_goal.set(active_name)
        self.combo_goal.pack(side="left", padx=(0, PAD_SMALL))

        self.btn_goal_edit = ctk.CTkButton(bar, text="편집", width=54, command=self._on_goal_edit)
        self.btn_goal_edit.pack(side="left", padx=(0, PAD_SMALL))

        spacer = ctk.CTkLabel(bar, text="", width=1)
        spacer.pack(side="left", expand=True)

        gear_img = self._load_icon("option.png", size=20)
        self._img_gear = gear_img
        self.btn_settings = ctk.CTkButton(
            bar, width=36, height=28, text="" if gear_img else "설정", image=gear_img, command=self._open_settings
        )
        self.btn_settings.pack(side="left", padx=(0, PAD_SMALL))

        self.toggle_btn = ctk.CTkButton(bar, text="", width=TOGGLE_WIDTH, command=self._toggle_mode)
        self.toggle_btn.pack(side="left")

        # ── 본문: 로그 ──────────────────────────────
        body = ctk.CTkFrame(self, fg_color=THEME["PANEL_BG"])
        body.pack(fill="both", expand=True, padx=padx, pady=(0, pady))
        self.log = ctk.CTkTextbox(body)
        self.log.pack(fill="both", expand=True)
        # 기본 로그 폰트(예: 12~13pt). settings로 노출하려면 'gui.log_font_size' 키를 읽어도 됨.
        log_font_size = int(self.settings.get("gui.log_font_size", 11))
        log_font_family = self.settings.get("gui.log_font_family", "맑은 고딕")
        ct_font = ctk.CTkFont(family=log_font_family, size=log_font_size)
        self.log.configure(
            fg_color=THEME["LOG_BG"],
            text_color=THEME["TEXT"],
            state="disabled",
            font=ct_font,  # ✅ CTkFont로 지정
        )

        # [ADD] 로그 컬러 태그 등록
        try:
            t = self.log._textbox
            t.tag_configure("base", font=(log_font_family, log_font_size))
            t.tag_configure("green", foreground="#16A34A")
            t.tag_configure("amber", foreground="#F59E0B")
            t.tag_configure("gray", foreground="#9CA3AF")
            t.tag_configure("red", foreground="#EF4444")
            t.tag_configure("bold", font=(log_font_family, log_font_size, "bold"))  # ✅ 위와 동일 크기
            t.tag_configure("cyan", foreground="#00BCD4")
        except Exception:
            pass

        # ── 하단 바: 좌표/검증 + 시작/정지 ────────────
        self.bottom = ctk.CTkFrame(self, fg_color=THEME["PANEL_BG"])
        self.bottom.pack(fill="x", padx=padx, pady=(0, 10))

        # === 좌측 컨트롤 ===
        left_controls = ctk.CTkFrame(self.bottom, fg_color="transparent")
        left_controls.pack(side="left")
        self.btn_roi = ctk.CTkButton(left_controls, text="좌표 설정", width=120, height=36, command=self._open_roi_editor)
        self.btn_roi.pack(side="left", padx=(10, 0))
        self.btn_calib = ctk.CTkButton(left_controls, text="테스트", width=120, height=36, command=self._open_verify)
        self.btn_calib.pack(side="left", padx=(10, 0))

        # [ADD] 좌표 유무에 따라 '좌표 확인' 초기 상태 반영
        self._update_calib_buttons_state()

        # ── 중간: 투명도 슬라이더 ─────────────────────────────────
        alpha_controls = ctk.CTkFrame(self.bottom, fg_color="transparent")
        alpha_controls.pack(side="left", padx=8)

        ctk.CTkLabel(alpha_controls, text="불투명도", width=0).pack(side="left", padx=(2, 6))
        self.alpha_slider = ctk.CTkSlider(
            alpha_controls,
            from_=0.35, to=1.0,
            number_of_steps=65,  # 0.01 단위 체감
            width=180,
            command=self._on_opacity_drag
        )
        self.alpha_slider.set(getattr(self, "_current_opacity", 1.0))
        self.alpha_slider.pack(side="left")

        # 드래그 종료 시 확정 적용(전환 최소화)
        self.alpha_slider.bind("<ButtonRelease-1>", self._on_opacity_release)

        # === 우측 컨트롤 ===
        right_controls = ctk.CTkFrame(self.bottom, fg_color=THEME["PANEL_BG"])
        right_controls.pack(side="right")
        img_play = os.path.join(str(ASSETS_DIR), "play.png")
        img_stop = os.path.join(str(ASSETS_DIR), "stop.png")
        self.stop_hold = HoldCircleButton(
            right_controls,
            diameter=72,
            hold_ms=int(self.settings.get("safety.stop_longpress_ms", 1200)),
            on_tap=self._on_start,
            on_hold_complete=self._on_stop,
            play_icon_path=img_play,
            stop_icon_path=img_stop,
            fg_color=THEME["PANEL_BG"],
            progress_width=3,
        )
        self.stop_hold.pack(side="right")
        self.stop_hold.set_running(False)

        self._apply_expanded_state()
        self.bind_all("<F9>", lambda e: self._on_start())
        self.bind_all("<F12>", lambda e: self._on_stop())
        if not getattr(self, "_global_hotkey_ok", False):
            # 전역 훅 실패시에만 폴백으로 GUI F8 바인딩
            self.bind_all("<F8>", lambda e: self._toggle_goal_switch())

    # ------------------------------------------------------------------
    # 상태/알파
    # ------------------------------------------------------------------
    # --- Opacity helpers (최소 비용) ---
    def _on_opacity_drag(self, val):
        # 저사양 모드에서는 슬라이더 입력 무시
        if bool(self.settings.get("gui.low_spec", False)):
            return

        # 슬라이더 미리보기: 항상 현재 값 즉시 반영(hover/inside 무시)
        try:
            v = max(0.35, min(1.0, float(val)))
        except Exception:
            return
        self._current_opacity = v
        self.idle_alpha = float(v)
        # busy 중일 땐 _set_alpha_if_needed 내부 가드로 자연 스킵됨
        self._set_alpha_if_needed(v, update_idle=True, force=True, tag="drag")

    def _on_opacity_release(self, _evt=None):
        # 저사양 모드에서는 커밋 무시
        if bool(self.settings.get("gui.low_spec", False)):
            return

        # 1) 드래그 종료 시 busy 먼저 OFF
        try:
            self._ui_busy_set(False)
        except Exception:
            pass

        v = float(getattr(self, "_current_opacity", 1.0))
        self.idle_alpha = float(v)

        # 2) 즉시 적용 (busy 가드 우회 보장을 위해 after(0)로 한 번 더 안전하게)
        try:
            # 즉시 한 번
            self._set_alpha_if_needed(v, update_idle=True, force=True, tag="release")
            # 이벤트 전파 순서에 대비해 다음 틱에서도 한 번 더
            self.after(0, lambda: self._set_alpha_if_needed(v, update_idle=True, force=True, tag="release+after"))
        except Exception:
            pass

        # 3) settings.json 저장
        try:
            self.settings.set("ui.opacity", float(v))
            self.settings.save()
        except Exception:
            pass

    def _on_root_configure(self, _e=None):
        # 디바운스: 50ms 이내 중복 Configure 이벤트 무시 (창 드래그 시 수십 회/초 발생 방지)
        try:
            if self._cfg_after is not None:
                return
            self._cfg_after = self.after(50, self._on_root_configure_debounced)
        except Exception:
            pass

    def _on_root_configure_debounced(self):
        self._cfg_after = None
        try:
            self._ui_busy_set(True)
        except Exception:
            pass

    def _is_effectively_one(self, v: float, eps: float = 1e-3) -> bool:
        try:
            return abs(float(v) - 1.0) <= eps
        except Exception:
            return False

    def _is_pointer_inside_window_precise(self, event=None) -> bool:
        """최상위 기준 내부 판정(경계 히스테리시스 포함). 자식 위젯 간 이동 오발 제거."""
        try:
            rx, ry = self.winfo_rootx(), self.winfo_rooty()
            rw, rh = self.winfo_width(), self.winfo_height()
            inset = int(getattr(self, "_ALPHA_BORDER_INSET_PX", 0))
            x0, y0 = rx + inset, ry + inset
            x1, y1 = rx + rw - inset, ry + rh - inset
            if event is not None and hasattr(event, "x_root") and hasattr(event, "y_root"):
                xr, yr = int(event.x_root), int(event.y_root)
            else:
                xr, yr = self.winfo_pointerx(), self.winfo_pointery()
            return (x0 <= xr < x1) and (y0 <= yr < y1)
        except Exception:
            return False

    def _set_alpha_if_needed(self, v, update_idle=False, *, force: bool = False, tag: str = ""):
        try:
            store = get_state_store()
            busy = bool(store.is_ui_busy())
        except Exception:
            busy = False

        try:
            prev = float(self.attributes("-alpha"))
        except Exception:
            prev = float(getattr(self, "_current_opacity", 1.0))

        # busy 가드
        if busy and not force:
            # self._log_gui(f"[alpha] SKIP busy tag={tag} v={v:.3f} prev={prev:.3f}")
            return

        # 동일값 스킵
        try:
            if abs(prev - float(v)) < 1e-3:
                if update_idle:
                    self.idle_alpha = float(v)
                return
        except Exception:
            pass

        # 실제 적용
        try:
            self.attributes("-alpha", float(v))
        except Exception as e:
            try:
                self._log_gui(f"[alpha][ERR] set failed: {e}")
            except Exception:
                pass

        if update_idle:
            self.idle_alpha = float(v)

    def _apply_alpha_hover(self):
        self._set_alpha_if_needed(1.0, update_idle=False)  # ★ hover는 idle_alpha 건드리지 않음

    def _apply_alpha_idle(self):
        self._set_alpha_if_needed(float(getattr(self, "idle_alpha", getattr(self, "_current_opacity", 1.0))),
                                  update_idle=False)

    def _on_win_enter(self, event=None):
        if not getattr(self, "_alpha_evt_enabled", True):
            return
        try:
            if get_state_store().is_ui_busy():
                return
        except Exception:
            pass

        if not self._is_pointer_inside_window_precise(event):
            return  # 자식→자식 이동 등 무시

        self._alpha_inside = True
        # Leave 지연 타이머가 있으면 취소
        aid = getattr(self, "_alpha_leave_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
            self._alpha_leave_after_id = None

        if getattr(self, "_ALPHA_ENTER_IMMEDIATE", True):
            self._apply_alpha_hover()

    def _on_win_leave(self, event=None):
        if not getattr(self, "_alpha_evt_enabled", True):
            return

        actually_out = not self._is_pointer_inside_window_precise(event)
        if not actually_out:
            return
        self._alpha_inside = False

        import time
        self._alpha_leave_deadline_ms = int(time.time() * 1000) + int(getattr(self, "_ALPHA_LEAVE_DELAY_MS", 120))

        if getattr(self, "_alpha_leave_after_id", None) is None:
            def _check():
                self._alpha_leave_after_id = None

                # busy면 조금 뒤 다시 체크
                try:
                    if get_state_store().is_ui_busy():
                        self._alpha_leave_after_id = self.after(getattr(self, "_ALPHA_LEAVE_DELAY_MS", 120), _check)
                        return
                except Exception:
                    pass

                import time as _t
                now = int(_t.time() * 1000)
                if (now < self._alpha_leave_deadline_ms) or self._is_pointer_inside_window_precise(None):
                    self._alpha_leave_after_id = self.after(60, _check)
                    return

                self._apply_alpha_idle()

            self._alpha_leave_after_id = self.after(getattr(self, "_ALPHA_LEAVE_DELAY_MS", 120), _check)

    def _apply_opacity(self, v: float) -> None:
        v = max(0.35, min(1.0, float(v)))
        # 외부에서 'idle 목표'를 명시적으로 바꿀 때만 사용
        self._set_alpha_if_needed(v, update_idle=True)

    def _ensure_win32_hooks(self, want: bool) -> None:
        """want=True → 설치, want=False → 해제. 이미 원하는 상태면 No-Op."""
        # (선택) 0.5s 디바운스가 있다면, 스킵 시에도 로그 남김
        try:
            import time as _t
            if (_t.time() - float(getattr(self, "_win32_hook_last_ts", 0.0))) < 0.5:
                return
        except Exception:
            pass

        installed = bool(getattr(self, "_wndproc_installed", False))
        if want and not installed:
            self._install_win32_movesize_hooks()
            return
        if (not want) and installed:
            self._uninstall_win32_hooks()
            return

    def _toggle_low_spec(self, enabled: bool) -> None:
        """
        저사양 on/off 시 런타임 적용.
        규칙: on → topmost=False, off → topmost=True (설정값 무시, 고정 규칙)
        부가: on 시 현재 불투명도 스냅샷 저장, off 시 원복 + 슬라이더 활성화
        """
        if enabled == getattr(self, "_low_spec_applied", False):
            return  # 상태 변화 없음 → 아무 것도 하지 않음

        slider = getattr(self, "alpha_slider", None)

        if enabled:
            # 1) 현재 불투명도 스냅샷 저장(복원용)
            try:
                cur_alpha = float(self.attributes("-alpha"))
            except Exception:
                cur_alpha = float(getattr(self, "_current_opacity", self.settings.get("gui.idle_opacity", 0.85)))
            self._opacity_before_low_spec = max(0.35, min(1.0, cur_alpha))

            # 2) 알파 1.0 강제 + 슬라이더 비활성
            try:
                self._current_opacity = 1.0
                self.idle_alpha = 1.0
                self._set_alpha_if_needed(1.0, update_idle=True, force=True, tag="low_spec:on")
            except Exception:
                pass
            try:
                if slider is not None:
                    slider.configure(state="disabled")
            except Exception:
                pass

            # 3) topmost 강제 OFF
            try:
                self.attributes("-topmost", False)
            except Exception:
                pass

            # win32 훅 해제
            try:
                # win32 훅 해제 (보장 로그)
                self._ensure_win32_hooks(False)
            except Exception:
                pass

            self._low_spec_applied = True
            try:
                self._log_gui("저사양 모드가 활성화되었습니다.")
            except Exception:
                pass
            return

        # enabled == False → 복원
        try:
            base = self._opacity_before_low_spec
            if base is None:
                base = float(self.settings.get("gui.idle_opacity", getattr(self, "_current_opacity", 0.85)))
            restore = max(0.35, min(1.0, float(base)))
            self._current_opacity = restore
            self.idle_alpha = restore
            self._set_alpha_if_needed(restore, update_idle=True, force=True, tag="low_spec:off")
        except Exception:
            pass

        # 슬라이더 활성화 + 노브 위치 복원
        try:
            if slider is not None:
                slider.configure(state="normal")
                slider.set(self.idle_alpha)
        except Exception:
            pass

        # topmost 강제 ON (고정 규칙)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        # win32 훅 재설치
        try:
            # win32 훅 재설치 (보장 로그)
            self._ensure_win32_hooks(True)
        except Exception:
            pass

        # 다음 사이클 대비 스냅샷 클리어
        self._opacity_before_low_spec = None
        self._low_spec_applied = False
        try:
            self._log_gui("저사양 모드가 비활성화되었습니다.")
        except Exception:
            pass

    def apply_runtime_perf_settings(self, changed_keys: list[str] | None = None) -> None:
        """
        settings_dialog에서 저장 직후 호출.
        - gui.low_spec: 즉시 on/off
        - gui.tick_ms: 다음 폴링부터 적용
        - gui.log_max_lines: 즉시 트리밍 1회
        """
        # 1) low_spec 즉시 적용
        try:
            low = bool(self.settings.get("gui.low_spec", False))
            self._toggle_low_spec(low)
        except Exception:
            pass

        # 2) 폴링 주기 갱신(다음 after부터 반영)
        try:
            new_tick = int(self.settings.get("gui.tick_ms", getattr(self, "_TICK_MS", 250)))
            self._TICK_MS = max(200, min(new_tick, 1000))
        except Exception:
            pass

        # 3) 로그 라인 상한 적용(즉시 1회 정리)
        try:
            if hasattr(self, "_trim_log_lines_if_needed"):
                self._trim_log_lines_if_needed()
        except Exception:
            pass

    def _force_non_layered_refresh(self) -> None:
        """layered→non-layered 복귀 보장 (희귀 케이스용)."""
        try:
            # 1) 알파 호출 금지 상태에서
            # 2) 가볍게 withdraw/deiconify로 스타일 재적용
            self.withdraw()
            self.deiconify()
        except Exception:
            pass

    def _toggle_mode(self):
        self._expanded = not self._expanded
        self._apply_expanded_state()

    def _canon_preset(self, p: dict | None) -> dict:
        """프리셋 스키마/타입 정규화: mode/type 폴백, 키 매핑, int 캐스팅."""
        if not isinstance(p, dict):
            return {}
        q = dict(p)

        # mode/type 정규화
        t = (q.get("mode") or q.get("type") or "rank")
        if isinstance(t, str):
            t = t.strip().lower()
        q["type"] = t
        q["mode"] = t

        # 신/구 키 매핑(→ 런타임에서 읽는 키 모두 채움)
        if "rank_target" not in q and "target_rank" in q:
            try:
                q["rank_target"] = int(q.get("target_rank") or 0)
            except:
                q["rank_target"] = 0
        if "rank_tolerance" not in q and "margin_rank" in q:
            try:
                q["rank_tolerance"] = int(q.get("margin_rank") or 0)
            except:
                q["rank_tolerance"] = 0

        if "points_target" not in q and "target_points" in q:
            try:
                q["points_target"] = int(q.get("target_points") or 0)
            except:
                q["points_target"] = 0
        if "points_margin" not in q and "margin_points" in q:
            try:
                q["points_margin"] = int(q.get("margin_points") or 0)
            except:
                q["points_margin"] = 0

        # 최종 int 캐스팅 보장
        for k in ("rank_target", "rank_tolerance", "points_target", "points_margin"):
            try:
                q[k] = int(q.get(k, 0) or 0)
            except:
                q[k] = 0

        return q

    def _apply_expanded_state(self):
        if self._expanded:
            self.bottom.pack(fill="x", padx=10, pady=(0, 10))
            self.toggle_btn.configure(text="버튼 숨기기")
            self.mode_var.set("expanded")
        else:
            self.bottom.forget()
            self.toggle_btn.configure(text="버튼 표시")
            self.mode_var.set("compact")

    def _ui_busy_set(self, busy: bool):
        store = get_state_store()

        if busy:
            try:
                store.set_ui_busy(True)
            except Exception:
                pass
            self._alpha_paused = True

            # 데드라인 갱신
            now_ms = int(time.time() * 1000)
            self._ui_busy_deadline_ms = now_ms + 2000  # 하드 워치독: 2초
            # 폴링 '정지' 대신 '감속': 다음 틱을 느리게 예약(있다면 그대로 두고, 없다면 새로 예약)
            try:
                if self._poll_after_id is None:
                    self._poll_after_id = self.after(400, self._poll_controller)  # 400~500ms 권장
            except Exception:
                pass

            # goal_ui 폴러도 감속
            try:
                if getattr(self, "_goal_ui_after_id", None) is None:
                    self._goal_ui_after_id = self.after(400, self._goal_ui_poll)
            except Exception:
                pass

            # 워치독(2초) — busy 해제 실패 시 강제 복구
            if self._ui_busy_off_checker_id is None:
                def _wd():
                    self._ui_busy_off_checker_id = None
                    try:
                        if not store.is_ui_busy():
                            return
                    except Exception:
                        pass
                    now = int(time.time() * 1000)
                    if now >= getattr(self, "_ui_busy_deadline_ms", 0):
                        # 강제 복구
                        try:
                            store.set_ui_busy(False)
                        except Exception:
                            pass
                        # 폴링 재개(즉시)
                        try:
                            if self._poll_after_id is None:
                                self._poll_after_id = self.after(10, self._poll_controller)
                        except Exception:
                            pass
                    else:
                        # 아직 데드라인 전이면 200ms 뒤 재확인
                        self._ui_busy_off_checker_id = self.after(200, _wd)

                self._ui_busy_off_checker_id = self.after(220, _wd)

        else:
            # busy 해제
            try:
                store.set_ui_busy(False)
            except Exception:
                pass
            self._alpha_paused = False
            # 폴링은 정상 주기로 재개(즉시 1회)
            try:
                if self._poll_after_id is None:
                    self._poll_after_id = self.after(self._TICK_MS, self._poll_controller)
            except Exception:
                pass

    def _install_win32_movesize_hooks(self):
        # 이미 서브클래싱 되어 있으면 No-Op
        if getattr(self, "_wndproc_installed", False):
            try:
                self._log_gui("[win32] install: noop (already installed)")
            except Exception:
                pass
            return

        import sys, time
        if sys.platform != "win32":
            return

        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32

        # ← 반드시 시그니처 지정 (64bit 핸들 문제 방지)
        GWL_WNDPROC = -4
        user32.GetWindowLongPtrW.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.CallWindowProcW.restype = ctypes.c_long
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM,
                                           wintypes.LPARAM]

        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)
        hwnd = wintypes.HWND(self.winfo_id())

        # 원래 프로시저 저장
        orig = user32.GetWindowLongPtrW(hwnd, GWL_WNDPROC)
        if not orig:
            try:
                self._log_gui("[win32] install: GetWindowLongPtrW returned NULL")
            except Exception:
                pass
            return
        self._orig_wndproc = ctypes.c_void_p(orig)

        # 콜백 보관( GC 방지 )
        self._wndproc_ref = None

        @WNDPROC
        def _proc(hWnd, msg, wParam, lParam):
            try:
                if msg == 0x0231:  # WM_ENTERSIZEMOVE
                    self._ui_busy_set(True)
                elif msg == 0x0232:  # WM_EXITSIZEMOVE
                    self._ui_busy_set(False)
            except Exception:
                pass
            return user32.CallWindowProcW(self._orig_wndproc, hWnd, msg, wParam, lParam)

        self._wndproc_ref = _proc
        res = user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, ctypes.cast(_proc, ctypes.c_void_p))
        if not res:
            try:
                self._log_gui("[win32] install: SetWindowLongPtrW failed")
            except Exception:
                pass
            # 실패 시 원상태 유지
            self._orig_wndproc = None
            self._wndproc_ref = None
            return

        # 상태 플래그/타임스탬프/로그
        self._wndproc_installed = True
        self._win32_hook_last_ts = time.time()

    def _uninstall_win32_hooks(self) -> None:
        if not getattr(self, "_wndproc_installed", False):
            try:
                self._log_gui("[win32] uninstall: noop (not installed)")
            except Exception:
                pass
            return
        import sys, time
        if sys.platform != "win32":
            return
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        GWL_WNDPROC = -4
        hwnd = wintypes.HWND(self.winfo_id())
        try:
            if self._orig_wndproc:
                user32.SetWindowLongPtrW(hwnd, GWL_WNDPROC, self._orig_wndproc)
        except Exception:
            pass
        # 상태 초기화
        self._wndproc_installed = False
        self._orig_wndproc = None
        self._wndproc_ref = None
        self._win32_hook_last_ts = time.time()

    def _apply_low_spec_profile(self) -> None:
        # 불투명도/TopMost 완화
        try:
            self._current_opacity = 1.0
            self.attributes("-alpha", 1.0)
        except Exception:
            pass
        try:
            # low_spec에서는 topmost를 강제 off
            self.attributes("-topmost", False)
        except Exception:
            pass
        # 틱 주기 보수적으로 상향 (설정값 기준, 최소 300ms)
        try:
            self._TICK_MS = max(int(self.settings.get("gui.tick_ms", self._TICK_MS)), 300)
        except Exception:
            self._TICK_MS = max(self._TICK_MS, 300)

    def _track_pointer_alpha(self):
        """[DEPRECATED] 이벤트 드리븐으로 대체됨. 호환용 더미."""
        return

    # ------------------------------------------------------------------
    # 알파 트래킹 제어 (다이얼로그가 떠있는 동안 일시 정지용)
    # ------------------------------------------------------------------
    def set_alpha_tracking_enabled(self, enabled: bool) -> None:
        """
        메인 창 투명도 자동 추적 on/off.
        off: 루프를 즉시 정지시킨다(다음 after 예약 없음).
        on : 루프를 재가동시킨다(한 번 킥해서 after 체인을 복원).
        """
        self._alpha_evt_enabled = bool(enabled)

        # Leave 지연 타이머 정리
        aid = getattr(self, "_alpha_leave_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:
                pass
        self._alpha_leave_after_id = None

        # 현재 상태에 맞춰 1회만 반영
        try:
            if self._alpha_evt_enabled:
                if self._is_pointer_inside_window_precise(None):
                    self._apply_alpha_hover()
                else:
                    self._apply_alpha_idle()
        except Exception:
            pass

    def _load_icon(self, filename: str, size: int = 18):
        path = os.path.join(ASSETS_DIR, filename)
        if not os.path.exists(path):
            return None
        try:
            img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
            return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
        except Exception as e:
            print(f"[GUI] icon load failed: {e}")
            return None

    # ------------------------------------------------------------------
    # 컨트롤러/프리셋
    # ------------------------------------------------------------------
    def _on_start(self):
        if self.controller.is_running():
            return

        enabled = bool(self.settings.get("goal.enabled", False))

        if enabled:
            ok, msg = self._validate_goal_ready()
            if not ok:
                messagebox.showwarning("시작 불가", msg, parent=self)
                return
            if self.controller.start():
                stop_event.clear()  # ★ 추가: 이전 F12(중지) 잔류 해제
                self.controller.reset_runtime_state()
                start_event.set()  # ✅ 루틴 쓰레드에 시작 신호
                self.stop_hold.set_running(True)
            return

        # 켜고 시작(위) 또는 그냥 시작(아래)
        if self.controller.start():
            stop_event.clear()  # ★ 추가: 일반 경로도 동일 처리
            self.controller.reset_runtime_state()
            start_event.set()
            self.stop_hold.set_running(True)

    def _on_stop(self):
        if not self.controller.is_running():
            return
        try:
            stop_event.set()  # ✅ 루틴 쓰레드 종료 신호 보강
        except Exception:
            pass
        self.controller.stop()
        self.stop_hold.set_running(False)
        self.controller.reset_runtime_state()

    def _poll_controller(self):
        # ── 재진입 가드 ──
        if getattr(self, "_ui_tick_guard", False):
            return
        self._ui_tick_guard = True
        try:
            # ── 사용자 상호작용/비가시/최소화면이면 이번 틱은 스킵 ──
            try:
                from core.state_store import get_state_store
                if get_state_store().is_ui_busy():
                    return
            except Exception:
                pass
            if (not self.winfo_viewable()) or (self.state() == "iconic"):
                return

            s = self.controller.poll_state()
            if s:
                disp = "IDLE" if s == "IDLE" else "RUNNING"
                if disp != getattr(self, "_last_state_disp", None):
                    self.state_var.set(disp)
                    self._set_state_color(disp)
                    self._last_state_disp = disp
                    self.stop_hold.set_running(disp == "RUNNING")
                    # 상태표시/버튼 상태 변경 직후에 추가
                    try:
                        if disp == "RUNNING":
                            # 토글/편집 비활성, 콤보는 선택 가능
                            try:
                                self.sw_goal.configure(state="disabled")
                            except Exception:
                                pass
                            try:
                                self.btn_goal_edit.configure(state="disabled")
                            except Exception:
                                pass
                            try:
                                # 콤보는 항상 'readonly' 유지(=선택 가능)
                                self.combo_goal.configure(state="readonly")
                            except Exception:
                                pass
                            try:
                                self.btn_calib.configure(state="disabled")
                            except Exception:
                                pass
                        else:  # IDLE
                            try:
                                self.sw_goal.configure(state="normal")
                            except Exception:
                                pass
                            try:
                                self.btn_goal_edit.configure(state="normal")
                            except Exception:
                                pass
                            try:
                                self.combo_goal.configure(state="readonly")
                            except Exception:
                                pass
                            try:
                                self.btn_calib.configure(state="normal")
                            except Exception:
                                pass
                    except Exception:
                        pass

            # 가능하면 여러 줄을 모아서 1회 삽입
            tick_budget_ms = 3.0  # 틱당 최대 3ms만 소비 (필요시 2~5 조정)
            t0 = time.perf_counter()
            lines = []
            MAX_LINES = 50  # 한 틱당 최대 50줄만
            while len(lines) < MAX_LINES:
                ln = self.controller.poll_log()
                if not ln:
                    break
                lines.append(ln)
                if (time.perf_counter() - t0) * 1000.0 >= tick_budget_ms:
                    break  # 이번 틱은 여기까지, 나머지는 다음 틱에서

            if lines:
                buf = "".join(l + "\n" for l in lines)
                self.log.configure(state="normal")
                self.log.insert("end", buf, ("base",))
                self._trim_log_lines_if_needed()
                self.log.see("end")
                self.log.configure(state="disabled")
        finally:
            self._ui_tick_guard = False
            try:
                self._poll_after_id = self.after(self._TICK_MS, self._poll_controller)
            except Exception:
                self._poll_after_id = None

    def _log_gui(self, s: str) -> None:
        def _do():
            try:
                t = getattr(self.log, "_textbox", None)
                self.log.configure(state="normal")
                if t is not None:
                    t.insert("end", s + "\n", ("base",))  # ★ 기본 폰트 태그 강제
                    t.see("end")
                else:
                    self.log.insert("end", s + "\n", ("base",))
                    self.log.see("end")
                self.log.configure(state="disabled")
            except Exception:
                pass

        self.after(0, _do)  # ✅ 메인스레드에서만 수행

    def _trim_log_lines_if_needed(self):
        try:
            max_lines = int(self.settings.get("gui.log_max_lines", 800))
            # 'end-1c' 인덱스에서 라인 수 추출
            total = int(float(self.log.index("end-1c").split(".")[0]))
            if total > max_lines:
                # 초과분 만큼 맨 앞 라인 삭제
                cut = total - max_lines
                self.log.configure(state="normal")
                self.log.delete("1.0", f"{cut + 1}.0")
                self.log.configure(state="disabled")
        except Exception:
            pass

    def _set_state_color(self, s: str):
        # 표시 상태(IDLE/RUNNING)에만 맞춰 색상 결정
        color = {"IDLE": "#9e9e9e", "RUNNING": "#43a047", "STOPPING": "#f9a825"}.get(s, "#9e9e9e")
        self.state_dot.delete("all")
        self.state_dot.create_oval(2, 2, 14, 14, fill=color, outline="")

    def _sync_goal_visual(self):
        """
        목표설정 모드 스위치 상태에 맞춰 라벨을 '상태 텍스트+색상'으로 동기화.
        ON = 초록(#16A34A), OFF = 앰버(#F59E0B)
        """
        enabled = bool(self.var_goal_enabled.get())
        color = "#16A34A" if enabled else "#F59E0B"
        text = f"목표달성 모드: {'ON' if enabled else 'OFF'}"
        try:
            self.lbl_goal.configure(text=text, text_color=color)
        except Exception:
            self.lbl_goal.configure(text=text)

    def _log_colored(self, msg: str, *tags: str):
        # UI 스레드에서 안전하게 동작
        def _do():
            try:
                taglist = ("base",) + (tuple(tags) if tags else ())
                t = getattr(self.log, "_textbox", None)

                self.log.configure(state="normal")
                if t is not None:
                    t.insert("end", msg + "\n", taglist)
                    t.see("end")
                else:
                    # 폴백: wrapper에라도 같은 태그로 삽입
                    self.log.insert("end", msg + "\n", taglist)
                    self.log.see("end")
                self.log.configure(state="disabled")
            except Exception:
                print(msg)

        self.after(0, _do)

    # === [ADD] main.set_status_logger 브릿지 ===
    def log_status(self, text: str, fg: str | None = None) -> None:
        """
        main 쪽에서 (text, fg)로 호출. fg는 'cyan bold' / '#00BCD4|bold' 등 토큰 조합 허용.
        """
        tagset = {"base"}
        if isinstance(fg, str):
            tokens = fg.replace("|", " ").replace(";", " ").replace(",", " ").lower().split()
            for tk in tokens:
                if tk in ("green", "#16a34a"):
                    tagset.add("green")
                elif tk in ("amber", "#f59e0b"):
                    tagset.add("amber")
                elif tk in ("gray", "grey", "#9ca3af"):
                    tagset.add("gray")
                elif tk in ("red", "#ef4444"):
                    tagset.add("red")
                elif tk in ("cyan", "#00bcd4"):
                    tagset.add("cyan")
                elif tk in ("bold",):
                    tagset.add("bold")

        # 태그가 하나도 매칭되지 않으면 기본 로그
        if tagset == {"base"}:
            self._log_gui(text)
        else:
            self._log_colored(text, *sorted(tagset))

    def _log_startup_goal_and_hotkey(self):
        """
        시작 시점에 '목표 모드 초기값'과 'F8 토글' 안내를 컬러로 표기.
        """
        enabled = bool(self.var_goal_enabled.get())
        tag = "green" if enabled else "amber"
        self._log_colored(f"▶ 목표달성 모드: {'ON' if enabled else 'OFF'}", tag, "bold")

    def _init_goal_ui_bindings(self):
        self._goal_ui_poll_active = True
        self._goal_ui_after_id = None
        self._goal_ui_poll()

    def _goal_ui_poll(self):
        if getattr(self, "_goal_ui_guard", False):
            return
        self._goal_ui_guard = True
        try:
            # 사용자 조작 중이면만 스킵 (비가시는 일부만 스킵)
            try:
                if get_state_store().is_ui_busy():
                    return
            except Exception:
                pass

            viewable = bool(self.winfo_viewable())
            iconic = (self.state() == "iconic")
            # 최소화(iconic) 상태만 즉시 종료. withdrawn(비가시)라도 복귀 신호는 처리해야 함.
            if iconic:
                return

            active = get_state_store().is_ocr_sampling_active()

            self._set_goal_controls_enabled(not active)

            prev = getattr(self, "_ocr_auto_overlay_last", False)
            if active and not prev:
                try:
                    self._ocr_auto_overlay_ctx = self._enter_overlay_mode(keep_goal_poll=True)
                except Exception:
                    self._ocr_auto_overlay_ctx = None
            elif (not active) and prev:
                try:
                    self._leave_overlay_mode(self._ocr_auto_overlay_ctx)
                finally:
                    self._ocr_auto_overlay_ctx = None

            self._ocr_auto_overlay_last = bool(active)
        finally:
            self._goal_ui_guard = False
            try:
                if getattr(self, "_goal_ui_poll_active", True):
                    self._goal_ui_after_id = self.after(300, self._goal_ui_poll)
            except Exception:
                self._goal_ui_after_id = None

    def _stop_goal_ui_poll(self):
        self._goal_ui_poll_active = False
        try:
            if self._goal_ui_after_id:
                self.after_cancel(self._goal_ui_after_id)
        except Exception:
            pass
        self._goal_ui_after_id = None

    def _set_goal_controls_enabled(self, enabled: bool):
        if getattr(self, "_goal_controls_enabled_cache", None) == enabled:
            return
        self._goal_controls_enabled_cache = enabled
        try:
            self.sw_goal.configure(state="normal" if enabled else "disabled")
            self.combo_goal.configure(state="readonly" if enabled else "disabled")
        except Exception:
            pass

    def _has_any_roi(self) -> bool:
        """
        settings['ocr']에 roi_points나 roi_rank 둘 중 하나라도 있으면 True.
        """
        rois_xyxy, _base = self._ocr_read_rois()
        return bool(rois_xyxy and (("roi_points" in rois_xyxy) or ("roi_rank" in rois_xyxy)))

    def _update_calib_buttons_state(self):
        """
        좌표 유무에 따라 '좌표 확인' 버튼의 사용 가능 상태를 갱신한다.
        """
        try:
            has_roi = self._has_any_roi()
            self.btn_calib.configure(state=("normal" if has_roi else "disabled"))
        except Exception:
            pass

    def _refresh_goal_combo(self):
        goal_presets = self.settings.get("goal.presets", {}) or {}
        ordered_ids = sorted(
            goal_presets.keys(), key=lambda k: int(k[1:]) if isinstance(k, str) and k.startswith("p") else 0
        )
        preset_names = [goal_presets[pid].get("name", pid) for pid in ordered_ids]

        # ★ 활성 프리셋 보정: 없으면 p1, 그것도 없으면 첫 번째
        active_id = self.settings.get("goal.active_preset_id", "")
        if active_id not in goal_presets:
            if "p1" in goal_presets:
                active_id = "p1"
            elif ordered_ids:
                active_id = ordered_ids[0]
            else:
                active_id = ""
            if active_id:
                try:
                    self.settings.set("goal.active_preset_id", active_id)
                    self.settings.queue_save()
                except Exception:
                    pass

        active_name = goal_presets.get(active_id, {}).get("name", active_id)

        try:
            self.combo_goal.configure(values=preset_names, state=("readonly" if preset_names else "disabled"))
            if active_name:
                self.combo_goal.set(active_name)
        except Exception:
            pass

    def _toggle_goal_switch(self):
        import time
        now = int(time.time() * 1000)
        if now - getattr(self, "_last_toggle_ms", 0) < 150:
            return
        self._last_toggle_ms = now
        try:
            self.var_goal_enabled.set(not bool(self.var_goal_enabled.get()))
        except Exception:
            return
        self._on_goal_toggle()

    def _on_goal_toggle(self):
        # RUNNING 상태에서는 토글 금지 + 즉시 되돌림
        if self.controller.is_running():
            try:
                # 직전 상태로 롤백
                self.var_goal_enabled.set(bool(self.settings.get("goal.enabled", False)))
                messagebox.showinfo("변경 불가", "실행 중에는 목표 토글을 변경할 수 없습니다.", parent=self)
            except Exception:
                pass
            return

        enabled = self.var_goal_enabled.get()
        g = self.settings.get("goal", {})
        pid = g.get("active_preset_id", "p1")
        preset = (g.get("presets") or {}).get(pid, {})
        mode = (preset.get("mode") or preset.get("type") or "").strip()

        if enabled:
            # [NEW] Home 등록 여부 선검사
            if not is_home_configured():
                messagebox.showwarning(
                    "목표달성 모드 사용 불가",
                    "홈화면 감지용 이미지가 등록되어 있지 않아 목표달성 모드를 사용할 수 없습니다.\n\n"
                    "Config.exe에서 이미지를 등록한 뒤 다시 시도하세요.",
                    icon="warning",
                    parent=self
                )
                self.var_goal_enabled.set(False)  # 되돌림
                return

            # [변경] settings['ocr'] 기반으로 ROI 존재 여부 확인
            rois_xyxy, _base = self._ocr_read_rois()
            rois = {
                "roi_rank": rois_xyxy.get("roi_rank"),
                "roi_points": rois_xyxy.get("roi_points"),
            }

            def _has_rank_roi(d: dict) -> bool:
                return bool(d) and (d.get("roi_rank") is not None)

            def _has_points_roi(d: dict) -> bool:
                return bool(d) and (d.get("roi_points") is not None)

            # 신/구 프리셋 키 모두 허용
            rank_target = int(preset.get("rank_target", preset.get("target_rank", 0)) or 0)
            points_target = int(preset.get("points_target", preset.get("target_points", 0)) or 0)

            # 위에서 mode를 (mode or type)로 정규화했음을 전제
            if mode == "rank":
                if (not _has_rank_roi(rois)) or (rank_target < 1):
                    messagebox.showwarning("ON 실패", "ROI 미설정 또는 프리셋 비활성", parent=self)
                    self.var_goal_enabled.set(False)
                    return
            elif mode == "points":
                if (not _has_points_roi(rois)) or (points_target < 1):
                    messagebox.showwarning("ON 실패", "ROI 미설정 또는 점수 프리셋 비활성", parent=self)
                    self.var_goal_enabled.set(False)
                    return
            else:
                messagebox.showwarning("ON 실패", "활성 프리셋/모드가 없습니다", parent=self)
                self.var_goal_enabled.set(False)
                return

        self.settings.set("goal.enabled", enabled)
        # 원자 저장(가능하면 save_strict 사용)
        if hasattr(self.settings, "save_strict"):
            self.settings.save_strict()
        else:
            self.settings.save()

        # [NEW] 스케줄 변경이 아니어도 안전: 저장 직후 메인에 재적용 시그널
        try:
            import main as _main
            if hasattr(_main, "reload_scheduled_shutdown"):
                _main.reload_scheduled_shutdown()
        except Exception:
            pass

        # 디스크 재조회로 검증
        try:
            ok = (bool(self.settings.get("goal.enabled", not enabled)) == bool(enabled))
        except Exception:
            ok = False
        if not ok:
            # 저장이 반영되지 않았으면 UI 롤백 + 경고
            self.var_goal_enabled.set(not bool(enabled))
            self._log_colored("[goal] 저장 검증 실패 → 토글 원복", "red")
            messagebox.showwarning("저장 오류", "설정 저장 직후 일치 확인에 실패했습니다.", parent=self)
            return

        try:
            self.goal_provider.set_enabled(bool(enabled))
        except Exception:
            pass

        # UI/로그 동기화
        self._sync_goal_visual()
        self._log_colored(f"목표달성 모드: {'ON' if enabled else 'OFF'}", "green" if enabled else "amber")
        self.controller.reset_runtime_state()

    def _validate_goal_ready(self) -> tuple[bool, str]:
        """
        현재 활성 프리셋 기준으로 목표달성 모드 시작 가능 여부를 검증한다.
        - ROI 존재
        - 목표값 존재(>0)
        반환: (ok, message)
        """
        g = self.settings.get("goal", {}) or {}
        pid = g.get("active_preset_id")
        presets = g.get("presets") or {}
        preset = presets.get(pid or "", {})

        # mode ↔ type 스키마 호환
        mode = (str(preset.get("mode"))
                or str(preset.get("type"))
                or "").strip()

        rois = load_scaled_rois_for_current_screen(auto_scale=True)

        def _has_rank_roi(d: dict) -> bool:
            return bool(d) and (("rank_roi" in d) or ("roi_rank" in d))

        def _has_points_roi(d: dict) -> bool:
            return bool(d) and (("points_roi" in d) or ("roi_points" in d))

        # 목표값 키도 구버전과 호환
        rank_target = int(preset.get("rank_target", preset.get("target_rank", 0)) or 0)
        points_target = int(preset.get("points_target", preset.get("target_points", 0)) or 0)

        if mode == "rank":
            if not _has_rank_roi(rois):
                return False, "ROI 미설정(등수)"
            if rank_target < 1:
                return False, "등수 목표 누락"
            return True, ""
        elif mode == "points":
            if not _has_points_roi(rois):
                return False, "ROI 미설정(점수)"
            if points_target < 1:
                return False, "점수 목표 누락"
            return True, ""
        else:
            return False, "활성 프리셋/모드가 없습니다"

    def _on_goal_preset_changed(self, name: str):
        goal_presets = self.settings.get("goal.presets", {}) or {}
        target_pid = None
        for pid, p in goal_presets.items():
            if p.get("name") == name:
                target_pid = pid
                break
        if not target_pid:
            return
        self.settings.set("goal.active_preset_id", target_pid)
        self.settings.queue_save()

        try:
            self.goal_provider.apply_active_preset()
        except Exception:
            pass

    def _on_goal_edit(self):
        # ★ RUNNING 가드
        if getattr(self.controller, "is_running", lambda: False)():
            try:
                messagebox.showwarning("편집 불가", "루틴 실행 중에는 프리셋 편집을 할 수 없습니다.", parent=self)
            except Exception:
                pass
            return

        prev_top = True
        try:
            prev_top = bool(self.attributes("-topmost"))
            self.attributes("-topmost", False)
        except Exception:
            pass

        # ── 추가: 편집기 띄우는 동안 알파 트래킹 일시 정지 ──
        try:
            if hasattr(self, "set_alpha_tracking_enabled"):
                self.set_alpha_tracking_enabled(False)
        except Exception:
            pass

        dlg = PresetEditorDialog(self, self.settings)
        try:
            dlg.transient(self)
            dlg.lift()
            dlg.focus_set()
            dlg.attributes("-topmost", True)
        except Exception:
            pass

        self.wait_window(dlg)

        # ── 복귀: 알파 트래킹 재개 ──
        try:
            if hasattr(self, "set_alpha_tracking_enabled"):
                self.set_alpha_tracking_enabled(True)
        except Exception:
            pass

        try:
            self.attributes("-topmost", prev_top)
        except Exception:
            pass

        # 설정 재적용
        try:
            self.goal_provider.reload_from_settings()
        except Exception:
            pass

        # ★ 활성 프리셋 보정: 현재 active_id가 삭제되었으면 p1(있으면)로 강제
        try:
            goal = self.settings.get("goal", {}) or {}
            presets = goal.get("presets", {}) or {}
            active_id = goal.get("active_preset_id", "")
            if active_id not in presets:
                new_id = "p1" if "p1" in presets else (sorted(presets.keys())[0] if presets else "")
                if new_id:
                    self.settings.set("goal.active_preset_id", new_id)
                    self.settings.save()
        except Exception:
            pass

        try:
            self._refresh_goal_combo()
        except Exception:
            pass
        try:
            self._sync_goal_visual()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 설정/좌표/검증
    # ------------------------------------------------------------------
    # === 저장 버튼 하이브리드 핸들러 ===
    def _on_save_click(self, btn):
        if getattr(self, "_save_in_progress", False):
            return
        self._save_in_progress = True
        try:
            btn.configure(state="disabled", text="저장 중…")
        except Exception:
            pass

        import threading

        def _work():
            try:
                # 즉시 커밋(버튼 저장은 지연 없이 확정)
                self.settings.flush_debounced(immediate=True)
            finally:
                # UI 복귀는 메인 스레드
                try:
                    self.after(0, lambda: self._after_save_ui(btn))
                except Exception:
                    self._save_in_progress = False

        threading.Thread(target=_work, daemon=True).start()

    def _after_save_ui(self, btn):
        try:
            btn.configure(text="저장됨 ✓")
        except Exception:
            pass

        # 1.2s 후 원복 + 재활성화
        def _restore():
            try:
                btn.configure(text="저장", state="normal")
            except Exception:
                pass
            self._save_in_progress = False

        try:
            self.after(1200, _restore)
        except Exception:
            _restore()

    def _open_roi_editor(self):
        """
        ROI 편집기(풀스크린, 원본 좌표) 실행.
        - 루틴 실행 중에는 편집 불가
        - 편집기 내부에서 메인 GUI withdraw/복원은 _enter/_leave_overlay_mode로 처리
        """
        if self.controller.is_running():
            messagebox.showwarning("편집 불가", "루틴 실행 중에는 ROI 편집을 할 수 없습니다.", parent=self)
            return
        try:
            # 동적 참조: RoiEditorOverlay는 파일 하단에 정의되어 있어도 런타임에 사용 가능
            RoiEditorOverlay(self)
        except Exception as e:
            messagebox.showerror("오류", f"ROI 편집기를 열 수 없습니다: {e}", parent=self)

    def _open_settings(self):
        dlg = SettingsDialog(self, self.settings, self.emailq)
        self.wait_window(dlg)

    def _open_calibration(self):
        """
        2-스텝 캡처:
          1) 점수(Points) 영역 → settings['ocr']['roi_points'] = [x,y,w,h]
          2) 등수(Rank)  영역 → settings['ocr']['roi_rank']   = [x,y,w,h]
        Enter/Space 확정, Esc/우클릭 취소.
        """
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass

        def _launch_rank_step():
            def _done_rank(rect_xywh):
                try:
                    if rect_xywh:
                        self._ocr_save_roi("rank", rect_xywh)  # 화면크기 함께 저장
                        messagebox.showinfo("ROI 저장", f"등수 영역 저장: {rect_xywh}", parent=self)
                finally:
                    try:
                        self.attributes("-topmost", True)
                    except Exception:
                        pass

            CaptureOverlay(self, _done_rank, banner_text="등수 영역 지정 (Enter/Space 확정, Esc 취소)")

        def _done_points(rect_xywh):
            if rect_xywh:
                try:
                    self._ocr_save_roi("points", rect_xywh)  # 화면크기 함께 저장
                    messagebox.showinfo("ROI 저장", f"점수 영역 저장: {rect_xywh}", parent=self)
                except Exception:
                    pass
            self.after(180, _launch_rank_step)

        CaptureOverlay(self, _done_points, banner_text="점수 영역 지정 (Enter/Space 확정, Esc 취소)")

    def _open_verify(self):
        """좌표 확인 뷰어(싱글턴). 열려 있으면 내용만 갱신."""
        # --- 메인 GUI 일시 숨김(overlay 모드 진입) → 캡처 → 즉시 복원 ---
        # RUNNING 중엔 테스트 진입 금지
        if getattr(self.controller, "is_running", lambda: False)():
            try:
                messagebox.showwarning("테스트 불가", "루틴 실행 중에는 OCR 테스트를 할 수 없습니다.", parent=self)
            except Exception:
                pass
            return

        ctx = self._enter_overlay_mode()  # ← 메인 창 withdraw + 폴링/알파 중지
        try:
            # compositor 안정화 약간 대기 (필요 최소)
            try:
                self.update_idletasks()
            except Exception:
                pass
            import time
            time.sleep(0.06)

            # 1) 캡처(mss)
            with mss.mss() as sct:
                mon = sct.monitors[0]  # 가상 전체 화면
                shot = sct.grab(mon)

                # 1) shot.rgb: RGB 바이트(연속 버퍼) → (H,W,3)로 reshape
                rgb = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)

                # 2) RGB→BGR 변환 + copy()...
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()

                # 3) 방어적 보장
                if (not bgr.flags["C_CONTIGUOUS"]) or (not bgr.flags["WRITEABLE"]):
                    bgr = np.ascontiguousarray(bgr)
                    bgr.setflags(write=1)
        finally:
            # 캡처 직후 즉시 복원 (메인 GUI를 다시 보이게)
            self._leave_overlay_mode(ctx)

        ih, iw = bgr.shape[:2]

        # 2) ROI 드로잉
        rois_xyxy, base = self._ocr_read_rois()
        if not rois_xyxy:
            messagebox.showwarning("좌표 확인", "표시할 좌표가 없습니다.\n좌표를 먼저 설정해주세요", parent=self)
            return

        self._alpha_track_enabled = False
        try:
            self.attributes("-topmost", False)  # 메인은 topmost 해제
        except:
            pass

        # BGR 기준: gold=(255,215,0), cyan=(0,255,255)
        for key, color, label in (
                ("roi_points", (0, 255, 255), "점수"),  # cyan
                ("roi_rank", (255, 215, 0), "등수")  # gold
        ):
            if key in rois_xyxy:
                x1, y1, x2, y2 = self._scale_from_base(rois_xyxy[key], base, (iw, ih))
                cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 3)

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        max_w = min(self.winfo_screenwidth() - 120, 1200)
        if w > max_w:
            s = max_w / float(w)
            rgb = cv2.resize(rgb, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)

        # 3) 윈도우 싱글턴
        if not hasattr(self, "_verify_win") or self._verify_win is None or not self._verify_win.winfo_exists():
            self._verify_win = ctk.CTkToplevel(self)
            ver = self._verify_win
            ver.title("점수/등수 인식 테스트")
            ver.transient(self)
            ver.attributes("-topmost", True)
            ver.protocol("WM_DELETE_WINDOW", lambda: self._destroy_verify_win())

            # (A) 이미지 표시용: CTkLabel → tk.Label + PhotoImage(재사용)
            ver._img_label = tk.Label(ver, bd=0, highlightthickness=0)
            ver._img_label.pack(fill="both", expand=True)

            # (B) 툴바 부착 — 팩토리는 '속성'으로 들고 있게 하고, 툴바는 동적으로 조회
            ver._pil_img_factory = lambda: None  # 자리만 먼저
            _attach_ocr_toolbar(ver, ver._img_label)

            # (C) 보조창 드래그/리사이즈 중 메인 알파 갱신 일시정지
            ver.bind("<ButtonPress-1>", lambda e: self._ui_busy_set(True))
            ver.bind("<B1-Motion>", lambda e: self._ui_busy_set(True))
            ver.bind("<ButtonRelease-1>", lambda e: self._ui_busy_set(False))
            ver.bind("<Configure>", lambda e: self._ui_busy_set(True))
        else:
            ver = self._verify_win

        # 4) 이미지 업데이트 — PhotoImage를 '재사용' (같은 크기면 paste, 아니면 재생성)
        pil_img = Image.fromarray(rgb)
        W, H = pil_img.size
        old = getattr(ver, "_photo", None)

        if (old is not None) and (old.width() == W) and (old.height() == H):
            # 재사용 경로: 픽셀만 교체
            try:
                old.paste(pil_img)  # ✅ 객체 churn 없이 업데이트
            except Exception:
                ver._photo = ImageTk.PhotoImage(pil_img)
                ver._img_label.configure(image=ver._photo)
        else:
            # 크기 바뀌면 새 객체 1개만 생성
            ver._photo = ImageTk.PhotoImage(pil_img)
            ver._img_label.configure(image=ver._photo)

        # 툴바에서 최신 이미지 얻을 수 있도록 '속성 팩토리' 갱신
        ver._pil_img_factory = lambda: pil_img

        try:
            ver.update_idletasks()
        except Exception:
            pass

    def _destroy_verify_win(self):
        """좌표 확인 창 정리"""
        try:
            ver = getattr(self, "_verify_win", None)
            if ver is None:
                return
            # 툴바 타이머/after 해제
            try:
                if hasattr(ver, "_ocr_after_id") and ver._ocr_after_id:
                    ver.after_cancel(ver._ocr_after_id)
            except Exception:
                pass
            # 이미지/팩토리/포토 참조 제거
            try:
                if hasattr(ver, "_img_label"):
                    ver._img_label.configure(image=None)
            except Exception:
                pass
            for attr in ("_photo", "_pil_img_factory", "_ocr_toolbar", "_ocr_result_label"):
                try:
                    if hasattr(ver, attr):
                        setattr(ver, attr, None)
                except Exception:
                    pass
            try:
                ver.destroy()
            finally:
                import gc
                gc.collect()  # 대형 이미지 사용 후 즉시 회수 트리거
        finally:
            self._verify_win = None

        try:
            self.attributes("-topmost", True)
        except:
            pass

        # 이벤트 드리븐 모드면, 상태에 맞춰 1회만 반영
        try:
            if getattr(self, "_alpha_evt_enabled", True):
                if self._is_pointer_inside_window_precise(None):
                    self._apply_alpha_hover()
                else:
                    self._apply_alpha_idle()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 내부 유틸(ROI/OCR)
    # ------------------------------------------------------------------
    def _reassert_topmost(self):
        try:
            self.winfo_toplevel().attributes("-topmost", True)
        except Exception:
            pass

    # =========================
    # Overlay-mode Infra (NEW)
    # =========================
    def _enter_overlay_mode(self, *, keep_goal_poll: bool = False) -> dict:
        """
        편집/캡처 전용 오버레이 진입.
        - keep_goal_poll=True 이면 goal UI 폴러를 멈추지 않는다(자동 OCR 오버레이용).
        """
        ctx: dict = {}
        # 1) 상태 스냅샷
        try:
            ctx["topmost"] = bool(self.attributes("-topmost"))
        except Exception:
            ctx["topmost"] = True
        try:
            ctx["alpha"] = float(self.attributes("-alpha"))
        except Exception:
            ctx["alpha"] = 1.0
        ctx["alpha_evt_enabled"] = bool(getattr(self, "_alpha_evt_enabled", True))
        ctx["goal_ui_poll_active"] = bool(getattr(self, "_goal_ui_poll_active", False))

        # 2) 주기 작업 정지(필요한 것만)
        if not keep_goal_poll:  # ← 조건부로만 폴러 정지
            try:
                self._stop_goal_ui_poll()
            except Exception:
                pass

        # 3) 메인 창 숨김
        try:
            self.withdraw()
            ctx["was_withdrawn"] = True
        except Exception:
            ctx["was_withdrawn"] = False
        return ctx

    def _leave_overlay_mode(self, ctx: dict | None) -> None:
        """
        오버레이 종료 시 복원.
        - 메인 GUI 재표시(deiconify)
        - topmost/alpha/트래킹/폴링 상태 복원
        - 포인터 알파 트래커 재가동
        """
        ctx = ctx or {}

        # 1) 메인 GUI 복귀
        try:
            if ctx.get("was_withdrawn", False):
                self.deiconify()
                # 전면/포커스 확보(일부 WM에서 deiconify만으론 뒤로 깔리는 현상 방지)
                try:
                    self.lift()
                    self.focus_force()
                except Exception:
                    pass
        except Exception:
            pass

        # 2) topmost/alpha 복원
        try:
            self.attributes("-topmost", bool(ctx.get("topmost", True)))
        except Exception:
            pass
        try:
            self.attributes("-alpha", float(ctx.get("alpha", 1.0)))
        except Exception:
            pass

        try:
            a = float(ctx.get("alpha", 1.0))
            self._last_attr_alpha = (a if a < 0.999 else None)
        except Exception:
            pass

        # 3) 이벤트 드리븐 플래그 복원(기본 True)
        try:
            self._alpha_evt_enabled = bool(ctx.get("alpha_evt_enabled", True))
        except Exception:
            self._alpha_evt_enabled = True
        if bool(ctx.get("goal_ui_poll_active", False)):
            try:
                # 이미 polling 중이 아니면 재시작
                if not getattr(self, "_goal_ui_poll_active", False):
                    self._goal_ui_poll_active = True
                if not getattr(self, "_goal_ui_after_id", None):
                    self._goal_ui_poll()
            except Exception:
                pass

        # 4) 현재 포인터 위치에 맞춰 1회만 적용
        try:
            if getattr(self, "_alpha_evt_enabled", True):
                if self._is_pointer_inside_window_precise(None):
                    self._apply_alpha_hover()
                else:
                    self._apply_alpha_idle()
        except Exception:
            pass

        # 레이아웃 안정화
        try:
            self.update_idletasks()
        except Exception:
            pass

    def _current_screen_size(self) -> tuple[int, int]:
        """다중 모니터 포함 전체 스크린 사이즈"""
        im = ImageGrab.grab(all_screens=True)
        return im.size  # (w, h)

    def _xywh_to_xyxy(self, r) -> tuple[int, int, int, int]:
        x, y, w, h = [int(round(float(v))) for v in r]
        return x, y, x + w, y + h

    def _xyxy_to_xywh(self, r) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = [int(round(float(v))) for v in r]
        return x1, y1, max(0, x2 - x1), max(0, y2 - y1)

    def _as_xyxy(self, r) -> tuple[int, int, int, int] | None:
        """[x,y,w,h] 또는 [x1,y1,x2,y2] → (x1,y1,x2,y2)"""
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            return None
        a, b, c, d = [int(round(float(x))) for x in r]
        if c <= a or d <= b:  # xywh
            return a, b, a + c, b + d
        return a, b, c, d

    def _ensure_tesseract_path(self, silent: bool = True) -> bool:
        """
        Tesseract 실행 경로를 보장한다.
        1) settings.ocr.tesseract_path 유효 → 사용
        2) 자동 탐지(auto_find_tesseract_path) → 저장/사용
        3) (silent=False일 때만) 파일 선택 대화상자 → 저장/사용
        성공 시 True, 실패 시 False 반환
        """
        try:
            # 1) settings 우선
            path = str(self.settings.get("ocr.tesseract_path", "") or "").strip()
            if path and validate_tesseract(path):
                set_tesseract_path(path)
                return True

            # 2) 자동 탐지
            auto = auto_find_tesseract_path()
            if auto and validate_tesseract(auto):
                set_tesseract_path(auto)
                try:
                    self.settings.set("ocr.tesseract_path", auto)
                    self.settings.save()
                except Exception:
                    pass
                self._log_gui(f"[OCR] Tesseract 경로 자동 설정: {auto}")
                return True

            # 3) 비대화식이면 여기서 종료
            if silent:
                self._log_gui("[OCR] Tesseract 경로를 찾지 못했습니다. (자동 탐지 실패)")
                return False

            # 3) 대화식: 파일 선택으로 받기(Windows)
            try:
                from tkinter import filedialog
                sel = filedialog.askopenfilename(
                    parent=self,
                    title="Tesseract 실행 파일 선택",
                    filetypes=[("tesseract.exe", "tesseract.exe"), ("실행 파일", "*.exe"), ("모든 파일", "*.*")],
                )
            except Exception:
                sel = ""

            sel = (sel or "").strip('"').strip()
            if sel and validate_tesseract(sel):
                set_tesseract_path(sel)
                try:
                    self.settings.set("ocr.tesseract_path", sel)
                    self.settings.save()
                except Exception:
                    pass
                self._log_gui(f"[OCR] Tesseract 경로 설정 완료: {sel}")
                return True

            self._log_gui("[OCR] Tesseract 경로 설정 실패(사용자 취소 또는 무효 경로)")
            return False
        except Exception as e:
            self._log_gui(f"[OCR] 경로 보장 중 예외: {type(e).__name__}: {e}")
            return False

    def _ocr_read_rois(self):
        """
        settings['ocr']에서 roi_points/roi_rank와 기준 스크린 크기를 읽어
        xyxy 좌표와 base_size(w,h)를 반환.
        """
        ocr = self.settings.get("ocr", {}) or {}
        screen = ocr.get("screen", None)
        base = None
        if isinstance(screen, dict) and "w" in screen and "h" in screen:
            base = (int(screen["w"]), int(screen["h"]))
        rois_xyxy = {}
        for key in ("roi_points", "roi_rank"):
            val = ocr.get(key)
            if isinstance(val, (list, tuple)) and len(val) == 4:
                x, y, w, h = map(int, val)
                if w > 0 and h > 0:
                    rois_xyxy[key] = (x, y, x + w, y + h)
        return rois_xyxy, base

    def _ocr_save_roi(self, kind: str, rect_any) -> None:
        """
        kind: 'points' | 'rank'
        rect_any: (x,y,w,h) | (x1,y1,x2,y2) | None
        - None 이면 해당 ROI를 [0,0,0,0]으로 저장(없음)
        """
        if kind not in ("points", "rank"):
            raise ValueError("kind must be 'points' or 'rank'")

        sw, sh = self._current_screen_size()
        node = self.settings.get("ocr", {}) or {}

        if rect_any is None:
            # 삭제 후 저장: '없음'을 명시적으로 기록
            node[f"roi_{kind}"] = [0, 0, 0, 0]
        else:
            xyxy = self._as_xyxy(rect_any)
            if not xyxy:
                # 사각형이 유효하지 않으면 없음으로 저장
                node[f"roi_{kind}"] = [0, 0, 0, 0]
            else:
                x, y, w, h = self._xyxy_to_xywh(xyxy)
                node[f"roi_{kind}"] = [int(x), int(y), int(w), int(h)]

        node["screen"] = {"w": int(sw), "h": int(sh)}
        self.settings.set("ocr", node)
        self.settings.queue_save()

        # 저장 후 버튼 상태 갱신
        try:
            self._update_calib_buttons_state()
        except Exception:
            pass

    def _ocr_save_rois_both(self, points_xyxy: tuple | None, rank_xyxy: tuple | None) -> None:
        """
        두 ROI를 한 번에 저장한다.
        - 존재하지 않는 ROI는 [0,0,0,0]로 저장해 '없음'을 명시적으로 기록
        - 화면 크기도 함께 갱신
        """

        def to_xywh(xyxy):
            if not xyxy:
                return [0, 0, 0, 0]
            x1, y1, x2, y2 = map(int, xyxy)
            w, h = max(0, x2 - x1), max(0, y2 - y1)
            if w <= 0 or h <= 0:
                return [0, 0, 0, 0]
            return [int(x1), int(y1), int(w), int(h)]

        # 현재 화면 크기
        sw, sh = self._current_screen_size()

        node = self.settings.get("ocr", {}) or {}
        node["roi_points"] = to_xywh(points_xyxy)
        node["roi_rank"] = to_xywh(rank_xyxy)
        node["screen"] = {"w": int(sw), "h": int(sh)}

        self.settings.set("ocr", node)
        self.settings.queue_save()

        # '좌표 확인' 버튼 상태 갱신
        try:
            self._update_calib_buttons_state()
        except Exception:
            pass

    def _scale_from_base(self, rect_xyxy, base_size, img_size) -> tuple[int, int, int, int]:
        """(x1,y1,x2,y2) @base_size → @img_size 로 스케일"""
        if not base_size:
            return tuple(map(int, rect_xyxy))
        bw, bh = base_size
        iw, ih = img_size
        sx, sy = iw / float(bw), ih / float(bh)
        x1, y1, x2, y2 = rect_xyxy
        return (int(round(x1 * sx)), int(round(y1 * sy)), int(round(x2 * sx)), int(round(y2 * sy)))

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------
    def _on_close(self):
        try:
            if self.controller.is_running():
                self.controller.stop()
        finally:
            try:
                # [ADD] 종료 시 현재 스위치 상태를 마지막 상태로 저장
                try:
                    self.settings.set("goal.enabled", bool(self.var_goal_enabled.get()))
                except Exception:
                    pass

                # 불투명도 보강 저장
                try:
                    v = float(getattr(self, "_current_opacity", 1.0))
                    self.settings.set("ui.opacity", v)
                except Exception:
                    pass

                self.settings.set("gui._last_geometry", self.geometry())
                self.settings.flush_debounced(immediate=True)
            except Exception:
                pass
            # 전역 핫키 해제(등록 실패했더라도 안전)
            try:
                self._hotkeys.unregister()
            except Exception:
                pass

            try:
                self._uninstall_win32_hooks()
            except Exception:
                pass

            self.destroy()


# ======================================================================
# 캡처 오버레이 (지금은 안씀)
# ======================================================================
class CaptureOverlay(tk.Toplevel):
    """화면 전체를 캡처하여 반투명 배경 위에 ROI를 드래그로 지정하는 오버레이"""

    def __init__(self, master, on_done, banner_text: str | None = None):
        super().__init__(master)
        self.on_done = on_done
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(cursor="crosshair")
        self.withdraw()

        self.screen_w = self.winfo_screenwidth()
        self.screen_h = self.winfo_screenheight()
        self.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.grab_set()

        img = self._grab_screen()
        dim = ImageEnhance.Brightness(img).enhance(0.5)
        self.img_orig = img
        self.img_dim_tk = ImageTk.PhotoImage(dim)

        self.canvas = tk.Canvas(self, width=self.screen_w, height=self.screen_h, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.bg = self.canvas.create_image(0, 0, anchor="nw", image=self.img_dim_tk)

        self.dragging = False
        self.x0 = self.y0 = 0
        self.rect_id = None
        self.crop_id = None
        self.sel = None

        self._draw_banner(banner_text)

        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Button-3>", self._on_cancel)
        self.bind("<Escape>", self._on_cancel)
        self.bind("<Return>", self._on_confirm)
        self.bind("<space>", self._on_confirm)

        self.deiconify()
        self.focus_force()

    def _grab_screen(self):
        if mss:
            with mss.mss() as sct:
                mon = sct.monitors[1]
                shot = sct.grab(mon)
                return Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)
        try:
            return ImageGrab.grab(all_screens=True)
        except Exception:
            import pyautogui as pgi

            return pgi.screenshot()

    def _on_press(self, e):
        self.dragging = True
        self.x0, self.y0 = e.x, e.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        if self.crop_id:
            self.canvas.delete(self.crop_id)
            self.crop_id = None

    def _on_motion(self, e):
        if not self.dragging:
            return
        x1, y1 = e.x, e.y
        x0, y0 = self.x0, self.y0
        x_min, y_min = min(x0, x1), min(y0, y1)
        x_max, y_max = max(x0, x1), max(y0, y1)
        w, h = max(1, x_max - x_min), max(1, y_max - y_min)

        if self.rect_id:
            self.canvas.coords(self.rect_id, x_min, y_min, x_max, y_max)
        else:
            self.rect_id = self.canvas.create_rectangle(x_min, y_min, x_max, y_max, outline="yellow", width=2)

        crop = self.img_orig.crop((x_min, y_min, x_max, y_max))
        crop_tk = ImageTk.PhotoImage(crop)
        self._last_crop_tk = crop_tk  # GC 방지
        if self.crop_id:
            self.canvas.delete(self.crop_id)
        self.crop_id = self.canvas.create_image(x_min, y_min, anchor="nw", image=crop_tk)

    def _on_release(self, _e):
        self.dragging = False
        self.sel = self._current_rect()

    def _on_confirm(self, _e=None):
        rect = self.sel or self._current_rect()
        if rect and self.on_done:
            x, y, w, h = rect
            self._finish()
            self.on_done((x, y, w, h))

    def _on_cancel(self, _e=None):
        self._finish()

    def _current_rect(self):
        if not self.rect_id:
            return None
        x1, y1, x2, y2 = self.canvas.coords(self.rect_id)
        x, y = int(min(x1, x2)), int(min(y1, y2))
        w, h = int(abs(x2 - x1)), int(abs(y2 - y1))
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def _finish(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _draw_banner(self, text: str | None):
        if not text:
            return
        pad_x, pad_y = 14, 10
        x0, y0 = 20, 20
        self.canvas.create_rectangle(x0, y0, x0 + 360, y0 + 40, fill="#000000", outline="")  # stipple 제거

        # 얇은 그림자
        self.canvas.create_text(
            x0 + pad_x + 1, y0 + pad_y + 1, anchor="nw", text=text, fill="#000000", font=("맑은 고딕", 14, "bold")
        )
        self.canvas.create_text(
            x0 + pad_x, y0 + pad_y, anchor="nw", text=text, fill="#ffffff", font=("맑은 고딕", 14, "bold")
        )


# ======================================================================
# ROI 편집 오버레이 (풀스크린, 원본 좌표 편집 전용)
# ======================================================================
class RoiEditorOverlay(tk.Toplevel):
    """
    - 원본 해상도 스크린샷 1회 캡처를 배경으로 사용(편집 내내 재사용)
    - 점수(노랑, '점수') / 등수(파랑, '등수') 두 ROI를 동시에 표시/편집
    - 현재 단계: 이동/선택/저장/닫기(리사이즈 핸들은 Step 3에서 추가)
    - 성능: 배경 고정, ROI 레이어만 갱신, 마우스 move 30~40ms 스로틀
    """

    _COLOR_POINTS = "#FFD700"  # gold
    _COLOR_RANK = "#00FFFF"  # cyan
    _LABEL_BG = "#000000"
    _LABEL_FG = "#ffffff"
    _DRAW_HIND_MESSAGE = "[Space/Enter]: 저장  ·  [Esc]: 취소\n---\n[Q]: 일반  ·  [W/E]: 그리기 모드 진입  ·  [Tab]: 선택 전환"
    _BG_DIM = 0.40  # 배경 디밍 강도(0=완전검정, 1=원본)

    def __init__(self, app: "OverlayApp"):
        super().__init__(app)
        self.app = app
        self.withdraw()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(cursor="tcross")

        # ── 오버레이 진입: 메인 GUI 숨김 및 타이머 정지 ──
        self._overlay_ctx = self.app._enter_overlay_mode()

        # ── 화면 크기/배경 캡처 ──
        self.screen_w = self.winfo_screenwidth()
        self.screen_h = self.winfo_screenheight()
        self.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.grab_set()

        self.bg_img = self._grab_fullscreen_pil()  # 원본 해상도 캡처(1회)
        self.bg_dim = ImageEnhance.Brightness(self.bg_img).enhance(self._BG_DIM)  # 너무 어두우면 0.5→0.85로 가독성 확보
        self._bg_tk = ImageTk.PhotoImage(self.bg_dim)

        # ▼ 스포트라이트(원본 크롭) 이미지 캐시
        self._spot_imgs_tk = []  # PhotoImage GC 방지용
        self._spot_item_ids = []  # 캔버스 아이템 id들

        # ── 캔버스 구성 ──
        self.canvas = tk.Canvas(self, width=self.screen_w, height=self.screen_h,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self._bg_id = self.canvas.create_image(0, 0, anchor="nw", image=self._bg_tk)

        # ── 상태 ──
        self._dragging = False
        self._drag_anchor = (0, 0)  # (mx,my)
        self._selected = "points"  # "points"|"rank"
        self._throttle_after = None

        # 리사이즈 핸들 상태
        self._resizing = False
        self._resize_handle = None  # "nw","n","ne","e","se","s","sw","w"
        self._drag_start_rect = None

        # 핸들/커서
        self._HANDLE = 3  # 정사각 핸들 반쪽=3px (전체 6px) ← 작게/깔끔하게
        self._MIN_W = 12
        self._MIN_H = 12

        # ROI(원본 좌표, xyxy)
        self.roi = {"points": None, "rank": None}
        self._load_existing_rois()  # settings → 현재 스크린 해상도 기준 xyxy

        # ── 온보딩/생성 상태 플래그 ──
        #  ※ 자동 유도(점수→등수) 모드는 비활성화한다.
        self._onboarding = False
        self._onboarding_step = None
        self._creating = False
        self._create_anchor = (0, 0)

        # ── 초기 모드/선택/힌트 ──
        has_pts = self.roi.get("points") is not None
        has_rnk = self.roi.get("rank") is not None

        # 초기 선택: 기존 좌표가 하나만 있으면 그걸 선택, 없거나 둘 다 있으면 선택 해제(None)
        if has_pts ^ has_rnk:
            self._selected = "points" if has_pts else "rank"
        else:
            self._selected = None

        # 하단 힌트
        try:
            self._set_hint(self._DRAW_HIND_MESSAGE)
        except Exception:
            pass

        # ★ 선택 시그널 커서(dotbox) 선호, 불가 시 tcross 폴백
        self._cursor_select_hint = "dotbox"
        try:
            # 일부 환경에서 미지원이면 알아서 기본 커서로 처리되므로 폴백을 준비
            self.configure(cursor=self._cursor_select_hint)
        except Exception:
            self._cursor_select_hint = "tcross"

        # ★ 일반 모드 기본 커서를 'arrow'로 고정(표시 복구)
        try:
            self.configure(cursor="arrow")
        except Exception:
            pass

        # 배지 갱신
        try:
            self._update_badge()
        except Exception:
            pass

        # ── 툴바 ──
        self._build_toolbar()

        # ── 바인딩 ──
        self.bind("<Escape>", lambda e: self._close())
        self.bind("<space>", lambda e: self._save_and_close())
        self.bind("<Return>", lambda e: self._save_and_close())
        self.bind("<Tab>", lambda e: self._toggle_selected())

        # ▼ Q/W/E 핫키
        # Q: 일반 모드(그리기 종료)
        def _hotkey_q(_e=None):
            try:
                self._exit_draw()
            except Exception:
                pass

        self.bind("q", _hotkey_q)
        self.bind("Q", _hotkey_q)

        # W: 점수 — 없으면 그리기 진입, 있으면 선택만
        def _hotkey_w(_e=None):
            if self.roi.get("points") is None:
                self._enter_draw("points")
            else:
                self._select("points")

        self.bind("w", _hotkey_w)
        self.bind("W", _hotkey_w)

        # E: 등수 — 없으면 그리기 진입, 있으면 선택만
        def _hotkey_e(_e=None):
            if self.roi.get("rank") is None:
                self._enter_draw("rank")
            else:
                self._select("rank")

        self.bind("e", _hotkey_e)
        self.bind("E", _hotkey_e)

        self.bind("<Button-1>", self._on_left_down)
        self.bind("<B1-Motion>", self._on_left_drag)
        self.bind("<ButtonRelease-1>", self._on_left_up)
        self.bind("<Motion>", self._on_motion_move)  # ← 커서 변경

        # 미세 이동(리사이즈 핸들은 Step 3)
        self.bind("<Left>", lambda e: self._nudge(dx=-1, dy=0))
        self.bind("<Right>", lambda e: self._nudge(dx=+1, dy=0))
        self.bind("<Up>", lambda e: self._nudge(dx=0, dy=-1))
        self.bind("<Down>", lambda e: self._nudge(dx=0, dy=+1))
        self.bind("<Shift-Left>", lambda e: self._nudge(dx=-5, dy=0))
        self.bind("<Shift-Right>", lambda e: self._nudge(dx=+5, dy=0))
        self.bind("<Shift-Up>", lambda e: self._nudge(dx=0, dy=-5))
        self.bind("<Shift-Down>", lambda e: self._nudge(dx=0, dy=+5))

        self.bind("<Delete>", lambda e: self._delete_selected())

        # ↓ 안내바 생성 및 기본 문구 세팅을 deiconify 전에 호출
        self._build_bottom_hint()
        self._set_hint(self._DRAW_HIND_MESSAGE)

        self.deiconify()
        self.focus_force()
        self._redraw()

    # -----------------------
    # 초기화/로딩/저장 유틸
    # -----------------------
    def _grab_fullscreen_pil(self) -> Image.Image:
        # monitors[0] = 가상 전체 화면 (verify와 동일 좌표계)
        with mss.mss() as sct:
            mon = sct.monitors[0]
            shot = sct.grab(mon)
            return Image.frombytes("RGB", (shot.width, shot.height), shot.rgb)

    def _load_existing_rois(self) -> None:
        # settings['ocr']에서 읽어서 현재 화면 크기에 스케일
        rois_xyxy, base = self.app._ocr_read_rois()
        if rois_xyxy:
            # 점수
            if "roi_points" in rois_xyxy:
                self.roi["points"] = self._scale_from_base_xyxy(rois_xyxy["roi_points"], base)
            # 등수
            if "roi_rank" in rois_xyxy:
                self.roi["rank"] = self._scale_from_base_xyxy(rois_xyxy["roi_rank"], base)

    def _scale_from_base_xyxy(self, xyxy, base_size):
        if not base_size:
            return tuple(map(int, xyxy))
        bw, bh = base_size
        iw, ih = self.screen_w, self.screen_h
        sx, sy = iw / float(bw), ih / float(bh)
        x1, y1, x2, y2 = xyxy
        return (int(round(x1 * sx)), int(round(y1 * sy)),
                int(round(x2 * sx)), int(round(y2 * sy)))

    def _xyxy_to_xywh(self, r):
        x1, y1, x2, y2 = [int(v) for v in r]
        return (x1, y1, max(0, x2 - x1), max(0, y2 - y1))

    def _save_roi(self, kind: str):
        r = self.roi.get(kind)
        if not r:
            # 삭제된 상태 저장(없음으로 덮어쓰기)
            self.app._ocr_save_roi(kind, None)
            return
        x1, y1, x2, y2 = r
        self.app._ocr_save_roi(kind, (x1, y1, x2, y2))

    def _delete_selected(self):
        sel = self._selected
        if not sel:
            return
        # 선택된 ROI가 있으면 지움
        if self.roi.get(sel) is not None:
            self.roi[sel] = None

        # 선택 해제
        self._selected = None
        try:
            self._update_badge()
        except Exception:
            pass

        # ★ NEW: 툴바 버튼 상태/하이라이트 즉시 동기화
        try:
            if hasattr(self, "_sync_toolbar_state"):
                self._sync_toolbar_state()
        except Exception:
            pass

        # 성능: 그리기 요청은 코얼레스
        self._schedule_redraw()

    # -----------------------
    # UI: 툴바/버튼
    # -----------------------
    def _build_toolbar(self):
        """
        상단 중앙 고정 툴바(오버레이)
        - 버튼: [일반] [점수(S)] [등수(R)] 만 표시
        - '점수'는 gold, '등수'는 cyan 컬러 버튼으로 강하게 구분(텍스트는 검정)
        """
        try:
            import customtkinter as ctk
        except Exception:
            return

        # 컬러 정의
        COLOR_POINTS = "#FFD700"  # gold
        COLOR_POINTS_HOVER = "#E6C200"
        COLOR_RANK = "#00FFFF"  # cyan
        COLOR_RANK_HOVER = "#00CED1"
        TEXT_DARK = "#000000"

        # ── 컨테이너: 상단 중앙 오버레이 ──
        # 코너 투명색 이슈 방지 위해 bg_color=검정
        self._toolbar_bar = ctk.CTkFrame(self, fg_color="#111318", bg_color="#000000", corner_radius=10)
        self._toolbar_bar.place(relx=0.5, y=12, anchor="n", relwidth=0.6)

        wrap = ctk.CTkFrame(self._toolbar_bar, fg_color="#111318")
        wrap.pack(pady=6)

        grid = wrap
        for c in range(6):
            grid.grid_columnconfigure(c, weight=0)

        def _btn(text, cmd, col, *, width=120, fg=None, hover=None, text_color=None, border=False):
            b = ctk.CTkButton(
                grid,
                text=text,
                width=width,
                height=36,
                command=cmd,
                font=ctk.CTkFont(size=15, weight="bold"),
                fg_color=fg,
                hover_color=hover if hover else fg,
                text_color=text_color,
                border_width=1 if border else 0,
                border_color="#0B0D10" if border else None,
            )
            b.grid(row=0, column=col, padx=8, pady=4)
            return b

        # ── 3버튼만 남김 ──
        # (1) 일반
        self._btn_mode_normal = _btn("일반(Q)", lambda: self._exit_draw(), 0, width=90, border=True)

        # (2) 점수(S) — gold
        self._btn_draw_points = _btn(
            "점수(W)",
            lambda: (self._enter_draw("points") if self.roi.get("points") is None else self._select("points")),
            1,
            fg=COLOR_POINTS,
            hover=COLOR_POINTS_HOVER,
            text_color=TEXT_DARK,
            border=True,
        )

        # (3) 등수(R) — cyan
        self._btn_draw_rank = _btn(
            "등수(E)",
            lambda: (self._enter_draw("rank") if self.roi.get("rank") is None else self._select("rank")),
            2,
            fg=COLOR_RANK,
            hover=COLOR_RANK_HOVER,
            text_color=TEXT_DARK,
            border=True,
        )

        # 우측 선택 배지(유지)
        self._badge = ctk.CTkLabel(
            grid,
            text=self._badge_text(),
            text_color="#E5E7EB",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self._badge.grid(row=0, column=5, padx=12)

        # 항상 위로
        try:
            self._toolbar_bar.lift()
        except Exception:
            pass

        # 상태 동기화
        try:
            if hasattr(self, "_sync_toolbar_state"):
                self._sync_toolbar_state()
        except Exception:
            pass

    def _sync_toolbar_state(self):
        """
        툴바 3버튼 동기화:
          - ROI 유무에 따라 [점수]/[등수] 그리기 버튼 enable/disable
          - normal 모드: [일반]도 점수/등수와 동일 컨벤션(배경 유지, '테두리 두껍게(2px, 흰색)'로 강조)
          - draw 모드: 대상 그리기 버튼은 원래 색(gold/cyan) 유지 + 테두리 두껍게(2px, 흰색) 강조
        """
        try:
            import customtkinter as ctk  # noqa: F401
        except Exception:
            return  # CTk 미사용 환경이면 스킵

        def _btn(name):
            return getattr(self, name, None)

        def _enable(name, enabled: bool):
            b = _btn(name)
            if not b:
                return
            try:
                b.configure(state=("normal" if enabled else "disabled"))
            except Exception:
                pass

        # 기본 스타일 초기화
        def _reset_style_normal_btn():
            b = _btn("_btn_mode_normal")
            if b:
                try:
                    b.configure(fg_color=None, text_color=None, border_width=1, border_color="#0B0D10")
                except Exception:
                    pass

        def _reset_style_draw_btn(btn_name):
            b = _btn(btn_name)
            if b:
                try:
                    b.configure(border_width=1, border_color="#0B0D10")
                except Exception:
                    pass

        # 강조(두꺼운 흰 테두리)
        def _emphasize_border(btn_name):
            b = _btn(btn_name)
            if b:
                try:
                    b.configure(border_width=2, border_color="#FFFFFF")
                except Exception:
                    pass

        # 상태
        mode = getattr(self, "mode", "normal")
        tgt = getattr(self, "draw_target", None)
        has_pts = self.roi.get("points") is not None
        has_rnk = self.roi.get("rank") is not None

        # 1) enable/disable
        _enable("_btn_draw_points", not has_pts)
        _enable("_btn_draw_rank", not has_rnk)
        _enable("_btn_mode_normal", True)

        # 2) 스타일 초기화
        _reset_style_normal_btn()
        _reset_style_draw_btn("_btn_draw_points")
        _reset_style_draw_btn("_btn_draw_rank")

        # 3) 모드별 강조
        if mode == "draw":
            if tgt == "points":
                _emphasize_border("_btn_draw_points")
            elif tgt == "rank":
                _emphasize_border("_btn_draw_rank")
        else:
            # ✅ 일반 모드도 점수/등수와 '동일한' 하이라이트(두꺼운 흰 테두리)
            _emphasize_border("_btn_mode_normal")

    def _badge_text(self):
        """
        상단 배지 텍스트:
        - 선택 없음: '선택: 없음'
        - 점수/등수 선택: '선택: 점수' / '선택: 등수'
        (향후 draw 모드가 들어오면 여기서 모드/타깃도 함께 표기 예정)
        """
        sel = getattr(self, "_selected", None)
        if sel == "points":
            return "선택: 점수"
        if sel == "rank":
            return "선택: 등수"
        return "선택: 없음"

    def _update_badge(self):
        """
        배지 레이블 텍스트를 갱신한다. 위젯이 없으면 조용히 무시.
        """
        try:
            badge = getattr(self, "_badge", None)
            if badge is not None:
                badge.configure(text=self._badge_text())
        except Exception:
            # 배지 갱신 실패해도 앱 동작엔 영향 없음
            pass

    def _save_and_close(self):
        pts = self.roi.get("points")
        rnk = self.roi.get("rank")
        try:
            self.app._ocr_save_rois_both(pts, rnk)
            messagebox.showinfo("ROI 저장", "좌표 설정이 저장되었습니다.", parent=self.app)
        except Exception as e:
            messagebox.showerror("저장 실패", str(e), parent=self.app)
        self._close()

    def _enter_draw(self, target: str):
        """
        그리기 모드 진입.
        - 일반 버튼 하이라이트 즉시 해제
        - ✅ 기존 선택을 해제하고(draw에 집중), 드래그로만 새 ROI 생성
        - 커서/배지/힌트/툴바 상태를 draw 기준으로 세팅
        """
        # 0) '일반' 버튼 하이라이트 즉시 제거
        try:
            b = getattr(self, "_btn_mode_normal", None)
            if b:
                b.configure(fg_color=None, text_color=None, border_width=1, border_color="#0B0D10")
        except Exception:
            pass

        # 1) 모드/타깃 설정
        self.mode = "draw"
        self.draw_target = target

        # ✅ 2) 기존 선택 해제(핸들/점선 등 모두 사라지게)
        self._selected = None
        try:
            self._update_badge()  # '선택: 없음'
        except Exception:
            pass

        # 생성 플래그 초기화
        self._creating = False

        # 3) 힌트 갱신
        try:
            label = "점수" if target == "points" else "등수"
            # Q/W/E 안내는 초기 힌트에 이미 있으므로 여기선 드래그 유도만
            self._set_hint(f"{label} 영역을 드래그해서 그리세요")
        except Exception:
            pass

        # 4) 커서: 드로우 모드 기본 'tcross'
        try:
            self.configure(cursor="tcross")
        except Exception:
            pass

        # 5) 툴바 동기화 + 화면 갱신
        try:
            sync = getattr(self, "_sync_toolbar_state", None)
            if callable(sync):
                sync()
        except Exception:
            pass
        # 선택 해제 반영을 끊김 없이 적용
        self._schedule_redraw()

    def _exit_draw(self):
        """
        그리기 모드 종료 → 일반 모드 복귀.
        - 온보딩과 무관하게 draw 모드만 닫는다.
        - 힌트/배지/커서/툴바 상태를 일반 모드로 돌린다.
        """
        # 상태 리셋(기본값 방어)
        self.mode = "normal"
        self.draw_target = None
        self._creating = False

        # 힌트 복귀
        try:
            self._set_hint(self._DRAW_HIND_MESSAGE)
        except Exception:
            pass

        # 배지 갱신(선택 유지)
        try:
            self._update_badge()
        except Exception:
            pass

        # 커서: 배경 기본
        try:
            self.configure(cursor="arrow")
        except Exception:
            pass

        # 툴바 상태 동기화(있을 때만)
        try:
            sync = getattr(self, "_sync_toolbar_state", None)
            if callable(sync):
                sync()
        except Exception:
            pass

    # -----------------------
    # 선택/이동(Step 2 범위)
    # -----------------------
    def _toggle_selected(self):
        self._selected = "rank" if self._selected == "points" else "points"
        self._update_badge()
        self._redraw()

    def _select(self, kind: str):
        if kind in ("points", "rank"):
            self._selected = kind
            self._update_badge()
            self._redraw()

    def _on_left_down(self, e):
        mx, my = e.x, e.y
        EDGE_HIT = 5

        # ----------------------------------------
        # 오버레이(툴바/힌트) 위 클릭은 무시
        # ----------------------------------------
        w = getattr(e, "widget", None)
        if w is not None:
            node = w
            for _ in range(12):
                if node is None:
                    break
                if node is getattr(self, "_toolbar_bar", None) or node is getattr(self, "_hint_bar", None):
                    return
                if node is self:
                    break
                try:
                    node = node.master
                except Exception:
                    break

        # ----------------------------------------
        # 0) DRAW 모드: 드래그로 '새 ROI 생성'
        # ----------------------------------------
        if getattr(self, "mode", "normal") == "draw" and self.draw_target in ("points", "rank"):
            tgt = self.draw_target
            if self.roi.get(tgt) is None:
                self._creating = True
                self._create_anchor = (mx, my)
                self.roi[tgt] = (mx, my, mx + 1, my + 1)
                self._selected = tgt
                try:
                    self._set_hint("")
                except Exception:
                    pass
                self._schedule_redraw()
                return
            else:
                # 이미 있으면 선택만 하고 draw 종료
                self._selected = tgt
                try:
                    self._update_badge()
                except Exception:
                    pass
                self._exit_draw()
                self._schedule_redraw()
                return

        # ----------------------------------------
        # 1) (선택된 ROI) 핸들 히트 → 리사이즈 시작
        # ----------------------------------------
        r_sel = self.roi.get(self._selected)
        if r_sel is not None:
            hit = self._hit_handle(r_sel, mx, my)
            if hit:
                self._resizing = True
                self._resize_handle = hit
                self._drag_start_rect = r_sel
                self._drag_anchor = (mx, my)
                return

            # 2) (선택된 ROI) 내부 클릭 → 이동 시작(테두리 띠 제외)
            x1, y1, x2, y2 = r_sel
            inside_strict = (x1 + EDGE_HIT < mx < x2 - EDGE_HIT) and (y1 + EDGE_HIT < my < y2 - EDGE_HIT)
            if inside_strict:
                self._dragging = True
                self._drag_start_rect = r_sel
                self._drag_anchor = (mx, my)
                return
            # 선택된 ROI의 테두리 띠 클릭은 아무 작업도 시작하지 않음(요구사항상 '선택만' 대상이 아님)

        # ----------------------------------------
        # 3) (비선택 ROI) 테두리 띠 클릭 → 선택만 수행
        #    - 커서가 arrow로 바뀌는 영역과 동일한 판정
        # ----------------------------------------
        for kind in ("points", "rank"):
            if kind == self._selected:
                continue
            r = self.roi.get(kind)
            if not r:
                continue
            x1, y1, x2, y2 = r
            on_left = (y1 - EDGE_HIT <= my <= y2 + EDGE_HIT) and (abs(mx - x1) <= EDGE_HIT)
            on_right = (y1 - EDGE_HIT <= my <= y2 + EDGE_HIT) and (abs(mx - x2) <= EDGE_HIT)
            on_top = (x1 - EDGE_HIT <= mx <= x2 + EDGE_HIT) and (abs(my - y1) <= EDGE_HIT)
            on_bottom = (x1 - EDGE_HIT <= mx <= x2 + EDGE_HIT) and (abs(my - y2) <= EDGE_HIT)
            if on_left or on_right or on_top or on_bottom:
                self._selected = kind
                try:
                    self._update_badge()
                except Exception:
                    pass
                self._schedule_redraw()
                return

        # ----------------------------------------
        # 4) 배경 클릭 → 선택 해제
        # ----------------------------------------
        self._selected = None
        try:
            self._update_badge()
        except Exception:
            pass
        self._schedule_redraw()

    def _on_left_drag(self, e):
        mx, my = e.x, e.y

        # -----------------------------------
        # A) 온보딩에서 '드래그로 새 박스 생성' 중
        #    - _on_left_down에서 _creating=True, _create_anchor가 세팅되어 있음
        #    - 드래그하는 동안 앵커↔현재 커서로 사각형을 실시간 갱신
        # -----------------------------------
        if getattr(self, "_creating", False):
            ax, ay = self._create_anchor
            x1, y1 = ax, ay
            x2, y2 = mx, my
            # 정규화 + 화면 경계 클램프
            x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2)

            # 너무 작은 값일 때도 라벨이 보이도록 최소 4x4까진 그려 준다
            MIN_DRAW = 4
            if (x2 - x1) < MIN_DRAW:
                if x2 >= ax:
                    x2 = x1 + MIN_DRAW
                else:
                    x1 = x2 - MIN_DRAW
            if (y2 - y1) < MIN_DRAW:
                if y2 >= ay:
                    y2 = y1 + MIN_DRAW
                else:
                    y1 = y2 - MIN_DRAW

            # 현재 선택된 ROI(점수/등수)에 실시간 반영
            self.roi[self._selected] = (x1, y1, x2, y2)
            self._schedule_redraw()
            return

        # -----------------------------------
        # B) 기존 리사이즈 동작
        # -----------------------------------
        if self._resizing and self._drag_start_rect is not None:
            self._apply_resize(mx, my)
            self._schedule_redraw()
            return

        # -----------------------------------
        # C) 기존 이동 동작
        # -----------------------------------
        if self._dragging and self._drag_start_rect is not None:
            ax, ay = self._drag_anchor
            dx, dy = mx - ax, my - ay
            x1, y1, x2, y2 = self._drag_start_rect
            nx1, ny1, nx2, ny2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
            nx1, ny1, nx2, ny2 = self._clamp_rect(nx1, ny1, nx2, ny2)
            if (nx2 - nx1) >= self._MIN_W and (ny2 - ny1) >= self._MIN_H:
                self.roi[self._selected] = (nx1, ny1, nx2, ny2)
                self._schedule_redraw()

    def _on_left_up(self, _e):
        """
        드래그/리사이즈/생성 종료 처리.
        - 생성 중이었다면 좌표 보정 후, draw 모드인 경우에만 _exit_draw() 호출
        - 온보딩 단계 전환 로직 제거(생성은 오직 draw 모드에서만)
        """
        creating = bool(getattr(self, "_creating", False))

        # 공통 플래그 해제
        self._dragging = False
        self._resizing = False
        self._resize_handle = None
        self._drag_start_rect = None
        self._creating = False

        if creating:
            sel = self._selected
            r = self.roi.get(sel)
            if r:
                x1, y1, x2, y2 = r
                # 정규화 + 화면 클램프 + 최소 크기 보장
                x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2)
                if (x2 - x1) < self._MIN_W:
                    x2 = x1 + self._MIN_W
                    if x2 > self.screen_w:
                        x1 = max(0, self.screen_w - self._MIN_W);
                        x2 = self.screen_w
                if (y2 - y1) < self._MIN_H:
                    y2 = y1 + self._MIN_H
                    if y2 > self.screen_h:
                        y1 = max(0, self.screen_h - self._MIN_H);
                        y2 = self.screen_h
                self.roi[sel] = (x1, y1, x2, y2)

            # draw 모드였다면 자동 종료(일반 모드면 아무 것도 안 함)
            if getattr(self, "mode", "normal") == "draw":
                try:
                    self._exit_draw()
                except Exception:
                    pass

            self._schedule_redraw()
            return

        # 생성이 아니면 프레임만 갱신
        self._schedule_redraw()

    def _nudge(self, dx=0, dy=0):
        r = self.roi.get(self._selected)
        if not r:
            return
        x1, y1, x2, y2 = r
        nx1, ny1, nx2, ny2 = x1 + dx, y1 + dy, x2 + dx, y2 + dy
        nx1 = max(0, min(self.screen_w - 1, nx1));
        nx2 = max(1, min(self.screen_w, nx2))
        ny1 = max(0, min(self.screen_h - 1, ny1));
        ny2 = max(1, min(self.screen_h, ny2))
        if nx2 <= nx1 + 1 or ny2 <= ny1 + 1:
            return
        self.roi[self._selected] = (nx1, ny1, nx2, ny2)
        self._redraw()

    # ---------- 커서/히트/리사이즈 유틸 ----------
    def _on_motion_move(self, e):
        """
        커서 우선순위(수정):
          1) (선택 ROI) 핸들 히트 → 방향 커서
          2) (선택 ROI) 순수 내부 → fleur
          3) (비선택 ROI) 테두리 띠 → arrow  ← '지금 클릭하면 선택' 시그널
          4) 그 외(선택 ROI 테두리 포함) → tcross
        """
        EDGE_HIT = 5
        mx, my = e.x, e.y

        # 호버 상태 초기화
        self._hover_kind = None
        self._hover_zone = "none"
        self._hover_handle = None

        # 1) 선택 ROI: 핸들 히트 → 리사이즈 커서
        r_sel = self.roi.get(self._selected)
        if r_sel:
            hit = self._hit_handle(r_sel, mx, my)
            if hit:
                self._hover_kind = self._selected
                self._hover_zone = "handle"
                self._hover_handle = hit
                try:
                    self.configure(cursor={
                        "n": "top_side", "s": "bottom_side", "w": "left_side", "e": "right_side",
                        "nw": "top_left_corner", "ne": "top_right_corner",
                        "sw": "bottom_left_corner", "se": "bottom_right_corner"
                    }[hit])
                except:
                    pass
                return

            # 선택 ROI: 순수 내부 → fleur (테두리 띠는 cursor 변경하지 않음)
            x1, y1, x2, y2 = r_sel
            inside_strict = (x1 + EDGE_HIT < mx < x2 - EDGE_HIT) and (y1 + EDGE_HIT < my < y2 - EDGE_HIT)
            if inside_strict:
                self._hover_kind = self._selected
                self._hover_zone = "inside"
                try:
                    self.configure(cursor="fleur")
                except:
                    pass
                return
            # 선택 ROI 테두리 띠에서는 커서를 바꾸지 않고 계속 진행(= 아래 비선택 ROI 체크로 넘어감)

        # 2) 비선택 ROI: 테두리 띠 위라면 arrow (선택 유도 전용)
        best = None  # (dist, kind)
        for kind in ("points", "rank"):
            if kind == self._selected:
                continue
            r = self.roi.get(kind)
            if not r:
                continue
            x1, y1, x2, y2 = r
            on_left = (y1 - EDGE_HIT <= my <= y2 + EDGE_HIT) and (abs(mx - x1) <= EDGE_HIT)
            on_right = (y1 - EDGE_HIT <= my <= y2 + EDGE_HIT) and (abs(mx - x2) <= EDGE_HIT)
            on_top = (x1 - EDGE_HIT <= mx <= x2 + EDGE_HIT) and (abs(my - y1) <= EDGE_HIT)
            on_bottom = (x1 - EDGE_HIT <= mx <= x2 + EDGE_HIT) and (abs(my - y2) <= EDGE_HIT)
            if on_left or on_right or on_top or on_bottom:
                dist = min(
                    abs(mx - x1) if on_left else EDGE_HIT + 1,
                    abs(mx - x2) if on_right else EDGE_HIT + 1,
                    abs(my - y1) if on_top else EDGE_HIT + 1,
                    abs(my - y2) if on_bottom else EDGE_HIT + 1,
                )
                if (best is None) or (dist < best[0]):
                    best = (dist, kind)

        # 비선택 ROI 테두리 띠 → '선택 시그널' 커서(dotbox, 폴백 tcross)
        if best is not None:
            self._hover_kind = best[1]
            self._hover_zone = "border"
            hint = getattr(self, "_cursor_select_hint", "tcross")
            try:
                self.configure(cursor=hint)
            except Exception:
                try:
                    self.configure(cursor="tcross")
                except Exception:
                    pass
            return

        # 그 외 → 배경: 드로우 모드면 tcross, 일반 모드면 arrow
        try:
            if getattr(self, "mode", "normal") == "draw":
                self.configure(cursor="tcross")
            else:
                self.configure(cursor="arrow")
        except Exception:
            pass

    def _clamp_rect(self, x1, y1, x2, y2):
        x1 = max(0, min(self.screen_w - 1, x1))
        y1 = max(0, min(self.screen_h - 1, y1))
        x2 = max(1, min(self.screen_w, x2))
        y2 = max(1, min(self.screen_h, y2))
        # 정규화
        if x2 < x1: x1, x2 = x2, x1
        if y2 < y1: y1, y2 = y2, y1
        return (x1, y1, x2, y2)

    def _point_in_rect(self, r, x, y):
        x1, y1, x2, y2 = r
        return (x1 <= x <= x2) and (y1 <= y <= y2)

    def _handle_positions(self, r):
        x1, y1, x2, y2 = [int(v) for v in r]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        return {
            "nw": (x1, y1), "n": (cx, y1), "ne": (x2, y1),
            "e": (x2, cy), "se": (x2, y2), "s": (cx, y2),
            "sw": (x1, y2), "w": (x1, cy)
        }

    def _hit_handle(self, r, mx, my):
        H = self._HANDLE
        for name, (hx, hy) in self._handle_positions(r).items():
            if abs(mx - hx) <= H and abs(my - hy) <= H:
                return name
        return None

    def _apply_resize(self, mx, my):
        if not self._resize_handle or self._drag_start_rect is None:
            return
        x1, y1, x2, y2 = self._drag_start_rect
        # 기준점별 업데이트
        h = self._resize_handle
        if "n" in h: y1 = my
        if "s" in h: y2 = my
        if "w" in h: x1 = mx
        if "e" in h: x2 = mx
        x1, y1, x2, y2 = self._clamp_rect(x1, y1, x2, y2)
        if (x2 - x1) < self._MIN_W:  # 최소 폭
            if "w" in h:
                x1 = x2 - self._MIN_W
            else:
                x2 = x1 + self._MIN_W
        if (y2 - y1) < self._MIN_H:  # 최소 높이
            if "n" in h:
                y1 = y2 - self._MIN_H
            else:
                y2 = y1 + self._MIN_H
        self.roi[self._selected] = (x1, y1, x2, y2)

    def _build_bottom_hint(self):
        """
        중앙·하단 사이(약 82%)에 더 크게 표시되는 안내 오버레이.
        - place(relx=0.5, rely=0.82, anchor="center")로 화면 중앙과 하단의 중간쯤 고정
        - 폭은 화면의 70% (relwidth=0.7)
        - 글자 크기 16pt, bold
        """
        try:
            import customtkinter as ctk
        except Exception:
            # tkinter 폴백
            import tkinter as tk
            self._hint_bar = tk.Frame(self, bg="#0B0D10")
            # 중앙과 하단의 중간 정도 높이
            self._hint_bar.place(relx=0.5, rely=0.82, anchor="center", relwidth=0.7)
            self._hint_label = tk.Label(
                self._hint_bar,
                text="",
                fg="#E5E7EB", bg="#0B0D10",
                font=("맑은 고딕", 16, "bold")
            )
            self._hint_label.pack(padx=12, pady=6)
            try:
                self._hint_bar.lift()
            except Exception:
                pass
            return

        # customtkinter 경로
        self._hint_bar = ctk.CTkFrame(self, fg_color="#0B0D10", bg_color="#000000", corner_radius=10)
        # 중앙과 하단의 중간 정도 위치에, 폭 70%로 표시
        self._hint_bar.place(relx=0.5, rely=0.82, anchor="center", relwidth=0.7)

        self._hint_label = ctk.CTkLabel(
            self._hint_bar,
            text="",  # 내용은 _set_hint로 주입
            text_color="#E5E7EB",
            font=ctk.CTkFont(size=20, weight="bold")  # ← 14 → 16
        )
        self._hint_label.pack(padx=14, pady=8)

        try:
            self._hint_bar.lift()
        except Exception:
            pass

    def _set_hint(self, text: str):
        try:
            if hasattr(self, "_hint_label") and self._hint_label is not None:
                self._hint_label.configure(text=text)
        except Exception:
            pass

    # -----------------------
    # 그리기(배경 고정, ROI 레이어만 갱신)
    # -----------------------
    def _schedule_redraw(self):
        """
        드래그/리사이즈 도중 다수의 redraw 요청을 한 틱으로 합쳐서 그린다.
        - 기존 34ms 타이머 기반 → after_idle 코얼레스(이벤트 루프당 1회)
        - 타이머 취소/재예약 오버헤드 제거로 끊김 완화
        """
        # 이미 그리기 예약이 있으면 추가 예약하지 않음
        if getattr(self, "_frame_pending", False):
            return

        self._frame_pending = True

        def _do():
            # 플래그 해제 후 실제 그리기
            self._frame_pending = False
            try:
                self._redraw()
            except Exception:
                # 문제가 생겨도 다음 프레임은 정상 동작하도록 보장
                pass

        # OS 이벤트가 한 번 모인 뒤 한 번만 그린다(프레임 코얼레스)
        try:
            self.after_idle(_do)
        except Exception:
            # after_idle 사용 불가 환경 폴백: 최소 지연으로 즉시 그리기
            self.after(1, _do)

    def _redraw(self):
        """디밍된 배경 + ROI 원본 크롭(스팟)으로 점무늬 없이 필름 느낌 구현"""
        W, H = self.screen_w, self.screen_h

        # 0) 기존 스팟/쉐이드 정리
        try:
            self.canvas.delete("shade")
        except Exception:
            pass
        for iid in getattr(self, "_spot_item_ids", []):
            try:
                self.canvas.delete(iid)
            except Exception:
                pass
        self._spot_item_ids = []
        self._spot_imgs_tk = []

        # 1) ROI 구멍 좌표 수집
        holes = []
        for kind in ("points", "rank"):
            r = self.roi.get(kind)
            if r:
                x1, y1, x2, y2 = [int(v) for v in r]
                if x2 < x1: x1, x2 = x2, x1
                if y2 < y1: y1, y2 = y2, y1
                x1 = max(0, min(W, x1));
                x2 = max(0, min(W, x2))
                y1 = max(0, min(H, y1));
                y2 = max(0, min(H, y2))
                if (x2 - x1 >= 1) and (y2 - y1 >= 1):
                    holes.append((x1, y1, x2, y2))

        # 2) 각 ROI에 원본 크롭 얹기(밝은 구멍처럼 보임)
        for (x1, y1, x2, y2) in holes:
            crop = self.bg_img.crop((x1, y1, x2, y2))
            crop_tk = ImageTk.PhotoImage(crop)
            iid = self.canvas.create_image(x1, y1, anchor="nw", image=crop_tk, tags=("spot",))
            self._spot_imgs_tk.append(crop_tk)  # GC 방지
            self._spot_item_ids.append(iid)

        # 3) ROI 테두리/라벨 및 선택 강조는 기존 루틴 재사용
        try:
            self.canvas.delete("roi")
        except Exception:
            pass
        self._draw_roi("points", self._COLOR_POINTS, "점수")
        self._draw_roi("rank", self._COLOR_RANK, "등수")
        self._draw_selection_outline()

    def _draw_roi(self, kind: str, color: str, label: str):
        r = self.roi.get(kind)
        if not r:
            return

        x1, y1, x2, y2 = [int(v) for v in r]

        # 1) ROI 테두리
        self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3, tags=("roi",))

        # 2) 라벨(상단) — 가독성 강화
        #    - 배경: 진한 회색 고정(#0B0D10), 불투명
        #    - 텍스트: ROI 색(color)로 크게(16px, bold) + 검정 1px 외곽선(그림자)
        tw = max(56, int((x2 - x1) * 0.25))  # 기존 규칙 유지
        # 라벨 박스 높이를 폰트(16px) 기준으로 약간 크게(24px)
        label_h = 24
        tx, ty = x1 + 8, max(0, y1 - label_h)  # 좌상단 시작점(여백 조금 늘림)

        # 배경 박스
        self.canvas.create_rectangle(
            tx - 8, ty, tx - 8 + tw, ty + label_h,
            fill="#0B0D10", outline="", tags=("roi",)
        )

        # 텍스트 외곽선(그림자) → 검정 1px offset
        font_spec = ("맑은 고딕", 16, "bold")
        self.canvas.create_text(
            tx + 1, ty + label_h // 2 + 1,
            anchor="w", text=label, fill="#000000",
            font=font_spec, tags=("roi",)
        )
        # 본문(ROI 색)
        self.canvas.create_text(
            tx, ty + label_h // 2,
            anchor="w", text=label, fill=color,
            font=font_spec, tags=("roi",)
        )

    def _draw_selection_outline(self):
        r = self.roi.get(self._selected)
        if not r:
            return
        x1, y1, x2, y2 = [int(v) for v in r]
        # 점선 박스
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            outline="#FFFFFF", width=1, dash=(4, 3),
            tags=("roi",)
        )
        # 8-방향 핸들
        H = self._HANDLE
        for _, (hx, hy) in self._handle_positions(r).items():
            self.canvas.create_rectangle(
                hx - H, hy - H, hx + H, hy + H,
                outline="#FFFFFF", fill="#FFFFFF",
                tags=("roi",)
            )

    # -----------------------
    # 종료/정리
    # -----------------------
    def _close(self):
        # 타이머/after 취소
        try:
            if self._throttle_after:
                self.after_cancel(self._throttle_after)
                self._throttle_after = None
        except Exception:
            pass

        # 캔버스 이미지/참조 해제
        try:
            self.canvas.delete("all")
        except Exception:
            pass
        self._bg_id = None
        self._bg_tk = None
        self.bg_img = None
        self.bg_dim = None

        # 그랩 해제 및 파괴
        try:
            self.grab_release()
        except Exception:
            pass
        try:
            self.destroy()
        finally:
            # 메인 GUI 복귀는 반드시 실행
            try:
                self.app._leave_overlay_mode(self._overlay_ctx)
            except Exception:
                pass


def _get_settings_path():
    """settings.json 절대 경로를 반환한다(SETTINGS_JSON 고정 사용)."""
    try:
        return str(SETTINGS_JSON)  # path_manager에서 가져온 확정 경로
    except Exception:
        return os.path.abspath("settings.json")


def _load_settings_json():
    import json, os
    p = _get_settings_path()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_points_roi_and_screen():
    """
    settings.json의 ocr.roi_points(없으면 ocr.roi_rank)와 저장 당시 screen(w,h)을 읽어온다.
    반환: ((x,y,w,h) 또는 None, (bw,bh) 또는 None)
    """
    s = _load_settings_json()
    ocr = s.get("ocr", {}) or {}
    roi = _as_roi_tuple(ocr.get("roi_points")) or _as_roi_tuple(ocr.get("roi_rank"))
    base = None
    scr = ocr.get("screen")
    if isinstance(scr, dict) and "w" in scr and "h" in scr:
        base = (int(scr["w"]), int(scr["h"]))
    return roi, base


def _scale_xywh_from_base(xywh, base_size, img_size):
    """
    (x,y,w,h) @base_size → @img_size 로 스케일링.
    base_size가 없으면 원본 반환.
    """
    if not xywh:
        return None
    if not base_size:
        return tuple(map(int, xywh))
    x, y, w, h = [int(v) for v in xywh]
    bw, bh = base_size
    iw, ih = img_size
    sx, sy = iw / float(bw), ih / float(bh)
    return (int(round(x * sx)), int(round(y * sy)), int(round(w * sx)), int(round(h * sy)))


def _as_roi_tuple(roi):
    """
    dict({x,y,w,h}) 또는 list/tuple([x,y,w,h]) → (x,y,w,h) 튜플 변환.
    잘못된 포맷이면 None.
    """
    if roi is None:
        return None
    if isinstance(roi, dict):
        try:
            return int(roi["x"]), int(roi["y"]), int(roi["w"]), int(roi["h"])
        except Exception:
            return None
    if isinstance(roi, (list, tuple)) and len(roi) == 4:
        try:
            x, y, w, h = [int(v) for v in roi]
            return x, y, w, h
        except Exception:
            return None
    return None


def _crop_with_roi(pil_img, roi):
    """
    PIL.Image + (x,y,w,h) → ROI 크롭 반환(안전 클램프).
    """
    from PIL import Image
    x, y, w, h = roi
    W, H = pil_img.size
    x2, y2 = x + w, y + h
    x = max(0, min(W, x))
    y = max(0, min(H, y))
    x2 = max(0, min(W, x2))
    y2 = max(0, min(H, y2))
    if x2 <= x or y2 <= y:
        return pil_img.copy()  # 비정상 ROI면 원본 반환
    return pil_img.crop((x, y, x2, y2))


def _ocr_digits_with_fallback(pil_img):
    """
    우선순위:
      0) (있으면) 전용 텍스트 탐지: read_rank_out_of_range_ko → 매칭되면 즉시 (None, raw_t)
      1) 일반 텍스트 읽기: read_text
      2) 숫자 읽기: read_digits → 성공 시 (val, raw_d)
      3) 구버전 숫자 폴백: ocr_digits/detect_score
      4) 최후: pytesseract 직접(Text→Digits)
    """
    try:
        from core import ocr as _ocr

        # (0) 전용 텍스트 탐지 (있을 때만)
        rr = getattr(_ocr, "read_rank_out_of_range_ko", None)
        if callable(rr):
            try:
                raw_rr = str(rr(pil_img) or "")
                if raw_rr:  # 전용 탐지 성공(문구 일부라도 잡힘)
                    return (None, raw_rr)
            except Exception:
                pass

        # (1) 일반 텍스트
        rt = getattr(_ocr, "read_text", None)
        raw_t = ""
        if callable(rt):
            raw_t = str(rt(pil_img) or "")
            try:
                if _is_oor_ko(raw_t):
                    return (None, raw_t)
            except Exception:
                pass

        # (2) 숫자
        rd = getattr(_ocr, "read_digits", None)
        if callable(rd):
            raw_d = str(rd(pil_img) or "")
            val = _parse_first_int(raw_d)
            if isinstance(val, int):
                return (val, raw_d)

        # (3) 구버전 숫자 폴백
        for fn_name in ("ocr_digits", "detect_score"):
            fn = getattr(_ocr, fn_name, None)
            if callable(fn):
                raw = str(fn(pil_img) or "")
                val = _parse_first_int(raw)
                if isinstance(val, int):
                    return (val, raw)
                return (None, raw_t or raw)

        if raw_t:
            return (None, raw_t)
    except Exception:
        pass

    # (4) pytesseract 최후 폴백 (Text→Digits)
    try:
        import pytesseract
        raw_t = pytesseract.image_to_string(pil_img, config="--psm 7") or ""
        try:
            if _is_oor_ko(raw_t):
                return (None, raw_t)
        except Exception:
            pass

        raw_d = pytesseract.image_to_string(
            pil_img, config="--psm 7 -c tessedit_char_whitelist=0123456789"
        ) or ""
        val = _parse_first_int(raw_d)
        if isinstance(val, int):
            return (val, raw_d)
        return (None, raw_t or raw_d)
    except Exception as e:
        return (None, f"no_ocr_module: {type(e).__name__}")


def _parse_first_int(s):
    """
    문자열에서 첫 번째 정수 토큰을 파싱. 없으면 None.
    """
    import re
    m = re.search(r"\d+", s or "")
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None


def _run_calib_ocr_and_render(parent_window, result_label, pil_img):
    """
    좌표 확인(OCR 테스트):
      - ROI를 미리보기 이미지 크기에 맞게 스케일링 후 크롭
      - 결과는 하단 중앙의 한 줄 라벨에만 표시
      - 색상: 두 항목 모두 '인식 성공'이면 초록, 아니면 주황
        (등수는 텍스트도 성공으로 간주)
    """
    # 0) ROI 읽기
    try:
        app = getattr(parent_window, "master", None)  # OverlayApp
        if app and hasattr(app, "_ocr_read_rois"):
            rois_xyxy, base = app._ocr_read_rois()
        else:
            roi_xywh, base = _load_points_roi_and_screen()
            rois_xyxy = {}
            if roi_xywh:
                x, y, w, h = roi_xywh
                rois_xyxy["roi_points"] = (x, y, x + w, y + h)
    except Exception:
        rois_xyxy, base = {}, None

    # 1) ROI → 현재 미리보기 사이즈로 스케일
    def _scale_xyxy(xyxy):
        if not xyxy:
            return None
        try:
            W, H = pil_img.size
            if app and hasattr(app, "_scale_from_base"):
                return app._scale_from_base(xyxy, base, (W, H))
        except Exception:
            pass
        return tuple(map(int, xyxy))

    pts_xyxy = _scale_xyxy(rois_xyxy.get("roi_points"))
    rank_xyxy = _scale_xyxy(rois_xyxy.get("roi_rank"))

    # 2) 크롭
    def _crop_xyxy(img, xyxy):
        x1, y1, x2, y2 = [int(v) for v in xyxy]
        return img.crop((x1, y1, x2, y2))

    # 3) 점수 OCR
    pts_text, pts_ok = "점수: 인식 실패", False
    if pts_xyxy is not None:
        val, raw = _ocr_digits_with_fallback(_crop_xyxy(pil_img, pts_xyxy))
        if isinstance(val, int) and val >= 0:
            pts_text, pts_ok = f"점수: {val}점", True

    # 4) 등수 OCR (숫자 또는 텍스트를 성공으로 인정)
    rank_text, rank_ok = "등수: 인식 실패", False
    if rank_xyxy is not None:
        val, raw = _ocr_digits_with_fallback(_crop_xyxy(pil_img, rank_xyxy))
        if isinstance(val, int) and val > 0:
            rank_text, rank_ok = f"등수: {val}등", True
        elif isinstance(raw, str) and _is_oor_ko(raw):
            rank_text, rank_ok = "등수: (숫자 아님)", True

    # 5) 하단 결과 라벨(한 줄) 업데이트
    overall_color = _COLOR_OK if (pts_ok and rank_ok) else _COLOR_WARN
    try:
        result_label.configure(text=f"{pts_text} | {rank_text}", text_color=overall_color)
    except Exception:
        # customtkinter 미지원 환경 대비
        try:
            result_label.config(text=f"{pts_text} | {rank_text}", fg=overall_color)
        except Exception:
            pass


def _attach_ocr_toolbar(parent_window, preview_label):
    """
    검증창 상단에 OCR 실행/토글용 툴바를 붙인다.
    최신 이미지는 parent_window._pil_img_factory()로 '매번' 얻는다.
    """
    bar = ctk.CTkFrame(parent_window)
    bar.pack(side="top", fill="x", padx=8, pady=(8, 4))
    # 가운데 정렬용 그리드 3분할
    bar.grid_columnconfigure(0, weight=1)
    bar.grid_columnconfigure(1, weight=0)
    bar.grid_columnconfigure(2, weight=1)
    parent_window._ocr_toolbar = bar

    result_label = ctk.CTkLabel(
        parent_window,
        text="",
        anchor="center",
        font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold")
    )
    result_label.pack(side="top", pady=(2, 10))
    parent_window._ocr_result_label = result_label

    def on_click_run():
        # 1) Tesseract 경로 보장
        try:
            ensure = getattr(parent_window.master, "_ensure_tesseract_path", None)
            if callable(ensure) and not ensure(silent=False):
                from tkinter import messagebox
                messagebox.showwarning("OCR 준비 실패", "Tesseract 경로를 설정하지 못했습니다.", parent=parent_window)
                return
        except Exception:
            pass

        # 2) 최신 이미지 동적 조회(클로저 캡쳐 금지)
        fac = getattr(parent_window, "_pil_img_factory", None)
        pil_img = fac() if callable(fac) else None
        if pil_img is None:
            return
        _run_calib_ocr_and_render(parent_window, result_label, pil_img)

    btn = ctk.CTkButton(
        bar, text="OCR 테스트 실행", command=on_click_run,
        width=200, height=36
    )
    btn.grid(row=0, column=1, pady=(6, 8))

    on_click_run()  # 창 열릴 때 1회 자동 실행
