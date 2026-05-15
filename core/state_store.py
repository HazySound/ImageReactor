import threading


class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._ocr_sampling_active = False
        # ★ 추가: UI 상호작용(창 이동/리사이즈 중) 플래그
        self._ui_busy = False
        # GUI hidden 동기화: GUI thread가 withdraw 호출 완료 직후 set.
        # worker thread 가 캡처 전에 이 값을 polling 해 OS 컴포지터 turn 까지 보장.
        # 0 = hidden 아님(visible).
        self._gui_hidden_at_ms = 0

    # ----- OCR 샘플링 플래그 -----
    def set_ocr_sampling_active(self, active: bool) -> None:
        with self._lock:
            self._ocr_sampling_active = bool(active)

    def is_ocr_sampling_active(self) -> bool:
        with self._lock:
            return self._ocr_sampling_active

    # ----- UI 상호작용 플래그(새로 추가) -----
    def set_ui_busy(self, busy: bool) -> None:
        """창 드래그/리사이즈 등 사용자가 GUI를 만지는 동안 True."""
        with self._lock:
            self._ui_busy = bool(busy)

    def is_ui_busy(self) -> bool:
        with self._lock:
            return self._ui_busy

    # ----- GUI hidden 동기화 -----
    def set_gui_hidden_at(self, ts_ms: int) -> None:
        """GUI thread 가 withdraw 호출 직후 timestamp 기록.
        0 으로 clear 하면 GUI 가 visible 임을 의미."""
        with self._lock:
            self._gui_hidden_at_ms = int(ts_ms)

    def get_gui_hidden_at(self) -> int:
        """가장 최근 GUI hidden 시각(ms). 0 이면 hidden 아님."""
        with self._lock:
            return self._gui_hidden_at_ms


# 싱글턴 접근자
_state_store_singleton = None


def get_state_store() -> "StateStore":
    global _state_store_singleton
    if _state_store_singleton is None:
        _state_store_singleton = StateStore()
    return _state_store_singleton
