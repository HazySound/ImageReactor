# ocr/metrics_reader.py
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict
from core.score_calib import load_scaled_rois_for_current_screen
import cv2
import numpy as np
# --- OCR 텍스트 판정(순위권 이탈)용: GUI 경로와 동일 함수 사용 시도 ---
try:
    from core.ocr import _match_out_of_range_ko as _is_oor_ko  # GUI에서 쓰는 한글 매칭
except Exception:
    _is_oor_ko = None
try:
    from core.ocr import read_text as _ocr_read_text  # GUI 테스트의 텍스트 OCR
except Exception:
    _ocr_read_text = None
    
# ----------------------------
# 설정/ROI 모델
# ----------------------------

@dataclass
class HomeOCRConfig:
    # 각 ROI는 (x, y, w, h) 픽셀 좌표 (스크린 기준)
    roi_rank: Tuple[int, int, int, int]
    roi_points: Tuple[int, int, int, int]
    # 전처리 파라미터
    bin_thresh: int = 170          # 이진화 임계
    max_val: int = 255             # 이진화 max
    invert: bool = True            # 흑백 반전 여부(밝은 배경/어두운 글씨 가정)
    open_ksize: int = 1            # 오프닝 커널 크기
    dilate_ksize: int = 0          # 팽창 커널 크기(필요 시)
    scale: float = 1.0             # ROI 스케일 조정(고해상도 대비)


# ----------------------------
# 퍼블릭 API
# ----------------------------

def read_metrics_from_home(frame_bgr: np.ndarray, cfg: HomeOCRConfig) -> Dict[str, Optional[int]]:
    ts = _now_ms()
    # 등수
    rank_img = _crop_roi(frame_bgr, cfg.roi_rank, cfg.scale)
    rank_val, rank_flag = _read_rank_with_flag(rank_img, cfg)

    # 점수
    pts_img = _crop_roi(frame_bgr, cfg.roi_points, cfg.scale)
    points_val = _read_int_from_patch(pts_img, cfg, allow_k=True)

    return {
        "rank": rank_val,
        "rank_flag": rank_flag,   # "NUMERIC" | "OUT_OF_RANGE" | "UNKNOWN"
        "points": points_val,
        "ts_ms": ts
    }


def format_ocr_result_line(frame_bgr, cfg) -> str:
    """
    캘리브 검증 팝업 전용: 현재 프레임에서 points/rank를 모두 읽어
    "OCR 결과: 점수=N점 / 등수 N등" 형식으로 문자열을 만든다.
    - 둘 중 하나의 ROI만 지정되어 있으면, 지정된 항목만 출력.
    - rank는 숫자 미인식(OUT_OF_RANGE)일 때 "등수 순위권 이탈"로 표기.
    """
    try:
        m = read_metrics_from_home(frame_bgr, cfg)  # 기존 함수 사용
    except Exception as e:
        return f"OCR 결과: (오류: {e})"

    parts = []

    # points
    if getattr(cfg, "roi_points", None):
        p = m.get("points", None)
        if isinstance(p, int) and p > 0:
            parts.append(f"점수={p}점")
        else:
            parts.append("점수 인식 실패")

    # rank
    if getattr(cfg, "roi_rank", None):
        if m.get("rank_flag") == "OUT_OF_RANGE":
            parts.append("등수 순위권 이탈")
        else:
            r = m.get("rank", None)
            if isinstance(r, int) and r > 0:
                parts.append(f"등수 {r}등")
            else:
                parts.append("등수 인식 실패")

    if not parts:
        return "OCR 결과: (대상 ROI 없음)"

    return "OCR 결과: " + " / ".join(parts)


# ----------------------------
# 내부 유틸
# ----------------------------

_DIGIT_PAT = re.compile(r"(\d{1,6})")


