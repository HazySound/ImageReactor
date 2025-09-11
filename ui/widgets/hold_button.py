from __future__ import annotations
import time
from typing import Callable, Optional
import customtkinter as ctk
from PIL import Image, ImageTk

class HoldCircleButton(ctk.CTkFrame):
    """
    단일 버튼:
      - idle(중지)   : 짧게 클릭 → on_tap()  (아이콘: play)
      - running(재생): 길게 누름 → on_hold_complete() (아이콘: stop, 진행 링)
    내부 캔버스는 부모 패널 배경과 동일한 단일 bg를 사용해 '투명처럼' 보이게 함.
    """

    def __init__(
        self,
        master,
        diameter: int = 72,
        hold_ms: int = 1200,
        on_tap: Optional[Callable] = None,              # 시작 콜백
        on_hold_complete: Optional[Callable] = None,    # 정지 콜백
        play_icon_path: Optional[str] = None,
        stop_icon_path: Optional[str] = None,
        fg_color="transparent",                         # 프레임 배경(부모와 동일 추천)
        progress_color="#00C853",
        track_color="#3A3A3A",
        show_track: bool = False,                       # 트랙(바탕 링) 표시 여부
        tap_max_ms: int = 500,                          # idle에서 탭으로 인정할 최대 press 시간
        # ▼ 추가
        progress_width: int = 4,  # 진행 링 두께
        track_width: Optional[int] = None,  # 트랙 두께(None이면 progress와 동일)
        **kwargs,
    ):
        super().__init__(master=master, fg_color=fg_color)
        self.diameter = diameter
        self.radius = diameter // 2
        self.hold_ms = max(200, int(hold_ms))
        self.on_tap = on_tap
        self.on_hold_complete = on_hold_complete
        self.progress_color = progress_color
        self.track_color = track_color
        self.show_track = bool(show_track)
        self.tap_max_ms = int(tap_max_ms)

        # ▼ 추가: 두께 계산
        self.progress_width = max(1, int(progress_width))
        self.track_width = max(1, int(track_width)) if track_width is not None else self.progress_width
        # 아이콘 여백(두께에 따라 자동 보정)
        self._ring_pad = max(6, self.progress_width + 4)

        # 상태
        self._running = False            # idle 시작
        self._press_start_ts = None
        self._running_after = None
        self._completed = False
        self._busy = False               # 콜백 중복 방지
        self._pressed_in_idle = False    # idle에서 눌렀는지
        self._suppress_next_release = False  # 홀드 완료 후 첫 release 무시

        # 내부 캔버스: bg는 부모 패널색으로 강제
        self.canvas = ctk.CTkCanvas(
            master=self,
            width=diameter,
            height=diameter,
            highlightthickness=0,
            bg=self._parent_bg(),       # 부모 fg_color 해석해 단일색으로
        )
        self.canvas.pack()

        # 아이콘
        self._img_play = self._load_icon(play_icon_path)
        self._img_stop = self._load_icon(stop_icon_path)

        # 이벤트
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.configure(cursor="hand2")

        self._render(progress=0.0)

    # -------- 공개 API --------
    def set_running(self, running: bool):
        """외부(컨트롤러 상태)에 따라 버튼 모드 변경."""
        self._running = bool(running)
        if not self._running:
            self._stop_progress(cancelled=True)
        self._render(0.0)

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        self.canvas.unbind("<ButtonPress-1>")
        self.canvas.unbind("<ButtonRelease-1>")
        self.canvas.unbind("<Leave>")
        if enabled:
            self.canvas.bind("<ButtonPress-1>", self._on_press)
            self.canvas.bind("<ButtonRelease-1>", self._on_release)
            self.canvas.bind("<Leave>", self._on_leave)
            self.canvas.configure(cursor="hand2")
        else:
            self.canvas.bind("<ButtonPress-1>", lambda e: None)
            self.canvas.bind("<ButtonRelease-1>", lambda e: None)
            self.canvas.bind("<Leave>", lambda e: None)
            self.canvas.configure(cursor="arrow")

    # -------- 내부 이벤트 --------
    def _on_press(self, _):
        if self._busy:
            return
        if self._running:
            # 정지: 홀드 진입
            self._completed = False
            self._press_start_ts = time.perf_counter()
            self._tick()
        else:
            # 시작: idle 클릭 플로우 추적
            self._pressed_in_idle = True
            self._press_start_ts = time.perf_counter()

    def _on_release(self, _):
        if self._busy:
            return

        # 홀드 완료 직후 release는 무시(재시작 방지)
        if self._suppress_next_release:
            self._suppress_next_release = False
            self._pressed_in_idle = False
            self._press_start_ts = None
            return

        if not self._running:
            # idle → 탭으로 간주(길게 눌러도 상관없이 release 시점에 실행)
            if self._pressed_in_idle:
                # (원하면 tap_max_ms로 제한)
                if self._press_start_ts is None or \
                   (time.perf_counter() - self._press_start_ts) * 1000.0 <= self.tap_max_ms:
                    self._call_on_tap()
            self._pressed_in_idle = False
            self._press_start_ts = None
        else:
            # running → 홀드 진행 중이면 취소
            self._stop_progress(cancelled=True)

    def _on_leave(self, _):
        if self._running:
            self._stop_progress(cancelled=True)

    # -------- 타이머/렌더 --------
    def _tick(self):
        if self._press_start_ts is None:
            return
        elapsed_ms = (time.perf_counter() - self._press_start_ts) * 1000.0
        progress = min(1.0, elapsed_ms / self.hold_ms)
        self._render(progress)

        if progress >= 1.0 and not self._completed:
            self._completed = True
            self._press_start_ts = None
            self._render(1.0)
            # 다음 release 무시(재시작 방지)
            self._suppress_next_release = True
            self._call_on_hold_complete()
            return

        self._running_after = self.after(16, self._tick)  # ~60fps

    def _stop_progress(self, cancelled: bool):
        if self._running_after:
            self.after_cancel(self._running_after)
            self._running_after = None
        self._press_start_ts = None
        if cancelled and not self._completed:
            self._render(0.0)

    def _render(self, progress: float):
        self.canvas.delete("all")
        cx = cy = self.radius
        r_outer = self.diameter - 2

        if self._running:
            if self.show_track:
                self.canvas.create_arc(
                    2, 2, r_outer, r_outer,
                    start=0, extent=359.9,
                    style="arc",
                    outline=self.canvas.cget("bg"),
                    width=self.track_width,  # ← 트랙 두께
                )
            if progress > 0:
                self.canvas.create_arc(
                    2, 2, r_outer, r_outer,
                    start=90, extent=-(progress * 360.0),  # 12시 시작, 시계방향
                    style="arc",
                    outline=self._mix_color(self.progress_color, alpha=0.95),
                    width=self.progress_width,  # ← 진행 링 두께
                )

        icon = self._img_stop if self._running else self._img_play
        if icon:
            self.canvas.create_image(cx, cy, image=icon)

    # -------- 콜백 호출 래퍼 --------
    def _call_on_tap(self):
        if not callable(self.on_tap):
            return
        self._busy = True
        try:
            self.on_tap()
        finally:
            self._busy = False

    def _call_on_hold_complete(self):
        if not callable(self.on_hold_complete):
            return
        self._busy = True
        try:
            self.on_hold_complete()
        finally:
            self._busy = False

    # -------- 유틸 --------
    def _parent_bg(self) -> str:
        """부모 CTk 계층의 fg_color를 파고들어 단일 bg로 환산."""
        try:
            dark = (ctk.get_appearance_mode() == "Dark")
        except Exception:
            dark = True
        try:
            fg = self.master.cget("fg_color")
        except Exception:
            fg = None

        def pick(value):
            if value in (None, "", "transparent", "systemTransparent"):
                return None
            if isinstance(value, (tuple, list)) and len(value) >= 2:
                return value[1] if dark else value[0]
            if isinstance(value, str) and " " in value:
                parts = value.split()
                if len(parts) >= 2:
                    return parts[1] if dark else parts[0]
            return value

        c = pick(fg)
        if c:
            return c
        return "#2b2b2b" if dark else "#e5e5e5"

    @staticmethod
    def _mix_color(hex_color: str, alpha: float = 1.0) -> str:
        hex_color = hex_color.strip()
        if not hex_color.startswith("#") or len(hex_color) != 7:
            return hex_color
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        r = int(r * alpha)
        g = int(g * alpha)
        b = int(b * alpha)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _load_icon(self, path: Optional[str]):
        if not path:
            return None
        try:
            img = Image.open(path).convert("RGBA")
            # ▼ 아이콘 박스 크기: 지름 - (좌우 패딩)
            icon_box = max(8, self.diameter - 2 * self._ring_pad)
            scale = icon_box / max(img.width, img.height)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception as e:
            print(f"[HoldCircleButton] icon load failed: {e}")
            return None
