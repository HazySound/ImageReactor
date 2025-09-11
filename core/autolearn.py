# core/autolearn.py

from __future__ import annotations
import math

class AutoLearnStore:
    """
    settle_ms / confirm_window_ms를 실측 기반으로 점진 튜닝하는 경량 스토어.
    - record_home_latency(dt_ms): 트리거→HOME hit 지연을 기록(EWMA + 분산)
    - record_ocr_attempts(n): 해당 HOME에서 OCR 시도 횟수(1~3)를 기록
    - compute_params(): (settle_ms, confirm_window_ms) 산출
    """
    # ---- 하한/상한 및 기본값 ----
    _SETTLE_MIN = 1000
    _SETTLE_MAX = 2000
    _CONFIRM_MIN = 250
    _CONFIRM_MAX = 600

    _SETTLE_DEFAULT = 1200
    _CONFIRM_DEFAULT = 350

    def __init__(self, alpha: float = 0.3, max_hist: int = 200):
        self.alpha = float(alpha)
        self.max_hist = int(max_hist)

        # EWMA + 분산(Welford)
        self._ema = None         # ms
        self._mean = 0.0
        self._m2 = 0.0
        self._n = 0

        self._shots_total = 0
        self._shots_ge2 = 0

        self._last_settle = self._SETTLE_DEFAULT
        self._last_confirm = self._CONFIRM_DEFAULT

    # ---- 데이터 기록 ----
    def record_home_latency(self, dt_ms: int) -> None:
        v = max(0, int(dt_ms))
        # EWMA
        if self._ema is None:
            self._ema = float(v)
        else:
            a = self.alpha
            self._ema = a * v + (1.0 - a) * self._ema
        # 분산(표준편차)
        self._n += 1
        delta = v - self._mean
        self._mean += delta / self._n
        self._m2 += delta * (v - self._mean)
        # 히스토리 크기 제한은 단순화(여기선 무시) — 필요 시 decimate 가능

    def record_ocr_attempts(self, n: int) -> None:
        n = int(n)
        if n < 1:
            return
        self._shots_total += 1
        if n >= 2:
            self._shots_ge2 += 1

    # ---- 파라미터 산출 ----
    def compute_params(self) -> tuple[int, int]:
        # 표본 부족 시 기본값 유지
        if self._ema is None or self._n < 5:
            return (self._last_settle, self._last_confirm)

        # 표준편차 추정
        std = 0.0
        if self._n > 1:
            variance = self._m2 / (self._n - 1)
            std = math.sqrt(max(0.0, variance))

        # settle_ms = ema + 안전여유
        safety_margin = max(120.0, 0.25 * std)
        settle = int(round(self._ema + safety_margin))
        settle = max(self._SETTLE_MIN, min(self._SETTLE_MAX, settle))

        # confirm_window_ms = base + k * 비율
        base = 300
        k = 200
        p_multi = (self._shots_ge2 / self._shots_total) if self._shots_total > 0 else 0.0
        confirm = int(round(base + k * p_multi))
        confirm = max(self._CONFIRM_MIN, min(self._CONFIRM_MAX, confirm))

        self._last_settle = settle
        self._last_confirm = confirm
        return (settle, confirm)

    # ---- 조회용 ----
    def current_settle_ms(self) -> int:
        return int(self._last_settle)

    def current_confirm_ms(self) -> int:
        return int(self._last_confirm)
