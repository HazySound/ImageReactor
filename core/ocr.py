# (참고용 안내 전용) 반드시 파일 상단에 추가
import re
import os
import sys
import winreg
from shutil import which
from typing import Optional
from PIL import Image, ImageOps, ImageFilter
import pytesseract
import shutil
import ctypes
from pathlib import Path


_TESS_CFG_CACHE = None

# 모듈 캐시(한 번만 계산)
_RESOLVED_TESSDATA_DIR = None

# 언어 가용성 캐시 (tessdata_dir → frozenset)
_AVAIL_LANGS_CACHE: dict = {}

# === (초저사양 최적화) Tesseract 쓰레드/우선순위 주입 ===
# 1) OpenMP 스레드 상한: Tesseract 내부 병렬화를 1코어로 제한
os.environ.setdefault("OMP_THREAD_LIMIT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def _install_lowprio_for_tesseract() -> None:
    """
    pytesseract가 띄우는 tesseract.exe를 'Below Normal' 우선순위로 실행.
    Windows에서만 적용, 1회 주입.
    """
    try:
        if not sys.platform.startswith("win"):
            return
        # pytesseract 내부 subprocess.Popen 래핑
        _orig_popen = pytesseract.pytesseract.subprocess.Popen  # type: ignore[attr-defined]
        BELOW_NORMAL = 0x00004000  # Windows Priority Class

        def _lowprio_popen(*args, **kwargs):
            # tesseract 호출만 대상으로 한정
            try:
                cmd0 = None
                if args and isinstance(args[0], (list, tuple)):
                    cmd0 = args[0][0]
                elif "args" in kwargs:
                    a = kwargs["args"]
                    cmd0 = a[0] if isinstance(a, (list, tuple)) else None
                is_tess = (cmd0 and "tesseract" in str(cmd0).lower())
            except Exception:
                is_tess = False

            if is_tess:
                flags = kwargs.get("creationflags", 0) | BELOW_NORMAL
                kwargs["creationflags"] = flags
            return _orig_popen(*args, **kwargs)

        if not getattr(pytesseract.pytesseract, "_lowprio_installed", False):  # type: ignore[attr-defined]
            pytesseract.pytesseract.subprocess.Popen = _lowprio_popen          # type: ignore[attr-defined]
            pytesseract.pytesseract._lowprio_installed = True                  # type: ignore[attr-defined]
    except Exception:
        # 실패해도 기능 저하 없음
        pass


# 모듈 로드 시 1회 설치
_install_lowprio_for_tesseract()


def _tess_cfg_and_env():
    """
    OCR 호출 구간 동안만:
      - cfg: --tessdata-dir <...\\tessdata>
      - env: TESSDATA_PREFIX = <...>  (tessdata 상위 폴더)
    """
    global _TESS_CFG_CACHE
    if _TESS_CFG_CACHE:
        cfg, env_key, prev, td_parent = _TESS_CFG_CACHE
    else:
        td = resolve_tessdata_dir()
        cfg = f'--tessdata-dir {td}' if td else ""
        td_parent = str(Path(td).parent) if td else ""
        env_key = "TESSDATA_PREFIX"
        prev = os.environ.get(env_key)
        _TESS_CFG_CACHE = (cfg, env_key, prev, td_parent)

    if _TESS_CFG_CACHE[3]:
        os.environ[env_key] = _TESS_CFG_CACHE[3]

    def _restore():
        if _TESS_CFG_CACHE[2] is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = _TESS_CFG_CACHE[2]

    return _TESS_CFG_CACHE[0], _restore


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _only_digits(s: str) -> str:
    return re.sub(r"[^0-9]", "", s or "")


def _only_hangul(s: str) -> str:
    # 한글 음절만 남김(공백/기호 제거)
    return "".join(ch for ch in (s or "") if "가" <= ch <= "힣")


def _is_subsequence(sub: str, base: str) -> bool:
    # sub가 base의 부분 subsequence(순서 유지, 글자 일부 생략 허용)인지 검사
    it = iter(base)
    return all(ch in it for ch in sub)


_OOTR_CHARS = set("순위권이탈")


def _hangul_subset_of_ootr(text: str) -> bool:
    """OCR 결과의 한글이 모두 '순위권이탈' 문자 집합에만 속하는지 확인.
    느슨한 폴백용: 패턴 매칭이 실패해도 순위권이탈 관련 한글만 보이면 True 반환.
    '갱신권' 등 다른 뱃지 한글은 집합 외 문자를 포함하므로 False가 됨.
    """
    hangul = _only_hangul(text)
    if not hangul:
        return False
    return all(ch in _OOTR_CHARS for ch in hangul)


def _match_out_of_range_ko(text: str) -> bool:
    """
    '순위권 이탈(임시 텍스트. 추후 변경 필요)'을 부분적으로라도 인식했는지 판단.
    허용 규칙:
      - 공백/개행/기호 제거 후 정확 부분문자열이거나
      - '순위권이탈'의 subsequence(순서 유지, 일부 누락 허용)
      - 최소 2글자 이상일 때만 참(과도한 오검 방지)  ← 필요시 1글자로 낮출 수 있음
    """
    base = "순위권이탈"
    s = _only_hangul(text).replace(" ", "")
    if not s:
        return False
    if s in base and len(s) >= 2:
        return True
    if _is_subsequence(s, base) and len(s) >= 2:
        return True
    return False


def _likely_korean_not_digits(pil_img) -> bool:
    """
    컨투어 수 기반 한글/숫자 판별. Tesseract가 완전히 실패한 경우의 최후 수단.

    근거:
    - 등수 숫자 1~9999 (최대 4자리): 외부 컨투어(획 덩어리) 최대 4개
    - 한글 2글자 이상: 각 음절이 여러 획으로 이뤄져 통상 6개 이상

    preprocess_digits 를 거쳐 노이즈/작은 텍스트를 제거한 뒤 카운트.
    임계값 6 → 4자리 숫자(≤4)와 한글(≥6) 사이에 충분한 여유.
    """
    try:
        import cv2
        import numpy as np
        img = preprocess_digits(pil_img)
        arr = np.array(img)
        cnts, _ = cv2.findContours(arr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        return len(cnts) >= 6
    except Exception:
        return False


def set_tesseract_path(tesseract_path: str) -> None:
    # Windows: e.g., "C:\\Program Files\\Tesseract-OCR\\tesseract.exe"
    if tesseract_path:
        from pytesseract import pytesseract as _pt
        _pt.tesseract_cmd = tesseract_path


def _is_executable(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)
    except Exception:
        return False


def validate_tesseract(path: str) -> bool:
    """
    주어진 경로가 실제 Tesseract 실행 가능 바이너리인지 간단 검증.
    - 파일 존재 & 실행 가능
    - pytesseract.get_tesseract_version() 호출이 예외 없이 통과
    """
    if not _is_executable(path):
        return False
    try:
        import pytesseract
        prev = getattr(pytesseract.pytesseract, "tesseract_cmd", "")
        try:
            pytesseract.pytesseract.tesseract_cmd = path
            _ = pytesseract.get_tesseract_version()  # subprocess 호출
            return True
        finally:
            pytesseract.pytesseract.tesseract_cmd = prev
    except Exception:
        return False


def auto_find_tesseract_path() -> Optional[str]:
    """
    Windows 한정: Tesseract 설치 경로 자동 탐지.
    우선순위:
      1) App Paths / Uninstall 레지스트리
      2) 환경변수 TESSERACT_PATH / TESSERACT_CMD
      3) PATH/where, shutil.which
      4) 흔한 설치 경로 (Program Files 계열)
    검증: validate_tesseract()로 실제 실행 가능 여부 확인.
    """
    # 윈도우만 지원
    if not sys.platform.startswith("win"):
        return None

    candidates: list[str] = []

    # 1) 레지스트리 기반 후보
    try:
        candidates += _win_reg_tesseract_candidates()
    except Exception:
        pass

    # 2) 환경변수
    for env_key in ("TESSERACT_PATH", "TESSERACT_CMD"):
        val = os.environ.get(env_key, "")
        if val:
            candidates.append(os.path.normpath(val))

    # 3) PATH / where
    try:
        # shutil.which
        found = which("tesseract.exe") or which("tesseract")
        if found:
            candidates.append(os.path.normpath(found))
        # where.exe (여러 개 나올 수 있음)
        import subprocess
        proc = subprocess.run(["where", "tesseract"], capture_output=True, text=True, shell=True)
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.splitlines():
                line = line.strip().strip('"')
                if line:
                    candidates.append(os.path.normpath(line))
    except Exception:
        pass

    # 4) 흔한 설치 경로
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates += [
        os.path.join(pf, "Tesseract-OCR", "tesseract.exe"),
        os.path.join(pf86, "Tesseract-OCR", "tesseract.exe"),
    ]

    # 중복 제거(순서 유지)
    seen = set()
    uniq = []
    for p in candidates:
        if p and p not in seen:
            uniq.append(p); seen.add(p)

    # 검증 후 첫 성공 리턴
    for c in uniq:
        if validate_tesseract(c):
            return c
    return None


def _win_reg_tesseract_candidates() -> list[str]:
    """Windows 레지스트리에서 tesseract.exe 경로 후보를 수집한다."""
    import os

    cands: list[str] = []

    def _read_app_paths(root):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\tesseract.exe") as k:
                # (기본값) 경로
                try:
                    val, _ = winreg.QueryValueEx(k, None)
                    if val:
                        cands.append(os.path.normpath(val))
                except OSError:
                    pass
                # Path 값은 디렉터리이므로 exe 직접 후보는 아님(무시)
        except OSError:
            pass

    def _scan_uninstall(root):
        # Uninstall 트리 전체를 훑어서 DisplayName에 'Tesseract' 포함 항목 추출
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall") as uni:
                for i in range(0, 10000):
                    try:
                        sub = winreg.EnumKey(uni, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(uni, sub) as k:
                            try:
                                name, _ = winreg.QueryValueEx(k, "DisplayName")
                            except OSError:
                                name = ""
                            if not (isinstance(name, str) and ("tesseract" in name.lower())):
                                continue
                            # 1순위: InstallLocation
                            try:
                                inst, _ = winreg.QueryValueEx(k, "InstallLocation")
                                if isinstance(inst, str) and inst:
                                    exe = os.path.join(inst, "tesseract.exe")
                                    cands.append(os.path.normpath(exe))
                            except OSError:
                                pass
                            # 2순위: DisplayIcon (exe 경로가 들어있는 경우가 흔함)
                            try:
                                icon, _ = winreg.QueryValueEx(k, "DisplayIcon")
                                if isinstance(icon, str) and icon:
                                    # "C:\...\tesseract.exe,0" 형태면 쉼표 앞까지
                                    exe = icon.split(",")[0].strip().strip('"')
                                    if exe.lower().endswith("tesseract.exe"):
                                        cands.append(os.path.normpath(exe))
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError:
            pass

    # App Paths (HKLM, HKCU)
    _read_app_paths(winreg.HKEY_LOCAL_MACHINE)
    _read_app_paths(winreg.HKEY_CURRENT_USER)

    # Uninstall (x64/x86)
    _scan_uninstall(winreg.HKEY_LOCAL_MACHINE)
    try:
        with winreg.ConnectRegistry(None, winreg.HKEY_LOCAL_MACHINE) as h:
            _ = h  # placeholder
        # WOW6432Node 아래도 확인
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"):
                _scan_uninstall(winreg.HKEY_LOCAL_MACHINE)  # 위 함수가 하위 Uninstall까지 다시 연다
        except OSError:
            pass
    except OSError:
        pass

    # 중복 제거(순서 유지)
    seen = set()
    uniq = []
    for p in cands:
        if p and p not in seen:
            uniq.append(p); seen.add(p)
    return uniq


def _is_ascii_path(p: str) -> bool:
    try:
        p.encode("ascii")
        return True
    except Exception:
        return False


def _win_short_path(p: str) -> str:
    """Windows: 8.3 숏패스로 변환(실패 시 원본 반환). ASCII 강제용."""
    if not sys.platform.startswith("win") or not p:
        return p
    try:
        buf = ctypes.create_unicode_buffer(32768)
        r = ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf))
        return buf.value if r > 0 and buf.value else p
    except Exception:
        return p


def _preferred_tessdata_cache_dir() -> str:
    """
    사용자 개입 없이 안전한 ASCII 경로를 반환.
    - Windows: %LOCALAPPDATA%\\ImageReactor\\tessdata
    - 기타: ~/.imagereactor/tessdata
    """
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        base = os.path.join(root, "ImageReactor", "tessdata")
    else:
        base = os.path.join(os.path.expanduser("~/.imagereactor"), "tessdata")
    return os.path.normpath(base)


def _bootstrap_tessdata() -> str:
    """
    settings에 경로가 없으면:
      - 소스: _default_tessdata_dir() (배포물 동봉)
      - 대상: _preferred_tessdata_cache_dir() (ASCII 보장)
      - 파일: kor.traineddata (+ 있으면 eng.traineddata)
      - 완료 후 settings.json에 ocr.tessdata_dir 저장
    항상 대상 경로를 반환(존재 보장).
    """
    src = _default_tessdata_dir()
    dst = _preferred_tessdata_cache_dir()
    os.makedirs(dst, exist_ok=True)

    for lang in ("kor.traineddata", "eng.traineddata"):
        s = os.path.join(src, lang)
        d = os.path.join(dst, lang)
        try:
            if os.path.isfile(s) and (not os.path.isfile(d) or os.path.getsize(d) != os.path.getsize(s)):
                os.makedirs(os.path.dirname(d), exist_ok=True)
                shutil.copy2(s, d)
        except Exception:
            # 없거나 복사 실패여도 계속 진행(최소 kor만 있어도 동작)
            pass

    # settings.json에 경로 저장
    try:
        from path_manager import SETTINGS_JSON
        from core.settings_manager import SettingsManager
        sm = SettingsManager(SETTINGS_JSON)
        sm.set("ocr.tessdata_dir", dst)
        sm.save()
    except Exception:
        pass

    return _win_short_path(dst)


# === tessdata 경로 해석 ===
def _default_tessdata_dir() -> str:
    """
    기본 tessdata 폴더: path_manager.DATA_DIR/tessdata (없으면 ../data/tessdata)
    """
    try:
        from path_manager import DATA_DIR  # 프로젝트 표준 data 디렉터리
        base = DATA_DIR
    except Exception:
        base = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data"))
    return os.path.normpath(os.path.join(base, "tessdata"))


def resolve_tessdata_dir() -> str:
    """
    1) settings.json의 ocr.tessdata_dir 있으면 그대로 사용
    2) 없으면 첫 실행 부트스트랩(_bootstrap_tessdata) 수행 후 저장된 경로 사용
    3) 그래도 없으면 배포 기본(_default_tessdata_dir)
    """
    global _RESOLVED_TESSDATA_DIR
    if _RESOLVED_TESSDATA_DIR:
        return _RESOLVED_TESSDATA_DIR

    try:
        from path_manager import SETTINGS_JSON
        from core.settings_manager import SettingsManager
        sm = SettingsManager(SETTINGS_JSON)
        td = sm.get("ocr.tessdata_dir", "")
        if isinstance(td, str) and td.strip():
            _RESOLVED_TESSDATA_DIR = _win_short_path(os.path.normpath(td.strip()))
            return _RESOLVED_TESSDATA_DIR
    except Exception:
        pass

    # 첫 실행: AppData로 복사 + 저장
    try:
        _RESOLVED_TESSDATA_DIR = _bootstrap_tessdata()
        return _RESOLVED_TESSDATA_DIR
    except Exception:
        # 최후: 배포 기본 경로
        _RESOLVED_TESSDATA_DIR = _win_short_path(_default_tessdata_dir())
        return _RESOLVED_TESSDATA_DIR


def _keep_large_components(img_l):
    """
    이진화된 L-mode 이미지에서 최대 높이의 40% 미만 connected component를 제거.
    주변 작은 텍스트/노이즈 필터링 (큰 숫자만 남긴다).
    혼합 크기가 없으면(모두 비슷한 높이) 필터를 적용하지 않는다.
    """
    try:
        import cv2
        import numpy as np
        arr = np.array(img_l)
        binary = (arr > 127).astype(np.uint8)
        cnts, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(cnts) < 2:
            return img_l
        heights = [cv2.boundingRect(c)[3] for c in cnts if cv2.boundingRect(c)[3] > 4]
        if not heights:
            return img_l
        max_h = max(heights)
        thr_h = max_h * 0.30
        if all(h >= thr_h for h in heights):
            return img_l  # 모두 비슷한 크기 → 필터 불필요
        mask = np.zeros_like(arr)
        for c in cnts:
            if cv2.boundingRect(c)[3] >= thr_h:
                cv2.drawContours(mask, [c], -1, 255, -1)
        return Image.fromarray(np.where(mask > 0, arr, 0).astype(np.uint8))
    except Exception:
        return img_l


def preprocess_digits(pil_image, upscale: float = 1.25, threshold: int = 170):
    img = pil_image.convert("L")
    img = ImageOps.autocontrast(img)
    if 1.0 < upscale <= 2.0:
        w, h = img.size
        img = img.resize((int(w * upscale), int(h * upscale)), Image.BICUBIC)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    # 고정 임계값(후속 튜닝 여지). 실패 케이스 쌓이면 OTSU/적응형 옵션 분기 검토
    img = img.point(lambda p: 255 if p > threshold else 0, mode="1")
    img = img.convert("L")
    img = _keep_large_components(img)  # 주변 작은 글자/노이즈 제거 (큰 숫자만 유지)
    return img


def read_digits(pil_image) -> str:
    """
    숫자 전용 OCR. eng가 없으면 kor로 폴백.
    """
    cfg, restore = _tess_cfg_and_env()
    try:
        td = resolve_tessdata_dir()
        # 파일 존재만 보고 언어 선택(빠름)
        avail = get_available_languages(td)
        lang = "eng" if "eng" in avail else ("kor" if "kor" in avail else "")
        cfg_full = (cfg + ' --psm 7 -c tessedit_char_whitelist=0123456789').strip()
        pre = preprocess_digits(pil_image)
        text = pytesseract.image_to_string(pre, config=cfg_full, lang=(lang or None)) or ""
        return _normalize_spaces(text)
    finally:
        restore()


def read_text(pil_img, lang: str | None = None):
    cfg, restore = _tess_cfg_and_env()
    try:
        img = preprocess_text(pil_img, upscale=1.5)
        lang = lang or "eng+kor"  # 둘 다 없으면 내부에서 폴백
        cfg_full = (cfg + " --psm 7").strip()
        try:
            out = pytesseract.image_to_string(img, lang=lang, config=cfg_full)
        except Exception:
            out = pytesseract.image_to_string(img, lang="kor", config=cfg_full) or ""
        return (out or "").strip()
    finally:
        restore()


def parse_points_text(text: str):
    raw = _normalize_spaces(text)
    digits = _only_digits(raw)  # "4,440" 등에서 콤마 제거 효과
    if digits == "":
        return {"value": None, "raw": raw, "kind": "points", "state": "PARSE_FAIL"}
    try:
        value = int(digits)
        # 가비지 억제: 합리적 범위로 클램프/검증(0..100000)
        if not (0 <= value <= 100000):
            return {"value": None, "raw": raw, "kind": "points", "state": "PARSE_FAIL"}
        return {"value": value, "raw": raw, "kind": "points", "state": "OK"}
    except Exception:
        return {"value": None, "raw": raw, "kind": "points", "state": "PARSE_FAIL"}


def preprocess_text(pil_img, upscale: float = 1.5):
    """
    한글 텍스트 OCR 최적 전처리:
      - 업스케일(1.25~1.5)
      - 자동 대비/약한 샤픈
      - OTSU 또는 적응형 임계는 상황에 따라 적용 (완전 이진화 지양)
    """
    from PIL import Image, ImageOps, ImageFilter
    if pil_img.mode not in ("L", "RGB", "RGBA"):
        pil_img = pil_img.convert("RGB")

    # 업스케일
    if upscale and upscale > 1.0:
        w, h = pil_img.size
        pil_img = pil_img.resize((int(w * upscale), int(h * upscale)), Image.LANCZOS)

    # 약한 자동대비 + 샤픈
    pil_img = ImageOps.autocontrast(pil_img, cutoff=2)
    pil_img = pil_img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=120, threshold=3))

    # 회색 유지 (한글 획 보존용)
    return pil_img.convert("L")


