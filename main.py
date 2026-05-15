# 제작 : killsonic@naver.com 불닭@네이버
# 수정 : <HazySound>

import tkinter as tk
from tkinter import messagebox
import pyautogui as pgi
import pyscreeze
from PIL import Image  # ✅ 템플릿 사이즈 확인용
import time
import pydirectinput as pyd
import random
import autoemail
import path_manager as pm
from path_manager import SETTINGS_JSON, BASE_DIR  # ✅ 단일 경로 SSOT
from lock_utils import create_lock, remove_lock, check_stale_lock
from control_bus import start_event, stop_event, exit_event
import json
import sys
import os
import mss
import re
import threading
from pathlib import Path
from core.settings_manager import SettingsManager
from core.goal_policy import GoalPolicy, build_goal_policy
import cv2, numpy as np
from typing import Dict, Set, Tuple, Optional, Callable, Any
from core.home_probe import HomeProbe, get_home_anchor_template  # 홈 확인 루틴/헬퍼
from core.state_store import get_state_store  # OCR 중 UI잠금 플래그 :contentReference[oaicite:3]{index=3}
from core.utils_anchor import load_home_anchor_threshold
from core.image_utils import safe_crop
from core.roi_cache import load_roi as roi_load, commit_roi as roi_commit
from core.freeze_monitor import FreezeMonitor
from core.autolearn import AutoLearnStore
from core.roi_from_settings import (
    run_home_ocr_like_gui,                # GUI와 동일 OCR 실행
    read_rois_xywh_from_settings,         # ← ROI (x,y,w,h) 그대로 읽기
    scale_xywh_from_base                  # ← (x,y,w,h) 스케일 함수
)  # ← 신규 모듈
from core.scheduled_shutdown import ScheduledShutdown


width, height = pgi.size()  # 화면해상도 확인

# ★ 성능/주기 설정(없으면 안전 기본값)
# ---- Settings SSOT 주입/지연 초기화 ----
_SETTINGS_SM = None  # type: Optional[SettingsManager]

# --- status logger (GUI로부터 주입) ---
# 항상 callable로 유지해 IDE/타입체커 경고 제거
_STATUS_LOGGER: Callable[[str, Optional[str]], None] = lambda msg, fg=None: print(msg)


def _goal_enabled_now() -> bool:
    try:
        return bool(SETTINGS().get("goal.enabled", False))
    except Exception:
        return False


def set_status_logger(fn: Any) -> None:
    """
    주입받은 로거를 (text, fg=None) 시그니처로 통일하는 래퍼를 셋업.
    - fn이 logging.Logger 계열이면 .info(text)
    - fn이 callable(text, fg)면 그대로
    - fn이 callable(text)만 받으면 그 형태로 호출
    - 그 외는 기본 print로 유지
    """
    global _STATUS_LOGGER

    # 1) 로거 객체(.info) 지원
    if hasattr(fn, "info") and callable(getattr(fn, "info")):
        def _wrapped(msg: str, fg: Optional[str] = None) -> None:
            try:
                fn.info(msg)
            except Exception:
                pass
        _STATUS_LOGGER = _wrapped
        return

    # 2) 일반 callable
    if callable(fn):
        def _wrapped(msg: str, fg: Optional[str] = None) -> None:
            try:
                # (text, fg) 시도
                return fn(msg, fg)
            except TypeError:
                # (text)만 받을 때
                return fn(msg)
            except Exception:
                pass
        _STATUS_LOGGER = _wrapped
        return

    # 3) 잘못된 주입 → 기본값 유지(이미 print 래퍼)
    # 아무 것도 하지 않음


def _log_status(text: str, fg: Optional[str] = None) -> None:
    """
    main 내부 상태 로그 출력.
    - GUI에서 set_status_logger로 주입되면 (text, fg) 호출
    - (text)만 받는 구버전 브릿지도 허용
    - 미주입/호출 실패 시 콘솔 print로 폴백
    """
    try:
        cb = _STATUS_LOGGER  # 주입 안 됐으면 NameError는 안 나고 기본 print 래퍼일 수 있음
    except NameError:
        cb = None

    if callable(cb):
        try:
            cb(text, fg)      # 선호 시그니처
            return
        except TypeError:
            try:
                cb(text)      # (text)만 받는 구버전
                return
            except Exception:
                pass
        except Exception:
            pass

    # 최종 폴백
    try:
        print(text)
    except Exception:
        pass


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


# --- 점수 앵커 싱글턴 (v2 파이프라인 전용) ---
_SCORE_ANCHOR = None  # type: Optional[Any]


def SCORE_ANCHOR():
    """ScoreAnchor 싱글턴. settings 주입 후 lazily 생성."""
    global _SCORE_ANCHOR
    if _SCORE_ANCHOR is None:
        from core.score_anchor import ScoreAnchor
        _SCORE_ANCHOR = ScoreAnchor(SETTINGS())
    return _SCORE_ANCHOR


# 예약 종료 컨트롤러 준비
_SCHED_SD = ScheduledShutdown(
    settings_getter=lambda: SETTINGS(),
    stop_callback=lambda: stop_event.set() if 'stop_event' in globals() else None
)


def reload_scheduled_shutdown():
    """settings.json 저장 직후 즉시 재적용을 위해 외부(UI)에서 호출."""
    try:
        _SCHED_SD.reload_from_settings()
    except Exception:
        pass


def reload_spam_mode_from_settings(changed_keys: list[str] | None = None) -> None:
    """
    settings.json 저장 직후 스팸 모드 설정을 인메모리로 재적용.
    - enabled, enter_images, exit_images, press_key, interval_ms, conf
    - 잘못된 타입/값은 안전한 기본으로 정규화
    - 비활성화로 전환되면 활성 상태일 경우 즉시 종료
    """
    try:
        s = SETTINGS().get("spam_mode", {}) or {}
        enabled = bool(s.get("enabled", False))

        enter = s.get("enter_images") or []
        exit_  = s.get("exit_images") or []
        if not isinstance(enter, (list, tuple)): enter = []
        if not isinstance(exit_,  (list, tuple)): exit_  = []

        key = str(s.get("press_key", "s")).strip().lower() or "s"
        itv = int(s.get("interval_ms", 60))
        itv = max(10, min(itv, 500))  # 10~500ms로 가드
        conf = float(s.get("conf", 0.80))  # 없으면 0.80
        conf = max(0.50, min(conf, 0.99))

        globals()["_SPAM_CFG"] = {
            "enabled": enabled,
            "enter": set([str(x).strip() for x in enter if str(x).strip()]),
            "exit":  set([str(x).strip() for x in exit_  if str(x).strip()]),
            "key": key,
            "dt": itv,
            "conf": conf,
        }

        # OFF로 바뀌었다면 즉시 해제
        if not enabled and globals().get("_SPAM_ACTIVE", False):
            try:
                _spam_deactivate(reason="disabled via settings")
            except Exception:
                pass

        log_spam_state()
    except Exception:
        pass


def log_spam_state():
    s = SETTINGS().get("spam_mode", {}) or {}
    enabled = bool(s.get("enabled", False))

    style = "green bold"
    if enabled:
        _log_status("스팸 모드 ON", fg=style)
    else:
        style = "gray bold"
        _log_status("스팸 모드 OFF", fg=style)


def log_scheduled_shutdown_state(enabled: bool, weekday_ko: str = "", hhmm: str = "", auto_poweroff: bool = False, delay_min: int = 20):
    """
    사용자에게 보여줄 상태 로그. '목표달성 모드: ON/OFF'와 유사한 스타일.
    색상: 사이언(직관적으로 '예약/알림' 성격을 띔).
    """
    msg = "예약종료 모드: OFF"
    style = "gray bold"
    if enabled:
        tail = f"{weekday_ko} {hhmm}"
        tail += " · PC 자동 종료" if auto_poweroff else ""
        tail += f" · 지연 {delay_min}분"
        msg = f"예약종료 모드: {tail}"
        style = "cyan bold"
    # GUI 로그 위젯이 있으면 색상 출력, 없으면 print
    # 출력
    try:
        _log_status(msg, fg=style)  # 사이언
    except Exception:
        pass


def log_scheduled_shutdown_state_current():
    """
    settings.schedule 스냅샷을 읽어 현재 예약 종료 상태를 GUI 로그(시안)로 1줄 요약.
    - entries[0]만 요약에 사용(다중 항목 UI면 활성/선택 항목을 GUI에서 넘겨도 OK)
    - UI가 따로 값을 넘겨주지 못할 때 호출하기 위한 편의 함수
    """
    try:
        s = SETTINGS().get("schedule", {}) or {}
        enabled = bool(s.get("enabled", False))
        entries = s.get("entries", []) or []
        weekday_ko = ""
        hhmm = ""
        if enabled and entries:
            wd = entries[0].get("weekday")
            tm = str(entries[0].get("time", "")).strip()
            # 요일 → 한글
            _day = {
                0: "월", 1: "화", 2: "수", 3: "목", 4: "금", 5: "토", 6: "일",
                "mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일",
            }
            if isinstance(wd, int):
                weekday_ko = f"{_day.get(wd, wd)}요일"
            elif isinstance(wd, str):
                weekday_ko = f"{_day.get(wd.strip().lower(), wd)}요일"
            hhmm = tm

        auto_poweroff = bool(s.get("auto_poweroff", int(s.get("shutdown_delay_min", 0)) > 0))
        delay_min = int(s.get("shutdown_delay_min", 20))
        log_scheduled_shutdown_state(enabled, weekday_ko, hhmm, auto_poweroff, delay_min)
    except Exception:
        pass


# ↓ 모든 전역 상수 계산부를 함수화/지연화하거나, 초기화 시점에만 한 번 읽도록 변경
_LOOP_INTERVAL_SEC = float(SETTINGS().get("perf.loop_interval_sec", 0.45))
_IDLE_SLEEP_SEC    = float(SETTINGS().get("perf.idle_sleep_sec", 0.016))
_AWAIT_STEP_MIN_MS = int(SETTINGS().get("perf.await_step_min_ms", 80))

# --- GUI busy 워치독/스로틀 파라미터 ---
# busy_throttle_sec 의미: ui_busy=True 일 때 worker thread 가 매 iter 양보로
# sleep 하는 시간. GUI thread(폴 300ms 주기)에 처리 시간 양보용.
# 기존 0.35s는 GUI 폴 1회분 대비 과도 → settle 가드(250ms) 안에서 5~7회 iter
# 누적되어 정체 2000ms+. 0.1s 면 GUI 폴(300ms) 1회 안에 3회 양보 → 동기화 유지
# 하면서 정체 대폭 단축.
_BUSY_WATCHDOG_MS = int(SETTINGS().get("perf.busy_watchdog_ms", 2000))   # 2s
_BUSY_THROTTLE_SEC = float(SETTINGS().get("perf.busy_throttle_sec", 0.1))# 100ms

# --- busy 워치 상태(전역) ---
_BUSY_SINCE_MS: int = 0
_BUSY_DID_GC: bool = False

pm.chdir_to_base()  # ★ 시작 즉시

img_path = pm.get_img_path()
res_path = pm.get_res_path()

is_crashed = False

# OCR ROI 설정: settings.ocr.* 사용 (없으면 안전값)
roi_rank = SETTINGS().get("ocr.roi_rank")
# 구버전 키 호환: roi_score → roi_points
roi_points = SETTINGS().get("ocr.roi_points", SETTINGS().get("ocr.roi_score"))

freeze = None  # ← 없으면 반드시 추가 (FreezeMonitor 활성화 보장)

# --- OCR base screen size (settings에 저장된 기준 해상도) ---
try:
    _OCR_BASE_WH = None
    _bw = SETTINGS().get("ocr.screen.w")
    _bh = SETTINGS().get("ocr.screen.h")
    if _bw and _bh:
        _OCR_BASE_WH = (int(_bw), int(_bh))
except Exception:
    _OCR_BASE_WH = None

# === Tesseract/Leptonica/OpenMP 스레드 상한(초저사양 최적화) ===
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _sync_screen_profile() -> None:
    """
    현재 화면 해상도(width,height)와 settings.ocr.screen.w/h가 다르면:
      - settings에 현재 해상도 기록(원자 저장)
      - 메모리 캐시/홈 ROI 캐시 무효화
      - 다음 1회 풀프레임 스캔 강제(_SKIP_FILE_ROI_ONCE=True)
    """
    try:
        cur_wh = (width, height)
        cfg_w = SETTINGS().get("ocr.screen.w")
        cfg_h = SETTINGS().get("ocr.screen.h")
        cfg_wh = (int(cfg_w), int(cfg_h)) if (cfg_w and cfg_h) else None
    except Exception:
        cfg_wh = None

    if cfg_wh != cur_wh:
        # settings 갱신 + 원자 저장
        SETTINGS().set("ocr.screen.w", cur_wh[0])
        SETTINGS().set("ocr.screen.h", cur_wh[1])
        try:
            SETTINGS().save()  # 원자 저장(임시파일→replace)
        except Exception:
            pass

        # 캐시/상태 무효화
        globals()["_ROUTINE_ROI"] = {}
        globals()["_CACHED_ROI"] = None
        globals()["_LAST_BBOX"] = None
        globals()["_HOME_FF_TRIES"] = 0
        globals()["_SKIP_FILE_ROI_ONCE"] = True


_PLACEHOLDER_EMAIL = "example@gmail.com"
_PLACEHOLDER_PW = "password"


# [추가] 프로젝트 OCR 모듈에 테서랙트 경로 주입(있을 때만)
try:
    from core import ocr as _ocr_core
    _ocr_core.set_tesseract_path(SETTINGS().get("ocr.tesseract_path", ""))
except Exception:
    pass

cv2.setNumThreads(1)


def _coerce_roi(v):
    if isinstance(v, (list, tuple)) and len(v) == 4:
        try:
            return tuple(int(x) for x in v)
        except Exception:
            pass
    return (0, 0, 0, 0)


def _capture_frame_bgr_like_gui() -> np.ndarray:
    """
    GUI 테스트(_open_verify)와 동일한 캡처 파이프라인:
      mss.grab → shot.rgb를 (H,W,3)로 reshape → RGB→BGR → copy()
    """
    import mss, numpy as np, cv2
    with mss.mss() as sct:
        mon = sct.monitors[0]  # 가상 전체 화면
        shot = sct.grab(mon)
        rgb = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()
        return bgr


def _ocr_diag_log(line: str) -> None:
    """OCR 진단 라인을 BASE_DIR/ocr_diag.log 에 append.

    settings의 ocr.diag_log_enabled 가 True 일 때만 동작.
    임시 진단용 — 미니PC에서 이 파일 하나만 복사해 분석할 수 있도록 한다.
    분석 끝나면 settings.json 에서 ocr.diag_log_enabled = false 로 끄기.
    """
    try:
        if not bool(SETTINGS().get("ocr.diag_log_enabled", False)):
            return
        from path_manager import BASE_DIR
        import os
        path = os.path.join(BASE_DIR, "ocr_diag.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ms = int(time.time() * 1000) % 1000
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts}.{ms:03d} | {line}\n")
    except Exception:
        pass


def _downscale_like_gui(bgr: np.ndarray) -> tuple[np.ndarray, float]:
    """
    GUI 검증창과 동일 규칙으로 프레임을 다운스케일.
    반환: (다운스케일된 BGR, scale_factor=preview_w/full_w)
    """
    ih, iw = bgr.shape[:2]
    # GUI: max_w = min(screen_w - 120, 1200)
    max_w = min(width - 120, 1200)
    if iw <= max_w:
        return bgr, 1.0
    s = max_w / float(iw)
    out = cv2.resize(bgr, (int(iw * s), int(ih * s)), interpolation=cv2.INTER_AREA)
    return out, s


