# core/roi_from_settings.py
from __future__ import annotations
from typing import Tuple, Dict, Optional
import numpy as np
from ocr.metrics_reader import HomeOCRConfig, read_metrics_from_home


def read_rois_xyxy_from_settings(settings_mgr) -> tuple[dict[str, tuple[int,int,int,int]], tuple[int,int] | None]:
    """settings['ocr']에서 roi_points/roi_rank와 기준 스크린(w,h) 읽어 xyxy와 base 반환."""
    try:
        s = settings_mgr.to_dict()
    except Exception:
        s = getattr(settings_mgr, "_data", {}) or {}
    ocr = s.get("ocr", {}) or {}

    base = None
    scr = ocr.get("screen")
    if isinstance(scr, dict) and "w" in scr and "h" in scr:
        base = (int(scr["w"]), int(scr["h"]))

    rois: Dict[str, tuple[int,int,int,int]] = {}
    for key in ("roi_points", "roi_rank"):
        v = ocr.get(key)
        if isinstance(v, (list, tuple)) and len(v) == 4:
            x, y, w, h = map(int, v)
            if w > 0 and h > 0:
                rois[key] = (x, y, x + w, y + h)
    return rois, base


def scale_xyxy_from_base(xyxy: tuple[int,int,int,int] | None,
                         base_wh: tuple[int,int] | None,
                         cur_wh: tuple[int,int] | None) -> tuple[int,int,int,int] | None:
    if not xyxy or not base_wh or not cur_wh:
        return xyxy
    bw, bh = base_wh; cw, ch = cur_wh
    sx = (cw / float(bw)) if bw > 0 else 1.0
    sy = (ch / float(bh)) if bh > 0 else 1.0
    x1, y1, x2, y2 = xyxy
    return (int(round(x1*sx)), int(round(y1*sy)), int(round(x2*sx)), int(round(y2*sy)))


def xyxy_to_xywh(rc: tuple[int,int,int,int] | None) -> tuple[int,int,int,int]:
    if not rc: return (0,0,0,0)
    x1, y1, x2, y2 = rc
    return (x1, y1, max(0, x2-x1), max(0, y2-y1))


def read_rois_xywh_from_settings(settings_mgr) -> tuple[dict[str, tuple[int,int,int,int]], tuple[int,int] | None]:
    """
    settings에서 roi_points/roi_rank를 (x,y,w,h)로 읽는다.
    1순위: settings_mgr.get('ocr.roi_points'), get('ocr.roi_rank'), get('ocr.screen.w/h')
    2순위: to_dict() 기반 트리/플랫 키 폴백
    """
    rois: dict[str, tuple[int,int,int,int]] = {}

    # ---- 1) get()로 직접 읽기 (가장 신뢰 가능) ----
    try:
        pts = settings_mgr.get("ocr.roi_points", None)
        rnk = settings_mgr.get("ocr.roi_rank", None)
        bw  = settings_mgr.get("ocr.screen.w", None)
        bh  = settings_mgr.get("ocr.screen.h", None)
    except Exception:
        pts = rnk = bw = bh = None

    base = None
    if isinstance(bw, (int, float)) and isinstance(bh, (int, float)):
        base = (int(bw), int(bh))

    def _coerce_xywh(v):
        if isinstance(v, (list, tuple)) and len(v) == 4:
            try:
                x, y, w, h = map(int, v)
                if w > 0 and h > 0:
                    return (x, y, w, h)
            except Exception:
                return None
        return None

    pts_xywh = _coerce_xywh(pts)
    rnk_xywh = _coerce_xywh(rnk)

    # get()에서 둘 다 확보되면 그대로 반환
    if pts_xywh or rnk_xywh:
        if pts_xywh: rois["roi_points"] = pts_xywh
        if rnk_xywh: rois["roi_rank"]   = rnk_xywh
        return rois, base

    # ---- 2) to_dict() 폴백 (트리/플랫 모두 시도) ----
    try:
        s = settings_mgr.to_dict()
    except Exception:
        s = getattr(settings_mgr, "_data", {}) or {}

    # base
    if base is None:
        scr_obj = None
        if isinstance(s.get("ocr", None), dict):
            scr_obj = s["ocr"].get("screen")
        if scr_obj is None:
            wv = s.get("ocr.screen.w", None)
            hv = s.get("ocr.screen.h", None)
            if wv is not None and hv is not None:
                try:
                    scr_obj = {"w": int(wv), "h": int(hv)}
                except Exception:
                    scr_obj = None
        if isinstance(scr_obj, dict) and "w" in scr_obj and "h" in scr_obj:
            try:
                base = (int(scr_obj["w"]), int(scr_obj["h"]))
            except Exception:
                base = None

    # rois
    def _get_roi_list_from_dict(name: str):
        if isinstance(s.get("ocr", None), dict):
            v = s["ocr"].get(name)
            if isinstance(v, (list, tuple)) and len(v) == 4:
                return v
        v = s.get(f"ocr.{name}", None)
        if isinstance(v, (list, tuple)) and len(v) == 4:
            return v
        return None

    for key in ("roi_points", "roi_rank"):
        v = _get_roi_list_from_dict(key)
        c = _coerce_xywh(v)
        if c:
            rois[key] = c

    return rois, base


def scale_xywh_from_base(xywh: tuple[int,int,int,int] | None,
                         base_wh: tuple[int,int] | None,
                         cur_wh: tuple[int,int] | None) -> tuple[int,int,int,int] | None:
    """(x,y,w,h)을 기준 해상도→현재 해상도로 선형 스케일."""
    if not xywh or not base_wh or not cur_wh:
        return xywh
    bw, bh = base_wh; cw, ch = cur_wh
    if bw <= 0 or bh <= 0:
        return xywh
    sx = cw / float(bw)
    sy = ch / float(bh)
    x, y, w, h = xywh
    nx = int(round(x * sx))
    ny = int(round(y * sy))
    nw = max(1, int(round(w * sx)))
    nh = max(1, int(round(h * sy)))
    return (nx, ny, nw, nh)


def run_home_ocr_like_gui(frame_bgr: np.ndarray, settings_mgr) -> dict:
    """GUI 테스트와 동일: settings.json의 (x,y,w,h)을 그대로 사용해 현재 프레임에 스케일→HomeOCRConfig→read_metrics_from_home."""
    h, w = frame_bgr.shape[:2]
    rois_xywh, base = read_rois_xywh_from_settings(settings_mgr)
    if not rois_xywh:
        return {}

    pts_xywh  = scale_xywh_from_base(rois_xywh.get("roi_points"), base, (w, h))
    rank_xywh = scale_xywh_from_base(rois_xywh.get("roi_rank"),   base, (w, h))

    if not pts_xywh and not rank_xywh:
        return {}

    def _pad(rc, pad=2):
        if not rc: return rc
        x, y, w, h = rc
        return (max(0, x - pad), max(0, y - pad), w + pad * 2, h + pad * 2)

    pts_xywh = _pad(pts_xywh, 2)
    rank_xywh = _pad(rank_xywh, 2)

    cfg = HomeOCRConfig(
        roi_rank   = rank_xywh or (0, 0, 0, 0),
        roi_points = pts_xywh  or (0, 0, 0, 0),
        bin_thresh=170, invert=True, open_ksize=1, dilate_ksize=0, scale=1.0
    )
    return read_metrics_from_home(frame_bgr, cfg)
