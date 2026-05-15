# core/updater.py — 업데이트 체크 및 자동 교체
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

from version import APP_VERSION, GITHUB_REPO

API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ALL_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
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

def _extract_exe_asset(assets: list) -> dict | None:
    """release assets 리스트에서 ImageReactor exe asset 추출. 없으면 None."""
    for a in assets:
        name: str = a.get("name", "")
        if name.lower().endswith(".exe") and "imagereactor" in name.lower():
            return {
                "name": name,
                "url": a["browser_download_url"],
                "size": a.get("size", 0),
            }
    return None


def check_latest_release(timeout: int = 15) -> dict | None:
    """
    자동 업데이트용 최신 릴리즈 정보 반환.
    {
      tag_name: str,
      html_url: str,
      body: str,
      exe_asset: {name, url, size},
    }

    동작 원리:
      /releases 로 published_at 내림차순 모든 릴리즈 조회.
      그 중 'exe asset 첨부 + prerelease/draft 아님' 인 가장 최근 릴리즈 반환.

      V2 같은 메인 패키지 zip 릴리즈는 exe asset 없어 자동으로 skip.
      → V2 를 GitHub UI 에서 'Latest' 로 설정해 신규 사용자가 zip 받게 해도,
        자동 업데이트는 별도 exe 릴리즈 중 최신을 찾아 알림.

    네트워크 오류 등 실패 시 None 반환.
    실패 원인은 check_latest_release.last_error 에 저장된다.
    """
    check_latest_release.last_error = ""
    try:
        req = Request(
            ALL_RELEASES_URL,
            headers={"User-Agent": "ImageReactor-Updater/1.0"},
        )
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            releases = json.loads(resp.read().decode())

        # published_at desc 순서로 응답됨. 첫 번째 'exe 첨부 + 정식 릴리즈' 채택
        for rel in releases:
            if rel.get("prerelease", False) or rel.get("draft", False):
                continue
            exe_asset = _extract_exe_asset(rel.get("assets", []) or [])
            if exe_asset is None:
                continue  # zip-only 메인 패키지 등 skip
            return {
                "tag_name": rel.get("tag_name", ""),
                "html_url": rel.get("html_url", RELEASES_URL),
                "body": rel.get("body", ""),
                "exe_asset": exe_asset,
            }
        return None  # exe 첨부된 릴리즈 없음
    except Exception as e:
        err = str(e)
        check_latest_release.last_error = err
        print(f"[updater] 업데이트 확인 실패: {err}")
        return None


check_latest_release.last_error = ""


def check_all_releases(timeout: int = 15) -> list[dict] | None:
    """
    [개발자 모드 전용] pre-release 포함 전체 릴리즈 목록을 반환한다.
    반환 형식: [
      {
        tag_name: str,
        html_url: str,
        body: str,
        prerelease: bool,
        exe_asset: {name, url, size} | None,
      },
      ...
    ]
    실패 시 None 반환.
    """
    check_all_releases.last_error = ""
    try:
        req = Request(
            ALL_RELEASES_URL,
            headers={"User-Agent": "ImageReactor-Updater/1.0"},
        )
        with urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            releases = json.loads(resp.read().decode())

        result = []
        for rel in releases:
            assets = rel.get("assets", [])
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
            result.append({
                "tag_name": rel.get("tag_name", ""),
                "html_url": rel.get("html_url", RELEASES_URL),
                "body": rel.get("body", ""),
                "prerelease": rel.get("prerelease", False),
                "exe_asset": exe_asset,
            })
        return result
    except Exception as e:
        err = str(e)
        check_all_releases.last_error = err
        print(f"[updater] check_all_releases 실패: {err}")
        return None


check_all_releases.last_error = ""


# ──────────────────────────────────────────
# 다운로드
# ──────────────────────────────────────────

