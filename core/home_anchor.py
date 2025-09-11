import json
import os
from pathlib import Path
import path_manager as pm  # 절대 임포트

HOME_ANCHOR_PATH = Path("home_anchor.json")  # ★ 상대경로


def _pick_template_field(d: dict) -> str | None:
    """
    home_anchor.json의 다양한 스키마를 허용적으로 처리:
    - {"image": "s2.png"}  (권장)
    - {"template": "s2.png"}
    - {"filename": "s2.png"}
    - {"path": "C:/.../s2.png"}  (절대/상대 모두 허용)
    반환: 해석된 경로 문자열(없으면 None)
    """
    for key in ("image", "template", "filename", "path"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def is_home_configured() -> bool:
    try:
        if not HOME_ANCHOR_PATH.exists():
            return False
        with HOME_ANCHOR_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        cand = _pick_template_field(data)
        if not cand:
            return False
        p = Path(cand)
        if not p.is_absolute():
            p = Path(pm.get_img_path()) / cand
        return p.resolve().exists()
    except Exception:
        return False
