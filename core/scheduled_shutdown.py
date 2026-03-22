# core/scheduled_shutdown.py
import os
import time
import threading
import platform
from typing import Callable, Dict, Any, List, Tuple, Optional

_WEEKDAY_MAP = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6}

def _norm_weekday(v) -> Optional[int]:
    if isinstance(v, int):
        return v if 0 <= v <= 6 else None
    if isinstance(v, str):
        vv = v.strip().lower()
        return _WEEKDAY_MAP.get(vv)
    return None

def _now_tuple() -> Tuple[str, int, str]:
    lt = time.localtime()
    ymd = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
    wd  = lt.tm_wday  # 0=Mon..6=Sun
    hm  = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    return ymd, wd, hm

class ScheduledShutdown:
    """
    SETTINGS().get('schedule') 형식:
    {
      "enabled": true,
      "entries": [
        {"weekday":"thu","time":"02:40"},
        {"weekday":3,    "time":"03:10"}
      ],
      "shutdown_delay_min": 20,
      "check_interval_sec": 15  # (선택) 초저사양용 검사 주기
    }
    """
    def __init__(self,
                 settings_getter: Callable[[], Any],
                 stop_callback: Callable[[], None],
                 shutdown_callback: Optional[Callable[[], None]] = None):
        self._get_settings = settings_getter
        self._stop_cb = stop_callback
        self._shutdown_cb = shutdown_callback or self._default_shutdown
        self._fired: set[str] = set()
        self._next_check_ts: float = 0.0
        self._enabled = False
        self._entries: List[Tuple[int, str]] = []
        self._auto_off = False
        self._delay_min = 20
        self._check_interval = 15
        self._timer: Optional[threading.Timer] = None
        self.reload_from_settings()

    def reload_from_settings(self) -> None:
        cfg = self._get_settings().get("schedule", {}) or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._entries = []
        for ent in cfg.get("entries", []) or []:
            wd = _norm_weekday(ent.get("weekday"))
            hm = str(ent.get("time", "")).strip()
            if wd is None or not hm:
                continue
            self._entries.append((wd, hm))
        self._auto_off = bool(cfg.get("auto_poweroff", int(cfg.get("shutdown_delay_min", 0)) > 0))
        self._delay_min = int(cfg.get("shutdown_delay_min", 20))
        self._check_interval = int(cfg.get("check_interval_sec", 15))
        if self._check_interval < 5:
            self._check_interval = 5  # 과도한 폴링 방지
        # 날짜 바뀌면 자연스럽게 키가 달라지므로 _fired는 유지해도 중복 방지에 문제 없음.

    def maybe_check_and_trigger(self) -> None:
        if not self._enabled or not self._entries:
            return
        now = time.time()
        if now < self._next_check_ts:
            return
        self._next_check_ts = now + self._check_interval

        ymd, wd, hm = _now_tuple()
        for (ewd, ehm) in self._entries:
            if ewd == wd and ehm == hm:
                key = f"{ymd}|{wd}|{hm}"
                if key in self._fired:
                    continue
                self._fired.add(key)
                # 1) 루틴 종료 요청
                try:
                    self._stop_cb()
                except Exception:
                    pass
                # 2) OS 종료: auto_poweroff=True 이고 delay>0 일 때만
                if getattr(self, "_auto_off", False) and self._delay_min > 0:
                    self._arm_shutdown_timer(self._delay_min)

                return

    def cancel_pending_shutdown(self) -> None:
        """UI에서 취소 버튼을 만들 경우 호출."""
        t = self._timer
        self._timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass

    # --- 내부 ---
    def _arm_shutdown_timer(self, delay_min: int) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
        delay = max(0, int(delay_min) * 60)
        t = threading.Timer(delay, self._safe_shutdown)
        t.daemon = True
        t.start()
        self._timer = t

    def _safe_shutdown(self) -> None:
        try:
            self._shutdown_cb()
        except Exception:
            pass
        finally:
            self._timer = None

    @staticmethod
    def _default_shutdown() -> None:
        if platform.system().lower().startswith("win"):
            os.system("shutdown /s /t 0")
        else:
            # Linux/macOS
            os.system("shutdown -h now")
