# core/home_probe.py
from __future__ import annotations

import time
from typing import Optional, Literal, Dict, Any, Tuple
import os, json, unicodedata

# 인터페이스 요구:
# - capture(): ndarray (현재 프레임) — 1회 캡처는 외부에서 호출해 전달
# - match_home_anchor(frame, roi) -> (hit:bool, score:float, bbox:tuple|None)
#   : NCC 기반 매칭 함수 (앵커 템플릿은 사용자 제공)
# - settings_manager.get("goal", {}) 등 접근 필요 없음 (여긴 홈확인만 담당)


ProbeResult = Literal["hit", "miss", "pending"]
DEFAULT_HOME_THRESHOLD = 0.88


class HomeProbe:
    """
    AwaitHome 루틴 상태기계.
    - start(): 초기화
    - step(frame, now_ms): 1회 프로브 실행, 결과 반환(hit/miss/pending)
    """

    def __init__(self, ncc_threshold: float = 0.88,
                 debounce_frames: int = 2,
                 settle_ms: int = 1000,
                 probe_interval_ms: int = 250,
                 roi_padding_base: int = 64,
                 roi_padding_expand: int = 96,
                 roi_expand_after_ms: int = 750,
                 coarse_after_ms: int = 5000,
                 coarse_scale: float = 0.5,
                 coarse_max_runs: int = 2,
                 soft_timeout_ms: int = 8000,
                 hard_timeout_ms: int = 12000):
        self.ncc_threshold = ncc_threshold
        self.debounce_frames = debounce_frames
        self.settle_ms = settle_ms
        self.probe_interval_ms = probe_interval_ms
        self.roi_padding_base = roi_padding_base
        self.roi_padding_expand = roi_padding_expand
        self.roi_expand_after_ms = roi_expand_after_ms
        self.coarse_after_ms = coarse_after_ms
        self.coarse_scale = coarse_scale
        self.coarse_max_runs = coarse_max_runs
        self.soft_timeout_ms = soft_timeout_ms
        self.hard_timeout_ms = hard_timeout_ms

        # 내부 상태
        self._start_ts: Optional[int] = None
        self._last_probe_ts: int = 0
        self._hits_in_a_row: int = 0
        self._expanded: bool = False
        self._coarse_runs: int = 0
        self._done: bool = False

        self._last_bbox: Optional[Tuple[int, int, int, int]] = None

    # ---- API ----

    def start(self, now_ms: Optional[int] = None) -> None:
        """AwaitHome 루틴 시작"""
        self._start_ts = now_ms or _now_ms()
        self._last_probe_ts = 0
        self._hits_in_a_row = 0
        self._expanded = False
        self._coarse_runs = 0
        self._done = False

    def step(self, frame, match_fn, cached_roi: Optional[Tuple[int, int, int, int]],
             now_ms: Optional[int] = None) -> ProbeResult:
        """
        frame: 현재 화면 이미지(ndarray)
        match_fn: callable(frame, roi) -> (hit, score, bbox)
        cached_roi: 이전 홈 앵커 bbox (x,y,w,h) or None
        반환: "hit" | "miss" | "pending"
        """
        if self._done:
            return "miss"  # 이미 종료됨

        now = now_ms or _now_ms()
        if self._start_ts is None:
            self.start(now)

        elapsed = now - self._start_ts

        # 타임아웃 검사
        if elapsed >= self.hard_timeout_ms:
            self._done = True
            return "miss"

        # 프로브 간격
        if now - self._last_probe_ts < self.probe_interval_ms:
            return "pending"

        self._last_probe_ts = now

        # ROI 선택
        roi = None
        if cached_roi:
            pad = self.roi_padding_base
            if (not self._expanded) and elapsed >= self.roi_expand_after_ms:
                pad = self.roi_padding_expand
                self._expanded = True
            roi = _expand_roi(cached_roi, pad, frame.shape[1], frame.shape[0])

        # 매칭 실행
        hit, score, bbox = match_fn(frame, roi)
        self._last_bbox = bbox  # 마지막 매칭 bbox 저장(원본 좌표계)
        if hit and score >= self.ncc_threshold:
            self._hits_in_a_row += 1
            if self._hits_in_a_row >= self.debounce_frames:
                self._done = True
                return "hit"
            return "pending"
        else:
            self._hits_in_a_row = 0

        # coarse fallback
        if elapsed >= self.coarse_after_ms and self._coarse_runs < self.coarse_max_runs:
            resized = _resize_frame(frame, self.coarse_scale)
            coarse_hit, score, bbox = match_fn(resized, None)

            # 다운스케일 프레임에서 나온 좌표를 원본 좌표계로 환산 → '바로' 저장(반드시 return 전에!)
            if bbox and len(bbox) == 4:
                inv = 1.0 / float(self.coarse_scale)
                bx = int(round(bbox[0] * inv))
                by = int(round(bbox[1] * inv))
                # 템플릿은 스케일하지 않았으므로 w,h는 그대로 사용
                bw = int(bbox[2])
                bh = int(bbox[3])
                self._last_bbox = (bx, by, bw, bh)
            else:
                self._last_bbox = None

            self._coarse_runs += 1
            if coarse_hit and score >= self.ncc_threshold:
                self._done = True
                return "hit"

        # soft timeout 처리
        if elapsed >= self.soft_timeout_ms:
            # 복구 플로우 1회 시도 후에도 hit 없으면 miss로 종료
            self._done = True
            return "miss"

        return "pending"

    def get_last_bbox(self) -> Optional[Tuple[int, int, int, int]]:
        """직전 step에서 사용된 매칭 bbox(원본 좌표계)를 돌려준다."""
        return self._last_bbox


