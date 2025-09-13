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
from pathlib import Path
from core.settings_manager import SettingsManager
from core.goal_policy import GoalPolicy, build_goal_policy
import cv2, numpy as np
from typing import Dict, Set, Tuple, Optional
from core.home_probe import HomeProbe, get_home_anchor_template  # 홈 확인 루틴/헬퍼
from ocr.metrics_reader import read_metrics_from_home, HomeOCRConfig  # 홈 OCR :contentReference[oaicite:2]{index=2}
from core.state_store import get_state_store  # OCR 중 UI잠금 플래그 :contentReference[oaicite:3]{index=3}
from core.utils_anchor import load_home_anchor_threshold
from core.image_utils import safe_crop
from core.roi_cache import load_roi as roi_load, commit_roi as roi_commit
from core.freeze_monitor import FreezeMonitor
from core.autolearn import AutoLearnStore
from core.ocr import _match_out_of_range_ko as _is_oor_ko
from core.score_calib import load_scaled_rois_for_current_screen
from core.roi_from_settings import (
    run_home_ocr_like_gui,                # GUI와 동일 OCR 실행
    read_rois_xywh_from_settings,         # ← ROI (x,y,w,h) 그대로 읽기
    scale_xywh_from_base                  # ← (x,y,w,h) 스케일 함수
)  # ← 신규 모듈


width, height = pgi.size()  # 화면해상도 확인

# ★ 성능/주기 설정(없으면 안전 기본값)
# ★ settings.json SSOT (Single Source Of Truth)
SETTINGS = SettingsManager(SETTINGS_JSON)  # pm.DATA_DIR 하의 공식 경로
_LOOP_INTERVAL_SEC = float(SETTINGS.get("perf.loop_interval_sec", 0.45))   # 루틴 호출 간격
_IDLE_SLEEP_SEC = float(SETTINGS.get("perf.idle_sleep_sec", 0.016))      # 내부 idle 슬립
_AWAIT_STEP_MIN_MS = int(SETTINGS.get("perf.await_step_min_ms", 140))      # AWAIT 프레임 간격

pm.chdir_to_base()  # ★ 시작 즉시

img_path = pm.get_img_path()
res_path = pm.get_res_path()

is_crashed = False

# OCR ROI 설정: settings.ocr.* 사용 (없으면 안전값)
roi_rank = SETTINGS.get("ocr.roi_rank")
# 구버전 키 호환: roi_score → roi_points
roi_points = SETTINGS.get("ocr.roi_points", SETTINGS.get("ocr.roi_score"))

# --- OCR base screen size (settings에 저장된 기준 해상도) ---
try:
    _OCR_BASE_WH = None
    _bw = SETTINGS.get("ocr.screen.w")
    _bh = SETTINGS.get("ocr.screen.h")
    if _bw and _bh:
        _OCR_BASE_WH = (int(_bw), int(_bh))
except Exception:
    _OCR_BASE_WH = None


_PLACEHOLDER_EMAIL = "example@gmail.com"
_PLACEHOLDER_PW = "password"