def parse_rank_text(text: str):
    raw = _normalize_spaces(text)
    if _match_out_of_range_ko(raw):
        return {"value": None, "raw": raw, "kind": "rank", "state": "OUT_OF_RANGE"}

    digits = _only_digits(raw)
    if digits == "":
        return {"value": None, "raw": raw, "kind": "rank", "state": "PARSE_FAIL"}
    try:
        value = int(digits)  # 1~4자리 자연수 가정
        if not (0 <= value <= 9999):
            return {"value": None, "raw": raw, "kind": "rank", "state": "PARSE_FAIL"}
        return {"value": value, "raw": raw, "kind": "rank", "state": "OK"}
    except Exception:
        return {"value": None, "raw": raw, "kind": "rank", "state": "PARSE_FAIL"}


def get_available_languages(tessdata_dir: Optional[str] = None) -> set[str]:
    """tessdata 폴더에서 *.traineddata만 스캔해 언어 코드를 얻는다(초고속). 결과 캐시."""
    td = tessdata_dir or resolve_tessdata_dir()
    if td in _AVAIL_LANGS_CACHE:
        return _AVAIL_LANGS_CACHE[td]
    try:
        result = {
            name[:-12]  # '.traineddata' 제거
            for name in os.listdir(td)
            if name.lower().endswith(".traineddata")
        }
    except Exception:
        result = set()
    _AVAIL_LANGS_CACHE[td] = result
    return result