# ---- 유틸 ----

def _expand_roi(bbox: Tuple[int, int, int, int], pad: int, W: int, H: int):
    x, y, w, h = bbox
    new_x = max(0, x - pad)
    new_y = max(0, y - pad)
    new_w = min(W - new_x, w + 2 * pad)
    new_h = min(H - new_y, h + 2 * pad)
    return (new_x, new_y, new_w, new_h)


def _resize_frame(frame, scale: float):
    import cv2
    H, W = frame.shape[:2]
    new_size = (int(W * scale), int(H * scale))
    return cv2.resize(frame, new_size, interpolation=cv2.INTER_AREA)


def _now_ms() -> int:
    return int(time.time() * 1000)


def get_home_anchor_template(image_folder: str, routine: list) -> Optional[str]:
    """
    CWD(= exe 폴더) 기준의 home_anchor.json을 읽어 템플릿 파일명을 얻고,
    image_folder/<파일명> 경로(상대경로)를 만들어 반환한다.
    - path_manager.chdir_to_base()로 CWD가 이미 exe 폴더로 고정되어 있다는 전제를 따른다.
    - 파일명만 사용(디렉터리 무시), 유니코드/대소문자 정규화.
    """
    try:
        # 1) 포인터 파일: exe 폴더(BASE_DIR) 기준 절대경로로 찾음
        import path_manager as pm
        ptr_path = str(pm.BASE_DIR / "home_anchor.json")
        if not os.path.exists(ptr_path):
            return None

        with open(ptr_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}

        # 2) 파일명만 추출 + 정규화
        raw = str(data.get("image", "")).strip()
        if not raw:
            return None
        name = os.path.basename(raw)                      # 디렉터리 제거
        name = unicodedata.normalize("NFC", name).lower() # 유니코드/소문자 정규화

        # 3) image_folder와 결합 (image_folder는 상대경로일 수 있음)
        #    - path_manager.chdir_to_base()가 선행되어 있으므로 상대경로 → exe 옆으로 수렴
        #    - image_folder 끝 슬래시 유무와 무관하게 안전 결합
        folder = str(image_folder or "").rstrip("\\/")
        tpl_path = os.path.join(folder, name)

        # 4) 존재 확인 후 반환
        return tpl_path if os.path.exists(tpl_path) else None

    except Exception as e:
        print(f"[HOME] home_anchor.json 읽기/해석 실패: {e}")
        return None