# [추가] 프로젝트 OCR 모듈에 테서랙트 경로 주입(있을 때만)
try:
    from core import ocr as _ocr_core
    _ocr_core.set_tesseract_path(SETTINGS.get("ocr.tesseract_path", ""))
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
_MAX_ROI_CANDIDATES = int(SETTINGS.get("routine.roi_max_candidates", 3))
# 루틴 ROI 여유 마진(px) — settings 없으면 기본 12px
_ROUTINE_ROI_MARGIN: int = int(SETTINGS.get("routine.roi_margin", 12))
# [ADD] AWAIT_HOME 세션 내 풀프레임 시도 누적(최대 2)
_HOME_FF_TRIES: int = 0
# --- 로그 레이트 리밋(공용) ---
_LOG_LAST_TS = {}
# --- AutoLearn 전역 ---
_AUTOLEARN = AutoLearnStore()
_CONFIRM_WINDOW_MS = 350  # 초기값(자동 튜닝으로 갱신)
# --- AWAIT_HOME settle 하드가드 ---
_AWAIT_ENTER_MS: Optional[int] = None   # AWAIT_HOME 진입 시각(ms)
_AWAIT_SETTLE_MS: int = 1200            # 현재 사이클 settle 목표(ms)
# [추가] 파일 상단 전역(다른 전역들과 같은 레벨)
_RUNTIME_LOG_CB = None
# --- [ADD] 홈-OCR 제어 플래그 ---
_WANT_AWAIT_HOME: bool = False   # 루틴 단락이 실제로 발생했을 때만 AWAIT_HOME 시작
_HOME_OCR_DONE: bool = False     # '현재 홈 체류'에서 OCR을 1회 수행했는가


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
def _wait_gui_hidden_for_ocr(max_wait_ms: int = 450) -> None:
    """
    GUI가 ocr_sampling_active=True를 감지하여 withdraw()될 시간을 짧게 확보한다.
    - 폴러 주기(≈200ms)를 감안해 기본 450ms 대기(최소치 보장).
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


def _log(msg: str) -> None:
    cb = globals().get("_RUNTIME_LOG_CB")
    try:
        (cb or print)(msg)
    except Exception:
        print(msg)


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
    cfg = SETTINGS.get("routine.roi_fallback", {}) or {}
    miss_th = int(cfg.get("miss_threshold", _DEFAULT_MISS_THRESHOLD))
    cooldown = int(cfg.get("cooldown_loops", _DEFAULT_COOLDOWN_LOOPS))

    return (miss_th, cooldown)


def _get_roi_margin():
    """settings.routine.roi_margin → int (기본 80)"""
    return int(SETTINGS.get("routine.roi_margin", _ROUTINE_ROI_MARGIN))


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
        pyd.keyDown(key); time.sleep(0.05); pyd.keyUp(key)
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
        band = int(SETTINGS.get("client.taskbar_band_px", 120))
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
def _email_guarded(ev_key: str, payload: dict | None = None) -> bool:
    """
    전역 ON + 개별 ON일 때만 메일 발송.
    저장창에서 설정이 바뀐 직후에도 최신값을 보도록, 읽기 전에 reload.
    """
    try:
        # ★ 최신 settings 반영
        try:
            SETTINGS.load()  # 디스크 → 메모리 재적재
        except Exception:
            pass

        cfg = SETTINGS.get("email", {}) or {}
        if not cfg.get("enabled", False):
            return False

        evs = (cfg.get("events") or {})
        ev_on = bool((evs.get(ev_key) or {}).get("enabled", False))
        if not ev_on:
            return False

        autoemail.send_email(event=ev_key, payload=payload or {})
        return True
    except Exception as e:
        try:
            print(f"[email] '{ev_key}' 발송 실패/스킵: {e}")
        except Exception:
            pass
        return False


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
    global _HOME_OCR_DONE, _WANT_AWAIT_HOME
    for item in routine_list:
        img_file = img_path + item["image"]
        action = item["action"]
        conf = item.get("conf", 0.8)
        # execute_routine
        if action in ("click", "space", "s", "esc"):
            hit, center = _routine_locate_adaptive(img_file, conf)
            if hit:
                # --- [ADD] 홈 앵커 단락: 홈 템플릿이면 입력(클릭/키) 스킵 → 화면 전환 방지
                # 바깥 루프가 직후 AWAIT_HOME(홈 OCR)을 기존 로직대로 시작한다.
                # 이번 '홈 체류'에서 아직 OCR을 하지 않았을 때만 단락
                if _is_home_anchor(img_file):
                    if not _HOME_OCR_DONE:
                        _WANT_AWAIT_HOME = True  # ← 이번 사이클에서만 AWAIT_HOME 허용
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
                    _WANT_AWAIT_HOME = True  # ← 이번 사이클에서만 AWAIT_HOME 허용
                    return
                else:
                    _HOME_OCR_DONE = False
            keys = [k.strip() for k in action.split('+')]
            try:
                for k in keys: pyd.keyDown(k)
                time.sleep(0.05)
                for k in reversed(keys): pyd.keyUp(k)
                # _log(f"[action] combo '{action}' img='{os.path.basename(img_file)}'")
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


# debug 시작
def _read_metrics_from_current_home(frame_bgr):
    """
    GUI의 OCR 테스트(_open_verify)와 동일 파이프라인으로 읽는다.
    - 프리뷰 폭 한계(최대 1200, 또는 화면폭-120 중 작은 값)로 다운스케일
    - 저장 당시 스크린 크기(ocr.screen.w/h) → 현재 프리뷰 크기로 ROI 스케일
    - 같은 다운스케일 프레임에 대해 OCR 엔진 폴백 체인을 실행
    """
    # 이 함수는 core.roi_from_settings.run_home_ocr_like_gui()가
    # 위 3단계를 모두 내부에서 수행하도록 통일돼 있다.
    return run_home_ocr_like_gui(frame_bgr, SETTINGS)
# debug 끝


def _is_goal_met_by_settings(m: dict) -> bool:
    """
    settings.json의 goal 프리셋을 직접 읽어 'rank 전용' 판정 가드.
    - mode == "rank"  : rank <= (rank_target + rank_tolerance)
    - mode == "points": points >= (points_target - points_margin)
    - 그 외/누락       : False
    """
    try:
        g = SETTINGS.get("goal", {}) or {}
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
    # Goal 정책 초기화 (현재 settings.json 기준)
    goal_policy = build_goal_policy(SETTINGS)
    GOAL_ENABLED = bool(SETTINGS.get("goal.enabled", False))  # [NEW] 스냅샷

    # [ADD] OCR 확정샘플 파라미터(설정값 적용)
    _GOAL_CONFIRM_SAMPLES = int(SETTINGS.get("goal.confirm_samples", 3))
    _GOAL_CONFIRM_WINDOW_MS = int(SETTINGS.get("goal.confirm_window_ms", 1200))
    # ★ 전처리 폴백 스위치(기본 False = raw-only)
    _USE_PREPROCESS_FALLBACK = bool(SETTINGS.get("ocr.use_preprocess_fallback", False))

    # 전역 간격 변수 초기화(초깃값 350 → 설정값으로 덮어씀)
    global _HOME_TPL_PATH, _CACHED_ROI, _SKIP_FILE_ROI_ONCE, _CONFIRM_WINDOW_MS, _AWAIT_ENTER_MS, _AWAIT_SETTLE_MS
    global _RUNTIME_LOG_CB, _WANT_AWAIT_HOME, _HOME_OCR_DONE
    _CONFIRM_WINDOW_MS = _GOAL_CONFIRM_WINDOW_MS
    _RUNTIME_LOG_CB = log_cb

    # --- [NEW] 초기 autoemail 설정 주입 ---
    email_cfg = SETTINGS.get("email", {})
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
    freeze = None  # type: Optional[FreezeMonitor]
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
            state_cb("RUNNING")

            while not stop_event.is_set() and not stop_event_global.is_set():
                now = time.time()

                # ★ UI 상호작용(창 이동/리사이즈) 중에는 무거운 작업을 잠깐 쉰다
                try:
                    if store.is_ui_busy():
                        time.sleep(0.02)  # CPU 점유 완화
                        last_run = now  # 주기 타이머 리셋(불필요한 실행 방지)
                        # 프리즈틱/캡처도 아래에서 스킵됨
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
                if now - last_run >= interval:
                    # ★ 드래그 중엔 루틴 실행 자체를 건너뛰어 클릭/탬플릿매칭 비용 차단
                    try:
                        if store.is_ui_busy():
                            last_run = now
                            # busy 중에는 아래 AWAIT_HOME 쪽도 스킵
                            continue
                    except Exception:
                        pass

                    # ★★★ 홈 판정(AWAIT_HOME) 중에는 루틴 실행/새 probe 생성 모두 중단
                    if probe is not None:
                        last_run = now
                        # AWAIT_HOME 상태 유지(정착 대기/프레임 스텝은 아래 AWAIT 분기에서 처리)
                        continue

                    try:
                        execute_routine(routine_items)
                    except Exception as e:
                        log_cb(f"[routine] 예외 흡수: {e!r}")  # 스레드 살리고 다음 사이클 진행
                    last_run = now

                    # 결과 트리거 직후 AWAIT_HOME 시작
                    # main.py - 반복 루프 내 probe 생성 지점
                    if GOAL_ENABLED and GOAL_AVAILABLE and _WANT_AWAIT_HOME:
                        if probe is None:
                            _WANT_AWAIT_HOME = False  # ← 소비하고 바로 리셋
                            probe = HomeProbe(
                                ncc_threshold=load_home_anchor_threshold(),
                                soft_timeout_ms=8000,
                                hard_timeout_ms=12000
                            )
                            probe.start()
                            _home_ff_limiter_reset()
                            _log_home_ff("reset", 0, 2, "enter")
                            settle_ms = _AUTOLEARN.current_settle_ms()
                            state_cb(f"AWAIT_HOME_ENTER|SETTLE={settle_ms}")
                            _AWAIT_ENTER_MS = int(time.time() * 1000)
                            _AWAIT_SETTLE_MS = max(int(settle_ms), 1000)
                            globals()["_DEBUG_OCR_DUMPED"] = False  # ← 덤프 1회 가드 리셋
                            # --- FREEZE 모니터 시작 ---
                            if freeze is None or freeze.is_disabled:
                                freeze = FreezeMonitor.from_settings_dict(SETTINGS.get("freeze", None),
                                                                          on_trip=_on_freeze_trip)
                            freeze.activate()
                            t_trigger_ms = int(time.time() * 1000)
                    else:
                        probe = None  # Goal 미사용/불가면 AWAIT_HOME 비활성

                if probe is not None:
                    # ★ 드래그 중이면 캡처/매칭/프리즈틱 자체를 잠깐 쉰다
                    try:
                        if store.is_ui_busy():
                            time.sleep(0.01)
                            continue
                    except Exception:
                        pass

                    # [ADD] AWAIT_HOME settle 하드가드: 아직 초기 대기(ms) 미만이면 step을 건너뜀
                    if _AWAIT_ENTER_MS is not None:
                        now_ms = int(time.time() * 1000)
                        if now_ms - _AWAIT_ENTER_MS < _AWAIT_SETTLE_MS:
                            # freeze 모니터링은 계속 진행(홈이 아니므로 안전)
                            if freeze is not None and not freeze.is_disabled:
                                nowf = time.time()
                                if nowf - _frz_last_cap_ts >= freeze.cfg.interval_sec:
                                    fbgr = _capture_frame_bgr()  # 필요시 중앙부 ROI로 줄여도 OK
                                    cnt = freeze.tick(fbgr)

                                    _frz_last_cap_ts = nowf

                            time.sleep(_IDLE_SLEEP_SEC)
                            continue

                    # ★ 프레임 캡처 최소 간격 가드(기본 140ms)
                    now_ms = int(time.time() * 1000)
                    _await_last_step = globals().get("_AWAIT_LAST_STEP_MS", 0)
                    if now_ms - _await_last_step < _AWAIT_STEP_MIN_MS:
                        time.sleep(_IDLE_SLEEP_SEC)
                        continue
                    globals()["_AWAIT_LAST_STEP_MS"] = now_ms

                    frame_bgr = _capture_frame_bgr_like_gui()  # GUI와 동일 파이프라인로 캡처
                    res = probe.step(frame_bgr, _match_home_anchor, cached_home_roi)

                    # ★ 여기서도 busy면 프리즈틱 스킵
                    try:
                        if store.is_ui_busy():
                            time.sleep(0.005)
                            continue
                    except Exception:
                        pass

                    # --- 홈 아님(pending/miss)일 때만 freeze 샘플 ---
                    if res != "hit" and freeze is not None and not freeze.is_disabled:
                        nowf = time.time()
                        if nowf - _frz_last_cap_ts >= freeze.cfg.interval_sec:
                            cnt = freeze.tick(frame_bgr)  # 이미 막 찍은 frame_bgr 재사용
                            if cnt is not None:
                                _log_throttled(log_cb, "FREEZE_TICK", f"[FREEZE] 동일 화면 샘플 누적: {cnt}", 60.0)
                            _frz_last_cap_ts = nowf

                    if res == "hit":
                        if freeze is not None:
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

                        # OCR 중 UI 잠금
                        get_state_store().set_ocr_sampling_active(True)

                        # [ADD] GUI withdraw/알파트래커 정지 반영될 때까지 짧게 대기
                        # 폴러 주기(≈200ms) + 컴포지터 반영 여유
                        try:
                            _wait_gui_hidden_for_ocr(max_wait_ms=450)
                        except Exception:
                            # 대기 실패는 치명 아님 → 무시하고 진행
                            pass

                        # ---- 조건부 다중확인(최대 2회 추가) ----
                        samples_ok = []
                        attempts = 0
                        reached = False
                        last_valid = None  # [debug] 미달 로그에서 안전하게 참조

                        # 샘플 간 간격(ms) = confirm_window_ms를 (샘플수-1)로 등분 (1회면 0)
                        _div = max(1, _GOAL_CONFIRM_SAMPLES - 1)
                        _spacing_ms = int(max(0, _CONFIRM_WINDOW_MS) / _div)

                        while attempts < _GOAL_CONFIRM_SAMPLES:
                            attempts += 1

                            # 처음 N-1회는 "원본(raw) 경로"로 시도, 마지막 1회만 기존 전처리 경로로 폴백
                            is_last_try = (attempts == _GOAL_CONFIRM_SAMPLES)

                            # 기본은 항상 '원본' 파이프라인
                            m = _read_metrics_from_current_home(frame_bgr)
                            m = _coerce_metric_values(m or {})

                            # 정책 판단/조기종료 로직(기존 그대로)
                            if m.get("rank_flag") == "OUT_OF_RANGE":
                                metrics = m
                                reached = goal_policy.on_sample(m)
                                break

                            sane = _is_sane_metrics(m)
                            if sane:
                                samples_ok.append(m)
                                ok = goal_policy.on_sample(m)
                                if not ok:
                                    ok = _is_goal_met_by_settings(m)  # ← 로컬 가드: settings 프리셋 직접 판정
                                if ok:
                                    reached = True
                                    metrics = m
                                    break

                            # 다음 샷 대기 및 최신 프레임
                            if attempts < _GOAL_CONFIRM_SAMPLES:
                                _div = max(1, _GOAL_CONFIRM_SAMPLES - 1)
                                _spacing_ms = int(max(0, _CONFIRM_WINDOW_MS) / _div)
                                time.sleep(max(0, _spacing_ms) / 1000.0)
                                frame_bgr = _capture_frame_bgr_like_gui()  # GUI와 동일 파이프라인

                        # 샘플 후처리: 확정 못했지만 유효 샘플이 있다면 융합해서 한 번 더 판단
                        if not reached and samples_ok:
                            fused = _fuse_metrics(samples_ok)
                            fused = _coerce_metric_values(fused)  # ★ 융합 결과 정규화
                            metrics = fused  # 로그용
                            reached = goal_policy.on_sample(fused)
                            if not reached:
                                reached = _is_goal_met_by_settings(fused)  # ← 로컬 가드

                        # [DEBUG] 미달 요약 로그
                        if not reached:
                            # fused/last_valid/OUT_OF_RANGE 등의 상황을 최대한 정보화
                            try:
                                def _fmt_line(m: dict) -> str:
                                    if m.get("rank_flag") == "OUT_OF_RANGE":
                                        pp = m.get("points")
                                        pp_txt = f"{pp}점" if isinstance(pp, int) else "N/A"
                                        return f"점수: {pp_txt} | 등수: 순위권 이탈"
                                    r, p = m.get("rank"), m.get("points")
                                    r_txt = f"{r}등" if isinstance(r, int) else "인식 실패"
                                    p_txt = f"{p}점" if isinstance(p, int) else "인식 실패"
                                    return f"점수: {p_txt} | 등수: {r_txt}"

                                if 'metrics' in locals() and metrics is not None:
                                    line = _fmt_line(metrics)
                                    log_cb(f"[목표미달] {line}")
                                elif ('last_valid' in locals()) and (last_valid is not None):
                                    line = _fmt_line(last_valid)
                                    log_cb(f"[목표미달] {line}")
                                else:
                                    log_cb("[목표미달] valid OCR sample 없음")
                            except Exception:
                                pass

                        # 잠금 해제
                        get_state_store().set_ocr_sampling_active(False)

                        if reached:
                            log_cb(f"[목표달성] 점수={metrics.get('points')}점 | 등수={metrics.get('rank')}등")
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
