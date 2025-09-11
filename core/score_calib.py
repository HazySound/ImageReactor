# core/score_calib.py
from __future__ import annotations

import json
import os
import time
from typing import Optional, Tuple, Dict, Any, List

import cv2
import numpy as np

# --- Dependencies ---
# pyautogui: screen size / screenshot
try:
    import pyautogui as pgi
except Exception as e:
    raise RuntimeError("pyautogui import 실패: pip install pyautogui") from e

# Pillow: 한글/TTF 텍스트 렌더링
try:
    from PIL import Image, ImageDraw, ImageFont
except Exception as e:
    raise RuntimeError("Pillow import 실패: pip install pillow") from e

# 선택적 path_manager 연동 (있으면 사용, 없으면 폴백)
try:
    import path_manager as pm  # type: ignore
except Exception:
    pm = None  # type: ignore


# =========================
# 타입 / 상수
# =========================
ROI = Tuple[int, int, int, int]
Size = Tuple[int, int]

# 프리뷰 모드 설정(축소 드래그용)
PREVIEW_SCALE = 0.5          # 0.4~0.6 권장(고해상도일수록 낮춰 부하↓)
GUIDE_FONT_SIZE = 26
GUIDE_COLOR = (50, 230, 50)  # BGR
RECT_COLOR = (0, 255, 255)   # 드래그 박스 색상(BGR)
RECT_THICKNESS = 2


