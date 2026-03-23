# ui/update_dialog.py — 업데이트 알림 다이얼로그
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox

from version import APP_VERSION
from core import updater
from path_manager import BASE_DIR


class UpdateDialog(ctk.CTkToplevel):
    """새 버전이 있을 때 표시되는 업데이트 다이얼로그."""

    def __init__(self, parent, release_info: dict):
        super().__init__(parent)
        self.release_info = release_info
        self.title("업데이트 알림")
        self.geometry("480x360")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        tag = release_info.get("tag_name", "")
        html_url = release_info.get("html_url", updater.RELEASES_URL)
        body = (release_info.get("body") or "").strip()
        exe_asset = release_info.get("exe_asset")

        self._new_exe_path: Path | None = None
        self._downloading = False

        # ── 레이아웃 ──
        pad = {"padx": 20, "pady": 8}

        ctk.CTkLabel(
            self, text="새 버전이 출시되었습니다!",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(**pad, anchor="w")

        ver_frame = ctk.CTkFrame(self, fg_color="transparent")
        ver_frame.pack(fill="x", **pad)
        ctk.CTkLabel(ver_frame, text=f"현재 버전:  v{APP_VERSION}", text_color="gray").pack(anchor="w")
        ctk.CTkLabel(ver_frame, text=f"최신 버전:  {tag}", text_color="#4ade80").pack(anchor="w")

        # 변경 사항
        if body:
            ctk.CTkLabel(self, text="변경 사항", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=20)
            txt = ctk.CTkTextbox(self, height=100, state="normal", wrap="word")
            txt.pack(fill="x", padx=20, pady=(0, 8))
            txt.insert("1.0", body)
            txt.configure(state="disabled")

        # 진행 바 (숨김 → 다운로드 시 표시)
        self._progress = ctk.CTkProgressBar(self, width=440)
        self._progress.set(0)
        self._progress_label = ctk.CTkLabel(self, text="", text_color="gray")

        # ── 버튼 ──
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="bottom", fill="x", padx=20, pady=12)

        ctk.CTkButton(
            btn_frame, text="릴리즈 페이지 열기",
            fg_color="transparent", border_width=1,
            command=lambda: webbrowser.open(html_url),
        ).pack(side="left")

        ctk.CTkButton(
            btn_frame, text="나중에",
            fg_color="transparent", border_width=1,
            command=self.destroy,
        ).pack(side="left", padx=(8, 0))

        if exe_asset:
            self._btn_update = ctk.CTkButton(
                btn_frame, text="지금 업데이트",
                fg_color="#2563eb",
                command=self._start_download,
            )
            self._btn_update.pack(side="right")
        else:
            ctk.CTkLabel(
                btn_frame, text="(직접 다운로드 필요)", text_color="gray", font=ctk.CTkFont(size=11),
            ).pack(side="right")

    # ──────────────────────────────────────────
    # 다운로드
    # ──────────────────────────────────────────

    def _start_download(self):
        if self._downloading:
            return
        exe_asset = self.release_info.get("exe_asset")
        if not exe_asset:
            return

        self._downloading = True
        self._btn_update.configure(state="disabled", text="다운로드 중...")
        self._progress.pack(fill="x", padx=20, pady=(0, 4))
        self._progress_label.pack(padx=20, anchor="w")

        dest = BASE_DIR / exe_asset["name"]
        self._new_exe_path = dest

        def _run():
            try:
                updater.download_exe(
                    exe_asset["url"],
                    dest,
                    progress_cb=self._on_progress,
                )
                self.after(0, self._on_download_complete)
            except Exception as e:
                self.after(0, lambda: self._on_download_error(str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _on_progress(self, downloaded: int, total: int):
        if total > 0:
            ratio = downloaded / total
            mb_done = downloaded / 1_048_576
            mb_total = total / 1_048_576
            label = f"{mb_done:.1f} / {mb_total:.1f} MB"
        else:
            ratio = 0
            mb_done = downloaded / 1_048_576
            label = f"{mb_done:.1f} MB"
        self.after(0, lambda r=ratio, l=label: self._update_progress_ui(r, l))

    def _update_progress_ui(self, ratio: float, label: str):
        try:
            self._progress.set(ratio)
            self._progress_label.configure(text=label)
        except Exception:
            pass

    def _on_download_complete(self):
        try:
            self._progress.set(1.0)
            self._progress_label.configure(text="다운로드 완료! 교체 후 재시작합니다...")
            self._btn_update.configure(text="재시작", state="normal", fg_color="#16a34a")
            self._btn_update.configure(command=self._apply_and_restart)
        except Exception:
            pass

    def _apply_and_restart(self):
        if self._new_exe_path is None:
            return
        try:
            updater.apply_update(self._new_exe_path)
        except RuntimeError as e:
            messagebox.showinfo(
                "안내",
                f"{e}\n\n다운로드된 파일:\n{self._new_exe_path}\n\n"
                "현재 exe와 수동으로 교체해 주세요.",
                parent=self,
            )

    def _on_download_error(self, msg: str):
        try:
            self._downloading = False
            self._btn_update.configure(state="normal", text="다시 시도")
            self._progress_label.configure(text=f"오류: {msg}", text_color="#f87171")
        except Exception:
            pass