def read_rank_out_of_range_ko(pil_img):
    cfg, restore = _tess_cfg_and_env()
    try:
        # 빠른 컨투어 게이트: 획 덩어리가 적으면 숫자(등수)일 가능성이 높음 → Tesseract 호출 생략
        # 등수 1~9999(최대 4자리): 외부 컨투어 ≤ 5, 한글 2자 이상: 통상 ≥ 6
        if not _likely_korean_not_digits(pil_img):
            return ""

        # '순위권 이탈'은 큰 숫자보다 폰트가 작아 upscale을 높게 → psm 7(한 줄), 6(블록) 순 시도
        any_ootr_hangul = False

        # Pass 1: kor 단독 — 엄격한 패턴 매칭 + 느슨한 문자집합 폴백
        for upscale_factor, psm in ((3.0, 7), (3.0, 6), (1.5, 7)):
            img = preprocess_text(pil_img, upscale=upscale_factor)
            cfg_full = (cfg + f" --psm {psm}").strip()
            try:
                txt = pytesseract.image_to_string(img, lang="kor", config=cfg_full) or ""
            except Exception:
                continue
            txt = txt.strip()
            if _match_out_of_range_ko(txt):
                return txt
            if _hangul_subset_of_ootr(txt):
                any_ootr_hangul = True

        # Pass 2: kor+eng 혼합 — 글자가 잘린 경우 kor 단독으로 한글을 아예 못 읽을 때 보완.
        # eng가 잘린 부분을 digit으로 읽더라도, kor이 나머지 한글을 함께 출력해 문자집합 검출 가능.
        avail = get_available_languages()
        if "kor" in avail and "eng" in avail:
            for upscale_factor, psm in ((3.0, 7), (3.0, 6)):
                img = preprocess_text(pil_img, upscale=upscale_factor)
                cfg_full = (cfg + f" --psm {psm}").strip()
                try:
                    txt = pytesseract.image_to_string(img, lang="kor+eng", config=cfg_full) or ""
                except Exception:
                    continue
                txt = txt.strip()
                if _match_out_of_range_ko(txt):
                    return txt
                if _hangul_subset_of_ootr(txt):
                    any_ootr_hangul = True

        if any_ootr_hangul:
            return "순위권 이탈"

        # Pass 3: 게이트는 통과했지만 Tesseract 5번 모두 한글을 못 읽음 → 확신 없음, 빈 문자열 반환
        # (뱃지/노이즈가 포함돼 컨투어 수가 많아진 경우 false-positive 방지)
        return ""
    finally:
        restore()


def log_tesseract_env(*_args, **_kwargs):
    """(no-op) 로그 기능 제거: 호출해도 아무 것도 하지 않음."""
    return None
