import threading


class StateStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._ocr_sampling_active = False
        # ★ 추가: UI 상호작용(창 이동/리사이즈 중) 플래그
        self._ui_busy = False

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


# 싱글턴 접근자
_state_store_singleton = None


def get_state_store() -> "StateStore":
    global _state_store_singleton
    if _state_store_singleton is None:
        _state_store_singleton = StateStore()
    return _state_store_singleton