# AWAIT_HOME용 상태 변수
probe = None
cached_home_roi = None     # 필요 시 hit bbox 캐시해 ROI 탐색 최적화
t_trigger_ms = None

# 전역변수 모음
# 영구 ROI 캐시 전역 상태 (Home 템플릿용)
_HOME_TPL_PATH: Optional[str] = None
_HOME_SCREEN_WH: Optional[Tuple[int, int]] = None
_CACHED_ROI: Optional[Tuple[int, int, int, int]] = None
_LAST_BBOX: Optional[Tuple[int, int, int, int]] = None  # 쓰고 있으면 유지, 아니면 생략
_SKIP_FILE_ROI_ONCE: bool = False  # 홈 miss 후 '다음 1회'는 풀프레임 스캔 강제
# 루틴 이미지별 ROI 캐시(메모리)
# 루틴 이미지별 ROI 캐시(메모리, 다중 후보 지원: 최신 우선)
_ROUTINE_ROI: dict[str, list[tuple[int,int,int,int]]] = {}
_MAX_ROI_CANDIDATES = int(SETTINGS().get("routine.roi_max_candidates", 3))
# 루틴 ROI 여유 마진(px) — settings 없으면 기본 12px
_ROUTINE_ROI_MARGIN: int = int(SETTINGS().get("routine.roi_margin", 12))
# [ADD] AWAIT_HOME 세션 내 풀프레임 시도 누적(최대 2)
_HOME_FF_TRIES: int = 0
# [FIX] AWAIT_HOME 트리거 시점의 루틴 conf 저장 (probe 임계값 동기화용)
_AWAIT_HOME_CONF: float = 0.80
# --- 로그 레이트 리밋(공용) ---
_LOG_LAST_TS = {}
# --- AutoLearn 전역 ---
_AUTOLEARN = AutoLearnStore()
_CONFIRM_WINDOW_MS = 350  # 초기값(자동 튜닝으로 갱신)
# --- AWAIT_HOME settle 하드가드 ---
_AWAIT_ENTER_MS: Optional[int] = None   # AWAIT_HOME 진입 시각(ms)
_AWAIT_SETTLE_MS: int = 1200            # 현재 사이클 settle 목표(ms)
_AWAIT_SETTLE_DONE_MS: Optional[int] = None  # 진단: settle 가드 통과 첫 시점(ms)
# GUI hidden 후 OS 컴포지터 turn 보장용 추가 grace (ms)
_GUI_HIDDEN_GRACE_MS: int = 50
# ui_busy stuck 등으로 GUI hidden 신호 못 받을 때 fallback timeout (ms)
_AWAIT_HARD_TIMEOUT_MS: int = 1500
# [추가] 파일 상단 전역(다른 전역들과 같은 레벨)
_RUNTIME_LOG_CB = None
# --- [ADD] 홈-OCR 제어 플래그 ---
_WANT_AWAIT_HOME: bool = False   # 루틴 단락이 실제로 발생했을 때만 AWAIT_HOME 시작
_HOME_OCR_DONE: bool = False     # '현재 홈 체류'에서 OCR을 1회 수행했는가
# --- UNCERTAIN streak (시즌말 stop 누락 대비 알림용) ---
_UNCERTAIN_STREAK: int = 0
_LAST_UNCERTAIN_ALERT_MS: int = 0
# 점수 anchor 누적 outlier 카운터: 임계 = base × 카운터.
# 1판당 base. outlier 발생 시 +1 → 다음 사이클은 2판 누적 가정 → 임계 2배.
# 정상 통과 시 1 로 reset (anchor 갱신과 함께).
# 상한(예: 10) 도달 시 anchor reset → 부트스트랩 재시작.
_ANCHOR_OUTLIER_COUNTER: int = 1
_ANCHOR_OUTLIER_COUNTER_MAX: int = 10
# --- 임시 진단: 사이클 단계별 timing ---
_ROUTINE_START_MS: int = 0      # "반복 시작" 시점
_HOME_HIT_MS: int = 0           # HomeProbe res == "hit" 시점
_OCR_START_MS: int = 0          # m0 OCR 호출 직전 시점

# --- [SPAM MODE] 전역 ---
_SPAM_CFG = {
    "enabled": bool(SETTINGS().get("spam_mode.enabled", False)),
    "enter": set(SETTINGS().get("spam_mode.enter_images", []) or []),
    "exit":  set(SETTINGS().get("spam_mode.exit_images", []) or []),
    "key":   str(SETTINGS().get("spam_mode.press_key", "s")),
    "dt":    int(SETTINGS().get("spam_mode.interval_ms", 60)),
    "conf":  float(SETTINGS().get("spam_mode.conf", 0.80)),
}
_SPAM_ACTIVE: bool = False
_SPAM_LAST_TS: float = 0.0
_SPAM_EXIT_CHECK_COOLDOWN_MS: int = 140   # 종료이미지 폴링 최소 간격(저부하)
_SPAM_LAST_EXIT_POLL_TS: int = 0


# [ADD] 공통 초기화: 토글 ON/OFF 전용
def reset_runtime_state() -> None:
    global probe, cached_home_roi, t_trigger_ms
    global _HOME_TPL_PATH, _HOME_SCREEN_WH, _CACHED_ROI, _LAST_BBOX, _SKIP_FILE_ROI_ONCE
    global _ROUTINE_ROI, _LOG_LAST_TS, _AUTOLEARN
    global _WANT_AWAIT_HOME, _HOME_OCR_DONE
    global _HOME_FF_TRIES, _AWAIT_HOME_CONF, _AWAIT_ENTER_MS

    probe = None
    cached_home_roi = None
    t_trigger_ms = None

    _CACHED_ROI = None
    _LAST_BBOX = None
    _SKIP_FILE_ROI_ONCE = False
    _HOME_FF_TRIES = 0
    _AWAIT_HOME_CONF = 0.80
    _AWAIT_ENTER_MS = None

    _WANT_AWAIT_HOME = False
    _HOME_OCR_DONE = False

    # anchor 누적 outlier 카운터 — routine 시작 시 항상 1 로 reset
    globals()["_ANCHOR_OUTLIER_COUNTER"] = 1

    # 루틴 ROI 캐시(메모리)도 세션 시작 시 초기화
    _ROUTINE_ROI.clear()

    # 레이트리밋/로그 히스토리 초기화
    _LOG_LAST_TS.clear()

    # busy 워치 상태 리셋
    globals()["_BUSY_SINCE_MS"] = 0
    globals()["_BUSY_DID_GC"] = False


# --- [ADD] 로컬 헬퍼: 이 루틴 이미지가 '등록된 홈 앵커 템플릿'인가? ---
def _is_home_anchor(img_file: str) -> bool:
    """
    - 문자열 이름이 아니라 '등록된 템플릿 경로'와 동일한지로만 판정
    - 경로가 같으면 True, 아니면 False
    """
    try:
        if not _HOME_TPL_PATH:
            return False
        import os
        a = os.path.abspath(img_file)
        b = os.path.abspath(_HOME_TPL_PATH)
        # samefile은 Windows에서도 가끔 예외가 나므로 절대경로 비교 우선
        if a == b:
            return True
        try:
            return os.path.samefile(a, b)
        except Exception:
            return False
    except Exception:
        return False


def _basename(p: str) -> str:
    try:
        import os
        return os.path.basename(p).strip()
    except Exception:
        return p


def _is_spam_enter_image(img_file: str) -> bool:
    if not _SPAM_CFG["enabled"]:
        return False
    name = _basename(img_file)
    return name in _SPAM_CFG["enter"]


def _is_spam_exit_hit(frame_bgr: "np.ndarray"):
    """
    반환값: (hit: bool, center: tuple|None, img_file: str|None)
    """
    if not _SPAM_CFG["enabled"]:
        return False, None, None
    conf = float(_SPAM_CFG["conf"])
    for img_name in _SPAM_CFG["exit"]:
        img_file = os.path.join(img_path, img_name)
        if not os.path.exists(img_file):
            continue
        hit, center = _routine_locate_adaptive(img_file, conf)
        if hit:
            return True, center, img_file
    return False, None, None


def _spam_activate() -> None:
    global _SPAM_ACTIVE, _SPAM_LAST_TS, _SPAM_LAST_EXIT_POLL_TS
    _SPAM_ACTIVE = True
    _SPAM_LAST_TS = 0.0
    _SPAM_LAST_EXIT_POLL_TS = 0
    try:
        _log("[SPAM] 모드 진입: key=%s, dt=%sms" % (_SPAM_CFG["key"], _SPAM_CFG["dt"]))
    except Exception:
        pass

    # [ADD] 스팸모드에서도 프리즈 모니터 활성화 보장
    try:
        global freeze
        if freeze is None or getattr(freeze, "is_disabled", False):
            freeze = FreezeMonitor.from_settings_dict(SETTINGS().get("freeze"), on_trip=_on_freeze_trip)
        freeze.activate()
    except Exception:
        pass


def _spam_deactivate() -> None:
    global _SPAM_ACTIVE
    _SPAM_ACTIVE = False
    try:
        _log("[SPAM] 모드 종료 → 루틴 복귀")
    except Exception:
        pass


def _spam_tick() -> None:
    """
    - press_key를 interval 간격으로 1타 입력
    - 저부하 간격으로 종료 이미지 탐지
    """
    import time
    now_ms = int(time.time() * 1000)
    global _SPAM_LAST_TS, _SPAM_LAST_EXIT_POLL_TS

    # 1) 키 입력(연타)
    if (_SPAM_LAST_TS == 0.0) or ((now_ms - int(_SPAM_LAST_TS)) >= int(_SPAM_CFG["dt"])):
        _do_key(_SPAM_CFG["key"], label="spam", src_img=None)
        _SPAM_LAST_TS = now_ms

    # 2) 종료 이미지 폴링(저부하)
    if (now_ms - _SPAM_LAST_EXIT_POLL_TS) >= _SPAM_EXIT_CHECK_COOLDOWN_MS:
        frame_bgr = _capture_frame_bgr_like_gui()
        hit, center, img_file = _is_spam_exit_hit(frame_bgr)
        if hit:
            # 전역 routine_items에서 동일 이미지의 액션을 찾아 1회 실행
            try:
                global routine_items  # 이미 전역으로 선언돼 있음
                base = os.path.basename(img_file)
                act = ""
                for it in (routine_items or []):
                    if os.path.basename(it.get("image", "")) == base:
                        act = (it.get("action") or "").strip().lower()
                        break
                if act == "click" and center is not None:
                    _do_click(center, label="exit_action", src_img=img_file)
                elif act:
                    _do_key(act, label="exit_action", src_img=img_file)
            except Exception as e:
                pass
            _spam_deactivate()
        _SPAM_LAST_EXIT_POLL_TS = now_ms


def _roi_candidates(key: str) -> list[tuple[int,int,int,int]]:
    return _ROUTINE_ROI.get(key, [])


def _iou_xywh(a, b) -> float:
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0: return 0.0
    area_a = aw * ah
    area_b = bw * bh
    return inter / float(area_a + area_b - inter + 1e-6)


def _roi_key_for_image(image_filename: str) -> str:
    # 디스크 캐시 파일 키(이미지 절대경로 기반이 가장 충돌 적음)
    return os.path.join(img_path, image_filename)


def _routine_roi_load(image_filename: str) -> list[tuple[int,int,int,int]]:
    # 해상도 프로파일 불일치 시 캐시 사용 금지
    try:
        cfg_w = SETTINGS().get("ocr.screen.w")
        cfg_h = SETTINGS().get("ocr.screen.h")
        if not (cfg_w and cfg_h) or int(cfg_w) != width or int(cfg_h) != height:
            return []
    except Exception:
        return []

    key = _roi_key_for_image(image_filename)
    lst = _ROUTINE_ROI.get(key)
    if isinstance(lst, list) and lst:
        return lst
    try:
        wh = (width, height)
        roi = roi_load(key, wh, auto_scale=True)
        if roi and len(roi) == 4:
            r = tuple(int(v) for v in roi)
            _ROUTINE_ROI[key] = [r]  # 최초 후보 1개
            return [r]
    except Exception:
        pass
    return []


def _routine_roi_commit_from_bbox(image_filename: str, bbox: tuple[int,int,int,int]) -> None:
    x,y,w,h = bbox
    m = max(0, int(_ROUTINE_ROI_MARGIN))
    rx = max(0, x - m); ry = max(0, y - m)
    rw = min(width - rx,  w + 2*m); rh = min(height - ry, h + 2*m)
    roi = (int(rx), int(ry), int(rw), int(rh))

    key = _roi_key_for_image(image_filename)
    lst = _ROUTINE_ROI.get(key, [])

    # 중복(겹침) 판단: IoU 0.5 이상이면 같은 후보로 간주 → 맨 앞으로 승격
    replaced = False
    for i, r in enumerate(list(lst)):
        if _iou_xywh(r, roi) >= 0.5:
            lst.pop(i)
            lst.insert(0, roi)
            replaced = True
            break
    if not replaced:
        lst.insert(0, roi)
        if len(lst) > _MAX_ROI_CANDIDATES:
            lst[:] = lst[:_MAX_ROI_CANDIDATES]

    _ROUTINE_ROI[key] = lst
    # 디스크에는 '주 후보(맨 앞)' 1개만 저장(파일 포맷 호환)
    try:
        roi_commit(key, lst[0], (width, height))
    except Exception:
        pass


# --- GUI 안정화 대기: OCR 전 GUI withdraw가 화면에서 완전히 사라질 시간을 보장 ---
def _wait_gui_hidden_for_ocr(max_wait_ms: int = 250) -> None:
    """
    GUI가 ocr_sampling_active=True를 감지하여 withdraw()될 시간을 짧게 확보한다.
    - 폴러 주기(≈200ms)를 감안해 기본 250ms 대기(최소치 보장).
    - 향후 state_store에 'gui_hidden_ack' 같은 플래그가 생기면 여기서 폴링하도록 확장 가능.
    """
    import time
    # 아주 짧은 최소 슬립으로 메인스레드 스케줄링/컴포지터 턴 확보
    time.sleep(0.06)
    # 남은 시간은 작은 청크로 쪼개어 대기(응답성 유지)
    remain = max(0, int(max_wait_ms) - 60)
    step = 40  # 40ms 단위
    loops = remain // step
    for _ in range(max(1, loops)):
        time.sleep(step / 1000.0)


def _busy_throttle_gate(store) -> bool:
    """
    UI busy일 때:
      - 폴링 감속(_BUSY_THROTTLE_SEC 만큼 sleep)
      - 연속 busy >= _BUSY_WATCHDOG_MS면 1회 gc.collect()
      - True 반환 시 '이번 턴은 스킵' 의미
    """
    global _BUSY_SINCE_MS, _BUSY_DID_GC
    try:
        if store.is_ui_busy():
            now_ms = int(time.time() * 1000)
            if _BUSY_SINCE_MS == 0:
                _BUSY_SINCE_MS = now_ms
            elif (now_ms - _BUSY_SINCE_MS) >= _BUSY_WATCHDOG_MS and not _BUSY_DID_GC:
                import gc
                gc.collect()
                _BUSY_DID_GC = True
            time.sleep(_BUSY_THROTTLE_SEC)
            return True
        else:
            # busy 해제 → 워치 상태 리셋
            _BUSY_SINCE_MS = 0
            _BUSY_DID_GC = False
            return False
    except Exception:
        # store 조회 실패 등은 busy 미적용으로 간주
        return False