def download_exe(url: str, dest: Path, progress_cb=None,
                 expected_size: int = 0) -> None:
    """
    exe를 dest에 다운로드한다.
    progress_cb(downloaded_bytes: int, total_bytes: int) — 선택
    expected_size > 0 이면 다운로드 완료 후 크기 검증.
    크기 불일치 시 손상된 dest 삭제 + IOError 발생 (사용자가 다시 시도 가능).
    """
    req = Request(url, headers={"User-Agent": "ImageReactor-Updater/1.0"})
    with urlopen(req, timeout=120, context=_SSL_CTX) as resp:
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

    # 무결성 검증: 네트워크 중단 등으로 손상된 exe 가 교체에 사용되는 것을 방지
    if expected_size > 0:
        try:
            actual = dest.stat().st_size
        except Exception:
            actual = -1
        if actual != expected_size:
            try:
                dest.unlink()
            except Exception:
                pass
            raise IOError(
                f"다운로드 크기 불일치: 예상 {expected_size:,} 실제 {actual:,}"
            )


# ──────────────────────────────────────────
# 자동 교체 (frozen exe 전용)
# ──────────────────────────────────────────

def apply_update(new_exe: Path) -> None:
    """
    배치 스크립트를 통해 현재 exe를 삭제하고 새 버전 exe를 실행한다.
    새 exe는 이미 다운로드된 파일 이름(버전 포함) 그대로 사용된다.
    성공하면 이 함수는 반환하지 않는다(sys.exit 호출됨).
    frozen 환경에서만 동작; 개발 환경에서는 RuntimeError.

    안전장치:
      - tasklist 매칭: process 이름 필터 + PID 라벨 정확 매칭으로 false positive 차단
      - del 최대 60회(약 1분) retry 후 포기 → 에러 로그 + 새 exe 보존
      - exe 교체 성공 후에만 explorer 재실행
    """
    if not getattr(sys, "frozen", False):
        raise RuntimeError("개발 환경에서는 자동 교체를 지원하지 않습니다.")

    current_exe = Path(sys.executable).resolve()
    bat_path = current_exe.parent / "_imagereactor_update.bat"
    log_path = current_exe.parent / "_imagereactor_update.log"
    pid = os.getpid()
    exe_name = current_exe.name

    # cmd if-block 안에서 set /a 후 변경된 변수를 같은 block 에서 비교하려면
    # delayed expansion (!var!) 필수. setlocal ENABLEDELAYEDEXPANSION + ! 표기 사용.
    bat = (
        "@echo off\n"
        "setlocal ENABLEDELAYEDEXPANSION\n"
        "set RETRY=0\n"
        "set WAIT_RETRY=0\n"
        # 현재 프로세스 종료 대기 — process 이름 + PID 라벨 정확 매칭
        ":wait\n"
        f'tasklist /FI "IMAGENAME eq {exe_name}" /FI "PID eq {pid}" /NH 2>NUL '
        f'| findstr /C:"{exe_name}" >NUL\n'
        "if not errorlevel 1 (\n"
        "    set /a WAIT_RETRY+=1\n"
        # 종료 대기 최대 60회(=1분) — 그 후 hang 방지를 위해 진행
        "    if !WAIT_RETRY! gtr 60 goto delstart\n"
        "    timeout /t 1 /nobreak >NUL\n"
        "    goto wait\n"
        ")\n"
        # 삭제 시도 — 최대 60회 retry 후 포기
        ":delstart\n"
        ":delloop\n"
        f'del /F /Q "{current_exe}" 2>NUL\n'
        f'if not exist "{current_exe}" goto delok\n'
        "set /a RETRY+=1\n"
        "if !RETRY! gtr 60 (\n"
        f'    echo [%date% %time%] del retry exceeded ({pid}) > "{log_path}"\n'
        f'    echo new_exe preserved at: {new_exe} >> "{log_path}"\n'
        "    goto end\n"
        ")\n"
        "timeout /t 1 /nobreak >NUL\n"
        "goto delloop\n"
        # 삭제 성공 → 교체 + 재실행
        ":delok\n"
        f'move /Y "{new_exe}" "{current_exe}"\n'
        f'if not exist "{current_exe}" (\n'
        f'    echo [%date% %time%] move failed > "{log_path}"\n'
        "    goto end\n"
        ")\n"
        # 교체 완료 — explorer 로 재실행 (UAC 거부해도 교체는 이미 성공)
        f'explorer.exe "{current_exe}"\n'
        ":end\n"
        "endlocal\n"
        'del "%~f0"\n'
    )
    bat_path.write_text(bat, encoding="mbcs")

    subprocess.Popen(
        ["cmd", "/c", str(bat_path)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    sys.exit(0)