def _read_int_from_patch(
    bgr_patch: np.ndarray,
    cfg: HomeOCRConfig,
    allow_hash: bool = False,
    allow_k: bool = False
) -> Optional[int]:
    """
    GUI OCR 테스트 경로와 동일한 엔진 순서로 숫자를 판독한다.
    1) core.ocr / pytesseract 기반 폴백 체인(_read_int_with_ocr_fallback) 호출
    2) 실패 시, 기존 이진화+간이 템플릿 파이프라인을 cfg 전처리 파라미터로 한 번 더 시도
    """
    if bgr_patch is None or bgr_patch.size == 0:
        return None

    # 1) GUI와 동일한 폴백 체인 우선 적용
    val = _read_int_with_ocr_fallback(
        bgr_patch,
        allow_hash=allow_hash,
        allow_k=allow_k
    )
    if isinstance(val, int):
        return val

    # 2) 최후 폴백: 기존 전처리 파이프라인(cfg 파라미터 사용)
    try:
        gray = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2GRAY)
        gray = _adaptive_contrast(gray)

        thtype = cv2.THRESH_BINARY_INV if cfg.invert else cv2.THRESH_BINARY
        _, binimg = cv2.threshold(gray, cfg.bin_thresh, cfg.max_val, thtype)

        if cfg.open_ksize > 0:
            k = cv2.getStructuringElement(cv2.MORPH_RECT, (cfg.open_ksize, cfg.open_ksize))
            binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, k, iterations=1)
        if cfg.dilate_ksize > 0:
            k2 = cv2.getStructuringElement(cv2.MORPH_RECT, (cfg.dilate_ksize, cfg.dilate_ksize))
            binimg = cv2.dilate(binimg, k2, iterations=1)

        text = _simple_ocr_digits(binimg)
        if not text:
            return None

        s = text.strip()
        if allow_hash and s.startswith("#"):
            s = s[1:].strip()

        if allow_k and (s.endswith("k") or s.endswith("K")):
            try:
                num = float(s[:-1].replace(",", "").replace(" ", ""))
                return int(num * 1000)
            except Exception:
                pass

        s = s.replace(",", "").replace(".", " ")
        m = _DIGIT_PAT.search(s)
        if not m:
            return None

        return int(m.group(1))
    except Exception:
        return None


def _read_int_with_ocr_fallback(bgr_patch: np.ndarray, *, allow_hash: bool, allow_k: bool) -> Optional[int]:
    """
    GUI OCR 테스트와 동일한 엔진 우선순위로 숫자 판독.
    1) core.ocr.read_text / read_digits / (구버전) 폴백
    2) pytesseract 1패스 (--psm 7, 숫자 whitelist)
    3) pytesseract 2패스 (ROI ×2 업스케일, 동일 옵션)
    4) 최후 폴백: 기존 이진 템플릿
    - '0'은 오인식 빈도가 높아 1/2패스에서는 실패로 간주(실제 점수 0은 본 UI상 나오지 않음)
    """
    if bgr_patch is None or bgr_patch.size == 0:
        return None

    # --- 공용: BGR→PIL ---
    from PIL import Image
    try:
        pil_img = Image.fromarray(cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2RGB))
    except Exception:
        pil_img = None

    def _parse_numeric(s: str) -> Optional[int]:
        if not s:
            return None
        s = s.strip()

        # 해시/축약 표기 우선 처리
        if allow_hash and s.startswith("#"):
            s = s[1:].strip()
        if allow_k and (s.endswith("k") or s.endswith("K")):
            try:
                num = float(s[:-1].replace(",", "").replace(" ", ""))
                return int(num * 1000)
            except Exception:
                return None

        # 1) 콤마/공백/개행/잡문자에 의해 쪼개진 숫자 조각들을 전부 추출
        #    예: "343\n9" -> ["343","9"] → "3439"
        parts = re.findall(r"\d+", s)
        if not parts:
            return None

        joined = "".join(parts)

        # 2) 비현실적인 길이는 거른다(과도한 오탐 방지)
        #    점수/등수는 1~6자리 이내로 가정
        if not (1 <= len(joined) <= 6):
            # 그래도 뭔가 하나는 뽑고 싶다면 '가장 긴 조각'을 사용
            cand = max(parts, key=len)
            try:
                return int(cand)
            except Exception:
                return None

        try:
            return int(joined)
        except Exception:
            return None

    # --- 1) core.ocr 우선 (GUI 테스트와 동일 체인) ---
    try:
        from core import ocr as _ocr

        # 일반 텍스트 먼저(불필요하지만 호환 유지)
        if callable(getattr(_ocr, "read_text", None)) and pil_img:
            _ = str(_ocr.read_text(pil_img) or "")  # 결과는 사용 안 해도 호출 비용 저렴

        # 숫자 전용
        rd = getattr(_ocr, "read_digits", None)
        if callable(rd) and pil_img:
            v = _parse_numeric(str(rd(pil_img) or ""))
            if isinstance(v, int) and v != 0:
                return v

        # 구버전 폴백들
        for fn_name in ("ocr_digits", "detect_score"):
            fn = getattr(_ocr, fn_name, None)
            if callable(fn) and pil_img:
                v = _parse_numeric(str(fn(pil_img) or ""))
                if isinstance(v, int) and v != 0:
                    return v
    except Exception:
        pass

    # --- 2) pytesseract 1패스 ---
    def _tesseract_to_int(pil: Image.Image) -> Optional[int]:
        try:
            import pytesseract
            raw = pytesseract.image_to_string(
                pil,
                config="--psm 8 -c tessedit_char_whitelist=0123456789,"
            ) or ""
            v = _parse_numeric(raw)
            return v
        except Exception:
            return None

    if pil_img is not None:
        v1 = _tesseract_to_int(pil_img)
        if isinstance(v1, int) and v1 != 0:
            return v1

    # --- 3) pytesseract 2패스(ROI 업스케일) ---
    try:
        up = cv2.resize(bgr_patch, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        pil_up = Image.fromarray(cv2.cvtColor(up, cv2.COLOR_BGR2RGB))
        v2 = _tesseract_to_int(pil_up)
        if isinstance(v2, int) and v2 != 0:
            return v2
    except Exception:
        pass

    # --- 4) 최후 폴백: 기존 이진 템플릿 파이프라인 ---
    try:
        gray = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2GRAY)
        gray = _adaptive_contrast(gray)
        _, binimg = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY_INV)
        text = _simple_ocr_digits(binimg)
        v = _parse_numeric(text or "")
        if isinstance(v, int):
            return v
    except Exception:
        pass

    return None