def _log(msg: str) -> None:
    cb = globals().get("_RUNTIME_LOG_CB")
    try:
        (cb or print)(msg)
    except Exception:
        print(msg)


# --- Freeze trip global callback (for spam mode etc.) ---
def _on_freeze_trip(count: int) -> None:
    """
    FreezeMonitor.on_trip 콜백 (모듈 전역).
    스팸모드 등 어디서든 호출 가능해야 하므로 전역에 둔다.
    """
    try:
        frz = globals().get("freeze", None)
        interval = getattr(getattr(frz, "cfg", None), "interval_sec", 60)
        minutes = (count * int(interval)) // 60
    except Exception:
        minutes = 0

    _log(f"[FREEZE] 화면 정지 {count}회 샘플(≈{minutes}분) 감지 → 자동 이메일 발송")
    try:
        autoemail.send_email(event="freeze_detected",
                             payload={"samples": int(count),
                                      "approx_minutes": int(minutes),
                                      "ts": int(time.time())})
    except Exception as e:
        _log(f"[FREEZE][email][err] {e}")


def _ensure_roi_min_for_image(image_path: str, roi: tuple[int,int,int,int]) -> tuple[int,int,int,int]:
    """
    ROI(좌상단 x,y, 폭, 높이)가 템플릿(needle)보다 작으면,
    템플릿 크기 이상으로 확장한 ROI를 반환한다. 화면 경계 안으로 클램프.
    """
    try:
        with Image.open(image_path) as im:
            tw, th = im.size  # 템플릿 크기
    except Exception:
        return roi

    x, y, w, h = roi
    # 최소 폭/높이: 템플릿보다 2px 여유
    req_w = max(w, tw + 2)
    req_h = max(h, th + 2)

    # 기존 ROI 중심을 유지하며 확장
    cx, cy = x + w // 2, y + h // 2
    nx = max(0, min(width - req_w, cx - req_w // 2))
    ny = max(0, min(height - req_h, cy - req_h // 2))
    return (int(nx), int(ny), int(req_w), int(req_h))


def _locate_in_roi(image_path: str, roi: tuple[int,int,int,int], conf: float):
    """
    한 번 캡처한 haystack(ROI)에서 템플릿을 찾는다. 매 호출마다 풀스크린 캡처하지 않음.
    """
    pyscreeze.USE_IMAGE_NOT_FOUND_EXCEPTION = False
    safe_roi = _ensure_roi_min_for_image(image_path, roi)
    try:
        hay = _screenshot_pil(safe_roi)  # ROI만 캡처
        # 템플릿 로드
        with Image.open(image_path) as needle:
            box = pyscreeze.locate(needle, hay, confidence=conf)  # OpenCV 있으면 사용
        if not box:
            return None
        # ROI 좌표계 → 스크린 좌표계 중심으로 변환
        x, y, w, h = box
        cx = safe_roi[0] + x + w // 2
        cy = safe_roi[1] + y + h // 2
        return pyscreeze.Point(cx, cy)  # ✅ namedtuple(Point)로 반환
    except Exception:
        return None


def _routine_locate_adaptive(img_file: str, conf: float) -> tuple[bool, Optional[Tuple[int,int]]]:
    """
    ROI 캐시 우선 → 미스 누적 시 주기적으로 풀프레임 한 번 시도 → 성공 시 ROI 자동 재학습.
    반환: (hit, center)
    """
    img_name = os.path.basename(img_file)
    miss_th, cooldown = _get_roi_params()   # ex) (3, 5)

    # ROI 우선
    rois = _routine_roi_load(img_name)
    if rois:
        for r in list(rois):  # 최신 후보부터 순차 탐색
            center = _locate_in_roi(img_file, r, conf)
            if center:
                _ROI_MISS_COUNT[img_name] = 0
                _routine_roi_commit_from_center(img_name, center)  # 맞춘 후보를 맨 앞으로
                return True, center
        # 모든 후보 miss → 누적/쿨다운 로직 그대로
        cnt = _ROI_MISS_COUNT.get(img_name, 0) + 1
        _ROI_MISS_COUNT[img_name] = cnt
        cd = _ROI_FALLBACK_COOLDOWN.get(img_name, 0) + 1
        _ROI_FALLBACK_COOLDOWN[img_name] = cd
        if cnt >= miss_th or cd >= cooldown:
            _ROI_FALLBACK_COOLDOWN[img_name] = 0
            center = import_img(img_file, conf)  # 풀프레임 1회
            if center:
                _routine_roi_commit_from_center(img_name, center)  # 새 후보 추가(승격)
                _ROI_MISS_COUNT[img_name] = 0
                # _log(f"[roi] fallback recaptured '{img_name}' at {center}")
                return True, center
        return False, None

    # ROI가 없으면 1회 풀프레임 허용
    center = import_img(img_file, conf)
    if center:
        _routine_roi_commit_from_center(img_name, center)
        _ROI_MISS_COUNT[img_name] = 0
        return True, center
    return False, None


def _routine_locate_strict(img_file: str, conf: float):
    """
    루틴 전용 엄격 탐색기:
      - ROI 캐시가 있으면 ROI 한정 탐색만 수행(미스여도 풀프레임 금지).
      - ROI 캐시가 없으면 '초회 1회'만 풀프레임 허용 → center 기준 ROI 커밋.
    반환: (hit: bool, center_xy or None)
    """
    img_basename = os.path.basename(img_file)

    # 1) ROI가 있으면 ROI 내에서만 탐색
    roi = _routine_roi_load(img_basename)
    if roi:
        center = _locate_in_roi(img_file, roi, conf)
        if center:
            # 미세 드리프트 보정: center로 ROI 재커밋
            _routine_roi_commit_from_center(img_basename, center)
            return True, center
        # 미스여도 풀프레임 금지
        return False, None

    # 2) ROI가 없으면 초회 1회만 풀프레임 허용
    center = import_img(img_file, conf)
    if center:
        _routine_roi_commit_from_center(img_basename, center)
        return True, center
    return False, None


# --- [NEW] ROI 공통 상태/헬퍼 (모든 액션 공용) ---

# 모듈 전역 상태
_ROI_MISS_COUNT: Dict[str, int] = {}
_ROI_FALLBACK_ONCE: Set[str] = set()
_ROI_FALLBACK_COOLDOWN: Dict[str, int] = {}

# 기본값(설정 없을 때)
_DEFAULT_MISS_THRESHOLD = 3
_DEFAULT_COOLDOWN_LOOPS = 5


def _get_roi_params():
    """settings.routine.roi_fallback.{miss_threshold,cooldown_loops} → (int,int)"""
    # 전역 SettingsManager 사용(없으면 안전 기본값)
    cfg = SETTINGS().get("routine.roi_fallback", {}) or {}
    miss_th = int(cfg.get("miss_threshold", _DEFAULT_MISS_THRESHOLD))
    cooldown = int(cfg.get("cooldown_loops", _DEFAULT_COOLDOWN_LOOPS))

    return (miss_th, cooldown)


def _get_roi_margin():
    """settings.routine.roi_margin → int (기본 80)"""
    return int(SETTINGS().get("routine.roi_margin", _ROUTINE_ROI_MARGIN))


def _roi_key(img_basename: str) -> str:
    """해상도/모니터 환경별 캐시 분리"""
    try:
        w, h = pgi.size()
        return f"{img_basename}@{w}x{h}"
    except Exception:
        return img_basename


def _routine_roi_commit_from_center(img_basename: str, center_xy: Tuple[int,int]) -> None:
    """center 기준 사각 ROI 합성 후 기존 커밋 함수로 위임"""
    cx, cy = center_xy
    _routine_roi_commit_from_bbox(img_basename, (cx, cy, 1, 1))


# --- [수정] 저수준 입력 헬퍼 ---
def _do_click(center_xy: Optional[Tuple[int,int]], *, label: str = "", src_img: str | None = None):
    if not center_xy:
        return
    x, y = center_xy
    try:
        pgi.moveTo(x, y, duration=0)
        pgi.click()
        _log(f"[action] click")
    except Exception as e:
        _log(f"[action][err] click failed: {e}")


def _do_key(key: str, *, label: str = "", src_img: str | None = None):
    try:
        k = key.lower()
        pyd.keyDown(k); time.sleep(0.05); pyd.keyUp(k)
        # 스팸모드에서는 키 로그 억제
        if label != "spam":
            _log(f"[action] key '{key}'")
    except Exception as e:
        _log(f"[action][err] key '{key}' failed: {e}")


def _home_ff_limiter_reset():
    """AWAIT_HOME 진입 시 풀프레임 시도 카운터 리셋"""
    global _HOME_FF_TRIES
    _HOME_FF_TRIES = 0


def _get_taskbar_roi() -> tuple[int, int, int, int]:
    # Settings에 있으면 사용, 없으면 120px
    try:
        band = int(SETTINGS().get("client.taskbar_band_px", 120))
    except Exception:
        band = 120
    band = max(40, min(band, height))  # 안전 범위
    return (0, height - band, width, band)  # (left, top, w, h)


def _read_text_first_line(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception:
        return ""


def _read_mail_content(p: Path) -> tuple[str, str]:
    # autoemail.load_email_data()와 동일 규칙 사용
    try:
        return autoemail.load_email_data(str(p))
    except Exception:
        return ("", "")


def _infer_smtp_by_domain(domain: str) -> dict:
    d = domain.lower()
    if d == "gmail.com":
        return {"provider": "gmail", "smtp_host": "", "smtp_port": 587, "use_tls": True}
    if d == "naver.com":
        return {"provider": "custom", "smtp_host": "smtp.naver.com", "smtp_port": 587, "use_tls": True}
    return {}


def _is_email_unset(addr: str) -> bool:
    return (not addr) or (addr.strip().lower() == _PLACEHOLDER_EMAIL)


def _is_pw_unset(pw: str) -> bool:
    return (not pw) or (pw.strip() == _PLACEHOLDER_PW)


def init_resources():
    pm.init_folder()


def clean_exit():
    if os.path.exists("routine.lock"):
        remove_lock()
    sys.exit()


def import_img(file, conf=0.8):
    pyscreeze.USE_IMAGE_NOT_FOUND_EXCEPTION = False
    try:
        return pgi.locateCenterOnScreen(file, confidence=conf)
    except pgi.ImageNotFoundException:
        return None


def imgclick(files, conf=0.8):
    imgfile = import_img(files, conf)
    if imgfile is None:
        return
    x, y = imgfile
    x = x - random.randint(1, 20) + random.randint(1, 20)
    y = y - random.randint(1, 20)
    pgi.click(x, y)


def spacepress(file):
    imgfile = import_img(file)
    if imgfile is None:
        return
    print("space 눌림")
    pyd.keyDown("space")
    time.sleep(0.05)
    pyd.keyUp("space")


def skeypress(file):
    imgfile = import_img(file)
    if imgfile is None:
        return
    print("s 눌림")
    pyd.keyDown("s")
    time.sleep(0.05)
    pyd.keyUp("s")


def esckeypress(files):
    imgfile = import_img(files)
    if imgfile is None:
        return
    print("esc 눌림")
    pyd.keyDown("esc")
    time.sleep(0.05)
    pyd.keyUp("esc")


# --- 이메일 발송 가드 ---
def _email_guarded(event_key: str, payload: dict | None = None) -> None:
    """
    디스크 재로딩 금지. 항상 런타임 스냅샷만 사용.
    """
    payload = payload or {}
    try:
        from core.email_queue import EmailQueue  # 타입 힌트 목적

        snap = autoemail.get_snapshot()  # { ..., "version": N, "taken_at": ts }
        if snap.get("enabled") is None:
            # Legacy 모드: autoemail.send_email로 그냥 보냄
            ok = autoemail.send_email(event=event_key, payload=payload)
            _log(f"[email][legacy] event={event_key} ok={ok}")
            return

        # GUI 모드: email_queue 사용 중이면 큐로 던진다.
        # (이 함수가 emailq 인스턴스에 접근 가능한 컨텍스트라면 다음과 같이 사용)
        try:
            emailq = globals().get("EMAIL_QUEUE")  # 전역으로 주입돼 있다면 사용
        except Exception:
            emailq = None

        if emailq is not None:
            emailq.enqueue(event_key, payload)
            _log(f"[email][enqueue] event={event_key} cfg_v={snap.get('version')} at={int(snap.get('taken_at') or 0)}")
        else:
            # 큐가 없으면 즉시 발송
            subj, body = _render_from_snap_for_main(snap, event_key, payload)
            ok = _send_mail_from_main(subj, body, snap)
            _log(f"[email][direct] event={event_key} ok={ok} cfg_v={snap.get('version')} at={int(snap.get('taken_at') or 0)}")

    except Exception as e:
        _log(f"[email][error] event={event_key} err={e}")


def _render_from_snap_for_main(c: dict, event: str, p: dict) -> tuple[str, str]:
    ev_map = (c.get("templates") or {})
    ev = (ev_map.get(event) or {})
    st = (ev.get("subject") or c.get("subject_tmpl") or "[Manager] {event}")
    bt = (ev.get("body") or c.get("body_tmpl") or "{event}")
    return st.format(event=event, **p), bt.format(event=event, **p)


def _send_mail_from_main(subject: str, body: str, c: dict) -> bool:
    import smtplib
    from email.mime.text import MIMEText

    if not c.get("enabled"): return False
    sender=c.get("sender",""); pwd=c.get("app_password","")
    rcpts=[r.strip() for r in (c.get("recipients","").replace("\n",",").split(",")) if r.strip()]
    if not sender or not pwd or not rcpts: return False
    host=c.get("smtp_host") or ("smtp.gmail.com" if c.get("provider")=="gmail" else "")
    port=int(c.get("smtp_port",587)); use_tls=bool(c.get("use_tls",True))
    msg=MIMEText(body,_charset="utf-8"); msg["Subject"]=subject; msg["From"]=sender; msg["To"]=", ".join(rcpts)
    if use_tls:
        with smtplib.SMTP(host=host, port=port, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo(); s.login(sender,pwd); s.sendmail(sender, rcpts, msg.as_string())
    else:
        with smtplib.SMTP(host=host, port=port, timeout=15) as s:
            s.login(sender,pwd); s.sendmail(sender, rcpts, msg.as_string())
    return True


def client_crashed(img):
    # 작업표시줄 전체 ROI만 탐색 (특례)
    region = _get_taskbar_roi()
    try:
        pyscreeze.USE_IMAGE_NOT_FOUND_EXCEPTION = False
        loc = pgi.locateCenterOnScreen(img, confidence=0.9, region=region)
    except pgi.ImageNotFoundException:
        loc = None

    if loc is None:
        # 아이콘이 작업표시줄에서 사라짐 → 이메일 시도하되 실패해도 크래시 방지
        try:
            _email_guarded("client_crashed")
        except Exception as e:
            print(f"[warn] email_guarded 예외: {e}")
        return True

    return False


def keep_awake():
    pyd.keyDown("s")
    time.sleep(0.05)
    pyd.keyUp("s")


def _log_home_ff(status: str, tries: int, limit: int = 2, reason: str = "") -> None:
    """
    HOME 풀프레임 시도/차단/리셋 로깅.
    status: "allowed" | "blocked" | "reset"
    tries:  현재까지 사용한 시도 횟수(1부터 기재)
    limit:  허용 상한(기본 2)
    reason: 부가 사유(예: "miss", "manual", "limit")
    """
    # NOTE: 프로젝트 표준 로그 콜백이 있으면 그걸로 대체
    msg = f"HOME_FF {status} | try={tries}/{limit}" if status != "reset" else "HOME_FF reset"
    if reason:
        msg += f" | reason={reason}"
    print(msg)


def show_popup_removed_images(removed_images):
    if removed_images:
        root = tk.Tk()
        root.withdraw()
        removed_list = "\n- " + "\n- ".join(removed_images)
        messagebox.showinfo(
            "루틴 항목 일부 제거됨",
            f"다음 이미지가 존재하지 않아 루틴에서 제외되었습니다:{removed_list}"
        )
        root.destroy()


def load_routine_from_json(path="./routine.json"):
    if not os.path.exists(path):
        return [], None

    with open(path, "r", encoding="utf-8") as f:
        raw_routine = json.load(f)

    cleaned_routine = []
    client_item = None
    removed_images = []

    for item in raw_routine:
        image_path = img_path + item["image"]
        if os.path.exists(image_path):
            if item["action"] == "Client":
                client_item = item
            else:
                cleaned_routine.append(item)
        else:
            removed_images.append(item["image"])

    if removed_images:
        show_popup_removed_images(removed_images)
        root = tk.Tk()
        root.withdraw()
        answer = messagebox.askyesno(
            "루틴 동기화",
            "존재하지 않는 이미지 항목이 제거되었습니다.\nroutine.json 파일을 동기화할까요?"
        )
        root.destroy()
        if answer:
            all_items = cleaned_routine + ([client_item] if client_item else [])
            with open(path, "w", encoding="utf-8") as f:
                json.dump(all_items, f, indent=2)
            print("[info] routine.json 파일이 자동 동기화되었습니다.")

    cleaned_routine = sorted(cleaned_routine, key=lambda x: x.get("order", 0))
    return cleaned_routine, client_item


def execute_routine(routine_list):
    global _HOME_OCR_DONE, _WANT_AWAIT_HOME, _AWAIT_HOME_CONF

    # [목표달성 모드 단축경로]
    # 게임이 이미 home 화면이면 routine_items 전체 매칭(50~200ms × N개) 비용을 피하고
    # home anchor 만 우선 시도. hit 이면 즉시 AWAIT_HOME 트리거.
    # miss 이면 게임 진행 중이므로 일반 routine 매칭으로 진행.
    try:
        goal_enabled = bool(SETTINGS().get("goal.enabled", False))
    except Exception:
        goal_enabled = False
    if goal_enabled and GOAL_AVAILABLE and not _HOME_OCR_DONE:
        for item in routine_list:
            img_file = img_path + item["image"]
            if _is_home_anchor(img_file):
                conf = item.get("conf", 0.8)
                hit, _ = _routine_locate_adaptive(img_file, conf)
                if hit:
                    _WANT_AWAIT_HOME = True
                    _AWAIT_HOME_CONF = float(conf)
                    return  # 일반 routine 매칭 skip
                break  # home anchor miss → 일반 routine 매칭 진행 (게임 중)

    for item in routine_list:
        img_file = img_path + item["image"]
        action = item["action"]
        conf = item.get("conf", 0.8)
        # execute_routine
        if action in ("click", "space", "s", "esc"):
            hit, center = _routine_locate_adaptive(img_file, conf)
            if hit:
                # [SPAM] 엔터 트리거: 이 이미지가 spam enter로 등록되었으면 루틴을 끊고 스팸 모드로
                # (ready/준비완료 등에 매핑)
                if _is_spam_enter_image(img_file):
                    act = (item.get("action") or "").strip().lower()
                    if act == "click" and center is not None:
                        _do_click(center, label="enter_action", src_img=img_file)
                    elif act:
                        _do_key(act, label="enter_action", src_img=img_file)
                    _spam_activate()
                    return  # ← 루틴 실행 조기 종료 → 상위 루프에서 spam_tick만 수행

                # --- [ADD] 홈 앵커 단락: 홈 템플릿이면 입력(클릭/키) 스킵 → 화면 전환 방지
                # 바깥 루프가 직후 AWAIT_HOME(홈 OCR)을 기존 로직대로 시작한다.
                # 이번 '홈 체류'에서 아직 OCR을 하지 않았을 때만 단락
                if _is_home_anchor(img_file):
                    if not _HOME_OCR_DONE:
                        _WANT_AWAIT_HOME = True
                        _AWAIT_HOME_CONF = float(conf)  # [FIX] probe 임계값 동기화
                        # 목표 ON에서만 조기 리턴 허용
                        try:
                            from core.goal_policy import GoalPolicy  # 이미 상단 import되어 있으면 생략
                            goal_enabled = bool(SETTINGS().get("goal.enabled", False))
                        except Exception:
                            goal_enabled = False

                        if goal_enabled and GOAL_AVAILABLE:
                            return
                    else:
                        _HOME_OCR_DONE = False

                if action == "click":
                    _do_click(center, label=action, src_img=img_file)
                else:
                    _do_key(action, label="single", src_img=img_file)
            continue
        else:
            # 조합키도 동일하게 위치 조건(이미지 히트) 만족해야 수행
            hit, _ = _routine_locate_adaptive(img_file, conf)
            if not hit:
                # _log(f"[routine] not found (combo-guard): img='{os.path.basename(img_file)}' action='{action}'")
                continue
            # --- [ADD] 홈 앵커면 조합키도 스킵
            if _is_home_anchor(img_file):
                if not _HOME_OCR_DONE:
                    _WANT_AWAIT_HOME = True
                    _AWAIT_HOME_CONF = float(conf)  # [FIX] probe 임계값 동기화
                    if GOAL_AVAILABLE and bool(SETTINGS().get("goal.enabled", False)):
                        return
                else:
                    _HOME_OCR_DONE = False
            keys = [k.strip().lower() for k in action.split('+')]
            try:
                for k in keys: pyd.keyDown(k)
                time.sleep(0.05)
                for k in reversed(keys): pyd.keyUp(k)
                _log(f"[action] key '{action}'")
            except Exception as e:
                _log(f"[action][err] combo '{action}' failed: {e}")


def _is_sane_metrics(m: dict) -> bool:
    if not isinstance(m, dict):
        return False
    r = m.get("rank")
    p = m.get("points")
    ok_r = isinstance(r, int) and (0 < r <= 9999)
    ok_p = isinstance(p, int) and (0 < p <= 200000)
    return ok_r or ok_p


def _coerce_metric_values(m: dict) -> dict:
    """'20', '3,439' 같은 문자열을 int로 정규화. 실패하면 해당 키 제거."""
    if not isinstance(m, dict):
        return {}

    def to_int_first_group(x, *, grouping: bool = False):
        """
        grouping=True:
          '2,713' / '2, 713' / '2 713' / '2.713' → 2713
        """
        if isinstance(x, int):
            return x
        if isinstance(x, str):
            s = x.strip()
            if grouping:
                # 천단위 구분기호: 콤마/공백/닷 허용(+ 콤마 뒤 공백 허용)
                m0 = re.search(r"(\d{1,3}(?:[,\.\s]\s?\d{3})*)", s)
            else:
                m0 = re.search(r"(\d+)", s)
            if m0:
                g = m0.group(1)
                try:
                    # 모든 구분기호 제거 후 정수화
                    return int(re.sub(r"[,\.\s]", "", g))
                except Exception:
                    return None
        return None

    # rank: 순수 정수만
    r = to_int_first_group(m.get("rank"), grouping=False)
    # points: 천단위 구분 허용
    p = to_int_first_group(m.get("points"), grouping=True)

    out = dict(m)
    if r is not None:
        out["rank"] = r
    else:
        out.pop("rank", None)
    if p is not None:
        out["points"] = p
    else:
        out.pop("points", None)

    # ★ 추가: 원본 OCR 경로에서의 '순위권 이탈' 플래그를 테스트 파이프라인과 동일하게 정규화
    if out.get("rank_oor") is True:
        out["rank_flag"] = "OUT_OF_RANGE"
        out.pop("rank", None)  # 숫자 등수 제거(충돌 방지)

    return out


def _median_int(vals):
    s = sorted(int(v) for v in vals)
    n = len(s)
    return s[n//2] if n % 2 == 1 else (s[n//2 - 1] + s[n//2]) // 2


def _fuse_metrics(samples: list[dict]) -> dict:
    """여러 샘플을 중앙값/최빈 비슷한 방식으로 융합."""
    if not samples: return {}
    ranks = [s["rank"] for s in samples if "rank" in s]
    points = [s["points"] for s in samples if "points" in s]
    if not ranks or not points: return samples[-1]
    return {"rank": _median_int(ranks), "points": _median_int(points)}


''' 디버그 이후 다시 살려주기
def _read_metrics_from_current_home(frame_bgr):
    # 홈 화면 프레임(= frame_bgr)에서 등수/점수 추출
    # 반환 dict 예: {"rank": 23, "points": 12500, "ts_ms": 169...}
    return read_metrics_from_home(frame_bgr, ocr_cfg)  # :contentReference[oaicite:7]{index=7}
'''


def _read_metrics_from_current_home_legacy(frame_bgr):
    """
    [DEPRECATED] 기존 OCR 파이프라인. v2 검증 완료 시 제거 예정.
    호출되지 않음. 비활성화 상태이며 폴백 경로도 아님.
    """
    from core import ocr as _ocr
    from core.roi_from_settings import read_rois_xywh_from_settings, scale_xywh_from_base
    from PIL import Image as _Image

    # 1) 다운스케일 (테스트와 동일)
    bgr_scaled, _ = _downscale_like_gui(frame_bgr)
    h_s, w_s = bgr_scaled.shape[:2]

    # 2) ROI 읽기 및 현재 프레임 크기로 스케일
    rois_xywh, base = read_rois_xywh_from_settings(SETTINGS())
    if not rois_xywh:
        return {}

    def _crop_pil(key):
        xywh = scale_xywh_from_base(rois_xywh.get(key), base, (w_s, h_s))
        if not xywh:
            return None
        x, y, cw, ch = xywh
        x = max(0, x - 2); y = max(0, y - 2)
        x2 = min(w_s, x + cw + 4); y2 = min(h_s, y + ch + 4)
        if x2 <= x or y2 <= y:
            return None
        crop_bgr = bgr_scaled[y:y2, x:x2]
        return _Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))

    def _first_int(s):
        m = re.search(r"\d+", s or "")
        return int(m.group(0)) if m else None

    rank_pil = _crop_pil("roi_rank")
    pts_pil  = _crop_pil("roi_points")
    result   = {}

    if rank_pil is not None:
        oor_fn = getattr(_ocr, "read_rank_out_of_range_ko", None)
        if callable(oor_fn):
            raw_rr = str(oor_fn(rank_pil) or "")
            if raw_rr:
                result["rank_flag"] = "OUT_OF_RANGE"
        if "rank_flag" not in result:
            rd = getattr(_ocr, "read_digits", None)
            if callable(rd):
                v = _first_int(str(rd(rank_pil) or ""))
                if isinstance(v, int):
                    result["rank"] = v
                    result["rank_flag"] = "NUMERIC"

    if pts_pil is not None:
        rd = getattr(_ocr, "read_digits", None)
        if callable(rd):
            v = _first_int(str(rd(pts_pil) or ""))
            if isinstance(v, int):
                result["points"] = v

    return result


def _read_metrics_from_current_home(frame_bgr):
    """
    [v2] 새 OCR 파이프라인.

    실행 흐름:
      Stage 0: 챔피언스 티어 게이트
        - "챔피언스 감독" / "슈퍼 챔피언스 감독" 헤더 검출
        - 없으면 NOT_CHAMPIONS 플래그로 즉시 종료 (점수/등수 OCR 스킵)
      Stage 2: HSV 마스킹 + image_to_data 단일 호출
        - 점수: 화이트리스트 0-9, lang=eng
        - 등수: 화이트리스트 0-9 + 순위권이탈, lang=kor
      Stage 3 일부: 앵커 검증
        - 점수가 anchor 대비 outlier면 앙상블 재검증
        - 앙상블도 어긋나면 OCR_UNCERTAIN

    반환 dict:
      성공:        {"rank": int, "rank_flag": "NUMERIC", "points": int, "ts_ms": int}
      순위권 이탈: {"rank_flag": "OUT_OF_RANGE", "points": int|absent, "ts_ms": int}
      티어 아님:   {"rank_flag": "NOT_CHAMPIONS", "ts_ms": int}
      신뢰 부족:   {"rank_flag": "OCR_UNCERTAIN", "ts_ms": int}
    """
    try:
        return _read_metrics_from_current_home_impl(frame_bgr)
    except Exception as e:
        # 어떤 예외든 routine_loop을 죽이지 않고 OCR_UNCERTAIN으로 처리
        _log(f"[OCRv2] exception: {e}")
        return {"rank_flag": "OCR_UNCERTAIN", "ts_ms": int(time.time() * 1000),
                "reason": f"exception: {type(e).__name__}"}


def _is_bootstrap_worthy(pts_result) -> bool:
    """앵커 부트스트랩 자격 판단.

    Tesseract LSTM 이 whitelist 사용 시 conf 를 매우 낮게(또는 0으로) 보고하는
    알려진 이슈가 있다. 실측 결과 정상 OCR 결과도 conf=50~60 정도로 잡힘.
    따라서 conf 자체보다는 자릿수 검증을 더 신뢰한다.

    통과 조건 (OR):
    - conf >= 임계값(기본 50) — LSTM 보고 conf 가 신뢰 영역
    - conf >= 30 AND 자릿수 == 4 — 챔피언스 점수 4자리 정상 케이스
    - conf == 0 AND 자릿수 == 4 — whitelist conf=0 알려진 이슈 보정
    """
    try:
        min_conf = int(SETTINGS().get("ocr.bootstrap_min_conf", 50))
    except Exception:
        min_conf = 50
    if pts_result.word_conf >= min_conf:
        return True
    if pts_result.word_conf >= 30 and pts_result.digit_count == 4:
        return True
    if pts_result.word_conf == 0 and pts_result.digit_count == 4:
        return True
    return False


def _read_metrics_from_current_home_impl(frame_bgr):
    """실제 OCR 처리 본체. 예외는 호출부에서 잡힘."""
    from core import ocr_v2 as _ocrv2
    from core.roi_from_settings import read_rois_xywh_from_settings, scale_xywh_from_base

    ts_ms = int(time.time() * 1000)
    result: dict = {"ts_ms": ts_ms}

    # ---- 1) 다운스케일 (기존 GUI 테스트 파이프라인과 동일) ----
    bgr_scaled, _ = _downscale_like_gui(frame_bgr)
    h_s, w_s = bgr_scaled.shape[:2]

    # ---- 2) Stage 0: 챔피언스 티어 게이트 ----
    _t_champ = time.time()
    is_champ, champ_text = _ocrv2.is_champions_tier(bgr_scaled, SETTINGS())
    result["_dbg_champ_elapsed_ms"] = int((time.time() - _t_champ) * 1000)
    try:
        _cache_exists = SETTINGS().get("ocr.champion_bbox_cache", None) is not None
    except Exception:
        _cache_exists = False
    result["_dbg_champ_cache"] = bool(_cache_exists)
    if not is_champ:
        # 챔피언스 아님 → 점수/등수 OCR 자체가 무의미. 즉시 반환.
        result["rank_flag"] = "NOT_CHAMPIONS"
        result["champ_raw"] = champ_text  # 디버깅용
        return result

    # ---- 3) ROI 크롭 (기존과 동일 스케일링) ----
    rois_xywh, base = read_rois_xywh_from_settings(SETTINGS())
    if not rois_xywh:
        # ROI 설정 없으면 진행 불가
        result["rank_flag"] = "OCR_UNCERTAIN"
        result["reason"] = "no ROI configured"
        return result

    def _crop_bgr(key):
        xywh = scale_xywh_from_base(rois_xywh.get(key), base, (w_s, h_s))
        if not xywh:
            return None
        x, y, cw, ch = xywh
        x = max(0, x - 2); y = max(0, y - 2)
        x2 = min(w_s, x + cw + 4); y2 = min(h_s, y + ch + 4)
        if x2 <= x or y2 <= y:
            return None
        return bgr_scaled[y:y2, x:x2].copy()

    rank_bgr = _crop_bgr("roi_rank")
    pts_bgr = _crop_bgr("roi_points")

    # ---- 4) Stage 2: 점수 OCR ----
    pts_result = None
    _t_pts = time.time()
    _pts_route = "none"
    if pts_bgr is not None:
        pts_result = _ocrv2.read_points_v2_ref(pts_bgr)
        _pts_route = "single"
        result["_dbg_pts_single_raw"] = pts_result.value
        result["_dbg_pts_single_conf"] = pts_result.word_conf
        result["_dbg_pts_raw_text"] = pts_result.raw
        result["_dbg_pts_digit_count"] = pts_result.digit_count
        if pts_result.kind == "NUMERIC" and isinstance(pts_result.value, int):
            # 점수 sanity range (챔피언스 2000-9999)
            if 2000 <= pts_result.value <= 9999:
                result["points"] = pts_result.value
            else:
                # range 밖 → 폐기 후 앙상블 재시도
                ens = _ocrv2.read_points_v2_ensemble(pts_bgr)
                _pts_route = "single+ens_range"
                result["_dbg_ens_range_val"] = ens.final.value
                result["_dbg_ens_range_agree"] = round(ens.agreement, 2)
                if (ens.final.kind == "NUMERIC"
                        and isinstance(ens.final.value, int)
                        and 2000 <= ens.final.value <= 9999
                        and ens.agreement >= 0.6):
                    result["points"] = ens.final.value
                    pts_result = ens.final
    result["_dbg_pts_elapsed_ms"] = int((time.time() - _t_pts) * 1000)
    result["_dbg_pts_route"] = _pts_route

    # ---- 5) 앵커 검증 (점수만) ----
    # 카운터 방식: 임계 = base × _ANCHOR_OUTLIER_COUNTER (1판당 base, 누적 outlier 시 동적 확장).
    # 통과 → 카운터 1 로 reset. outlier 확정 → routine_loop 사이클 끝에서 += 1.
    if "points" in result:
        anchor = SCORE_ANCHOR()
        val = anchor.validate(int(result["points"]),
                              thresh_multiplier=_ANCHOR_OUTLIER_COUNTER)
        result["_dbg_anchor_val"] = anchor.get_any()
        result["_dbg_anchor_state"] = val.state
        if val.state in ("OUTLIER_UP", "OUTLIER_DOWN", "DIGIT_MISMATCH"):
            result["_dbg_pts_route"] = (result.get("_dbg_pts_route", "") + "+ens_anchor").strip("+")
            # 누적 임계도 초과 → 앙상블 재검증
            if pts_bgr is not None:
                ens = _ocrv2.read_points_v2_ensemble(pts_bgr)
                if (ens.final.kind == "NUMERIC"
                        and isinstance(ens.final.value, int)
                        and 2000 <= ens.final.value <= 9999
                        and ens.agreement >= 0.6):
                    ens_val = anchor.validate(ens.final.value,
                                              thresh_multiplier=_ANCHOR_OUTLIER_COUNTER)
                    if ens_val.state in ("OK", "NO_ANCHOR", "STALE_ANCHOR"):
                        # 앙상블 결과가 앵커와 정합 → 채택 + 카운터 reset
                        result["points"] = ens.final.value
                        anchor.update(ens.final.value)
                        globals()["_ANCHOR_OUTLIER_COUNTER"] = 1
                    else:
                        # 앙상블도 outlier → 신뢰 불가 (카운터는 routine_loop에서 +1)
                        result.pop("points", None)
                        result["anchor_outlier"] = val.reason
                else:
                    result.pop("points", None)
                    result["anchor_outlier"] = val.reason
        elif val.state == "NO_ANCHOR":
            # 부트스트랩: 첫 신뢰 OCR로 앵커 설정
            if pts_result is not None and _is_bootstrap_worthy(pts_result):
                anchor.update(int(result["points"]))
                globals()["_ANCHOR_OUTLIER_COUNTER"] = 1
        elif val.state == "STALE_ANCHOR":
            # 오래된 앵커 무시. 충분히 신뢰 가능하면 갱신.
            if pts_result is not None and _is_bootstrap_worthy(pts_result):
                anchor.update(int(result["points"]))
                globals()["_ANCHOR_OUTLIER_COUNTER"] = 1
        elif val.state == "OK":
            # 정상 통과 — 앵커 업데이트 + 카운터 reset
            anchor.update(int(result["points"]))
            globals()["_ANCHOR_OUTLIER_COUNTER"] = 1

    # ---- 5.5) Stage 3: 임계 근처 conf 강화 검증 ----
    # 목표 임계 근처(boundary_distance 이내)면 conf 임계를 normal(70)→boundary(85)로 강화.
    # conf 부족 시 앙상블 재검증, 그래도 정합 실패면 OCR_UNCERTAIN 처리.
    # 시즌말 한 판 차이 상황에서 의심스러운 결과로 stop 하지 않게 막는 안전망.
    if "points" in result and pts_result is not None:
        try:
            _target_pts2 = _active_points_target()
            if _target_pts2 is not None:
                _dist2 = abs(int(result["points"]) - _target_pts2)
                _boundary = int(SETTINGS().get("ocr.boundary_distance", 30))
                if _dist2 <= _boundary:
                    _conf_thr = int(SETTINGS().get("ocr.conf_thresh_boundary", 60))
                    _is_conf_low = (pts_result.word_conf < _conf_thr)
                    # Tesseract LSTM whitelist 사용 시 conf 를 매우 낮게 보고하는 이슈 예외:
                    #   - conf == 0 + 자릿수 == 4
                    #   - conf >= 30 + 자릿수 == 4 (실측 정상 OCR도 conf 50~60 수준)
                    _is_lstm_low_4d = (
                        (pts_result.word_conf == 0 and pts_result.digit_count == 4)
                        or (pts_result.word_conf >= 30 and pts_result.digit_count == 4)
                    )
                    if _is_conf_low and not _is_lstm_low_4d:
                        if pts_bgr is not None:
                            _ens2 = _ocrv2.read_points_v2_ensemble(pts_bgr)
                            _agree_min = float(SETTINGS().get("ocr.ensemble_min_agreement", 0.8))
                            if (_ens2.final.kind == "NUMERIC"
                                    and isinstance(_ens2.final.value, int)
                                    and 2000 <= _ens2.final.value <= 9999
                                    and _ens2.agreement >= _agree_min):
                                result["points"] = _ens2.final.value
                                result["boundary_strict_passed"] = True
                            else:
                                # 앙상블도 정합 실패 → 신뢰 불가
                                result.pop("points", None)
                                result["boundary_strict_failed"] = True
                                result["rank_flag"] = "OCR_UNCERTAIN"
                                result["reason"] = "boundary conf strict failed"
        except Exception:
            # 검증 실패는 routine 죽이지 않음 — 기존 결과 그대로 진행
            pass

    # ---- 6) Stage 2: 등수 OCR ----
    _t_rank = time.time()
    _rank_route = "none"
    if rank_bgr is not None:
        rank_result = _ocrv2.read_rank_v2_ref(rank_bgr, SETTINGS())
        _rank_route = "single"
        result["_dbg_rank_raw"] = rank_result.raw
        result["_dbg_rank_conf"] = rank_result.word_conf
        result["_dbg_rank_digit_count"] = rank_result.digit_count
        if rank_result.kind == "OUT_OF_RANGE":
            result["rank_flag"] = "OUT_OF_RANGE"
        elif rank_result.kind == "NUMERIC" and isinstance(rank_result.value, int):
            if 0 < rank_result.value <= 9999:
                result["rank"] = rank_result.value
                result["rank_flag"] = "NUMERIC"
            else:
                # range 밖 → 앙상블 재시도
                ens = _ocrv2.read_rank_v2_ensemble(rank_bgr)
                if (ens.final.kind == "NUMERIC"
                        and isinstance(ens.final.value, int)
                        and 0 < ens.final.value <= 9999
                        and ens.agreement >= 0.6):
                    result["rank"] = ens.final.value
                    result["rank_flag"] = "NUMERIC"
                    _rank_route = "single+ens"
                elif ens.final.kind == "OUT_OF_RANGE" and ens.agreement >= 0.6:
                    result["rank_flag"] = "OUT_OF_RANGE"
                    _rank_route = "single+ens"
    result["_dbg_rank_elapsed_ms"] = int((time.time() - _t_rank) * 1000)
    result["_dbg_rank_route"] = _rank_route

    # ---- 6.5) cross-validation: 점수 outlier 확정 시 등수도 의심 처리 ----
    # 점수 OCR이 anchor 대비 outlier로 확정됐고 (앙상블도 정합 실패),
    # 같은 사이클의 등수 OCR도 동일 엔진/프레임에서 추출됐으므로 신뢰 불가.
    # 단, anchor 자체가 부재했다면 (NO_ANCHOR/STALE) outlier 비교가 무의미하므로 제외.
    if (result.get("anchor_outlier")
            and ("rank" in result or result.get("rank_flag") == "OUT_OF_RANGE")):
        result.pop("rank", None)
        # OUT_OF_RANGE 도 신뢰 못 함 → 덮어씀
        result["rank_flag"] = "OCR_UNCERTAIN"
        result["reason"] = "score outlier → rank cross-distrust"

    # ---- 7) 결과 미수렴 처리 ----
    # rank_flag 가 아직 설정 안 됐고 points도 없으면 OCR_UNCERTAIN
    if "rank_flag" not in result and "points" not in result:
        result["rank_flag"] = "OCR_UNCERTAIN"

    return result


def _active_goal_info() -> Optional[Dict[str, Any]]:
    """현재 활성 프리셋의 모드/실질 목표/마진 반환. goal 비활성 시 None.

    반환 dict:
      {
        "mode": "points" | "rank",
        "effective_target": int,  # 마진 적용된 실질 목표 (점수: target-margin, 등수: target+tol)
        "raw_target": int,        # 원본 목표
        "margin": int,            # 마진/tolerance
      }
    """
    try:
        g = SETTINGS().get("goal", {}) or {}
        if not g.get("enabled", False):
            return None
        presets = g.get("presets", {}) or {}
        pid = g.get("active_preset_id", "")
        p = presets.get(pid, {}) or {}
        mode = str(p.get("mode", "rank")).lower().strip()
        if mode == "points":
            target = int(p.get("points_target", 0) or 0)
            margin = int(p.get("points_margin", 0) or 0)
            return {"mode": "points",
                    "effective_target": max(0, target - margin),
                    "raw_target": target, "margin": margin}
        elif mode == "rank":
            target = int(p.get("rank_target", 0) or 0)
            tol = int(p.get("rank_tolerance", 0) or 0)
            return {"mode": "rank",
                    "effective_target": max(1, target + tol),
                    "raw_target": target, "margin": tol}
        return None
    except Exception:
        return None


def _fmt_goal_line(m: dict, status_tag: str) -> str:
    """목표달성/목표미달 로그 1줄 포맷.

    형식: [status_tag] 점수 X / 등수 Y   ←  목표: 점수/등수 N 이상/이내
    OUT_OF_RANGE / 인식 실패 케이스도 통합 처리.
    goal 비활성 시 목표 부분 생략.
    """
    if not isinstance(m, dict):
        m = {}
    # 현재 값 포맷
    p = m.get("points")
    p_txt = f"{p}" if isinstance(p, int) else "?"
    if m.get("rank_flag") == "OUT_OF_RANGE":
        r_txt = "순위권 이탈"
    else:
        r = m.get("rank")
        r_txt = f"{r}" if isinstance(r, int) else "?"
    current = f"점수 {p_txt} / 등수 {r_txt}"

    info = _active_goal_info()
    if info is None:
        return f"[{status_tag}] {current}"
    if info["mode"] == "points":
        target_txt = f"점수 {info['effective_target']} 이상"
    else:
        target_txt = f"등수 {info['effective_target']} 이내"
    return f"[{status_tag}] {current}   ←  목표: {target_txt}"


def _active_points_target() -> Optional[int]:
    """현재 활성 프리셋의 points_target 반환 (점수 모드일 때만).

    거리 기반 적응형 confirm_samples 와 conf_thresh_boundary 적용 판단용.
    등수 모드면 None 반환 (점수 임계가 정의 안 됨).
    """
    try:
        g = SETTINGS().get("goal", {}) or {}
        if not g.get("enabled", False):
            return None
        presets = g.get("presets", {}) or {}
        pid = g.get("active_preset_id", "")
        p = presets.get(pid, {}) or {}
        if str(p.get("mode", "")).lower().strip() != "points":
            return None
        t = int(p.get("points_target", 0) or 0)
        return t if t > 0 else None
    except Exception:
        return None


def _adaptive_confirm_samples(distance: Optional[int], base: int) -> int:
    """목표까지 거리에 따라 confirm_samples 동적 조정.

    distance:
      ≤ 10     → adaptive_confirm_close (기본 5)
      ≤ 30     → adaptive_confirm_near  (기본 4)
      ≤ 100    → base
      그 외/None → base
    """
    if not isinstance(distance, int):
        return base
    try:
        s = SETTINGS()
        if distance <= 10:
            return max(base, int(s.get("ocr.adaptive_confirm_close", 5)))
        if distance <= 30:
            return max(base, int(s.get("ocr.adaptive_confirm_near", 4)))
        return base
    except Exception:
        return base


def _is_goal_met_by_settings(m: dict) -> bool:
    """
    settings.json의 goal 프리셋을 직접 읽어 'rank 전용' 판정 가드.
    - mode == "rank"  : rank <= (rank_target + rank_tolerance)
    - mode == "points": points >= (points_target - points_margin)
    - 그 외/누락       : False
    """
    try:
        g = SETTINGS().get("goal", {}) or {}
        if not g.get("enabled", False):
            return False
        presets = g.get("presets", {}) or {}
        pid = g.get("active_preset_id", "")
        p = presets.get(pid, {}) or {}
        mode = str(p.get("mode", "rank")).lower().strip()

        if mode == "rank":
            r = m.get("rank")
            if not isinstance(r, int) or r <= 0:
                return False
            target = int(p.get("rank_target", 0))
            tol    = int(p.get("rank_tolerance", 0))
            # 등수는 낮을수록 좋음 → 상한 = target + tol (포함)
            return r <= max(1, target + tol)

        elif mode == "points":
            v = m.get("points")
            if not isinstance(v, int) or v <= 0:
                return False
            target = int(p.get("points_target", 0))
            margin = int(p.get("points_margin", 0))
            # 점수는 높을수록 좋음 → 하한 = target - margin (포함)
            return v >= max(0, target - margin)

        return False
    except Exception:
        return False


def routine_loop(stop_event_global, state_cb, log_cb):
    # 설정 최신화: GUI가 디스크에 저장한 값을 메인 인메모리로 강제 반영
    try:
        SETTINGS().flush_debounced(immediate=True)
    except Exception:
        pass
    try:
        SETTINGS().load()  # ★ 디스크 재로딩 (핵심)
    except Exception:
        pass

    # Goal 정책 초기화 (현재 settings.json 기준)
    goal_policy = build_goal_policy(SETTINGS())
    # ★ 스냅샷 제거: 아래 1-B, 1-C에서 즉시반영 방식으로 전환

    # [ADD] OCR 확정샘플 파라미터(설정값 적용)
    _GOAL_CONFIRM_SAMPLES = int(SETTINGS().get("goal.confirm_samples", 3))
    _GOAL_CONFIRM_WINDOW_MS = int(SETTINGS().get("goal.confirm_window_ms", 1200))
    # ★ 전처리 폴백 스위치(기본 False = raw-only)
    _USE_PREPROCESS_FALLBACK = bool(SETTINGS().get("ocr.use_preprocess_fallback", False))

    # 전역 간격 변수 초기화(초깃값 350 → 설정값으로 덮어씀)
    global _HOME_TPL_PATH, _CACHED_ROI, _SKIP_FILE_ROI_ONCE, _CONFIRM_WINDOW_MS, _AWAIT_ENTER_MS, _AWAIT_SETTLE_MS
    global _RUNTIME_LOG_CB, _WANT_AWAIT_HOME, _HOME_OCR_DONE
    global _UNCERTAIN_STREAK, _LAST_UNCERTAIN_ALERT_MS
    global _ROUTINE_START_MS, _HOME_HIT_MS, _OCR_START_MS
    _CONFIRM_WINDOW_MS = _GOAL_CONFIRM_WINDOW_MS
    _RUNTIME_LOG_CB = log_cb

    _sync_screen_profile()

    # --- [NEW] 초기 autoemail 설정 주입 ---
    email_cfg = SETTINGS().get("email", {})
    # 기본값 보정
    email_cfg.setdefault("enabled", False)
    email_cfg.setdefault("provider", "gmail")
    email_cfg.setdefault("smtp_host", "")
    email_cfg.setdefault("smtp_port", 587)
    email_cfg.setdefault("use_tls", True)
    email_cfg.setdefault("sender", "")
    email_cfg.setdefault("app_password", "")
    email_cfg.setdefault("recipients", "")

    # ✅ 공백 유지(예시는 주입 금지)
    email_cfg.setdefault("subject_tmpl", "")
    email_cfg.setdefault("body_tmpl", "")
    # ✅ 과거 빌드에서 주입돼 저장된 예시 문자열은 1회 정리
    if email_cfg.get("subject_tmpl") in ("[ImageReactor] ${event}", "[Manager] {event}"):
        email_cfg["subject_tmpl"] = ""
    if email_cfg.get("body_tmpl") in (
            "Event: ${event}\nAt: ${timestamp}\nHost: ${hostname}",
            "Event: {event}\nTime: {timestamp}\nHost: {hostname}",
    ):
        email_cfg["body_tmpl"] = ""

    email_cfg.setdefault("rate_limit_min_interval", 600)
    email_cfg.setdefault("rate_limit_burst", 1)

    if isinstance(email_cfg.get("recipients"), (list, tuple, set)):
        email_cfg["recipients"] = ",".join(map(str, email_cfg["recipients"]))
    autoemail.configure(email_cfg)

    # --- Freeze 감지기 준비 ---
    global freeze  # 전역 인스턴스만 사용
    _frz_last_cap_ts = 0.0  # ⬅️ 추가: 마지막 캡처 시각

    def _on_freeze_trip(count: int):
        # freeze 객체가 있으면 interval 반영, 없으면 60초 가정
        minutes = (count * (freeze.cfg.interval_sec if (freeze and freeze.cfg) else 60)) // 60
        log_cb(f"[FREEZE] 화면 정지 {count}회 샘플(≈{minutes}분) 감지 → 자동 이메일 발송")
        try:
            _email_guarded("freeze_detected", {
                "samples": count,
                "approx_minutes": int(minutes),
                "ts": int(time.time())
            })
        except Exception as e:
            log_cb(f"[FREEZE] 이메일 발송 실패: {e}")

    # keep_alive 등 시작 차단 검사
    if goal_policy.should_block_start():
        log_cb("keep_alive 모드: 시작 차단")
        state_cb("IDLE")
        return

    """
    GUI RunController에서 실행할 worker 함수.
    """
    global is_crashed, client_item, routine_items, probe, cached_home_roi, t_trigger_ms, home_tpl_bgr
    last_run = 0
    interval = max(0.2, float(_LOOP_INTERVAL_SEC))  # ★ 설정 반영(최소 0.2s 가드)

    store = get_state_store()  # ★ 추가: UI busy 플래그 조회용
    while not stop_event_global.is_set():
        if exit_event.is_set():
            log_cb("루틴 종료 요청")
            break

        if start_event.is_set():
            start_event.clear()
            stop_event.clear()  # ★ 추가: 재시작 시 전역 stop 잔류 제거
            log_cb("\n============반복 시작============")
            _ROUTINE_START_MS = int(time.time() * 1000)
            _HOME_HIT_MS = 0
            _OCR_START_MS = 0
            _ocr_diag_log(f"[Timing] === 반복 시작 (ts={_ROUTINE_START_MS}) ===")
            # routine 시작 = 사용자가 클릭 완료하고 진행 의사를 명확히 한 시점.
            # ButtonRelease-1 이벤트가 누락된 경우라도 ui_busy 잔여로 routine_loop
            # 첫 iter들이 매번 0.3초 throttle 누적되어 start→await가 3초+로 부풀려짐.
            # 시작 신호 = ui_busy 강제 해제로 첫 사이클부터 정상 진행.
            try:
                get_state_store().set_ui_busy(False)
            except Exception:
                pass
            reset_runtime_state()
            state_cb("RUNNING")

            while not stop_event.is_set() and not stop_event_global.is_set():
                now = time.time()

                # 예약 종료 스케줄 검사 (저부하 폴링)
                try:
                    _SCHED_SD.maybe_check_and_trigger()
                except Exception:
                    pass

                # ★ UI 상호작용(창 이동/리사이즈) 중에는 무거운 작업을 잠깐 쉰다
                try:
                    if _busy_throttle_gate(store):
                        last_run = now  # 주기 타이머 리셋
                        continue
                except Exception:
                    pass

                if client_item:
                    client_img_path = img_path + client_item["image"]
                    if os.path.exists(client_img_path) and client_crashed(client_img_path):
                        log_cb("**아이콘 사라짐 → 절전방지 모드 진입**")
                        is_crashed = True
                        break
                if stop_event.is_set():
                    log_cb("============중지됨============")
                    state_cb("IDLE")
                    break
                if now - last_run < interval:
                    # AWAIT_HOME 상태(probe 살아있음)에서는 interval 가드 우회.
                    # 별도의 settle 250ms + polling 가드(80ms)가 이미 적용되므로
                    # routine 매칭 빈도용 interval 까지 더해질 이유가 없다.
                    if probe is None:
                        time.sleep(0.01)  # interval 대기 중 CPU 스핀 방지
                        continue
                if now - last_run >= interval or probe is not None:
                    # AWAIT_HOME(probe 살아있음) 중에는 busy_throttle_gate 우회.
                    # busy 상태가 watchdog 2s + cleanup 220ms 동안 stuck 되면
                    # 매 iter 350ms throttle 누적 → AWAIT_HOME 정체 직접 원인.
                    # probe.step 은 force_first_hit으로 1회 hit이라 throttle 의미 없음.
                    if probe is None:
                        try:
                            if _busy_throttle_gate(store):
                                last_run = now
                                continue
                        except Exception:
                            pass

                    if _SPAM_ACTIVE:
                        _spam_tick()
                        # [NEW] 스팸 모드에서도 프리즈 모니터 유지 가동
                        try:
                            if freeze is not None and not freeze.is_disabled:
                                nowf = time.time()
                                # _frz_last_cap_ts 는 루프 상단에서 0.0으로 초기화해둔 float
                                # freeze.cfg.interval_sec (freeze_monitor 내부) 간격에 맞춰 샘플링
                                if (nowf - _frz_last_cap_ts) >= getattr(freeze.cfg, "interval_sec", 60.0):
                                    fbgr = _capture_frame_bgr_like_gui()  # GUI와 동일 캡처 파이프라인
                                    freeze.tick(fbgr)
                                    _frz_last_cap_ts = nowf
                        except Exception:
                            pass

                        last_run = now
                        continue

                    # 목표달성 토글이 런타임에서 OFF로 바뀌었으면 홈 OCR 강제 중단
                    if not _goal_enabled_now():
                        if probe is not None:
                            try:
                                probe.stop()
                            except Exception:
                                pass
                        probe = None
                        _WANT_AWAIT_HOME = False
                        _HOME_OCR_DONE = False
                        state_cb("RUNNING")

                    # ★★★ 홈 판정(AWAIT_HOME) 중에는 루틴 실행/새 probe 생성 모두 중단
                    # probe가 없을 때만 루틴 실행 (있으면 아래 probe.step() 분기에서 처리)
                    if probe is None:
                        try:
                            execute_routine(routine_items)
                        except Exception as e:
                            log_cb(f"[routine] 예외 흡수: {e!r}")  # 스레드 살리고 다음 사이클 진행
                    last_run = now

                    # 결과 트리거 직후 AWAIT_HOME 시작
                    # main.py - 반복 루프 내 probe 생성 지점
                    if _goal_enabled_now() and GOAL_AVAILABLE and _WANT_AWAIT_HOME:
                        if probe is None:
                            _WANT_AWAIT_HOME = False  # ← 소비하고 바로 리셋
                            # [옵션 A] routine 매칭에서 이미 home anchor 가 hit 된 시점이
                            # home 화면 확정의 가장 강한 증거. probe 의 settle / polling /
                            # 재매칭 단계는 redundant 이므로 fast track 으로 즉시 hit 반환.
                            _probe_thr = min(load_home_anchor_threshold(), float(_AWAIT_HOME_CONF))
                            probe = HomeProbe(
                                ncc_threshold=_probe_thr,
                                soft_timeout_ms=8000,
                                hard_timeout_ms=12000,
                                force_first_hit=True,
                            )
                            probe.start()
                            _home_ff_limiter_reset()
                            _log_home_ff("reset", 0, 2, "enter")
                            state_cb("AWAIT_HOME_ENTER|FAST_TRACK")
                            # [옵션 3] AWAIT_HOME 진입 시 GUI 숨김 시작.
                            # settle 250ms 동안 GUI 폴러가 withdraw 처리 → 그 후 캡처 1회.
                            get_state_store().set_ocr_sampling_active(True)
                            _AWAIT_ENTER_MS = int(time.time() * 1000)
                            _AWAIT_SETTLE_MS = 250  # GUI 사라질 시간
                            _ocr_diag_log(
                                f"[Timing] probe created force_first_hit={getattr(probe, 'force_first_hit', '?')} "
                                f"settle_ms={_AWAIT_SETTLE_MS} loop_interval_s={_LOOP_INTERVAL_SEC}"
                            )
                            globals()["_DEBUG_OCR_DUMPED"] = False  # ← 덤프 1회 가드 리셋
                            # --- FREEZE 모니터 시작 ---
                            if freeze is None or freeze.is_disabled:
                                freeze = FreezeMonitor.from_settings_dict(SETTINGS().get("freeze", None),
                                                                          on_trip=_on_freeze_trip)
                            freeze.activate()
                            t_trigger_ms = int(time.time() * 1000)
                    elif not _goal_enabled_now() or not GOAL_AVAILABLE:
                        probe = None  # Goal 비활성/불가 → AWAIT_HOME 강제 해제

                if probe is not None:
                    # AWAIT_HOME 진행 중에는 busy_throttle_gate 우회 (직접 정체 원인).
                    # probe.step은 force_first_hit으로 즉시 hit이라 throttle 의미 없음.

                    # AWAIT_HOME settle 가드: GUI hidden 명시적 동기화 + hard timeout fallback.
                    # 기존 settle_ms 단순 sleep 대신 store.gui_hidden_at_ms polling 으로
                    # OS 컴포지터 turn 까지 보장. ui_busy stuck 등으로 신호 못 받으면
                    # hard timeout 후 캡처 진행 (이전 동작과 동등).
                    if _AWAIT_ENTER_MS is not None:
                        now_ms = int(time.time() * 1000)
                        elapsed = now_ms - _AWAIT_ENTER_MS
                        hidden_at = get_state_store().get_gui_hidden_at()
                        # GUI 가 _AWAIT_ENTER_MS 이후에 hidden 됐고, 컴포지터 turn grace 도 경과
                        gui_ready = (hidden_at >= _AWAIT_ENTER_MS
                                     and (now_ms - hidden_at) >= _GUI_HIDDEN_GRACE_MS)
                        # hard timeout: ui_busy stuck 등으로 GUI hidden 신호 못 받으면 fallback
                        timed_out = elapsed >= _AWAIT_HARD_TIMEOUT_MS
                        if not gui_ready and not timed_out:
                            # freeze 모니터링은 계속 진행(홈이 아니므로 안전)
                            if freeze is not None and not freeze.is_disabled:
                                nowf = time.time()
                                if nowf - _frz_last_cap_ts >= freeze.cfg.interval_sec:
                                    fbgr = _capture_frame_bgr()
                                    cnt = freeze.tick(fbgr)
                                    _frz_last_cap_ts = nowf
                            time.sleep(_IDLE_SLEEP_SEC)
                            continue
                        # 진단: settle 가드 통과 첫 시점 1회 기록
                        if globals().get("_AWAIT_SETTLE_DONE_MS") is None:
                            globals()["_AWAIT_SETTLE_DONE_MS"] = now_ms
                            # 진단용 부가 정보: 통과 사유 (gui_ready / timed_out)
                            globals()["_AWAIT_SETTLE_REASON"] = (
                                "gui_ready" if gui_ready else "timed_out"
                            )

                    # ★ 프레임 캡처 최소 간격 가드(기본 140ms)
                    now_ms = int(time.time() * 1000)
                    _await_last_step = globals().get("_AWAIT_LAST_STEP_MS", 0)
                    if now_ms - _await_last_step < _AWAIT_STEP_MIN_MS:
                        time.sleep(_IDLE_SLEEP_SEC)
                        continue
                    globals()["_AWAIT_LAST_STEP_MS"] = now_ms

                    # 진단: capture 단독 시간 측정 (첫 호출만)
                    _cap_t0 = int(time.time() * 1000)
                    frame_bgr = _capture_frame_bgr_like_gui()  # GUI와 동일 파이프라인로 캡처
                    _cap_ms = int(time.time() * 1000) - _cap_t0

                    res = probe.step(frame_bgr, _match_home_anchor, cached_home_roi)
                    # 진단: probe 의 첫 step 시점 / 결과 / settle 후 경과 시간
                    if probe is not None and not getattr(probe, "_logged_first_step", False):
                        try:
                            _now_ms = int(time.time() * 1000)
                            _elapsed = _now_ms - (_AWAIT_ENTER_MS or 0)
                            _settle_done = globals().get("_AWAIT_SETTLE_DONE_MS")
                            _settle_done_at = (_settle_done - (_AWAIT_ENTER_MS or 0)) if _settle_done else -1
                            # 통합 timing 라인: settle 통과 시점 + capture 단독 시간 포함
                            _settle_reason = globals().get("_AWAIT_SETTLE_REASON", "?")
                            _ocr_diag_log(
                                f"[Timing] probe.step first call: res={res} "
                                f"elapsed_from_await_enter={_elapsed}ms "
                                f"settle_done_at={_settle_done_at}ms "
                                f"settle_reason={_settle_reason} "
                                f"capture_ms={_cap_ms}ms"
                            )
                        except Exception:
                            pass
                        probe._logged_first_step = True
                        # 다음 AWAIT_HOME 사이클 위해 settle_done / reason 리셋
                        globals()["_AWAIT_SETTLE_DONE_MS"] = None
                        globals()["_AWAIT_SETTLE_REASON"] = None

                    # AWAIT_HOME busy_throttle_gate 우회 (직접 정체 원인 제거).

                    # --- 홈 아님(pending/miss)일 때만 freeze 샘플 ---
                    if res != "hit" and freeze is not None and not freeze.is_disabled:
                        nowf = time.time()
                        if nowf - _frz_last_cap_ts >= freeze.cfg.interval_sec:
                            cnt = freeze.tick(frame_bgr)  # 이미 막 찍은 frame_bgr 재사용
                            if cnt is not None:
                                _log_throttled(log_cb, "FREEZE_TICK", f"[FREEZE] 동일 화면 샘플 누적: {cnt}", 60.0)
                            _frz_last_cap_ts = nowf

                    if res == "hit":
                        _HOME_HIT_MS = int(time.time() * 1000)
                        if freeze is not None:
                            try:
                                # 홈 진입 순간엔 프리즈 누적 카운터/해시를 초기화하고 샘플링을 끈다.
                                cnt = getattr(freeze, "_consec", 0)
                                freeze.reset()  # ← 추가
                                freeze.deactivate()  # 기존 유지
                                _log_throttled(log_cb, "FREEZE_RESET", f"[FREEZE] 홈 hit → freeze.reset() 수행 (누적={cnt})",
                                               60.0)
                            except Exception:
                                freeze.deactivate()
                        _home_ff_limiter_reset()

                        # ---- 최초 1회 커밋: 파일 캐시가 비어 있으면 방금 매칭한 bbox로 커밋 ----
                        if _CACHED_ROI is None and _HOME_TPL_PATH is not None and _HOME_SCREEN_WH is not None:
                            last_bbox = probe.get_last_bbox() if probe else None
                            if last_bbox and len(last_bbox) == 4:
                                roi_commit(_HOME_TPL_PATH, last_bbox, _HOME_SCREEN_WH)  # 이미 있으면 내부에서 no-op
                                _CACHED_ROI = last_bbox  # 이후 프레임부터 ROI 전용 매칭

                        # HOME 진입 훅: 정책 스냅샷 고정
                        goal_policy.on_home_enter()  # 프리셋 스냅샷 고정/버퍼 초기화 :contentReference[oaicite:10]{index=10}

                        # [옵션 3] frame_bgr 재사용 + GUI 즉시 복구
                        # AWAIT_HOME 진입 시점에 이미 GUI 숨김 신호를 보냈고
                        # settle 250ms 동안 withdraw 처리됨. probe.step 시점 캡처는
                        # GUI 없는 상태이므로 OCR 에 그대로 재사용.
                        # 캡처가 끝났으니 GUI 즉시 복구 — OCR 진행 중에도 사용자 시야 확보.
                        get_state_store().set_ocr_sampling_active(False)

                        samples_ok = []
                        reached = False
                        metrics = None

                        _ocr_frame = frame_bgr  # probe.step 시점 캡처 재사용
                        _OCR_START_MS = int(time.time() * 1000)
                        m0 = _read_metrics_from_current_home(_ocr_frame)
                        m0 = _coerce_metric_values(m0 or {})

                        # ── 진단 로그: OCR 단계별 결과/타이밍 한 줄 압축 ──
                        try:
                            _ch_ms = m0.get("_dbg_champ_elapsed_ms", "?")
                            _ch_cache = "Y" if m0.get("_dbg_champ_cache") else "N"
                            _pts_ms = m0.get("_dbg_pts_elapsed_ms", "?")
                            _rk_ms = m0.get("_dbg_rank_elapsed_ms", "?")
                            _pts_route = m0.get("_dbg_pts_route", "?")
                            _rk_route = m0.get("_dbg_rank_route", "?")
                            _pts_single = m0.get("_dbg_pts_single_raw", "?")
                            _pts_conf = m0.get("_dbg_pts_single_conf", "?")
                            _anchor_v = m0.get("_dbg_anchor_val", "?")
                            _anchor_s = m0.get("_dbg_anchor_state", "?")
                            _final_pts = m0.get("points", "?")
                            _final_rk = m0.get("rank", m0.get("rank_flag", "?"))
                            _pts_raw_text = m0.get("_dbg_pts_raw_text", "")
                            _pts_dc = m0.get("_dbg_pts_digit_count", "?")
                            _rank_raw_text = m0.get("_dbg_rank_raw", "")
                            _rank_conf = m0.get("_dbg_rank_conf", "?")
                            _rank_dc = m0.get("_dbg_rank_digit_count", "?")
                            _diag_line = (
                                f"[OCRv2] champ={_ch_ms}ms(cache={_ch_cache}) "
                                f"pts:{_pts_route}={_pts_single}(conf={_pts_conf},{_pts_dc}d,raw={_pts_raw_text!r})→{_final_pts} "
                                f"anchor={_anchor_v}/{_anchor_s} "
                                f"rank:{_rk_route}(conf={_rank_conf},{_rank_dc}d,raw={_rank_raw_text!r})→{_final_rk} "
                                f"({_pts_ms}+{_rk_ms}ms)"
                            )
                            _ocr_diag_log(_diag_line)

                            # 단계별 timing 진단 — 정체 단계 식별용
                            _ocr_end_ms = int(time.time() * 1000)
                            _await_ms = _AWAIT_ENTER_MS or 0  # settle/probe 진입 시각
                            _t_routine = (_await_ms - _ROUTINE_START_MS) if (_ROUTINE_START_MS and _await_ms) else -1
                            _t_settle = (_HOME_HIT_MS - _await_ms) if (_await_ms and _HOME_HIT_MS) else -1
                            _t1 = _HOME_HIT_MS - _ROUTINE_START_MS if _ROUTINE_START_MS else -1
                            _t2 = _OCR_START_MS - _HOME_HIT_MS if _HOME_HIT_MS else -1
                            _t3 = _ocr_end_ms - _OCR_START_MS if _OCR_START_MS else -1
                            _t_total = _ocr_end_ms - _ROUTINE_START_MS if _ROUTINE_START_MS else -1
                            _timing_line = (
                                f"[Timing] start→await={_t_routine}ms "
                                f"await→home_hit={_t_settle}ms "
                                f"home_hit→ocr_start={_t2}ms "
                                f"ocr={_t3}ms total={_t_total}ms"
                            )
                            _ocr_diag_log(_timing_line)
                        except Exception:
                            pass

                        # [v2 단축경로] 챔피언스 티어가 아니면 재시도 무의미.
                        # 점수/등수 화면 자체가 안 뜨므로 즉시 미달 처리하고 종료.
                        not_champ = (m0.get("rank_flag") == "NOT_CHAMPIONS")

                        # 정책 판단/조기종료 동일 처리
                        if m0.get("rank_flag") == "OUT_OF_RANGE":
                            metrics = m0
                            reached = goal_policy.on_sample(m0)
                        elif not_champ:
                            # 챔피언스 아님 → 샘플 추가 의미 없음
                            metrics = m0
                            _raw = str(m0.get("champ_raw", "") or "")
                            _raw_clean = _raw.replace("\n", " / ").strip()
                            if len(_raw_clean) > 120:
                                _raw_clean = _raw_clean[:120] + "..."
                            _not_champ_line = f"[목표체크] 챔피언스 티어 아님 → OCR 스킵 (raw={_raw_clean!r})"
                            log_cb(_not_champ_line)
                            _ocr_diag_log(_not_champ_line)
                        else:
                            sane0 = _is_sane_metrics(m0)
                            if sane0:
                                samples_ok.append(m0)
                                ok0 = goal_policy.on_sample(m0)
                                if not ok0:
                                    ok0 = _is_goal_met_by_settings(m0)
                                if ok0:
                                    reached = True
                                    metrics = m0

                        # === 추가 샘플은 m0가 의심스러울 때만 ===
                        # m0 신뢰 판정:
                        #   - NUMERIC + sane → 모든 가드(sanity/anchor/conf/boundary)를
                        #     이미 통과한 결과이므로 confirm 불필요. 즉시 종료.
                        #   - OUT_OF_RANGE / NOT_CHAMPIONS → 이미 위에서 처리됨 (reached 결정).
                        #   - OCR_UNCERTAIN 또는 sane fail → 추가 샘플로 재시도.
                        attempts = 1  # m0를 첫 샘플로 이미 사용
                        m0_trusted = (
                            m0.get("rank_flag") == "NUMERIC"
                            and _is_sane_metrics(m0)
                        )
                        _local_confirm = _GOAL_CONFIRM_SAMPLES

                        if not reached and not not_champ and not m0_trusted:
                            # 추가 샘플은 새 프레임 캡처가 필요하므로 GUI 를 다시 숨긴다.
                            # (옵션 3 에서 OCR 시작 직전 GUI 를 복구해놓은 상태)
                            get_state_store().set_ocr_sampling_active(True)
                            try:
                                _wait_gui_hidden_for_ocr(max_wait_ms=250)
                            except Exception:
                                pass
                            _div = max(1, _local_confirm - 1)
                            _spacing_ms = int(max(0, _CONFIRM_WINDOW_MS) / _div)

                            while attempts < _local_confirm:
                                attempts += 1
                                is_last_try = (attempts == _GOAL_CONFIRM_SAMPLES)

                                # [FIX] 매 재시도마다 새 프레임 캡처 (기존: 첫 재시도가 m0와 동일 프레임)
                                time.sleep(max(0, _spacing_ms) / 1000.0)
                                frame_bgr = _capture_frame_bgr_like_gui()
                                m = _read_metrics_from_current_home(frame_bgr)
                                m = _coerce_metric_values(m or {})

                                if m.get("rank_flag") == "OUT_OF_RANGE":
                                    metrics = m
                                    reached = goal_policy.on_sample(m)
                                    break

                                sane = _is_sane_metrics(m)
                                if sane:
                                    samples_ok.append(m)
                                    ok = goal_policy.on_sample(m)
                                    if not ok:
                                        ok = _is_goal_met_by_settings(m)
                                    if ok:
                                        reached = True
                                        metrics = m
                                        break

                        # 샘플 후처리: 확정 못했지만 유효 샘플이 있다면 융합해서 한 번 더 판단
                        if not reached and samples_ok:
                            fused = _fuse_metrics(samples_ok)
                            fused = _coerce_metric_values(fused)  # ★ 융합 결과 정규화
                            metrics = fused  # 로그용
                            reached = goal_policy.on_sample(fused)
                            if not reached:
                                reached = _is_goal_met_by_settings(fused)  # ← 로컬 가드

                        # anchor outlier 카운터 갱신 — 사이클 끝 시점에서 한 번만
                        try:
                            _outlier_seen = False
                            for _src in (m0,
                                         locals().get("fused"),
                                         locals().get("metrics")):
                                if isinstance(_src, dict) and _src.get("anchor_outlier"):
                                    _outlier_seen = True
                                    break
                            if _outlier_seen:
                                globals()["_ANCHOR_OUTLIER_COUNTER"] = _ANCHOR_OUTLIER_COUNTER + 1
                                # 상한 도달 시 anchor reset → 부트스트랩 재시작
                                if _ANCHOR_OUTLIER_COUNTER > _ANCHOR_OUTLIER_COUNTER_MAX:
                                    try:
                                        SCORE_ANCHOR().reset()
                                    except Exception:
                                        pass
                                    globals()["_ANCHOR_OUTLIER_COUNTER"] = 1
                        except Exception:
                            pass

                        # 미달 요약 로그
                        if not reached:
                            try:
                                if 'metrics' in locals() and metrics is not None:
                                    _miss_line = _fmt_goal_line(metrics, "목표미달")
                                elif ('samples_ok' in locals()) and samples_ok:
                                    _miss_line = _fmt_goal_line(samples_ok[-1], "목표미달")
                                else:
                                    # 점수/등수 둘 다 없음 — 목표 정보만이라도 표시
                                    _miss_line = _fmt_goal_line({}, "목표미달")
                                log_cb(_miss_line)
                                _ocr_diag_log(_miss_line)
                            except Exception:
                                pass

                        # ── UNCERTAIN streak 검사 및 알림 ──
                        # "valid OCR sample 없음" 조건과 동일: 챔피언스 화면이지만 OCR이 흔들려
                        # 점수/등수를 못 얻은 경우. 시즌말 stop 누락 방지용 알림.
                        try:
                            _is_uncertain_cycle = (
                                (not reached)
                                and (not samples_ok)
                                and (not not_champ)
                                and (m0.get("rank_flag") != "OUT_OF_RANGE")
                            )
                            if _is_uncertain_cycle:
                                _UNCERTAIN_STREAK += 1
                                _u_thresh = int(SETTINGS().get("ocr.uncertain_alert_threshold", 3))
                                _u_cool_min = float(SETTINGS().get("ocr.uncertain_alert_cooldown_min", 30))
                                _now_ms = int(time.time() * 1000)
                                _cool_ms = int(_u_cool_min * 60 * 1000)
                                if (_UNCERTAIN_STREAK >= _u_thresh
                                        and (_now_ms - _LAST_UNCERTAIN_ALERT_MS) >= _cool_ms):
                                    try:
                                        _anchor_val = SCORE_ANCHOR().get_any()
                                    except Exception:
                                        _anchor_val = None
                                    _payload = {
                                        "streak": _UNCERTAIN_STREAK,
                                        "raw": str(m0.get("champ_raw", ""))[:120],
                                        "reason": str(m0.get("reason", "")),
                                        "anchor": _anchor_val,
                                    }
                                    _email_guarded("goal_uncertain", _payload)
                                    _uncertain_line = f"[목표체크] OCR_UNCERTAIN {_UNCERTAIN_STREAK}회 연속 — 알림 발송"
                                    log_cb(_uncertain_line)
                                    _ocr_diag_log(_uncertain_line)
                                    _LAST_UNCERTAIN_ALERT_MS = _now_ms
                            else:
                                # 정상 사이클 (NOT_CHAMPIONS/OUT_OF_RANGE/sample 있음/달성) → 리셋
                                if _UNCERTAIN_STREAK > 0:
                                    _reset_line = f"[목표체크] UNCERTAIN streak 리셋 (이전={_UNCERTAIN_STREAK})"
                                    log_cb(_reset_line)
                                    _ocr_diag_log(_reset_line)
                                _UNCERTAIN_STREAK = 0
                        except Exception as _e:
                            try:
                                log_cb(f"[목표체크] uncertain check error: {_e}")
                            except Exception:
                                pass

                        # 잠금 해제
                        get_state_store().set_ocr_sampling_active(False)

                        if reached:
                            _achieve_line = _fmt_goal_line(metrics or {}, "목표달성")
                            log_cb(_achieve_line)
                            _ocr_diag_log(_achieve_line)
                            _email_guarded("goal_achieved", {"points": metrics.get("points"), "rank": metrics.get("rank")})

                            # 1) 러너 쪽에 명시적 종료 신호
                            try:
                                stop_event.set()
                            except Exception:
                                pass
                            # 2) 내부 상태 리셋(다음 싸이클을 '정상 IDLE'로 만들기)
                            probe = None
                            _AWAIT_ENTER_MS = None
                            _AWAIT_SETTLE_MS = 1200
                            get_state_store().set_ocr_sampling_active(False)
                            state_cb("IDLE")
                            break

                        # --- [AutoLearn] 지표 기록 및 파라미터 갱신 ---
                        try:
                            now_ms = int(time.time() * 1000)
                            if t_trigger_ms is not None:
                                dt_ms = max(0, now_ms - int(t_trigger_ms))
                                _AUTOLEARN.record_home_latency(dt_ms)
                            # attempts 변수는 위 OCR 루프에서 증가(1~3)
                            _AUTOLEARN.record_ocr_attempts(attempts)

                            # 새 파라미터 산출 → 다음 사이클부터 사용
                            new_settle, new_confirm = _AUTOLEARN.compute_params()
                            # state_cb에는 다음 AWAIT_HOME에서 반영되고,
                            # OCR 재시도 간격은 전역으로 갱신해 즉시 반영 가능
                            _CONFIRM_WINDOW_MS = int(new_confirm)
                        except Exception as e:
                            log_cb(f"[AutoLearn] update skipped: {e}")

                        # 여기서 HOME을 유지하며 추가 샘플링하고 싶으면 위 OCR/판정을 주기적으로 반복
                        # 최소 통합: 1샷 판정 후 종료/이탈 처리
                        _HOME_OCR_DONE = True  # ← 이번 '홈 체류'에서 OCR 완료 표시 (달성/미달 모두 공통)
                        goal_policy.on_home_exit()  # 홈 이탈 훅(버퍼 정리) :contentReference[oaicite:12]{index=12}
                        probe = None
                        # [ADD] settle 가드 리셋
                        _AWAIT_ENTER_MS = None
                        _AWAIT_SETTLE_MS = 1200

                        _home_ff_limiter_reset()

                    elif res == "miss":
                        # [DEBUG] Home화면 인식 실패
                        try:
                            log_cb("[HOME] miss → probe reset")
                            print("[HOME] miss → probe reset")
                        except Exception:
                            pass
                        # HOME 확정 실패 → 캐시 리셋
                        _CACHED_ROI = None
                        _HOME_OCR_DONE = False
                        # [CHANGE] 풀프레임 강제는 세션 누적 2회까지만 허용
                        if _HOME_FF_TRIES < 2:
                            _SKIP_FILE_ROI_ONCE = True
                        else:
                            _SKIP_FILE_ROI_ONCE = False
                        probe = None
                        # [ADD]
                        _AWAIT_ENTER_MS = None
                        _AWAIT_SETTLE_MS = 1200

        if is_crashed:
            keep_awake()
            time.sleep(3)
            if stop_event.is_set():
                log_cb("절전방지 종료, 초기화면 복귀")
                is_crashed = False

        time.sleep(0.1)

    clean_exit()


def _log_throttled(log_cb, key: str, text: str, min_interval_sec: float):
    """
    key별로 min_interval_sec 안에는 중복 로그를 억제.
    """
    now = time.time()
    last = _LOG_LAST_TS.get(key, 0.0)
    if now - last >= min_interval_sec:
        _LOG_LAST_TS[key] = now
        try:
            log_cb(text)
        except Exception:
            pass


def _capture_frame_bgr(region: tuple[int,int,int,int] | None = None):
    """
    [동일화] GUI 테스트 뷰와 같은 캡처 파이프라인 적용:
      - shot.rgb → (H,W,3) → RGB→BGR 변환
      - copy() 후 연속(contiguous)/쓰기 가능 보장
    """
    with mss.mss() as sct:
        if region is None:
            mon = sct.monitors[0]  # 전체 화면
            bbox = {"top": mon["top"], "left": mon["left"], "width": mon["width"], "height": mon["height"]}
        else:
            x, y, w, h = region
            bbox = {"top": y, "left": x, "width": w, "height": h}

        shot = sct.grab(bbox)

        # 1) RGB 바이트 버퍼 → (H, W, 3) ndarray
        rgb = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)

        # 2) RGB→BGR + copy() (테스트 뷰와 동일)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR).copy()

        # 3) 방어적 보장: contiguous & writeable
        if (not bgr.flags["C_CONTIGUOUS"]) or (not bgr.flags["WRITEABLE"]):
            bgr = np.ascontiguousarray(bgr)
            bgr.setflags(write=1)

        return bgr


def _screenshot_pil(region: tuple[int,int,int,int] | None = None) -> Image.Image:
    """
    region=(x,y,w,h) 기준으로 mss로 빠르게 캡처해 PIL.Image(RGB) 반환.
    region=None이면 풀스크린.
    """
    with mss.mss() as sct:
        if region is None:
            mon = sct.monitors[0]  # 전체화면
            bbox = {"top": mon["top"], "left": mon["left"], "width": mon["width"], "height": mon["height"]}
        else:
            x, y, w, h = region
            bbox = {"top": y, "left": x, "width": w, "height": h}
        shot = sct.grab(bbox)  # BGRA
        img = Image.frombytes("RGB", shot.size, shot.rgb)  # RGB로 즉시 변환
        return img


# ======== [DEBUG OCR DUMP HELPERS] ========
def _safe_imwrite(path: str, img: np.ndarray) -> bool:
    """
    cv2.imwrite가 유니코드 경로에서 실패할 수 있어 Pillow로 폴백.
    True=성공, False=실패.
    """
    try:
        ok = cv2.imwrite(path, img)
        if ok:
            return True
    except Exception:
        pass
    # Pillow 폴백
    try:
        from PIL import Image
        if img.ndim == 2:
            im = Image.fromarray(img)
        else:
            # BGR -> RGB
            im = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        im.save(path)
        return True
    except Exception:
        return False


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def _preprocess_and_binarize(bgr: np.ndarray,
                             *, invert: bool = True,
                             bin_thresh: int = 170,
                             open_ksize: int = 1,
                             dilate_ksize: int = 0) -> np.ndarray:
    """
    metrics_reader._adaptive_contrast + threshold 흐름을 로컬 복제.
    """
    if bgr is None or bgr.size == 0:
        return np.zeros((1,1), dtype=np.uint8)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    # 대비 증강 (normalize + CLAHE)
    g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    g = clahe.apply(g)
    # 이진화
    thtype = cv2.THRESH_BINARY_INV if invert else cv2.THRESH_BINARY
    _, binimg = cv2.threshold(g, bin_thresh, 255, thtype)
    # 모폴로지
    if open_ksize > 0:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_ksize, open_ksize))
        binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, k, iterations=1)
    if dilate_ksize > 0:
        k2 = cv2.getStructuringElement(cv2.MORPH_RECT, (dilate_ksize, dilate_ksize))
        binimg = cv2.dilate(binimg, k2, iterations=1)
    return binimg


def _crop_xywh(frame: np.ndarray, rc: tuple[int,int,int,int]) -> np.ndarray:
    x,y,w,h = [int(v) for v in rc]
    H,W = frame.shape[:2]
    x = max(0, min(x, W-1)); y = max(0, min(y, H-1))
    w = max(1, min(w, W - x)); h = max(1, min(h, H - y))
    return frame[y:y+h, x:x+w].copy()


def _dump_ocr_debug(frame_bgr: np.ndarray, settings_mgr, log_cb, *, tag: str = "") -> None:
    """
    OCR 직전 프레임에서 ROI 시각화 및 크롭/이진 덤프.
    - full: 전체 프레임에 points(초록), rank(파랑) 박스 + 좌표 라벨
    - raw:  points, rank 크롭
    - bin:  points, rank 이진화 결과
    """
    try:
        H, W = frame_bgr.shape[:2]
        rois_xywh, base = read_rois_xywh_from_settings(settings_mgr)
        if not rois_xywh:
            log_cb("[DEBUG] settings.ocr ROI 미존재 → 덤프 스킵")
            return

        pts = rois_xywh.get("roi_points")
        rnk = rois_xywh.get("roi_rank")
        # 현재 프레임 해상도로 스케일
        cur = (W, H)
        if pts: pts = scale_xywh_from_base(pts, base, cur)
        if rnk: rnk = scale_xywh_from_base(rnk, base, cur)

        debug_dir = os.path.join(BASE_DIR, "data", "debug")
        _ensure_dir(debug_dir)
        ts = int(time.time() * 1000)
        if tag:
            tag = f"_{tag}"

        # 1) full frame with boxes
        full = frame_bgr.copy()
        if pts:
            x,y,w,h = pts
            cv2.rectangle(full, (x,y), (x+w, y+h), (0,255,0), 2)  # green
            cv2.putText(full, f"PTS {pts}", (x, max(0,y-6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
        if rnk:
            x,y,w,h = rnk
            cv2.rectangle(full, (x,y), (x+w, y+h), (255,128,0), 2)  # orange-ish (BGR)
            cv2.putText(full, f"RANK {rnk}", (x, min(H-2, y+h+14)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,128,0), 1)
        full_path = os.path.join(debug_dir, f"debug_home_full{tag}_{ts}.png")
        ok_full = _safe_imwrite(full_path, full)
        log_cb(f"[DEBUG] FULL:{full_path} saved={ok_full}")
        cv2.imwrite(full_path, full)

        # 2) crops + bin
        def dump_one(rc: tuple[int,int,int,int] | None, name: str):
            if not rc: return None
            raw = _crop_xywh(frame_bgr, rc)
            binimg = _preprocess_and_binarize(raw, invert=True, bin_thresh=170, open_ksize=1, dilate_ksize=0)
            raw_path = os.path.join(debug_dir, f"debug_{name}_raw{tag}_{ts}.png")
            bin_path = os.path.join(debug_dir, f"debug_{name}_bin{tag}_{ts}.png")
            cv2.imwrite(raw_path, raw)
            cv2.imwrite(bin_path, binimg)
            # 통계 로그
            var = float(np.var(raw)) if raw.size else -1.0
            white_ratio = float(np.mean(binimg) / 255.0) if binimg.size else -1.0
            ok_raw = _safe_imwrite(raw_path, raw)
            ok_bin = _safe_imwrite(bin_path, binimg)
            log_cb(
                f"[DEBUG] {name} rc={rc} area={rc[2] * rc[3]} var={var:.2f} white_ratio={white_ratio:.3f} -> raw:{raw_path} saved={ok_raw} bin:{bin_path} saved={ok_bin}")

        dump_one(pts, "points")
        dump_one(rnk, "rank")

        # 좌표 자체 로그
        log_cb(f"[DEBUG] FULL:{full_path}")
    except Exception as e:
        try:
            log_cb(f"[DEBUG] dump 실패: {e}")
        except Exception:
            pass
# ======== [/DEBUG OCR DUMP HELPERS] ========


def _match_home_anchor(frame_bgr, _roi_ignored):
    """
    파일 캐시 ROI가 있으면 반드시 그 영역만 매칭(풀프레임 금지).
    파일 캐시가 없을 때만 '최초 1회' 풀프레임 허용.
    임계(히트/미스) 판정은 HomeProbe에서만 수행한다.
    """
    global _HOME_SCREEN_WH, _CACHED_ROI, _SKIP_FILE_ROI_ONCE, _HOME_FF_TRIES
    tpl = home_tpl_bgr

    # 화면 크기 기록 + 파일 캐시 1회 로드
    if _HOME_SCREEN_WH is None:
        _HOME_SCREEN_WH = (int(frame_bgr.shape[1]), int(frame_bgr.shape[0]))  # (w,h)

    # miss 직후 1회에 한해 풀프레임 스캔 강제
    load_file_roi = (not _SKIP_FILE_ROI_ONCE)

    if _CACHED_ROI is None and _HOME_TPL_PATH is not None and load_file_roi:
        loaded = roi_load(_HOME_TPL_PATH, _HOME_SCREEN_WH, auto_scale=True)
        if loaded and len(loaded) == 4:
            _CACHED_ROI = (int(loaded[0]), int(loaded[1]), int(loaded[2]), int(loaded[3]))

    # ROI 선택: 파일 캐시 우선
    if _CACHED_ROI is not None:
        src = safe_crop(frame_bgr, _CACHED_ROI)
        roi_used = _CACHED_ROI
    else:
        # [ADD] 세션 내 풀프레임 최대 2회
        if _HOME_FF_TRIES >= 2:
            # 더 이상 풀프레임 불가 → 점수 0으로 빠르게 미스 유도
            h, w = tpl.shape[:2]
            bbox = (0, 0, int(w), int(h))
            return True, 0.0, bbox  # HomeProbe가 임계 미달로 miss 처리
        # 여기서만 실제 풀프레임 수행
        src = frame_bgr
        roi_used = None
        _HOME_FF_TRIES += 1
        # 이유: miss 직후 강제 풀프레임이면 'miss', 초회 등 파일 ROI 부재로 인한 풀프레임이면 'initial'
        _reason = "miss" if _SKIP_FILE_ROI_ONCE else "initial"  # ← 추가
        _log_home_ff("allowed", _HOME_FF_TRIES, 2, _reason)  # ← 추가
        if _SKIP_FILE_ROI_ONCE:
            _SKIP_FILE_ROI_ONCE = False

    # src가 tpl보다 작으면 matchTemplate 단언 실패 → 캐시 무효화 후 풀프레임으로 폴백
    if src.shape[0] < tpl.shape[0] or src.shape[1] < tpl.shape[1]:
        _CACHED_ROI = None
        src = frame_bgr
    res = cv2.matchTemplate(src, tpl, cv2.TM_CCOEFF_NORMED)
    _, maxVal, _, maxLoc = cv2.minMaxLoc(res)

    h, w = tpl.shape[:2]
    top_left = maxLoc
    if roi_used is not None:
        top_left = (top_left[0] + roi_used[0], top_left[1] + roi_used[1])
    bbox = (int(top_left[0]), int(top_left[1]), int(w), int(h))

    # 임계 판단은 HomeProbe 전담. 여기서는 score/bbox만 반환.
    hit = True
    return hit, float(maxVal), bbox


# --- 메인 실행 파트 ---
if os.path.exists("routine.lock"):
    if check_stale_lock():
        messagebox.showwarning(
            "실행 차단됨",
            "루틴 설정 프로그램(config)이 실행 중입니다."
        )
        sys.exit()

create_lock()
init_resources()
routine_items, client_item = load_routine_from_json()
if not routine_items:
    root = tk.Tk(); root.withdraw()
    messagebox.showwarning("루틴 없음", "루틴이 존재하지 않습니다.\nConfig.exe에서 루틴을 설정하세요.")
    root.destroy()
    clean_exit()

# Home 템플릿 이미지 경로 확보 (포인터: 프로젝트 루트 home_anchor.json)
home_img_path = get_home_anchor_template(pm.get_img_path(), routine_items)

# [NEW] 목표달성 모드 사용 가능 여부 플래그
GOAL_AVAILABLE = True
home_tpl_bgr = None

# 영구 ROI 캐시용 템플릿 경로 기록
_HOME_TPL_PATH = home_img_path

if not home_img_path:
    GOAL_AVAILABLE = False
else:
    home_tpl_bgr = cv2.imread(home_img_path, cv2.IMREAD_COLOR)
    if home_tpl_bgr is None:
        GOAL_AVAILABLE = False

# 여기서 GUI 버전은 RunController(main.routine_loop)를 실행하게 된다.
