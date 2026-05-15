# core/score_anchor.py
"""
점수 앵커 시스템.

목적:
  - 직전 신뢰 점수를 anchor로 저장
  - 신규 OCR 결과를 anchor와 비교하여 outlier 검출
  - 비대칭 변화 임계 (상승 50점 / 하강 70점)으로 1판 변화 가능 범위 검증
  - 디스크 영속화 (settings.json goal.anchor.*)
  - 나이(age) 추적 — 너무 오래된 앵커는 무효화

용도:
  - 점수 모드: 1차 검증 도구
  - 등수 모드: 점수가 보조 anchor 역할 (점수가 안정적이면 등수도 신뢰)

설계 메모:
  - 등수 자체는 anchor로 안 함 (한 판 변동폭이 점수보다 훨씬 큼)
  - 첫 부트스트랩은 고신뢰 single-read 우선 (~50-150ms)
  - 미수렴 시 settings.json에 누적되지 않도록 명시적 reset() 필요
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple


# ============================================================
# 기본 임계값
# ============================================================

DEFAULT_ANCHOR_MAX_AGE_HOURS = 6.0
# 1판 최대 점수 변동 임계 (사용자 실측: 최대 ±60, 노이즈 마진 +20 = 80)
# 누적(연속 outlier)은 main 측에서 multiplier 로 처리: 임계 = base × counter
DEFAULT_DELTA_UP = 80
DEFAULT_DELTA_DOWN = 80
DEFAULT_BOUNDARY_DISTANCE = 30  # 목표 임계까지 이 거리 이내면 boundary 케이스


# ============================================================
# 데이터 모델
# ============================================================

@dataclass
class AnchorValue:
    value: int
    timestamp_ms: int


@dataclass
class ValidationResult:
    """anchor 대비 신규 값 검증 결과."""
    state: str
    # 상태:
    #   "OK"            — anchor와 정합. 신뢰 가능.
    #   "NO_ANCHOR"     — anchor 없음 (부트스트랩 단계)
    #   "STALE_ANCHOR"  — anchor가 너무 오래됨 (무효화 권장)
    #   "OUTLIER_UP"    — 상승폭이 비현실적 (누적 임계 base*mult 초과)
    #   "OUTLIER_DOWN"  — 하강폭이 비현실적
    #   "DIGIT_MISMATCH"— 자릿수가 다름 (4자리 → 3자리 등)
    delta: Optional[int] = None
    anchor_value: Optional[int] = None
    reason: str = ""


# ============================================================
# 앵커 매니저
# ============================================================

class ScoreAnchor:
    """
    점수 anchor 관리자.
    - 디스크 영속화 (settings.json goal.anchor.{value, timestamp_ms})
    - 검증/업데이트 API
    - 스레드 안전 (lock 보호)
    """

    def __init__(self, settings_mgr):
        self._settings = settings_mgr
        self._lock = threading.RLock()
        self._value: Optional[int] = None
        self._timestamp_ms: Optional[int] = None
        self._load_from_settings()

    # ---- 영속화 ----

    def _load_from_settings(self) -> None:
        try:
            v = self._settings.get("goal.anchor.value", None)
            ts = self._settings.get("goal.anchor.timestamp_ms", None)
            if isinstance(v, int) and v > 0 and isinstance(ts, int) and ts > 0:
                self._value = v
                self._timestamp_ms = ts
        except Exception:
            pass

    def _save_to_settings(self) -> None:
        try:
            self._settings.set("goal.anchor.value", int(self._value or 0))
            self._settings.set("goal.anchor.timestamp_ms", int(self._timestamp_ms or 0))
            try:
                self._settings.queue_save(delay_ms=500)
            except Exception:
                try:
                    self._settings.save()
                except Exception:
                    pass
        except Exception:
            pass

    # ---- 조회 ----

    def get(self) -> Optional[AnchorValue]:
        with self._lock:
            if self._value is None or self._timestamp_ms is None:
                return None
            if self._is_stale():
                return None
            return AnchorValue(value=self._value, timestamp_ms=self._timestamp_ms)

    def get_any(self) -> Optional[AnchorValue]:
        """나이 무관 raw 앵커. STALE_ANCHOR 진단용."""
        with self._lock:
            if self._value is None or self._timestamp_ms is None:
                return None
            return AnchorValue(value=self._value, timestamp_ms=self._timestamp_ms)

    def age_seconds(self) -> Optional[float]:
        with self._lock:
            if self._timestamp_ms is None:
                return None
            return max(0.0, (self._now_ms() - self._timestamp_ms) / 1000.0)

    def _is_stale(self) -> bool:
        if self._timestamp_ms is None:
            return True
        max_age = float(self._settings.get("ocr.anchor_max_age_hours",
                                           DEFAULT_ANCHOR_MAX_AGE_HOURS))
        max_age_ms = int(max_age * 3600 * 1000)
        return (self._now_ms() - self._timestamp_ms) > max_age_ms

    # ---- 갱신 ----

    def update(self, value: int) -> None:
        """확정된 신뢰 점수로 anchor 업데이트."""
        if not isinstance(value, int) or value <= 0:
            return
        with self._lock:
            self._value = value
            self._timestamp_ms = self._now_ms()
            self._save_to_settings()

    def reset(self) -> None:
        with self._lock:
            self._value = None
            self._timestamp_ms = None
            self._save_to_settings()

    # ---- 검증 ----

    def validate(self, new_value: int, thresh_multiplier: int = 1) -> ValidationResult:
        """
        신규 OCR 점수를 anchor와 비교.

        thresh_multiplier:
          누적 outlier 카운터 — 1판당 base × multiplier 만큼 허용.
          기본 1 (정상 1판). outlier 발생 후 다음 사이클은 2, 그 다음은 3 ...
          호출자(main.py)가 카운터 관리. 정상 통과 시 1 로 reset.

        반환 state로 후속 처리 결정:
          - OK                 → 통과 (anchor 업데이트 권장)
          - NO_ANCHOR          → 부트스트랩 모드 (다른 검증 수단 필요)
          - STALE_ANCHOR       → 앵커 만료. 재부트스트랩 권장.
          - DIGIT_MISMATCH     → 자릿수 다름 (천의자리 미인식 의심)
          - OUTLIER_*          → 누적 임계도 초과. 점수 폐기 권장.
        """
        with self._lock:
            if self._value is None or self._timestamp_ms is None:
                return ValidationResult(state="NO_ANCHOR")

            if self._is_stale():
                return ValidationResult(state="STALE_ANCHOR",
                                        anchor_value=self._value,
                                        reason="anchor older than max_age")

            anchor = int(self._value)
            delta = int(new_value) - anchor

            # 자릿수 변화 검증 (앵커가 4자리인데 신규가 3자리 → 천의자리 미인식 의심)
            anchor_digits = len(str(abs(anchor)))
            new_digits = len(str(abs(new_value)))
            if anchor_digits != new_digits:
                return ValidationResult(state="DIGIT_MISMATCH",
                                        delta=delta,
                                        anchor_value=anchor,
                                        reason=f"anchor {anchor_digits}d vs new {new_digits}d")

            mult = max(1, int(thresh_multiplier))
            up_thresh = int(self._settings.get("ocr.delta_thresh_up", DEFAULT_DELTA_UP)) * mult
            down_thresh = int(self._settings.get("ocr.delta_thresh_down", DEFAULT_DELTA_DOWN)) * mult

            if delta > up_thresh:
                return ValidationResult(state="OUTLIER_UP",
                                        delta=delta,
                                        anchor_value=anchor,
                                        reason=f"delta +{delta} > {up_thresh} (mult={mult})")
            if delta < -down_thresh:
                return ValidationResult(state="OUTLIER_DOWN",
                                        delta=delta,
                                        anchor_value=anchor,
                                        reason=f"delta {delta} < -{down_thresh} (mult={mult})")

            return ValidationResult(state="OK", delta=delta, anchor_value=anchor)

    # ---- 목표 거리 기반 boundary 판정 ----

    def distance_to_threshold(self, value: int, threshold: int) -> int:
        """현재 OCR 값과 목표 임계 간 거리(양수)."""
        return abs(int(value) - int(threshold))

    def is_boundary(self, value: int, threshold: int) -> bool:
        """boundary zone (목표에 매우 가까움) 여부."""
        boundary_dist = int(self._settings.get("ocr.boundary_distance",
                                               DEFAULT_BOUNDARY_DISTANCE))
        return self.distance_to_threshold(value, threshold) <= boundary_dist

    # ---- 유틸 ----

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)


# ============================================================
# 부트스트랩 헬퍼 (외부에서 OCR 결과를 받아 anchor 초기 설정)
# ============================================================

def bootstrap_from_high_conf(anchor: ScoreAnchor,
                             value: int,
                             word_conf: int,
                             min_conf: int = 80) -> bool:
    """
    단일 OCR 결과가 부트스트랩 자격이 되는지 판단하고 충족 시 anchor 설정.
    반환: True if anchor was set.
    """
    if not isinstance(value, int) or value <= 0:
        return False
    if word_conf < min_conf:
        return False
    # 챔피언스 sane range
    if not (2000 <= value <= 9999):
        return False
    anchor.update(value)
    return True


def bootstrap_from_majority(anchor: ScoreAnchor,
                            samples: list[Tuple[int, int]],
                            min_agree: int = 2) -> Optional[int]:
    """
    여러 OCR 샘플 [(value, conf), ...]에서 다수파 값을 찾아 anchor 설정.
    min_agree 개수 이상 일치할 때만 confirm.
    반환: 설정된 anchor 값 (또는 None).
    """
    if not samples:
        return None
    from collections import Counter
    values = [v for (v, _c) in samples if isinstance(v, int) and 2000 <= v <= 9999]
    if not values:
        return None
    cnt = Counter(values)
    (top_val, top_count) = cnt.most_common(1)[0]
    if top_count >= min_agree:
        anchor.update(top_val)
        return top_val
    return None
