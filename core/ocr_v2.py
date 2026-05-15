# core/ocr_v2.py
"""
새 OCR 파이프라인 v2.

설계 원칙:
  1. HSV 흰색 마스킹으로 뱃지/배경 노이즈 색 단계에서 제거
  2. 컴포넌트 높이 분포 단절 탐지로 잔여 노이즈 컷
  3. tight bbox 재크롭으로 깨끗한 한 줄만 Tesseract에 전달
  4. image_to_data 단일 호출로 값 + 신뢰도 동시 수집
  5. 경계 케이스에서만 앙상블(여러 전처리 변형) 발동

기존 core/ocr.py 의 함수는 건드리지 않음(테스트 검증 후 일괄 제거 예정).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional, Tuple, List, Dict, Callable, Any

import cv2
import numpy as np
from PIL import Image, ImageFont, ImageDraw
import pytesseract

from core import ocr as _ocr_legacy  # _tess_cfg_and_env, resolve_tessdata_dir 재사용

# Tesseract 단일 실행 보장 (Windows에서 동시 호출 불안정 회피)
_OCR_LOCK = threading.Lock()

# ============================================================
# 상수 / 기본 임계값
# ============================================================

# HSV 흰색 마스킹 기본 범위 — 게임 메인 텍스트(순백) 추출용
DEFAULT_V_MIN = 200      # Value(밝기) 하한
DEFAULT_S_MAX = 60       # Saturation(채도) 상한

# 컴포넌트 높이 단절 탐지 기준 — h[i]/h[i+1] >= 이 값이면 단절
HEIGHT_GAP_RATIO = 1.5

# 절대 최소 컴포넌트 높이(픽셀). 이보다 작은 컴포넌트는 항상 노이즈 처리
MIN_ABSOLUTE_HEIGHT = 8

# "순위권 이탈" 매칭용 한글 집합
_OOR_CHARS = set("순위권이탈")

# 챔피언스 티어 게이트 ROI 비율 (x, y, w, h)
# 1920x1080 캡처 측정 + 슈퍼 챔피언스 케이스 안전마진
DEFAULT_CHAMPION_ROI_RATIO = (0.14, 0.22, 0.25, 0.13)


# ============================================================
# 결과 데이터 모델
# ============================================================

@dataclass
class OcrResult:
    """단일 OCR 결과."""
    value: Optional[int] = None
    kind: str = "UNKNOWN"   # "NUMERIC" | "OUT_OF_RANGE" | "PARSE_FAIL" | "EMPTY"
    raw: str = ""
    word_conf: int = 0      # 0~100
    digit_count: int = 0    # 인식된 숫자 자릿수
    bbox: Optional[Tuple[int, int, int, int]] = None  # tight bbox (마스킹 후)


@dataclass
class EnsembleResult:
    """앙상블 OCR 결과."""
    final: OcrResult
    agreement: float = 0.0  # 0.0~1.0 (다수파 비율)
    passes: List[OcrResult] = field(default_factory=list)


# ============================================================
# 저수준 전처리 프리미티브
# ============================================================

def white_mask(bgr: np.ndarray,
               v_min: int = DEFAULT_V_MIN,
               s_max: int = DEFAULT_S_MAX) -> np.ndarray:
    """
    BGR 이미지에서 흰색에 가까운 픽셀만 남기는 마스크 반환.
    반환: uint8 0/255 마스크.

    근거:
      - 게임 메인 텍스트(점수/등수/한글)는 모두 순백색
      - 뱃지 배경/타이머 회색/배너 어두운 색은 V<200 또는 S>60에서 걸러짐
      - fade 단계에서 흰 글씨가 배경과 블렌딩되면 V가 떨어지거나 S가 올라가서 자동 제거
    """
    if bgr is None or bgr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1]
    v = hsv[..., 2]
    mask = ((v >= v_min) & (s <= s_max)).astype(np.uint8) * 255
    return mask


def _components_from_mask(mask: np.ndarray) -> List[Tuple[int, int, int, int, int]]:
    """
    마스크에서 connected component 추출.
    반환: [(x, y, w, h, area), ...] — 모두 픽셀 단위
    """
    if mask is None or mask.size == 0:
        return []
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out: List[Tuple[int, int, int, int, int]] = []
    for i in range(1, n):  # 0은 배경
        x, y, w, h, a = (int(stats[i, cv2.CC_STAT_LEFT]),
                         int(stats[i, cv2.CC_STAT_TOP]),
                         int(stats[i, cv2.CC_STAT_WIDTH]),
                         int(stats[i, cv2.CC_STAT_HEIGHT]),
                         int(stats[i, cv2.CC_STAT_AREA]))
        if h >= MIN_ABSOLUTE_HEIGHT and w >= 2:
            out.append((x, y, w, h, a))
    return out


def filter_by_height_gap(components: List[Tuple[int, int, int, int, int]],
                         gap_ratio: float = HEIGHT_GAP_RATIO,
                         min_keep: int = 1) -> List[Tuple[int, int, int, int, int]]:
    """
    컴포넌트를 높이 내림차순 정렬한 뒤, 인접 비율 >= gap_ratio 인
    첫 단절점을 찾아 그 이전까지만 유지.

    예:
      heights [80, 78, 76, 35, 30]  → 76→35 = 2.17 → [80,78,76] 유지
      heights [80, 35]              → 80→35 = 2.29 → [80] 유지
      heights [60, 58, 55, 50, 50]  → 모두 비슷 → 전부 유지
    """
    if not components:
        return []
    if len(components) == 1:
        return components
    # 높이 내림차순
    sorted_c = sorted(components, key=lambda c: -c[3])
    heights = [c[3] for c in sorted_c]
    # 첫 단절점 탐색
    cut_at = len(sorted_c)  # 단절 없으면 끝까지
    for i in range(len(heights) - 1):
        if heights[i + 1] <= 0:
            cut_at = i + 1
            break
        ratio = heights[i] / heights[i + 1]
        if ratio >= gap_ratio:
            cut_at = i + 1
            break
    cut_at = max(cut_at, min_keep)
    return sorted_c[:cut_at]


def tight_bbox(components: List[Tuple[int, int, int, int, int]],
               img_w: int, img_h: int,
               pad: int = 2) -> Optional[Tuple[int, int, int, int]]:
    """필터된 컴포넌트들의 합집합 bbox(약간 패딩 포함)."""
    if not components:
        return None
    x1 = min(c[0] for c in components)
    y1 = min(c[1] for c in components)
    x2 = max(c[0] + c[2] for c in components)
    y2 = max(c[1] + c[3] for c in components)
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(img_w, x2 + pad)
    y2 = min(img_h, y2 + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2 - x1, y2 - y1)


def apply_mask_as_bw(bgr: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """
    BGR + 마스크 → 검은 배경 위 흰 글씨 이미지(uint8 단일 채널).
    OCR 친화 포맷.
    """
    if bgr is None or bgr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    out = np.zeros(bgr.shape[:2], dtype=np.uint8)
    out[mask > 0] = 255
    return out


def upscale_for_ocr(gray: np.ndarray, factor: float = 2.0) -> np.ndarray:
    """OCR 정확도를 위해 업스케일. 작은 글자가 안정적으로 인식됨."""
    if gray is None or gray.size == 0 or factor == 1.0:
        return gray
    h, w = gray.shape[:2]
    return cv2.resize(gray, (int(w * factor), int(h * factor)),
                      interpolation=cv2.INTER_CUBIC)


# ============================================================
# Tesseract 래퍼
# ============================================================

def _pick_digit_lang() -> str:
    """가용 언어에서 숫자 OCR에 쓸 lang 선택. eng 우선, 없으면 kor."""
    try:
        avail = _ocr_legacy.get_available_languages()
        if "eng" in avail:
            return "eng"
        if "kor" in avail:
            return "kor"
    except Exception:
        pass
    return "eng"  # 마지막 폴백 (실패 시 OCR이 자체 에러로 처리)


def _ocr_image_to_data(pil_img: Image.Image,
                       lang: str,
                       whitelist: str,
                       psm: int = 7) -> Tuple[str, int, Dict[str, Any]]:
    """
    Tesseract image_to_data 호출.
    반환: (joined_text, max_word_conf, raw_data_dict)

    image_to_data 는 단어 단위 conf를 반환. 한 ROI에 보통 단어 1~2개라
    word_conf의 최댓값/유효 word 평균을 쓰면 충분.
    """
    cfg, restore = _ocr_legacy._tess_cfg_and_env()
    try:
        config_str = f"{cfg} --psm {psm}".strip()
        if whitelist:
            config_str += f" -c tessedit_char_whitelist={whitelist}"
        with _OCR_LOCK:
            data = pytesseract.image_to_data(
                pil_img,
                lang=lang,
                config=config_str,
                output_type=pytesseract.Output.DICT,
            )
    except Exception:
        return "", 0, {}
    finally:
        restore()

    # 유효 단어만 모아 텍스트/conf 추출
    texts: List[str] = []
    confs: List[int] = []
    n = len(data.get("text", []) or [])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        try:
            c = int(float(data["conf"][i]))
        except Exception:
            c = -1
        if txt and c >= 0:
            texts.append(txt)
            confs.append(c)
    joined = " ".join(texts).strip()
    max_conf = max(confs) if confs else 0
    return joined, int(max_conf), data


# ============================================================
# 파싱 유틸
# ============================================================

def _extract_digits(text: str) -> str:
    """텍스트에서 콤마/공백 제거하고 숫자만 추출."""
    return re.sub(r"[^0-9]", "", text or "")


# Tesseract OCR confusable 문자 → 숫자 매핑
# 등수처럼 whitelist 를 못 쓰는(한글 OOR 매칭 필요) 케이스에서 사용.
# 실측 사례:
#   - 폰트의 "1" 이 직선이라 "]" / "[" / "|" 로 잘못 잡힘 (1317→317, 1320→320)
_RANK_CONFUSABLE = {
    ']': '1', '[': '1', '|': '1', 'I': '1', 'l': '1', 'i': '1',
    'O': '0', 'Q': '0', 'o': '0',
}


def _extract_digits_for_rank(text: str) -> str:
    """등수 전용 숫자 추출. OCR confusable 문자도 숫자로 매핑한 뒤 추출."""
    if not text:
        return ""
    out = []
    for ch in text:
        if '0' <= ch <= '9':
            out.append(ch)
        elif ch in _RANK_CONFUSABLE:
            out.append(_RANK_CONFUSABLE[ch])
        # 한글 / 공백 / 콤마 등은 무시
    return "".join(out)


def _extract_hangul(text: str) -> str:
    return "".join(ch for ch in (text or "") if "가" <= ch <= "힣")


def _match_out_of_range(text: str) -> bool:
    """'순위권 이탈' 패턴 매칭. 일부 글자 누락 허용."""
    s = _extract_hangul(text).replace(" ", "")
    if not s or len(s) < 2:
        return False
    base = "순위권이탈"
    if s in base:
        return True
    # subsequence (순서 유지, 일부 누락 허용)
    it = iter(base)
    if all(ch in it for ch in s):
        return True
    # 모든 글자가 OOR 문자집합에 속하면 통과 (느슨한 폴백)
    return all(ch in _OOR_CHARS for ch in s)


# ============================================================
# 고수준 OCR — 점수/등수
# ============================================================

def _preprocess_for_main_text(bgr: np.ndarray,
                              v_min: int = DEFAULT_V_MIN,
                              s_max: int = DEFAULT_S_MAX,
                              upscale: float = 2.0,
                              dilate_px: int = 0
                              ) -> Tuple[Optional[Image.Image], Optional[Tuple[int, int, int, int]]]:
    """
    공통 전처리:
      1) HSV 흰색 마스킹
      2) 컴포넌트 추출 + 높이 단절 필터
      3) tight bbox 재크롭
      4) 업스케일
      5) (옵션) dilation

    반환: (PIL grayscale image, tight_bbox) — 실패 시 (None, None)
    """
    if bgr is None or bgr.size == 0:
        return None, None
    H, W = bgr.shape[:2]

    mask = white_mask(bgr, v_min=v_min, s_max=s_max)
    comps = _components_from_mask(mask)
    if not comps:
        return None, None

    kept = filter_by_height_gap(comps)
    bbox = tight_bbox(kept, W, H, pad=2)
    if bbox is None:
        return None, None

    x, y, w, h = bbox
    cropped_mask = mask[y:y + h, x:x + w]
    bw = apply_mask_as_bw(bgr[y:y + h, x:x + w], cropped_mask)

    if dilate_px > 0:
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (dilate_px, dilate_px))
        bw = cv2.dilate(bw, k, iterations=1)

    bw_up = upscale_for_ocr(bw, factor=upscale)
    return Image.fromarray(bw_up), bbox


def _ocr_single_digit(component_bgr: np.ndarray, lang: str) -> str:
    """단일 글자 BGR 패치에서 숫자 1자 추출. PSM 10(single char).

    whitelist 사용 시 conf 가 낮게 잡히고 일부 "1"이 빈 결과로 반환되는 케이스가
    있어, whitelist 미사용 + confusable 매핑 fallback 을 같이 시도한다.
    """
    if component_bgr is None or component_bgr.size == 0:
        return ""
    mask = white_mask(component_bgr)
    bw = apply_mask_as_bw(component_bgr, mask)
    bw_up = upscale_for_ocr(bw, factor=3.0)  # 단일 글자는 더 크게
    pil = Image.fromarray(bw_up)
    # 1차: whitelist 0-9
    text, _conf, _ = _ocr_image_to_data(pil, lang=lang,
                                        whitelist="0123456789", psm=10)
    d = _extract_digits(text)
    if d:
        return d[:1]
    # 2차: whitelist 없이 (confusable 매핑으로 회복 시도)
    text2, _conf2, _ = _ocr_image_to_data(pil, lang=lang,
                                          whitelist="", psm=10)
    d2 = _extract_digits_for_rank(text2)
    return d2[:1] if d2 else ""


def _fix_ctc_collapse_if_needed(bgr_roi: np.ndarray,
                                ocr_digits: str,
                                lang: str) -> str:
    """
    Tesseract LSTM CTC 디코더가 연속된 동일 글자를 1개로 줄이는 알려진 버그 보정.
      예) "777" → "7", "4441" → "41"

    동작:
      1) bgr_roi에서 HSV 흰색 마스킹 + 컴포넌트 추출
      2) 메인 글자 컴포넌트 개수가 OCR 결과 자릿수보다 많으면 의심
      3) 의심 시 컴포넌트별로 개별 OCR(PSM 10) 후 자릿수 일치 시 채택
    """
    if not ocr_digits:
        return ocr_digits

    mask = white_mask(bgr_roi)
    comps = _components_from_mask(mask)
    kept = filter_by_height_gap(comps)

    main_count = len(kept)
    if main_count <= len(ocr_digits):
        # 컴포넌트가 OCR 자릿수 이하 → CTC collapse 아님. 그대로 반환.
        return ocr_digits

    # 컴포넌트가 더 많음 → 콜랩스 의심. 좌→우 정렬 후 글자별 OCR.
    sorted_c = sorted(kept, key=lambda c: c[0])
    chars: List[str] = []
    for (cx, cy, cw, ch, _area) in sorted_c:
        pad = 2
        x0 = max(0, cx - pad); y0 = max(0, cy - pad)
        x1 = min(bgr_roi.shape[1], cx + cw + pad)
        y1 = min(bgr_roi.shape[0], cy + ch + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        comp_bgr = bgr_roi[y0:y1, x0:x1]
        d = _ocr_single_digit(comp_bgr, lang=lang)
        if d:
            chars.append(d)

    per_char = "".join(chars)
    # 글자별 OCR로 얻은 자릿수가 더 많고 컴포넌트 수와 일치하면 채택
    if len(per_char) > len(ocr_digits) and len(per_char) == main_count:
        return per_char
    # 그렇지 않으면 보수적으로 원본 유지
    return ocr_digits


def read_points_v2(bgr_roi: np.ndarray) -> OcrResult:
    """
    점수 OCR. 챔피언스 티어에서 2000~9999 보장.
    화이트리스트: 0-9, lang은 eng 가용 시 eng, 없으면 kor 폴백.
    (eng가 가벼우니 우선이지만 kor.traineddata도 숫자 인식 포함)
    """
    pil, bbox = _preprocess_for_main_text(bgr_roi, upscale=2.0)
    if pil is None:
        return OcrResult(kind="EMPTY")

    lang = _pick_digit_lang()
    text, conf, _ = _ocr_image_to_data(pil, lang=lang,
                                       whitelist="0123456789", psm=7)
    digits = _extract_digits(text)

    # CTC collapse 보정: 연속 동일 숫자(예: 4441, 7777)에서 LSTM이 글자를 줄이는 이슈
    digits = _fix_ctc_collapse_if_needed(bgr_roi, digits, lang=lang)

    if not digits:
        return OcrResult(kind="PARSE_FAIL", raw=text, word_conf=conf, bbox=bbox)
    try:
        val = int(digits)
    except Exception:
        return OcrResult(kind="PARSE_FAIL", raw=text, word_conf=conf, bbox=bbox)

    return OcrResult(value=val, kind="NUMERIC", raw=text,
                     word_conf=conf, digit_count=len(digits), bbox=bbox)


_DIGIT_GATE_MAX_COMPONENTS = 5   # 컴포넌트 이 수 이하 → 숫자 케이스로 우선 처리
_HANGUL_GATE_MIN_COMPONENTS = 7  # 컴포넌트 이 수 이상 → 한글 우선
# 6 컴포넌트는 모호 영역 (4자리 숫자 + 노이즈, 또는 짧은 한글) → 두 OCR 모두 시도


def read_rank_v2(bgr_roi: np.ndarray) -> OcrResult:
    """등수 OCR. 숫자(1~9999) 또는 "순위권 이탈" 한글.

    설계 의도:
      - 일반 등수(숫자)는 whitelist="0123456789" 로 OCR — confusable 원천 차단
        (Tesseract가 가는 "1" 을 "]"/"|"/"I" 로 잡는 회귀 방지)
      - "순위권 이탈" 한글은 whitelist 없이 lang=kor 로 OCR + OOR 매칭
      - 한글/숫자 사전 판단은 HSV 흰색 마스크의 컴포넌트 수로 게이트
        · 1~4자리 숫자: 보통 컴포넌트 1~5개
        · "순위권 이탈"(공백 포함 5글자): 자모 분해되어 컴포넌트 7+
      - 게이트 결과에 따라 1회 OCR 로 종료. 모호 케이스만 안전망으로 둘 다 시도.

    confusable 매핑(_extract_digits_for_rank)은 한글 OCR 분기의 fallback 으로만 유지.
    """
    if bgr_roi is None or bgr_roi.size == 0:
        return OcrResult(kind="EMPTY")

    # 전처리
    pil, bbox = _preprocess_for_main_text(bgr_roi, upscale=2.0)
    if pil is None:
        return OcrResult(kind="EMPTY")

    # 컴포넌트 수 게이트
    mask = white_mask(bgr_roi)
    comps = _components_from_mask(mask)
    main_comps = filter_by_height_gap(comps)
    comp_count = len(main_comps)
    try_digits = comp_count <= _DIGIT_GATE_MAX_COMPONENTS or comp_count < _HANGUL_GATE_MIN_COMPONENTS
    try_hangul = comp_count >= _HANGUL_GATE_MIN_COMPONENTS or comp_count > _DIGIT_GATE_MAX_COMPONENTS

    # 1) 숫자 OCR — whitelist 사용으로 confusable 차단
    if try_digits:
        digit_lang = _pick_digit_lang()
        text_d, conf_d, _ = _ocr_image_to_data(pil, lang=digit_lang,
                                               whitelist="0123456789", psm=7)
        digits = _extract_digits(text_d)
        digits = _fix_ctc_collapse_if_needed(bgr_roi, digits, lang=digit_lang)
        if digits:
            try:
                val = int(digits)
                if 0 < val <= 9999:
                    return OcrResult(value=val, kind="NUMERIC", raw=text_d,
                                     word_conf=conf_d, digit_count=len(digits), bbox=bbox)
            except Exception:
                pass

    # 2) 한글 OCR — 순위권 이탈 매칭
    if try_hangul:
        text_k, conf_k, _ = _ocr_image_to_data(pil, lang="kor",
                                               whitelist="", psm=7)
        if _match_out_of_range(text_k):
            return OcrResult(kind="OUT_OF_RANGE", raw=text_k, word_conf=conf_k, bbox=bbox)
        # 한글 부분 매칭 (보수적 OOR 판정)
        hangul = _extract_hangul(text_k)
        if hangul and len(hangul) >= 2 and all(ch in _OOR_CHARS for ch in hangul):
            return OcrResult(kind="OUT_OF_RANGE", raw=text_k, word_conf=conf_k, bbox=bbox)
        # 한글 OCR 에서 숫자 fallback (게이트가 잘못 한글로 분류한 경우 안전망)
        digits_k = _extract_digits_for_rank(text_k)
        digits_k = _fix_ctc_collapse_if_needed(bgr_roi, digits_k, lang="kor")
        if digits_k:
            try:
                val = int(digits_k)
                if 0 < val <= 9999:
                    return OcrResult(value=val, kind="NUMERIC", raw=text_k,
                                     word_conf=conf_k, digit_count=len(digits_k), bbox=bbox)
            except Exception:
                pass
        return OcrResult(kind="PARSE_FAIL", raw=text_k, word_conf=conf_k, bbox=bbox)

    # try_hangul == False 이고 숫자 OCR 도 실패한 경우
    return OcrResult(kind="PARSE_FAIL", raw="", word_conf=0, bbox=bbox)


# ============================================================
# 앙상블 — 경계 케이스에서 여러 전처리 변형으로 합의 검증
# ============================================================

def _ensemble_variants(bgr_roi: np.ndarray) -> List[Tuple[Optional[Image.Image], Optional[Tuple[int, int, int, int]]]]:
    """5가지 전처리 변형 이미지 반환."""
    return [
        _preprocess_for_main_text(bgr_roi, v_min=200, s_max=60, upscale=2.0, dilate_px=0),
        _preprocess_for_main_text(bgr_roi, v_min=220, s_max=50, upscale=2.0, dilate_px=0),
        _preprocess_for_main_text(bgr_roi, v_min=200, s_max=60, upscale=2.5, dilate_px=0),
        _preprocess_for_main_text(bgr_roi, v_min=200, s_max=60, upscale=2.0, dilate_px=2),
        _preprocess_for_main_text(bgr_roi, v_min=190, s_max=70, upscale=2.0, dilate_px=0),
    ]


def _ocr_pil_for_kind(pil: Image.Image, kind: str) -> OcrResult:
    """단일 PIL 이미지를 점수/등수 모드별로 OCR. (앙상블 내부에서 사용)"""
    if kind == "points":
        text, conf, _ = _ocr_image_to_data(pil, lang=_pick_digit_lang(),
                                           whitelist="0123456789", psm=7)
        digits = _extract_digits(text)
        if digits:
            try:
                val = int(digits)
                return OcrResult(value=val, kind="NUMERIC", raw=text,
                                 word_conf=conf, digit_count=len(digits))
            except Exception:
                pass
        return OcrResult(kind="PARSE_FAIL", raw=text, word_conf=conf)
    else:  # rank — 숫자 우선, 한글 fallback (read_rank_v2 와 동일 설계)
        # 1) 숫자 OCR (whitelist) — confusable 차단
        digit_lang = _pick_digit_lang()
        text_d, conf_d, _ = _ocr_image_to_data(pil, lang=digit_lang,
                                               whitelist="0123456789", psm=7)
        digits = _extract_digits(text_d)
        if digits:
            try:
                val = int(digits)
                if 0 < val <= 9999:
                    return OcrResult(value=val, kind="NUMERIC", raw=text_d,
                                     word_conf=conf_d, digit_count=len(digits))
            except Exception:
                pass
        # 2) 한글 OCR — 순위권 이탈 매칭 또는 confusable 매핑 fallback
        text_k, conf_k, _ = _ocr_image_to_data(pil, lang="kor",
                                               whitelist="", psm=7)
        if _match_out_of_range(text_k):
            return OcrResult(kind="OUT_OF_RANGE", raw=text_k, word_conf=conf_k)
        digits_k = _extract_digits_for_rank(text_k)
        if digits_k:
            try:
                val = int(digits_k)
                if 0 < val <= 9999:
                    return OcrResult(value=val, kind="NUMERIC", raw=text_k,
                                     word_conf=conf_k, digit_count=len(digits_k))
            except Exception:
                pass
        return OcrResult(kind="PARSE_FAIL", raw=text_k, word_conf=conf_k)


def read_points_v2_ensemble(bgr_roi: np.ndarray) -> EnsembleResult:
    """점수 앙상블 OCR (5패스)."""
    return _run_ensemble(bgr_roi, kind="points")


def read_rank_v2_ensemble(bgr_roi: np.ndarray) -> EnsembleResult:
    """등수 앙상블 OCR (5패스)."""
    return _run_ensemble(bgr_roi, kind="rank")


def _run_ensemble(bgr_roi: np.ndarray, kind: str) -> EnsembleResult:
    """공통 앙상블 실행."""
    passes: List[OcrResult] = []
    variants = _ensemble_variants(bgr_roi)
    for pil, _bbox in variants:
        if pil is None:
            continue
        r = _ocr_pil_for_kind(pil, kind)
        passes.append(r)

    if not passes:
        return EnsembleResult(final=OcrResult(kind="EMPTY"), agreement=0.0, passes=[])

    # 다수결: (value, kind) 튜플로 카운트
    from collections import Counter
    keys = [(r.kind, r.value) for r in passes]
    cnt = Counter(keys)
    (top_key, top_count) = cnt.most_common(1)[0]
    agreement = top_count / len(passes)

    # 최종 결과: 다수파에서 conf가 가장 높은 것
    candidates = [r for r in passes if (r.kind, r.value) == top_key]
    final = max(candidates, key=lambda r: r.word_conf)
    return EnsembleResult(final=final, agreement=agreement, passes=passes)


# ============================================================
# 챔피언스 티어 게이트
# ============================================================

def _champion_roi_xywh(frame_bgr: np.ndarray,
                      ratio: Tuple[float, float, float, float],
                      base_wh: Optional[Tuple[int, int]] = None
                      ) -> Tuple[int, int, int, int]:
    """비율 → 픽셀 ROI.

    base_wh가 주어지면 base 해상도에서 비율 적용 후 현재 프레임으로 선형 스케일.
    이는 기존 rank/points ROI 스케일링과 동일한 방식으로 동작 보장.
    """
    H, W = frame_bgr.shape[:2]
    rx, ry, rw, rh = ratio
    if base_wh and base_wh[0] > 0 and base_wh[1] > 0:
        bw, bh = base_wh
        # base 좌표계에서 비율 적용
        bx, by, bw_px, bh_px = int(bw * rx), int(bh * ry), int(bw * rw), int(bh * rh)
        # 현재 프레임 좌표계로 선형 스케일
        sx = W / float(bw)
        sy = H / float(bh)
        return (int(round(bx * sx)), int(round(by * sy)),
                int(round(bw_px * sx)), int(round(bh_px * sy)))
    # base 없으면 현재 프레임 직접 사용 (fallback)
    return (int(W * rx), int(H * ry), int(W * rw), int(H * rh))


def _ocr_text_korean(bgr_roi: np.ndarray) -> Tuple[str, int]:
    """챔피언스 헤더용 한글 텍스트 OCR.

    이 함수는 점수/등수 OCR보다 단순한 처리를 한다:
      1) HSV 흰색 마스킹
      2) 수평 dilation으로 자모 간격을 닫아 글자 단위로 묶음
      3) 가장 큰 area의 컴포넌트(들)만 남김 — 메인 헤더 추정
      4) 해당 영역만 tight crop 후 PSM 6 (block) OCR

    PSM 7(line)이 아닌 6(block)을 쓰는 이유:
      - 한글 문자는 자모 사이 미세 간격이 있어 multi-block으로 인식될 수 있음
      - PSM 6이 한글 헤더에 더 안정적
    """
    if bgr_roi is None or bgr_roi.size == 0:
        return "", 0

    H, W = bgr_roi.shape[:2]

    # 1) 흰색 마스킹
    mask = white_mask(bgr_roi)

    # 2) 수평 dilation — 자모/글자 사이 가까운 간격을 닫음
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)

    # 3) 컴포넌트 추출 후 가장 큰 area 1~3개 선택
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    if n < 2:
        return "", 0
    blobs: List[Tuple[int, int, int, int, int]] = []
    for i in range(1, n):
        x, y, w, h, a = (int(stats[i, cv2.CC_STAT_LEFT]),
                         int(stats[i, cv2.CC_STAT_TOP]),
                         int(stats[i, cv2.CC_STAT_WIDTH]),
                         int(stats[i, cv2.CC_STAT_HEIGHT]),
                         int(stats[i, cv2.CC_STAT_AREA]))
        if h < 10 or w < 10:
            continue
        blobs.append((x, y, w, h, a))
    if not blobs:
        return "", 0

    # area 기준 내림차순. 가장 큰 것의 50% 이상인 블롭만 유지(메인 텍스트 라인)
    blobs.sort(key=lambda b: -b[4])
    top_area = blobs[0][4]
    main_blobs = [b for b in blobs if b[4] >= top_area * 0.5]

    # 4) tight bbox
    x1 = min(b[0] for b in main_blobs)
    y1 = min(b[1] for b in main_blobs)
    x2 = max(b[0] + b[2] for b in main_blobs)
    y2 = max(b[1] + b[3] for b in main_blobs)
    pad = 4
    x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
    x2 = min(W, x2 + pad); y2 = min(H, y2 + pad)
    if x2 <= x1 or y2 <= y1:
        return "", 0

    # 원본 마스킹된 BW 영역만 잘라서 OCR
    cropped_mask = mask[y1:y2, x1:x2]
    bw = apply_mask_as_bw(bgr_roi[y1:y2, x1:x2], cropped_mask)
    bw_up = upscale_for_ocr(bw, factor=2.0)
    pil = Image.fromarray(bw_up)

    # PSM 6 (block) + lang=kor
    text, conf, _ = _ocr_image_to_data(pil, lang="kor", whitelist="", psm=6)
    return text, conf


# ---------- 챔피언 ROI 캐시 ----------

CACHE_MAX_AGE_HOURS = 24.0
CACHE_MIN_UNION_W_RATIO = 0.30   # union_w / ratio_roi_w 이 미만이면 캐싱 거부
CACHE_PAD_CHARS = 3              # 좌/우 패딩 (폰트 1자 분량 단위)
CACHE_EST_CHARS = 5              # "챔피언스 감독"의 한글 글자 수 (공백 제외)


def _measure_text_union_bbox(crop_bgr: np.ndarray
                             ) -> Optional[Tuple[int, int, int, int]]:
    """크롭 영역 내 흰색 텍스트 컴포넌트의 union bbox (crop 좌표계, x,y,w,h)."""
    if crop_bgr is None or crop_bgr.size == 0:
        return None
    mask = white_mask(crop_bgr)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 3))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    if n < 2:
        return None
    blobs = []
    for i in range(1, n):
        bx = int(stats[i, cv2.CC_STAT_LEFT])
        by = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        ba = int(stats[i, cv2.CC_STAT_AREA])
        if bh < 10 or bw < 10:
            continue
        blobs.append((bx, by, bw, bh, ba))
    if not blobs:
        return None
    blobs.sort(key=lambda b: -b[4])
    top_area = blobs[0][4]
    main = [b for b in blobs if b[4] >= top_area * 0.5]
    x1 = min(b[0] for b in main)
    y1 = min(b[1] for b in main)
    x2 = max(b[0] + b[2] for b in main)
    y2 = max(b[1] + b[3] for b in main)
    return (x1, y1, x2 - x1, y2 - y1)


def _load_champ_cache(settings_mgr, frame_size: Tuple[int, int]
                      ) -> Optional[Tuple[int, int, int, int]]:
    """캐시 로드 + 유효성 검증. 무효면 None."""
    if settings_mgr is None:
        return None
    try:
        c = settings_mgr.get("ocr.champion_bbox_cache", None)
        if not isinstance(c, dict):
            return None
        # 필드 검증
        for k in ("x", "y", "w", "h", "frame_w", "frame_h", "ts_ms"):
            if k not in c:
                return None
        # frame size 일치
        if int(c["frame_w"]) != int(frame_size[0]) or int(c["frame_h"]) != int(frame_size[1]):
            return None
        # stale 검사
        import time as _t
        age_ms = int(_t.time() * 1000) - int(c["ts_ms"])
        if age_ms > int(CACHE_MAX_AGE_HOURS * 3600 * 1000):
            return None
        return (int(c["x"]), int(c["y"]), int(c["w"]), int(c["h"]))
    except Exception:
        return None


def _save_champ_cache(settings_mgr, bbox: Tuple[int, int, int, int],
                      frame_size: Tuple[int, int]) -> None:
    if settings_mgr is None:
        return
    try:
        import time as _t
        settings_mgr.set("ocr.champion_bbox_cache", {
            "x": int(bbox[0]), "y": int(bbox[1]),
            "w": int(bbox[2]), "h": int(bbox[3]),
            "frame_w": int(frame_size[0]), "frame_h": int(frame_size[1]),
            "ts_ms": int(_t.time() * 1000),
        })
    except Exception:
        pass


def _invalidate_champ_cache(settings_mgr) -> None:
    if settings_mgr is None:
        return
    try:
        settings_mgr.set("ocr.champion_bbox_cache", None)
    except Exception:
        pass


def is_champions_tier(frame_bgr: np.ndarray,
                      settings_mgr=None) -> Tuple[bool, str]:
    """
    화면에 "챔피언스" 키워드가 포함된 티어 헤더가 존재하는지 검사.

    챔피언스 / 슈퍼 챔피언스 → True
    챌린저 / 슈퍼 챌린저 → False
    화면 자체가 다른 경우 → False

    캐시 흐름:
      1) frame_size 일치 + stale 아닌 캐시가 있으면 캐시 bbox로 OCR
      2) 캐시 미스 또는 캐시 사용 OCR 실패 → ratio ROI로 fallback
      3) 게이트 통과 시 union bbox 측정 + 좌/우 패딩 추가하여 캐시 저장

    반환: (is_champions, raw_ocr_text)
    """
    if frame_bgr is None or frame_bgr.size == 0:
        return False, ""

    H, W = frame_bgr.shape[:2]

    # ROI 비율 결정 (settings override 가능)
    ratio = DEFAULT_CHAMPION_ROI_RATIO
    base_wh: Optional[Tuple[int, int]] = None
    if settings_mgr is not None:
        try:
            v = settings_mgr.get("ocr.champion_roi_ratio", None)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                ratio = tuple(float(x) for x in v)
        except Exception:
            pass
        try:
            bw = settings_mgr.get("ocr.screen.w", None)
            bh = settings_mgr.get("ocr.screen.h", None)
            if isinstance(bw, (int, float)) and isinstance(bh, (int, float)) and bw > 0 and bh > 0:
                base_wh = (int(bw), int(bh))
        except Exception:
            pass

    # ratio ROI 계산 (캐시 fallback 용 + 캐싱 시 폭 검증 기준)
    rx, ry, rw, rh = _champion_roi_xywh(frame_bgr, ratio, base_wh=base_wh)
    rx = max(0, min(rx, W - 1)); ry = max(0, min(ry, H - 1))
    rw = max(1, min(rw, W - rx)); rh = max(1, min(rh, H - ry))

    # 1) 캐시 시도
    used_cache = False
    cache_xywh = _load_champ_cache(settings_mgr, (W, H))
    if cache_xywh is not None:
        cx, cy, cw, ch = cache_xywh
        cx = max(0, min(cx, W - 1)); cy = max(0, min(cy, H - 1))
        cw = max(1, min(cw, W - cx)); ch = max(1, min(ch, H - cy))
        crop = frame_bgr[cy:cy + ch, cx:cx + cw]
        text, _conf = _ocr_text_korean(crop)
        if _classify_tier(text):
            return True, text
        # 캐시 사용했는데 게이트 실패 → 무효화 + fallback
        _invalidate_champ_cache(settings_mgr)
        used_cache = True  # fallback 진입 표시 (디버그용)

    # 2) ratio ROI 사용 (캐시 미스 또는 fallback)
    x, y, w, h = rx, ry, rw, rh
    crop = frame_bgr[y:y + h, x:x + w]

    text, _conf = _ocr_text_korean(crop)
    is_champ = _classify_tier(text)

    # 3) 게이트 통과 시 캐시 학습
    if is_champ and settings_mgr is not None:
        try:
            ub = _measure_text_union_bbox(crop)
            if ub is not None:
                ub_x, ub_y, ub_w, ub_h = ub
                # 최소 폭 검증 — 너무 짧은 잡음 텍스트면 거부
                if ub_w >= int(rw * CACHE_MIN_UNION_W_RATIO):
                    char_w = max(1, ub_w // CACHE_EST_CHARS)
                    pad_x = CACHE_PAD_CHARS * char_w
                    pad_y = max(8, int(ub_h * 0.4))  # 상하는 한글 자모 안전 마진
                    # crop 좌표계 → frame 좌표계
                    abs_x = x + ub_x - pad_x
                    abs_y = y + ub_y - pad_y
                    abs_w = ub_w + 2 * pad_x
                    abs_h = ub_h + 2 * pad_y
                    # frame 경계 클램프
                    abs_x = max(0, abs_x); abs_y = max(0, abs_y)
                    abs_w = min(abs_w, W - abs_x); abs_h = min(abs_h, H - abs_y)
                    _save_champ_cache(settings_mgr, (abs_x, abs_y, abs_w, abs_h), (W, H))
        except Exception:
            pass

    # 디버그 dump: NOT_CHAMPIONS 판정 시 ROI crop과 mask를 디스크 저장
    if (not is_champ) and (settings_mgr is not None):
        try:
            if bool(settings_mgr.get("ocr.debug_champ_dump", False)):
                import os, time as _t
                from path_manager import BASE_DIR
                dump_dir = os.path.join(BASE_DIR, "debug_champ")
                os.makedirs(dump_dir, exist_ok=True)
                ts = _t.strftime("%Y%m%d_%H%M%S") + f"_{int(_t.time()*1000)%1000:03d}"
                # crop 저장 (한글 경로 안전)
                ok, buf = cv2.imencode(".png", crop)
                if ok:
                    with open(os.path.join(dump_dir, f"{ts}_crop.png"), "wb") as f:
                        f.write(buf.tobytes())
                # raw text 저장
                with open(os.path.join(dump_dir, f"{ts}_raw.txt"), "w", encoding="utf-8") as f:
                    f.write(f"frame_shape={W}x{H}\n")
                    f.write(f"base_wh={base_wh}\n")
                    f.write(f"ratio={ratio}\n")
                    f.write(f"roi_xywh=({x},{y},{w},{h})\n")
                    f.write(f"cache_fallback={used_cache}\n")
                    f.write(f"conf={_conf}\n")
                    f.write(f"text={text!r}\n")
        except Exception:
            pass

    return is_champ, text


def _classify_tier(ocr_text: str) -> bool:
    """OCR 결과 텍스트에서 챔피언스 계열 키워드 검출.

    매칭 키워드: '챔피' / '피언' / '언스' 셋 중 하나 + '감독' 동시 존재.
    오인식에 강건하도록 셋 중 하나만 잡혀도 통과.
    '챌린저' 계열에는 챔/피/언/스 글자가 단 하나도 없어서 false positive 거의 0.
    """
    cleaned = (ocr_text or "").replace(" ", "").replace("\n", "")
    has_keyword = any(k in cleaned for k in ("챔피", "피언", "언스"))
    has_director = "감독" in cleaned
    return has_keyword and has_director


# ============================================================
# 자릿수 빠른 게이트 — OCR 없이 컴포넌트 카운트만
# ============================================================

def quick_digit_count(bgr_roi: np.ndarray) -> int:
    """
    HSV 마스킹 + 단절 필터 후 살아남은 주요 컴포넌트 개수 반환.
    OCR 호출 없이 ~1ms로 자릿수 추정.

    용도: 목표 범위와 자릿수가 명백히 다르면 OCR 스킵.
    """
    if bgr_roi is None or bgr_roi.size == 0:
        return 0
    mask = white_mask(bgr_roi)
    comps = _components_from_mask(mask)
    kept = filter_by_height_gap(comps)
    return len(kept)


# ============================================================
# Reference Template Matching — 폰트 기반 숫자/콤마 인식
# ============================================================
#
# 설계:
#   - Cruyff Sans Condensed Medium TTF로 0-9 + ',' reference 렌더링
#   - 컴포넌트 height에 맞춰 자연 비율 (폰트 본래 폭 유지) 동적 렌더링
#   - 컴포넌트와 ref 둘 다 same canvas (수직 중앙 / 좌측 정렬) 후 1:1 NCC
#   - 폰트 동일 → self-match ~0.99, confusable 마진 충분 (자체 실측 검증)
#   - hole count 같은 보조 feature 불필요 (letterbox stretch가 원인이었음)
#
# 분기:
#   - 큰 컴포넌트 수 ≥ 7 → 한글로 판단 (Tesseract kor fallback)
#   - 모든 컴포넌트 NCC top < 0.50 → 매칭 실패 (Tesseract fallback)
#   - 어떤 컴포넌트의 마진 < 0.03 → OCR_UNCERTAIN

_REF_TTF_NAME    = "CruyffSansCondensed-Medium.ttf"
_REF_CHARS       = "0123456789,"
_REF_TOP_MIN     = 0.50       # 미만이면 매칭 실패 (한글/노이즈)
_REF_MARGIN_MIN  = 0.015      # 미만이면 OCR_UNCERTAIN. 영역별 min NCC로 점수가
                              # 전반적으로 더 보수적이라 절대 임계도 보수적으로 조정.
                              # 0의 자기매칭 점수가 게임/ref 폭 미세차로 다른 글자보다
                              # 살짝 낮은 특성(실측 0.61 vs 0.83) 반영.
_REF_HANGUL_GATE = 7          # 큰 컴포넌트 수 이 이상이면 한글 분기

_REF_FONT_PATH: Optional[str] = None
_REF_LOCK = threading.Lock()
_REF_CACHE: Dict[int, Dict[str, np.ndarray]] = {}   # {target_h: {char: array}}
_REF_CACHE_MAX  = 5                                  # LRU 크기


def _get_ref_font_path() -> str:
    global _REF_FONT_PATH
    if _REF_FONT_PATH is None:
        try:
            from path_manager import get_resource_path
            _REF_FONT_PATH = get_resource_path(_REF_TTF_NAME)
        except Exception:
            _REF_FONT_PATH = _REF_TTF_NAME
    return _REF_FONT_PATH


def _render_at_height(char: str, target_height: int) -> Optional[np.ndarray]:
    """글자를 정확히 target_height 픽셀로 자연 비율 렌더링.
    폰트가 그린 그대로의 폭 유지(stretch 없음). 글리프 없으면 None.
    """
    if target_height < MIN_ABSOLUTE_HEIGHT:
        return None
    try:
        font_size = max(20, target_height * 2)
        font = ImageFont.truetype(_get_ref_font_path(), font_size)
        bbox = font.getbbox(char)
        x0, y0, x1, y1 = bbox
        w = max(1, x1 - x0); h = max(1, y1 - y0)
        img = Image.new("L", (w + 8, h + 8), 0)
        draw = ImageDraw.Draw(img)
        draw.text((-x0 + 4, -y0 + 4), char, fill=255, font=font)
        arr = np.array(img)
        ys, xs = np.where(arr > 32)
        if len(xs) == 0:
            return None
        cropped = arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
        ch, cw = cropped.shape
        scale = target_height / ch
        new_w = max(1, int(round(cw * scale)))
        return cv2.resize(cropped, (new_w, target_height), interpolation=cv2.INTER_AREA)
    except Exception:
        return None


def _get_refs_at_height(target_h: int) -> Dict[str, np.ndarray]:
    """target_h 에 맞는 ref 세트 반환. LRU 캐시."""
    with _REF_LOCK:
        cached = _REF_CACHE.get(target_h)
        if cached is not None:
            return cached
        if len(_REF_CACHE) >= _REF_CACHE_MAX:
            oldest = next(iter(_REF_CACHE))
            _REF_CACHE.pop(oldest, None)
        refs = {c: _render_at_height(c, target_h) for c in _REF_CHARS}
        _REF_CACHE[target_h] = refs
        return refs


def _match_natural(comp_bw: np.ndarray, refs: Dict[str, np.ndarray]
                   ) -> Tuple[Optional[str], float, float]:
    """컴포넌트(BW)와 ref들 자연 비율 NCC 매칭.

    캔버스 중앙 정렬 (수평 + 수직). 폭이 다른 글자(좁은 1, 넓은 4)도 가운데 위치
    → 좌측 정렬 시 발생하던 우측 가장자리 패딩-vs-글자 mismatch 인공물 제거.

    영역별 분할 매칭:
      - full (전체)
      - top    (위쪽 절반)
      - bottom (아래쪽 절반)
    최종 점수 = min(full, top, bottom) — 어느 영역도 약하면 안 됨.

    이유:
      - 0/9: 위쪽 절반은 둘 다 닫힌 원 (full만 보면 유사) → 아래쪽에서 갈라짐(0=닫힘, 9=직선)
      - 0/6: 아래쪽은 둘 다 닫힌 원 → 위쪽에서 갈라짐(0=닫힘, 6=좌측만)
      - 8/3 등: 영역별로 모든 곳이 일치해야 정답
    min 채택으로 confusable 자동 분리.

    반환: (top_char, top_score, margin) — 매칭 불가 시 (None, 0, 0).
    """
    ch, cw = comp_bw.shape
    scores: List[Tuple[str, float]] = []
    for c, ref in refs.items():
        if ref is None:
            continue
        rh, rw = ref.shape
        H, W = max(ch, rh), max(cw, rw)
        c_canvas = np.zeros((H, W), dtype=np.uint8)
        r_canvas = np.zeros((H, W), dtype=np.uint8)
        # 수평 + 수직 둘 다 중앙 정렬
        c_canvas[(H - ch) // 2:(H - ch) // 2 + ch,
                 (W - cw) // 2:(W - cw) // 2 + cw] = comp_bw
        r_canvas[(H - rh) // 2:(H - rh) // 2 + rh,
                 (W - rw) // 2:(W - rw) // 2 + rw] = ref
        try:
            full = float(cv2.matchTemplate(c_canvas, r_canvas, cv2.TM_CCOEFF_NORMED)[0, 0])
            half_h = H // 2
            top    = float(cv2.matchTemplate(c_canvas[:half_h],  r_canvas[:half_h],
                                              cv2.TM_CCOEFF_NORMED)[0, 0])
            bot    = float(cv2.matchTemplate(c_canvas[half_h:],  r_canvas[half_h:],
                                              cv2.TM_CCOEFF_NORMED)[0, 0])
            scores.append((c, min(full, top, bot)))
        except Exception:
            continue
    if not scores:
        return None, 0.0, 0.0
    scores.sort(key=lambda x: -x[1])
    top_char, top_score = scores[0]
    second = scores[1][1] if len(scores) >= 2 else 0.0
    return top_char, top_score, top_score - second


def _digits_via_ref(bgr_roi: np.ndarray, allow_comma: bool
                    ) -> Tuple[str, str, Dict[str, Any]]:
    """ROI → 폰트 reference matching 으로 자릿수 추출.

    반환: (digits_str, kind, info)
      digits_str: 콤마 제거된 숫자 문자열 ('' if 실패)
      kind: 'NUMERIC' | 'NEEDS_HANGUL' | 'NEEDS_FALLBACK' | 'EMPTY'
      info: top_min, margin_min, big_count, picks
    """
    info: Dict[str, Any] = {
        "top_min": 1.0, "margin_min": 1.0, "big_count": 0,
        "picks": [], "all_count": 0,
    }
    if bgr_roi is None or bgr_roi.size == 0:
        return "", "EMPTY", info

    mask = white_mask(bgr_roi)
    all_comps = _components_from_mask(mask)
    info["all_count"] = len(all_comps)
    if not all_comps:
        return "", "EMPTY", info

    kept_big = filter_by_height_gap(all_comps)
    info["big_count"] = len(kept_big)

    if len(kept_big) >= _REF_HANGUL_GATE:
        return "", "NEEDS_HANGUL", info

    # 콤마 후보: kept_big 외 작은 컴포넌트 중 큰 글자 높이의 ~1/4~1/2 사이
    candidates = list(kept_big)
    if allow_comma and kept_big:
        max_h = max(c[3] for c in kept_big)
        comma_min_h = max(MIN_ABSOLUTE_HEIGHT, max_h // 4)
        comma_max_h = max(comma_min_h, max_h // 2)
        kept_set = {(c[0], c[1]) for c in kept_big}
        for c in all_comps:
            if (c[0], c[1]) in kept_set:
                continue
            if comma_min_h <= c[3] <= comma_max_h:
                candidates.append(c)

    candidates.sort(key=lambda c: c[0])  # 좌→우

    picks: List[Tuple[str, float, float, int]] = []
    for c in candidates:
        x, y, w, h, _ = c
        bw = mask[y:y + h, x:x + w]
        refs = _get_refs_at_height(h)
        top_char, top_score, margin = _match_natural(bw, refs)
        if top_char is None:
            continue
        if top_score < _REF_TOP_MIN:
            # 폰트 매칭 실패 — 한글 또는 노이즈
            return "", "NEEDS_FALLBACK", info
        info["top_min"]    = min(info["top_min"], top_score)
        info["margin_min"] = min(info["margin_min"], margin)
        picks.append((top_char, top_score, margin, x))

    info["picks"] = picks
    if not picks:
        return "", "NEEDS_FALLBACK", info

    if info["margin_min"] < _REF_MARGIN_MIN:
        # 마진 부족 컴포넌트 — fallback 시도
        return "", "NEEDS_FALLBACK", info

    digits = "".join([p[0] for p in picks]).replace(",", "")
    return digits, "NUMERIC", info


def read_points_v2_ref(bgr_roi: np.ndarray) -> OcrResult:
    """점수 OCR — reference matching 우선, 실패 시 Tesseract fallback."""
    digits, kind, info = _digits_via_ref(bgr_roi, allow_comma=True)
    if kind == "NUMERIC" and digits:
        try:
            val = int(digits)
            return OcrResult(
                value=val, kind="NUMERIC", raw=digits,
                word_conf=int(info["top_min"] * 100),
                digit_count=len(digits),
            )
        except Exception:
            pass
    # fallback: 기존 Tesseract 점수 OCR
    return read_points_v2(bgr_roi)


def _maybe_dump_rank_roi(bgr_roi: np.ndarray, digits: str, info: Dict[str, Any],
                         settings_mgr=None) -> None:
    """등수 OCR 의심 케이스 자동 dump.

    조건: 결과에 0/6/9 포함 + (마진 좁음 OR 점수 낮음). 다음 라운드 fine-tune 진단용.
    토글: settings.ocr.debug_rank_dump (기본 False).
    """
    try:
        from path_manager import BASE_DIR
        if settings_mgr is None or not bool(settings_mgr.get("ocr.debug_rank_dump", False)):
            return
        # 0/6/9 confusable 의심 글자가 있고 마진/점수가 낮을 때만 dump
        has_risky = any(d in digits for d in ("0", "6", "9"))
        if not has_risky:
            return
        if info.get("top_min", 1.0) >= 0.80 and info.get("margin_min", 1.0) >= 0.10:
            return  # 충분히 안전 — dump 불필요
        import os, time as _t
        dump_dir = os.path.join(str(BASE_DIR), "debug_rank")
        os.makedirs(dump_dir, exist_ok=True)
        ts = _t.strftime("%Y%m%d_%H%M%S") + f"_{int(_t.time()*1000)%1000:03d}"
        fn = f"{ts}_{digits}_top{info['top_min']:.2f}_mg{info['margin_min']:.2f}.png"
        ok, buf = cv2.imencode(".png", bgr_roi)
        if ok:
            with open(os.path.join(dump_dir, fn), "wb") as f:
                f.write(buf.tobytes())
    except Exception:
        pass


def read_rank_v2_ref(bgr_roi: np.ndarray, settings_mgr=None) -> OcrResult:
    """등수 OCR — reference matching 우선, 한글/실패 시 Tesseract fallback.

    settings_mgr: dump 토글 확인용 (선택). None이면 dump 안 함.
    """
    digits, kind, info = _digits_via_ref(bgr_roi, allow_comma=False)
    if kind == "NUMERIC" and digits:
        try:
            val = int(digits)
            if 0 < val <= 9999:
                _maybe_dump_rank_roi(bgr_roi, digits, info, settings_mgr)
                return OcrResult(
                    value=val, kind="NUMERIC", raw=digits,
                    word_conf=int(info["top_min"] * 100),
                    digit_count=len(digits),
                )
        except Exception:
            pass
    # kind in (NEEDS_HANGUL, NEEDS_FALLBACK, EMPTY) — 기존 한글/Tesseract 분기
    return read_rank_v2(bgr_roi)
