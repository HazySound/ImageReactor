# core/image_utils.py
from __future__ import annotations
from typing import Optional, Tuple
import numpy as np

ROI = Tuple[int, int, int, int]


def safe_crop(img: np.ndarray, roi: Optional[ROI]) -> np.ndarray:
    """
    img: BGR ndarray
    roi: (x, y, w, h) or None
    - 음수/초과 좌표를 경계 내로 클램프
    - w/h <= 0 이거나 roi=None이면 원본 img 그대로 반환
    """
    if img is None or roi is None:
        return img
    try:
        x, y, w, h = map(int, roi)
    except Exception:
        return img
    H, W = img.shape[:2]
    if w <= 0 or h <= 0 or W <= 0 or H <= 0:
        return img
    x = max(0, min(x, W - 1))
    y = max(0, min(y, H - 1))
    w = max(1, min(w, W - x))
    h = max(1, min(h, H - y))
    return img[y:y + h, x:x + w]
