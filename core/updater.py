# core/updater.py — 업데이트 체크 및 자동 교체
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from version import APP_VERSION, GITHUB_REPO

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"


# ──────────────────────────────────────────
# 버전 파싱/비교
# ──────────────────────────────────────────

def _parse_version(v: str) -> tuple[int, ...]:
    """'v2.1.0', 'V2', '2.1.0' → (2, 1, 0)"""
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(remote_tag: str, current: str = APP_VERSION) -> bool:
    return _parse_version(remote_tag) > _parse_version(current)


# ──────────────────────────────────────────
# GitHub API
# ──────────────────────────────────────────

def check_latest_release(timeout: int = 10) -> dict | None:
    """
    최신 릴리즈 정보를 반환한다.
    {
      tag_name: str,
      html_url: str,
      body: str,
      exe_asset: {name, url, size} | None,
    }
    네트워크 오류 등 실패 시 None 반환.
    """
    try:
        req = Request(
            API_URL,
            headers={"User-Agent": "ImageReactor-Updater/1.0"},
        )
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())

        assets = data.get("assets", [])
        exe_asset = None
        for a in assets:
            name: str = a.get("name", "")
            if name.lower().endswith(".exe") and "imagereactor" in name.lower():
                exe_asset = {
                    "name": name,
                    "url": a["browser_download_url"],
                    "size": a.get("size", 0),
                }
                break

        return {
            "tag_name": data.get("tag_name", ""),
            "html_url": data.get("html_url", RELEASES_URL),
            "body": data.get("body", ""),
            "exe_asset": exe_asset,
        }
    except Exception:
        return None


# ──────────────────────────────────────────
# 다운로드
# ──────────────────────────────────────────

def download_exe(url: str, dest: Path, progress_cb=None) -> None:
    """
    exe를 dest에 다운로드한다.
    progress_cb(downloaded_bytes: int, total_bytes: int) — 선택
    """
    req = Request(url, headers={"User-Agent": "ImageReactor-Updater/1.0"})
    with urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        downloaded = 0
        chunk_size = 65536
        with open(dest, "wb") as f:
            while True:
                buf = resp.read(chunk_size)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if progress_cb:
                    progress_cb(downloaded, total)


# ──────────────────────────────────────────
# 자동 교체 (frozen exe 전용)
# ──────────────────────────────────────────

def apply_update(new_exe: Path) -> None:
    """
    배치 스크립트를 통해 현재 exe를 new_exe로 교체하고 재시작한다.
    성공하면 이 함수는 반환하지 않는다(sys.exit 호출됨).
    frozen 환경에서만 동작; 개발 환경에서는 RuntimeError.
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("개발 환경에서는 자동 교체를 지원하지 않습니다.")

    current_exe = Path(sys.executable).resolve()
    old_exe = current_exe.with_name(current_exe.stem + "_old.exe")
    bat_path = current_exe.parent / "_imagereactor_update.bat"
    pid = os.getpid()

    bat = (
        "@echo off\n"
        ":wait\n"
        f'tasklist /FI "PID eq {pid}" 2>NUL | find /I "{pid}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto wait\n"
        ")\n"
        f'move /Y "{current_exe}" "{old_exe}"\n'
        f'move /Y "{new_exe}" "{current_exe}"\n'
        f'start "" "{current_exe}"\n'
        f'del "{old_exe}" 2>NUL\n'
        'del "%~f0"\n'
    )
    bat_path.write_text(bat, encoding="mbcs")

    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    sys.exit(0)
