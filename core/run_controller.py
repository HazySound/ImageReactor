from __future__ import annotations
from threading import Event, Thread
from queue import Queue, Empty
from typing import Callable, Optional
import re
import time


_SETTLE_RE = re.compile(r'(?:^|\|)SETTLE=(\d{1,7})(?:\||$)')


class RunController:
    """
    기존 자동진행 루틴을 스레드로 감싸 Start/Stop/상태/로그 이벤트를 중계.
    worker 시그니처 권장:
      def worker(stop_event: Event, state_cb: Callable[[str], None], log_cb: Callable[[str], None]) -> None: ...
    """

    def __init__(self, worker: Callable[[Event, Callable[[str], None], Callable[[str], None]], None],
                 reset_fn: Optional[Callable[[], None]] = None):
        self._worker = worker
        self._reset_fn = reset_fn
        self._stop_event = Event()
        self._th: Optional[Thread] = None
        self._state_q: "Queue[str]" = Queue()
        self._log_q: "Queue[str]" = Queue()
        self._running = False
        self._watch_th: Optional[Thread] = None  # ← 워커 종료 감시용

    # --- 공통 초기화(shim) ---
    def reset_runtime_state(self) -> None:
        fn = getattr(self, "_reset_fn", None)
        if callable(fn):
            fn()

    # 외부 폴링
    def poll_state(self) -> Optional[str]:
        try: return self._state_q.get_nowait()
        except Empty: return None

    def poll_log(self) -> Optional[str]:
        try: return self._log_q.get_nowait()
        except Empty: return None

    # 내부 콜백
    def _state_cb(self, s: str):
        # 상태 문자열에 "SETTLE=<ms>" 지시가 있으면 해당 ms만 슬립 후, 지시어는 제거하고 큐에 넣는다.
        m = _SETTLE_RE.search(s)
        if m:
            try:
                ms = int(m.group(1))
                ms = max(0, min(ms, 600000))  # 상한 10분
                time.sleep(ms / 1000.0)
            except Exception:
                pass
            # 지시어 제거(보기 좋게 앞뒤 '|'도 정리)
            s = _SETTLE_RE.sub('', s).strip('| ')

        # --- 컨트롤러 러닝 플래그를 상태에 맞춰 동기화 ---
        if s == "RUNNING":
            self._running = True
        elif s in ("IDLE", "STOPPED", "DONE", "EXIT") or s.startswith("IDLE"):
            # 워커가 자연 종료되며 IDLE을 보낸 경우까지 반영
            self._running = False
            # 워커 스레드가 이미 끝났으면 포인터 정리
            if self._th is not None and not self._th.is_alive():
                self._th = None
                self._stop_event.clear()

        self._state_q.put(s)

    def _log_cb(self, line: str):
        if len(line) > 2000: line = line[:2000] + " ..."
        self._log_q.put(line)

    def _watch_worker_exit(self):
        """워커 자연 종료를 감지해 컨트롤러 상태를 정리한다."""
        th = self._th
        if not th:
            return
        th.join()  # 워커 종료 대기
        # stop() 경로에서도 호출될 수 있으니 idempotent 하게 처리
        self._running = False
        try:
            self._state_cb("IDLE")  # 중복 enqueue 무해
        except Exception:
            pass
        self._th = None
        self._stop_event.clear()

    # 수명주기
    def start(self) -> bool:
        if self._running: return False
        self._stop_event.clear()
        self._th = Thread(target=self._worker, args=(self._stop_event, self._state_cb, self._log_cb), daemon=True)
        self._th.start()
        # 워커 자연 종료 시 컨트롤러 상태를 자동 동기화
        self._watch_th = Thread(target=self._watch_worker_exit, daemon=True)
        self._watch_th.start()

        self._running = True
        self._state_cb("RUNNING")
        return True

    def stop(self, timeout: float = 10.0):
        if not self._running: return
        self._state_cb("STOPPING")
        self._stop_event.set()
        if self._th: self._th.join(timeout=max(0.5, timeout))
        self._running = False
        self._state_cb("IDLE")
        # 포인터/이벤트 정리 (watcher와 중복되어도 무해)
        self._th = None
        self._stop_event.clear()

    def is_running(self) -> bool:
        return self._running