def _read_rank_with_flag(bgr_patch: np.ndarray, cfg: HomeOCRConfig) -> Tuple[Optional[int], str]:
    """
    등수 ROI 판독: GUI 테스트와 동일 우선순위
    1) 텍스트 우선 감지 → '순위권 이탈'이면 즉시 OUT_OF_RANGE
    2) 숫자 판독(_read_int_with_ocr_fallback)
    3) 모폴로지 텍스트 패턴 보조 판정
    """
    if bgr_patch is None or bgr_patch.size == 0:
        return None, "UNKNOWN"

    # --- 1) 텍스트 우선: GUI와 동일하게 '순위권 이탈' 먼저 잡는다 ---
    try:
        if _ocr_read_text is not None:
            from PIL import Image
            pil = Image.fromarray(cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2RGB))
            t = str(_ocr_read_text(pil) or "").strip()
            if _is_oor_ko and callable(_is_oor_ko):
                try:
                    if _is_oor_ko(t):
                        return None, "OUT_OF_RANGE"
                except Exception:
                    pass
            # 폴백 키워드 매칭
            if ("순위권" in t) or ("이탈" in t):
                return None, "OUT_OF_RANGE"
    except Exception:
        pass

    # --- 2) 숫자 판독(해시 허용) ---
    rank_val = _read_int_with_ocr_fallback(bgr_patch, allow_hash=True, allow_k=False)
    if isinstance(rank_val, int):
        return rank_val, "NUMERIC"

    # --- 3) 모폴로지로 텍스트 패턴이면 OUT_OF_RANGE 보조 판정 ---
    try:
        gray = cv2.cvtColor(bgr_patch, cv2.COLOR_BGR2GRAY)
        gray = _adaptive_contrast(gray)
        thtype = cv2.THRESH_BINARY_INV if cfg.invert else cv2.THRESH_BINARY
        _, binimg = cv2.threshold(gray, cfg.bin_thresh, cfg.max_val, thtype)
        if cfg.open_ksize > 0:
            k = cv2.getStructuringElement(cv2.MORPH_RECT, (cfg.open_ksize, cfg.open_ksize))
            binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, k, iterations=1)
        if cfg.dilate_ksize > 0:
            k2 = cv2.getStructuringElement(cv2.MORPH_RECT, (cfg.dilate_ksize, cfg.dilate_ksize))
            binimg = cv2.dilate(binimg, k2, iterations=1)
        if _looks_like_text(binimg):
            return None, "OUT_OF_RANGE"
    except Exception:
        pass

    return None, "UNKNOWN"


