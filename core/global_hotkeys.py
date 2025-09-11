from __future__ import annotations
from typing import Callable, List, Optional
import atexit


class GlobalHotkeys:
    """
    F9(시작) / F12(중지) / ESC(종료) 전역 핫키 등록기.
    - keyboard 모듈을 사용(미설치/권한 문제 시 False 반환).
    - unregister는 창 종료 시 자동 호출(atexit) + 수동 호출 가능.
    """
    def __init__(self,
                 on_start: Callable[[], None],
                 on_stop: Callable[[], None],
                 on_quit: Callable[[], None],
                 log: Optional[Callable[[str], None]] = None,
                 on_toggle: Optional[Callable[[], None]] = None):
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_quit = on_quit
        self._on_toggle = on_toggle  # [ADD]
        self._log = log
        self._handles: List[int] = []

    def register(self) -> bool:
        try:
            import keyboard
        except Exception as e:
            if self._log: self._log(f"[hotkey] 전역 핫키 비활성화: {e}")
            return False
        try:
            self._handles.append(keyboard.add_hotkey("f9", self._safe(self._on_start)))
            self._handles.append(keyboard.add_hotkey("f12", self._safe(self._on_stop)))
            # ==== [ADD] F8 등록(있을 때만) ====
            if self._on_toggle is not None:
                self._handles.append(keyboard.add_hotkey("f8", self._safe(self._on_toggle)))
            import atexit;
            atexit.register(self.unregister)
            # ==== [CHG] 안내 로그 갱신 ====
            if self._log:
                if self._on_toggle is not None:
                    self._log("[hotkey] F9=시작 / F12=중지 / F8=목표모드 토글")
                else:
                    self._log("[hotkey] F9=시작 / F12=중지")
            return True
        except Exception as e:
            if self._log: self._log(f"[hotkey] 전역 핫키 등록 실패: {e}")
            return False

    def unregister(self) -> None:
        try:
            import keyboard
            for h in self._handles:
                try:
                    keyboard.remove_hotkey(h)
                except Exception:
                    pass
        except Exception:
            pass
        self._handles.clear()
        if self._log:
            self._log("[hotkey] 전역 핫키 해제")

    def _safe(self, fn: Callable[[], None]) -> Callable[[], None]:
        def wrapper():
            try:
                fn()
            except Exception as e:
                if self._log:
                    self._log(f"[hotkey] 핸들러 예외: {e}")
        return wrapper
