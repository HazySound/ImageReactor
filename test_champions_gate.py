"""
챔피언스 티어 게이트 단독 검증 스크립트.

사용법:
    venv\\Scripts\\python.exe test_champions_gate.py <image_path> [<image_path> ...]

예시:
    venv\\Scripts\\python.exe test_champions_gate.py capture1.png
    venv\\Scripts\\python.exe test_champions_gate.py "챔스화면 캡처.png" "슈챌화면 캡처.png"

또는 인자 없이 실행하면 프로젝트 루트의 캡처 파일 후보를 자동 검색.
"""
from __future__ import annotations
import sys
import os
import io
import time
from pathlib import Path

import cv2
import numpy as np

# Windows 콘솔에서 UTF-8 출력 강제 (cp949 인코딩 오류 회피)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    else:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

# Tesseract 경로 자동 설정 (settings.json 의 ocr.tesseract_path 사용)
def _setup_tesseract() -> None:
    try:
        from core import ocr as _ocr
        import json
        sp = Path("data/settings.json")
        tess_path = ""
        if sp.exists():
            try:
                s = json.loads(sp.read_text(encoding="utf-8"))
                tess_path = (s.get("ocr") or {}).get("tesseract_path") or ""
            except Exception:
                pass
        if not tess_path:
            tess_path = _ocr.auto_find_tesseract_path() or ""
        if tess_path:
            _ocr.set_tesseract_path(tess_path)
            print(f"[setup] tesseract: {tess_path}")
        else:
            print("[setup] WARN: tesseract path not found — OCR will likely fail")
    except Exception as e:
        print(f"[setup] error: {e}")


def _imread(path: str) -> np.ndarray | None:
    """한글 파일명 안전 로드."""
    try:
        with open(path, "rb") as f:
            data = np.frombuffer(f.read(), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _make_settings():
    """SettingsManager 인스턴스를 반환. base 해상도 등 ROI 매핑에 필요."""
    try:
        from pathlib import Path as _P
        from core.settings_manager import SettingsManager
        return SettingsManager(_P("data/settings.json"))
    except Exception as e:
        print(f"[settings] load failed: {e}")
        return None


def test_one(image_path: str, settings_mgr) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {image_path}")
    print(f"{'=' * 70}")

    bgr = _imread(image_path)
    if bgr is None:
        print("  ERROR: 이미지 로드 실패")
        return
    H, W = bgr.shape[:2]
    print(f"  크기: {W}x{H}")

    from core import ocr_v2

    t0 = time.time()
    is_champ, raw_text = ocr_v2.is_champions_tier(bgr, settings_mgr)
    elapsed = (time.time() - t0) * 1000

    print(f"  소요: {elapsed:.0f}ms")
    print(f"  raw OCR: {raw_text!r}")
    print(f"  -> 챔피언스 티어 여부: {'[TRUE]' if is_champ else '[FALSE]'}")

    if is_champ:
        print("  판정 근거: '챔피'/'피언'/'언스' 중 하나 + '감독' 키워드 모두 검출됨")
    else:
        if not raw_text.strip():
            print("  판정 근거: OCR 결과 비어있음 (영역 텍스트 미인식)")
        else:
            cleaned = raw_text.replace(" ", "").replace("\n", "")
            kw_found = [k for k in ("챔피", "피언", "언스") if k in cleaned]
            has_dir = "감독" in cleaned
            print(f"  판정 근거: 키워드 검출={kw_found}, '감독' 검출={has_dir}")
            print("  (둘 다 만족해야 챔피언스 판정. 챌린저/슈퍼챌린저 화면이면 정상)")


def main():
    _setup_tesseract()
    settings_mgr = _make_settings()
    if settings_mgr:
        base_w = settings_mgr.get("ocr.screen.w")
        base_h = settings_mgr.get("ocr.screen.h")
        print(f"[settings] base resolution: {base_w}x{base_h}")
        print(f"[settings] champion_roi_ratio: {settings_mgr.get('ocr.champion_roi_ratio')}")

    args = sys.argv[1:]
    if not args:
        # 인자 없으면 프로젝트 루트에서 후보 자동 검색
        candidates = []
        for ext in ("*.png", "*.jpg", "*.jpeg"):
            candidates.extend([str(p) for p in Path(".").glob(ext)])
        # 캡처/화면 키워드 있는 것 우선
        priority = [p for p in candidates
                    if any(k in p.lower() for k in ("capture", "캡처", "화면", "champ", "챔스", "슈챌"))]
        args = priority if priority else candidates[:5]
        if args:
            print(f"\n[auto] 인자 없음 → 자동 후보 {len(args)}개:")
            for a in args:
                print(f"  - {a}")

    if not args:
        print("\n사용법: python test_champions_gate.py <image_path> [...]")
        return

    for path in args:
        if not os.path.exists(path):
            print(f"\n[SKIP] 파일 없음: {path}")
            continue
        try:
            test_one(path, settings_mgr)
        except Exception as e:
            print(f"\n[ERROR] {path}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