def _looks_like_text(binimg: np.ndarray) -> bool:
    """
    한글/영문 텍스트처럼 보이는 컨투어 패턴을 러프하게 감지.
    - 너무 작은 점/노이즈 제외
    - 세로 높이 비율 0.3~0.85 사이 컨투어가 일정 개수 이상이면 '텍스트 유사'로 간주
    """
    if binimg is None or binimg.size == 0:
        return False
    h, w = binimg.shape[:2]
    if h == 0 or w == 0:
        return False

    cnts, _ = cv2.findContours(binimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    glyph_like = 0
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < 4 or bh < 8:
            continue
        # 너무 얇거나 너무 큰 덩어리 제외
        if bh < h * 0.30 or bh > h * 0.90:
            continue
        # 글리프처럼 보이는 개수 카운트
        glyph_like += 1

    # 글리프 유사 컨투어가 일정 이상이면 '텍스트'로 본다
    return glyph_like >= 6


def _simple_ocr_digits(binimg: np.ndarray) -> str:
    """
    단순 숫자 OCR: 컨투어에서 영역 분리 → 각 숫자 너비/비율 필터 → KNN-like 매칭(옵션).
    여기서는 안정성을 위해 '숫자 아닌 것 제거 + 숫자만 남긴다' 수준으로 최소 구현.
    필요 시 Tesseract로 교체 가능.
    """
    # 숫자 이외의 잡영역 제거(너무 얇거나 작은 것)
    h, w = binimg.shape[:2]
    cnts, _ = cv2.findContours(binimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < 6 or bh < 10:
            continue
        if bh < h * 0.35:  # 너무 작으면 제외
            continue
        candidates.append((x, y, bw, bh))
    # 왼→오 정렬
    candidates.sort(key=lambda b: b[0])

    # 영역 합치기(간격이 작으면 병합)
    merged = []
    for box in candidates:
        if not merged:
            merged.append(box); continue
        px, py, pw, ph = merged[-1]
        x, y, bw, bh = box
        if x <= px + pw + 2:  # 2px gap 이하면 병합
            nx = px; ny = min(py, y)
            nw = max(px+pw, x+bw) - nx
            nh = max(ph, bh)
            merged[-1] = (nx, ny, nw, nh)
        else:
            merged.append(box)

    # 최종 추정: 숫자열 길이만큼 '0'을 리턴하고 실제 숫자는 상위 로직에서 정규식으로 추림
    # (이 함수는 '숫자 형태가 있다'를 확인하는 역할 위주)
    # 필요하면 여기에 템플릿 매칭/분류기 붙여도 됨.
    if not merged:
        return ""

    # 히스토그램 프로젝션으로 숫자 칸 추정
    roi = binimg
    col_sum = roi.sum(axis=0)
    # 연속된 nonzero 구간 → rough한 자릿수 판정
    slots = 0
    inside = False
    for v in col_sum:
        if v > 0 and not inside:
            slots += 1; inside = True
        elif v == 0 and inside:
            inside = False

    # 자리수가 너무 작으면 빈값
    if slots < 1:
        return ""

    # 숫자 자릿수만 추정해 더미 문자열 생성 (정규식에서 실제 숫자 추출)
    return "0" * min(slots, 8)


def _adaptive_contrast(gray: np.ndarray) -> np.ndarray:
    # 밝기 보정 + CLAHE
    g = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    return clahe.apply(g)


def _crop_roi(frame_bgr: np.ndarray, roi: Tuple[int, int, int, int], scale: float) -> np.ndarray:
    x, y, w, h = roi
    if scale != 1.0:
        x = int(x * scale); y = int(y * scale)
        w = int(w * scale); h = int(h * scale)
    H, W = frame_bgr.shape[:2]
    x = max(0, min(x, W-1)); y = max(0, min(y, H-1))
    w = max(1, min(w, W - x)); h = max(1, min(h, H - y))
    return frame_bgr[y:y+h, x:x+w].copy()


def load_default_home_ocr_config() -> Optional[HomeOCRConfig]:
    """score_rois.json을 읽어 현재 화면에 맞게 스케일링한 기본 OCR 설정을 만든다."""
    rois = load_scaled_rois_for_current_screen(auto_scale=True)
    if not rois:
        return None
    return HomeOCRConfig(
        roi_rank=tuple(rois["rank_roi"]),
        roi_points=tuple(rois["points_roi"]),
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