# =========================
# 경로 유틸
# =========================
def _project_root() -> str:
    # 현재 파일: <ROOT>/core/score_calib.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _data_path() -> str:
    """
    점수/등수 캘리브레이션 파일 경로.
    - 우선: path_manager.DATA_DIR("score_rois.json")
    - 폴백: <ROOT>/data/score_rois.json
    """
    if pm is not None and hasattr(pm, "DATA_DIR"):
        try:
            p = pm.DATA_DIR("score_rois.json") if callable(pm.DATA_DIR) else os.path.join(pm.DATA_DIR, "score_rois.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            return p
        except Exception:
            pass
    p = os.path.join(_project_root(), "data", "score_rois.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


# =========================
# 글꼴/텍스트 유틸 (한글 TrueType 사용)
# =========================
def _find_font_path() -> Optional[str]:
    candidates = [
        "C:/Windows/Fonts/malgun.ttf",                     # 맑은 고딕
        "C:/Windows/Fonts/malgunbd.ttf",                   # 맑은 고딕 Bold
        "C:/Windows/Fonts/NanumGothic.ttf",                # 나눔고딕
        os.path.join(_project_root(), "assets", "fonts", "NotoSansKR-Regular.otf"),
        os.path.join(_project_root(), "assets", "fonts", "NotoSansKR-Regular.ttf"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _draw_multiline_pil(bgr: np.ndarray, lines: List[str], origin=(40, 60), font_size=GUIDE_FONT_SIZE) -> np.ndarray:
    """
    한글 TrueType 폰트로 선명하게 텍스트를 그린다.
    폰트를 찾지 못하면 cv2.putText로 폴백(영문/숫자 권장).
    """
    font_path = _find_font_path()
    if font_path:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil_img)
        font = ImageFont.truetype(font_path, font_size)
        x, y = origin
        for line in lines:
            # PIL은 RGB, GUIDE_COLOR는 BGR이므로 순서 변환
            draw.text((x, y), line, fill=(GUIDE_COLOR[2], GUIDE_COLOR[1], GUIDE_COLOR[0]), font=font)
            y += int(font_size * 1.4)
        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    # 폰트 미발견 폴백(영문/숫자)
    out = bgr.copy()
    x, y = origin
    for line in lines:
        cv2.putText(out, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, GUIDE_COLOR, 2, cv2.LINE_AA)
        y += 40
    return out


# =========================
# 스케일/검증 유틸
# =========================
def _scale_roi(roi: ROI, from_size: Size, to_size: Size) -> ROI:
    fx = to_size[0] / float(from_size[0]) if from_size[0] else 1.0
    fy = to_size[1] / float(from_size[1]) if from_size[1] else 1.0
    x, y, w, h = roi
    sx = int(round(x * fx))
    sy = int(round(y * fy))
    sw = max(1, int(round(w * fx)))
    sh = max(1, int(round(h * fy)))
    return (sx, sy, sw, sh)


def _clamp_roi(roi: ROI, size: Size) -> ROI:
    W, H = size
    x, y, w, h = roi
    x = max(0, min(x, max(0, W - 1)))
    y = max(0, min(y, max(0, H - 1)))
    w = max(1, min(w, max(1, W - x)))
    h = max(1, min(h, max(1, H - y)))
    return (x, y, w, h)


def _is_valid_roi(roi: Optional[ROI]) -> bool:
    if not roi or len(roi) != 4:
        return False
    _, _, w, h = roi
    return (w > 0 and h > 0)


# =========================
# 저장/로드
# =========================
def save_score_rois(rank_roi: Optional[Tuple[int,int,int,int]],
                    points_roi: Optional[Tuple[int,int,int,int]],
                    screen_size: Optional[Tuple[int,int]] = None) -> None:
    """
    settings.json 의 ocr.*에 직접 쓴다.
    - rank_roi → ocr.roi_rank
    - points_roi → ocr.roi_points
    - screen_size → ocr.screen
    """
    from path_manager import SETTINGS_JSON as _SZ
    from core.settings_manager import SettingsManager
    sm = SettingsManager(_SZ)

    if rank_roi:
        sm.set("ocr.roi_rank", [int(v) for v in rank_roi])
    if points_roi:
        sm.set("ocr.roi_points", [int(v) for v in points_roi])
    if screen_size and len(screen_size) == 2:
        sw, sh = int(screen_size[0]), int(screen_size[1])
        if sw > 0 and sh > 0:
            sm.set("ocr.screen", {"w": sw, "h": sh})
    sm.save()


# === settings 기반 구현 ===
def load_score_rois() -> Optional[Dict[str, Any]]:
    """
    settings.json 의 ocr.roi_rank / ocr.roi_points 및 ocr.screen을 읽어온다.
    반환 예시:
      {"screen": {"w":2560,"h":1440}, "rank_roi": (x,y,w,h), "points_roi": (x,y,w,h)}
    """
    try:
        # settings 로드
        from path_manager import SETTINGS_JSON as _SZ
        from core.settings_manager import SettingsManager
        sm = SettingsManager(_SZ)

        ocr = sm.get("ocr", {}) or {}
        rr = ocr.get("roi_rank")
        pp = ocr.get("roi_points", ocr.get("roi_score"))
        scr = ocr.get("screen") or {}

        out: Dict[str, Any] = {}
        if isinstance(rr, (list, tuple)) and len(rr) == 4:
            out["rank_roi"] = tuple(int(v) for v in rr)
        if isinstance(pp, (list, tuple)) and len(pp) == 4:
            out["points_roi"] = tuple(int(v) for v in pp)
        if isinstance(scr, dict):
            w = int(scr.get("w") or 0); h = int(scr.get("h") or 0)
            if w > 0 and h > 0:
                out["screen"] = {"w": w, "h": h}
        return out or None
    except Exception:
        return None


# =========================
# 화면 캡처/프리뷰 (OpenCV 경량 모드)
# =========================
def _grab_screen_bgr(preview_scale: float = PREVIEW_SCALE) -> Tuple[np.ndarray, np.ndarray, Size, float]:
    """
    전체 화면 스크린샷을 받아 BGR로 반환 + 축소 프리뷰와 스케일 정보도 함께 반환.
    """
    img = np.array(pgi.screenshot())[:, :, ::-1]  # RGB → BGR
    H, W = img.shape[:2]
    scale = float(preview_scale)
    if not (0.1 <= scale <= 1.0):
        scale = 0.5
    if scale < 1.0:
        preview = cv2.resize(img, (int(W * scale), int(H * scale)), interpolation=cv2.INTER_AREA)
    else:
        preview = img.copy()
    return img, preview, (W, H), scale


class _DragSelector:
    """
    프리뷰 이미지에서 드래그 → ROI를 프리뷰 좌표계로 반환.
    마우스 이벤트 발생시에만 redraw하여 부하를 줄인다.
    """
    def __init__(self, preview_bgr: np.ndarray, window_name: str):
        self.base = preview_bgr
        self.overlay = preview_bgr.copy()
        self.wn = window_name
        self.start_pt: Optional[Tuple[int, int]] = None
        self.curr_pt: Optional[Tuple[int, int]] = None
        self.roi: Optional[ROI] = None
        self._dirty = True  # 첫 프레임 표시

    def _on_mouse(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.start_pt = (x, y)
            self.curr_pt = (x, y)
            self._dirty = True
        elif event == cv2.EVENT_MOUSEMOVE and self.start_pt is not None:
            self.curr_pt = (x, y)
            self._dirty = True
        elif event == cv2.EVENT_LBUTTONUP and self.start_pt is not None:
            self.curr_pt = (x, y)
            self._finalize()
            self._dirty = True

    def _redraw(self):
        img = self.base.copy()
        if self.start_pt and self.curr_pt:
            x0, y0 = self.start_pt
            x1, y1 = self.curr_pt
            x, y = min(x0, x1), min(y0, y1)
            w, h = abs(x1 - x0), abs(y1 - y0)
            cv2.rectangle(img, (x, y), (x + w, y + h), RECT_COLOR, RECT_THICKNESS)
        self.overlay = img
        self._dirty = False

    def _finalize(self):
        if not (self.start_pt and self.curr_pt):
            self.roi = None
            return
        x0, y0 = self.start_pt
        x1, y1 = self.curr_pt
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        self.roi = (x, y, max(1, w), max(1, h))

    def select(self) -> Optional[ROI]:
        cv2.namedWindow(self.wn, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.wn, self._on_mouse)
        while True:
            if self._dirty:
                self._redraw()
                cv2.imshow(self.wn, self.overlay)
            key = cv2.waitKey(16) & 0xFF  # ~60fps
            if key in (13, 32):   # ENTER / SPACE
                return self.roi if _is_valid_roi(self.roi) else None
            elif key in (27,):    # ESC
                return None
            elif key in (ord('r'), ord('R')):
                self.start_pt = None
                self.curr_pt = None
                self.roi = None
                self._dirty = True


def calibrate_score_rois(interactive_save: bool = True) -> Optional[Dict[str, Any]]:
    """
    (경량 OpenCV 프리뷰 버전)
    순서: rank → points 두 번 드래그 받아 score_rois.json 저장.
    프리뷰(축소본)에서 드래그하고, 좌표는 원본 화면 크기로 환산하여 저장.
    """
    full_bgr, preview_bgr, screen_size, scale = _grab_screen_bgr(PREVIEW_SCALE)

    # 안내 텍스트(PIL: 한글 선명)
    guide_rank = _draw_multiline_pil(
        preview_bgr, ["[1/2] 등수 영역을 드래그로 선택", "ENTER/SPACE=확정, R=다시 선택, ESC=취소"], origin=(40, 60)
    )
    selector = _DragSelector(guide_rank, "Select Rank ROI")
    rank_roi_preview = selector.select()
    if not _is_valid_roi(rank_roi_preview):
        cv2.destroyAllWindows()
        return None

    guide_points = _draw_multiline_pil(
        preview_bgr, ["[2/2] 점수 영역을 드래그로 선택", "ENTER/SPACE=확정, R=다시 선택, ESC=취소"], origin=(40, 60)
    )
    selector = _DragSelector(guide_points, "Select Points ROI")
    points_roi_preview = selector.select()
    cv2.destroyAllWindows()
    if not _is_valid_roi(points_roi_preview):
        return None

    # 프리뷰 → 원본 좌표 환산
    inv = 1.0 / float(scale)

    def up(roi: ROI) -> ROI:
        x, y, w, h = roi
        return (int(round(x * inv)), int(round(y * inv)),
                max(1, int(round(w * inv))), max(1, int(round(h * inv))))

    rank_roi = _clamp_roi(up(rank_roi_preview), screen_size)
    points_roi = _clamp_roi(up(points_roi_preview), screen_size)

    result = {"rank_roi": rank_roi, "points_roi": points_roi, "screen": screen_size}
    if interactive_save:
        save_score_rois(rank_roi, points_roi, screen_size)
    return result


# =========================
# Windows 투명 오버레이 버전 (Snipping Tool 유사)
# =========================
def _ensure_dpi_aware():
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class _TkOverlaySelector:
    """
    전체 화면 위에 투명/반투명 오버레이를 띄워 드래그로 ROI 선택.
    - 좌클릭 드래그: 박스 지정
    - Enter/Space: 확정, Esc: 취소, R: 리셋
    """
    def __init__(self, title: str, guide_lines: list[str]):
        import tkinter as tk  # 지연 import
        self.tk = tk.Tk()
        self.tk.title(title)
        self.tk.attributes("-topmost", True)
        self.tk.attributes("-fullscreen", True)

        # 투명 배경(Windows/Tk 8.6+). 미지원 환경에서는 알파로 대체.
        self.tk.configure(bg="#00FF00")
        try:
            self.tk.wm_attributes("-transparentcolor", "#00FF00")
        except Exception:
            self.tk.attributes("-alpha", 0.85)

        # 화면 크기
        self.W = self.tk.winfo_screenwidth()
        self.H = self.tk.winfo_screenheight()

        # 캔버스: 투명색으로 채워놓고 선/텍스트만 그림
        self.cv = tk.Canvas(self.tk, width=self.W, height=self.H, bg="#00FF00", highlightthickness=0)
        self.cv.pack()

        # 한글 폰트(있으면 맑은 고딕)
        try:
            self.font = ("맑은 고딕", 16, "bold")
        except Exception:
            self.font = ("Arial", 16, "bold")

        y = 40
        for line in guide_lines:
            self.cv.create_text(40, y, anchor="nw", text=line, fill="#32E632", font=self.font)
            y += 28

        self.start: Optional[Tuple[int, int]] = None
        self.curr: Optional[Tuple[int, int]] = None
        self.rect_id: Optional[int] = None
        self.ok = False
        self.roi: Optional[ROI] = None

        # 이벤트
        self.cv.bind("<ButtonPress-1>", self._on_down)
        self.cv.bind("<B1-Motion>", self._on_move)
        self.cv.bind("<ButtonRelease-1>", self._on_up)
        self.tk.bind("<Escape>", self._on_cancel)
        self.tk.bind("<Return>", self._on_confirm)
        self.tk.bind("<space>", self._on_confirm)
        self.tk.bind("<Key-r>", self._on_reset)
        self.tk.bind("<Key-R>", self._on_reset)

    def _on_down(self, e):
        self.start = (e.x, e.y)
        self.curr = (e.x, e.y)
        if self.rect_id is None:
            self.rect_id = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#FFFF00", width=2)

    def _on_move(self, e):
        if not self.start:
            return
        self.curr = (e.x, e.y)
        x0, y0 = self.start
        x1, y1 = self.curr
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        if self.rect_id is not None:
            self.cv.coords(self.rect_id, x, y, x + w, y + h)

    def _on_up(self, e):
        if not (self.start and self.curr):
            return
        x0, y0 = self.start
        x1, y1 = (e.x, e.y)
        x, y = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        self.roi = (x, y, max(1, w), max(1, h))

    def _on_confirm(self, _e):
        if _is_valid_roi(self.roi):
            self.ok = True
            self.tk.destroy()

    def _on_cancel(self, _e):
        self.ok = False
        self.tk.destroy()

    def _on_reset(self, _e):
        self.start = None
        self.curr = None
        self.roi = None
        if self.rect_id is not None:
            self.cv.delete(self.rect_id)
            self.rect_id = None

    def select(self) -> Optional[ROI]:
        self.tk.mainloop()
        return self.roi if (self.ok and _is_valid_roi(self.roi)) else None


def calibrate_score_rois_overlay(interactive_save: bool = True) -> Optional[Dict[str, Any]]:
    """
    Snipping Tool 유사: 화면은 그대로, 투명 오버레이에서 드래그만.
    스크린샷/리사이즈 반복이 없어 화질 저하/프레임드랍 최소화.
    """
    _ensure_dpi_aware()

    rank_sel = _TkOverlaySelector(
        "Select Rank ROI",
        ["[1/2] 등수 영역을 드래그해 선택", "ENTER/SPACE=확정, R=다시, ESC=취소"],
    )
    rank_roi = rank_sel.select()
    if not _is_valid_roi(rank_roi):
        return None

    points_sel = _TkOverlaySelector(
        "Select Points ROI",
        ["[2/2] 점수 영역을 드래그해 선택", "ENTER/SPACE=확정, R=다시, ESC=취소"],
    )
    points_roi = points_sel.select()
    if not _is_valid_roi(points_roi):
        return None

    # 현재 화면 크기
    try:
        width, height = pgi.size()
    except Exception:
        width, height = (rank_sel.W, rank_sel.H)

    # 경계 클램프 후 저장/반환
    rank_roi = _clamp_roi(rank_roi, (width, height))   # type: ignore[arg-type]
    points_roi = _clamp_roi(points_roi, (width, height))  # type: ignore[arg-type]
    result = {"rank_roi": rank_roi, "points_roi": points_roi, "screen": (width, height)}
    if interactive_save:
        if interactive_save:
            save_score_rois(rank_roi, points_roi, (width, height))  # ← settings.json 으로 저장
    return result


# =========================
# 편의 함수
# =========================
def load_scaled_rois_for_current_screen(auto_scale: bool = True) -> Dict[str, Tuple[int,int,int,int]]:
    """
    settings 기반 ROI를 현재 화면 해상도(pgi.size())에 맞춰 스케일링해서 반환.
    반환 키는 기존과 동일하게 'rank_roi', 'points_roi'.
    """
    base = load_score_rois() or {}
    out: Dict[str, Tuple[int,int,int,int]] = {}

    # 원본 ROI와 기준 해상도
    rr = base.get("rank_roi")
    pp = base.get("points_roi")
    scr = base.get("screen") or {}
    bw, bh = int(scr.get("w") or 0), int(scr.get("h") or 0)

    if not auto_scale:
        if rr: out["rank_roi"] = tuple(int(v) for v in rr)
        if pp: out["points_roi"] = tuple(int(v) for v in pp)
        return out

    # 현재 화면
    try:
        import pyautogui as pgi
        sw, sh = pgi.size()
    except Exception:
        sw = int(bw or 0); sh = int(bh or 0)

    def _scale(xywh):
        x,y,w,h = [int(v) for v in xywh]
        if bw > 0 and bh > 0 and sw > 0 and sh > 0:
            sx, sy = sw/float(bw), sh/float(bh)
            return (int(round(x*sx)), int(round(y*sy)), int(round(w*sx)), int(round(h*sy)))
        return (x,y,w,h)

    if rr: out["rank_roi"] = _scale(rr)
    if pp: out["points_roi"] = _scale(pp)
    return out


# =========================
# 단독 실행
# =========================
if __name__ == "__main__":
    # 기본은 오버레이 버전 실행(버벅임/화질 이슈 없음)
    ret = calibrate_score_rois_overlay(interactive_save=True)
    if ret:
        try:
            from path_manager import SETTINGS_JSON as _SZ
            print("[OK] settings.json 저장 완료:", _SZ)
        except Exception:
            print("[OK] settings.json 저장 완료")
    else:
        print("[CANCELLED] 취소되었거나 선택이 유효하지 않습니다.")
