import os
import sys
import pyautogui as pgi
import tkinter as tk
from tkinter import messagebox
from pathlib import Path


# 1) exe 폴더 계산: 쓰기/생성 기준은 exe_dir, _MEIPASS는 "읽기 전용 번들"에만 사용
def _app_base_dir() -> Path:
    """개발: 이 파일 폴더 / 빌드: exe가 있는 폴더 (쓰기 기준)"""
    if getattr(sys, "frozen", False) and hasattr(sys, "executable"):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE_DIR = _app_base_dir()
DATA_DIR = (BASE_DIR / "data")
LOGS_DIR = (BASE_DIR / "logs")
ASSETS_DIR = (BASE_DIR / "assets")
RESOURCES_DIR = (BASE_DIR / "resources")

SETTINGS_JSON = DATA_DIR / "settings.json"
LOCK_FILE = DATA_DIR / "app.lock"


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def get_resource_path(rel_path: str) -> str:
    """읽기 전용 리소스 경로. PyInstaller --onefile에선 _MEIPASS, 개발 환경에선 BASE_DIR."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return str(Path(sys._MEIPASS) / rel_path)
    return str(BASE_DIR / rel_path)


width, height = pgi.size()


# 2) 항상 '상대경로' 반환. CWD=BASE_DIR이므로 실제 위치는 exe 옆으로 수렴.
def chdir_to_base() -> None:
    """앱 시작 시 반드시 호출: CWD를 exe 폴더로 고정해 모든 상대경로가 exe 옆을 가리키게 한다."""
    os.chdir(BASE_DIR)


def get_res_path() -> str:
    res = f"{width}x{height}"
    return f"resources/{res}"


def get_img_path() -> str:
    res = f"{width}x{height}"
    return f"resources/{res}/"


def show_popup_folder_made():
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "해상도 폴더 생성",
        "폴더가 생성되었습니다.\n\n필요한 이미지들을 폴더 안에 추가해주세요."
    )
    root.destroy()


def show_popup_folder_error():
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "해상도 폴더 생성",
        "경로를 만들지 못했습니다."
    )
    root.destroy()


def init_folder():
    res_path = Path(get_res_path())        # 상대경로
    if res_path.exists():
        return
    try:
        res_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        show_popup_folder_error()
        sys.exit(0)
    else:
        show_popup_folder_made()