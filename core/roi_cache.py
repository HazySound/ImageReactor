# core/roi_cache.py
from __future__ import annotations

import json
import os
import time
import hashlib
import threading
from typing import Optional, Tuple, Dict, Any

ROI = Tuple[int, int, int, int]
Size = Tuple[int, int]

# 선택적 path_manager 연동 (있으면 사용, 없으면 안전한 폴백)
try:
    import path_manager as pm  # type: ignore
except Exception:
    pm = None  # type: ignore

_LOCK = threading.RLock()


def _project_root() -> str:
    # 이 파일: <ROOT>/core/roi_cache.py
    return os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _cache_path() -> str:
    """
    ROI 캐시 파일 경로를 반환한다.
    - 우선순위 1: path_manager.DATA_DIR("roi_cache.json")
    - 폴백: <ROOT>/data/roi_cache.json
    """
    if pm is not None and hasattr(pm, "DATA_DIR"):
        try:
            v = pm.DATA_DIR("roi_cache.json") if callable(pm.DATA_DIR) else os.path.join(pm.DATA_DIR, "roi_cache.json")
            os.makedirs(os.path.dirname(v), exist_ok=True)
            return v
        except Exception:
            pass
    path = os.path.join(_project_root(), "data", "roi_cache.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _read() -> Dict[str, Any]:
    path = _cache_path()
    if not os.path.exists(path):
        return {"version": 1, "items": {}}
    with _LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "version" not in data:
                data["version"] = 1
            if "items" not in data or not isinstance(data["items"], dict):
                data["items"] = {}
            return data
        except Exception:
            # 손상 시 초기화
            return {"version": 1, "items": {}}


def _write(data: Dict[str, Any]) -> None:
    path = _cache_path()
    tmp = path + ".tmp"
    with _LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


def _norm_path(p: str) -> str:
    ap = os.path.abspath(p)
    root = _project_root()
    try:
        rel = os.path.relpath(ap, root)
        if not rel.startswith(".."):
            ap = rel
    except Exception:
        pass
    return ap.replace("\\", "/")


def make_template_id(template_path: str) -> str:
    """사람이 읽을 수 있게, 루트 기준 상대경로(가능하면)로 키를 만든다."""
    return _norm_path(template_path)


def _sha1_file(path: str) -> Optional[str]:
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def load_roi(template_path: str, screen_size: Optional[Size] = None, auto_scale: bool = True) -> Optional[ROI]:
    """
    저장된 ROI를 로드한다.
    - screen_size가 주어지면 저장된 화면 크기와 다를 경우 비례 스케일을 적용한다.
    - 스케일 후 화면 경계로 클램프한다.
    """
    tid = make_template_id(template_path)
    data = _read()
    item = data["items"].get(tid)
    if not item:
        return None

    roi = tuple(item.get("roi", []))
    if len(roi) != 4:
        return None

    # 화면 크기 차이에 따른 비례 스케일
    if screen_size and auto_scale:
        saved = item.get("screen", {})
        w0, h0 = saved.get("w"), saved.get("h")
        if isinstance(w0, (int, float)) and isinstance(h0, (int, float)):
            w1, h1 = int(screen_size[0]), int(screen_size[1])
            if w0 > 0 and h0 > 0 and (w0 != w1 or h0 != h1):
                sx, sy = w1 / float(w0), h1 / float(h0)
                x, y, w, h = roi
                roi = (
                    int(round(x * sx)),
                    int(round(y * sy)),
                    max(1, int(round(w * sx))),
                    max(1, int(round(h * sy))),
                )

    # 화면 경계 클램프
    if screen_size:
        x, y, w, h = roi
        sw, sh = screen_size
        x = max(0, min(x, max(0, sw - 1)))
        y = max(0, min(y, max(0, sh - 1)))
        w = max(1, min(w, max(1, sw - x)))
        h = max(1, min(h, max(1, sh - y)))
        roi = (x, y, w, h)

    return roi  # type: ignore


def commit_roi(
    template_path: str,
    roi: ROI,
    screen_size: Optional[Size],
    tpl_meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    템플릿의 ROI를 '처음 한 번만' 저장한다.
    이미 존재하면 저장하지 않고 False, 새로 저장하면 True를 반환.
    """
    tid = make_template_id(template_path)
    data = _read()
    items = data["items"]
    if tid in items:
        return False

    x, y, w, h = roi
    entry = {
        "roi": [int(x), int(y), int(w), int(h)],
        "screen": {
            "w": int(screen_size[0]) if screen_size else None,
            "h": int(screen_size[1]) if screen_size else None,
        },
        "tpl": {
            "path": _norm_path(template_path),
            "mtime": os.path.getmtime(template_path) if os.path.exists(template_path) else None,
            "sha1": _sha1_file(template_path),
        },
        "first_committed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if tpl_meta:
        entry["tpl"].update(tpl_meta)

    items[tid] = entry
    _write(data)
    return True


def cache_path() -> str:
    """캐시 파일의 절대 경로를 돌려준다(디버그용)."""
    return _cache_path()


def drop_roi(template_path: str) -> bool:
    """
    특정 템플릿의 ROI 엔트리를 삭제한다(개발/운영자용).
    실제 런타임에서는 자동 삭제/갱신을 절대 하지 않는다.
    """
    tid = make_template_id(template_path)
    data = _read()
    if tid in data["items"]:
        del data["items"][tid]
        _write(data)
        return True
    return False
