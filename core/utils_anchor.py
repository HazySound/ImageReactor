# core/utils_anchor.py
from __future__ import annotations
import os, json
from typing import Optional


def load_home_anchor_threshold(default: float = 0.88) -> float:
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ptr_path = os.path.join(root_dir, "home_anchor.json")
    try:
        if not os.path.exists(ptr_path):
            return float(default)
        with open(ptr_path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        # ★ 'threshold'와 'ncc_threshold' 모두 허용
        for k in ("threshold", "ncc_threshold"):
            v = data.get(k)
            if isinstance(v, (int, float)):
                return float(v)
        return float(default)
    except Exception:
        return float(default)
