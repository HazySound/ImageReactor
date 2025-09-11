# core/freeze_monitor.py

from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple
import cv2
import numpy as np

OnTrip = Callable[[int], None]

@dataclass
class FreezeConfig:
    interval_sec: int = 60       # 샘플 간격(초)
    consecutive: int = 12        # 연속 동일 프레임 샘플 개수 → 트립
    tol_ahash: int = 2           # aHash 허용 해밍거리
    tol_dhash: int = 2           # dHash 허용 해밍거리
    cooldown_sec: int = 0        # 트립 후 재활성 대기(0=영구 비활성)

    @staticmethod
    def _coerce_int(val, default: int) -> int:
        try:
            return int(val)
        except Exception:
            return int(default)

    @staticmethod
    def _clamp(v: int, lo: int, hi: int) -> int:
        return lo if v < lo else (hi if v > hi else v)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "FreezeConfig":
        if not isinstance(d, dict):
            return cls()
        interval = cls._coerce_int(d.get("interval_sec", 60), 60)
        consec   = cls._coerce_int(d.get("consecutive", 12), 12)
        tol_a    = cls._coerce_int(d.get("tol_ahash", 2), 2)
        tol_d    = cls._coerce_int(d.get("tol_dhash", 2), 2)
        cd       = cls._coerce_int(d.get("cooldown_sec", 0), 0)

        # 안정 범위 클램프
        interval = cls._clamp(interval, 10, 300)   # 10s ~ 5m
        consec   = cls._clamp(consec,   3, 60)
        tol_a    = cls._clamp(tol_a,    0, 8)
        tol_d    = cls._clamp(tol_d,    0, 8)
        cd       = max(0, cd)

        return cls(interval_sec=interval, consecutive=consec,
                   tol_ahash=tol_a, tol_dhash=tol_d, cooldown_sec=cd)


def _ahash(img_bgr: np.ndarray) -> int:
    """Average Hash (64bit)."""
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA)
    avg = g.mean()
    bits = (g >= avg).astype(np.uint8).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _dhash(img_bgr: np.ndarray) -> int:
    """Difference Hash (64bit)."""
    g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (9, 8), interpolation=cv2.INTER_AREA)  # 9x8 → 가로차 8x8
    diff = g[:, 1:] > g[:, :-1]
    bits = diff.astype(np.uint8).flatten()
    h = 0
    for b in bits:
        h = (h << 1) | int(b)
    return h


def _hamming64(a: int, b: int) -> int:
    return int(bin((a ^ b) & ((1 << 64) - 1)).count("1"))


class FreezeMonitor:
    """
    AWAIT_HOME 구간(홈 아님 상태)에서만 tick(frame)으로 샘플링하도록 호출하는 모니터.
    - activate()  : 샘플링 on
    - deactivate(): 샘플링 off
    - tick(frame) : 활성 상태일 때 aHash/dHash 비교 → 동일 프레임 누적 카운트 반환(또는 None)
    트립 시 on_trip 콜백 1회 호출 후 비활성화(쿨다운>0이면 경과 후 재활성 가능).
    """
    def __init__(self,
                 cfg: FreezeConfig = FreezeConfig(),
                 on_trip: Optional[OnTrip] = None):
        self.cfg = cfg
        self.on_trip = on_trip

        self._active: bool = False
        self.is_disabled: bool = False
        self._disabled_until_ts: float = 0.0

        self._last_sample_ts: float = 0.0
        self._last_ah: Optional[int] = None
        self._last_dh: Optional[int] = None
        self._consec: int = 0  # 현재 누적 카운트

    # ---------- 설정 파서(외부 JSON → 안전한 런타임 파라미터) ----------
    @staticmethod
    def from_settings_dict(cfg_dict: Optional[dict], on_trip: Optional[OnTrip] = None) -> "FreezeMonitor":
        """
        settings.json의 "freeze" 블록을 안전하게 파싱/검증해 FreezeMonitor 인스턴스를 만든다.
        - 타입/범위 보정(잘못된 값은 기본값으로 대체, 범위 밖은 클램프)
        """
        cfg = FreezeConfig.from_dict(cfg_dict)
        return FreezeMonitor(cfg=cfg, on_trip=on_trip)

    # ---------- 수명 제어 ----------
    def activate(self) -> None:
        """샘플링 활성화(단, 쿨다운 중이면 즉시 off 유지)."""
        if self.is_disabled and self.cfg.cooldown_sec > 0:
            if time.time() >= self._disabled_until_ts:
                # 쿨다운 종료 → 재활성 허용
                self.is_disabled = False
        if not self.is_disabled:
            self._active = True
            # 첫 샘플 간격 보장 위해 타임스탬프 조정
            self._last_sample_ts = 0.0

    def deactivate(self) -> None:
        """샘플링 비활성화(상태만 내림)."""
        self._active = False

    # ---------- 핵심 로직 ----------
    def tick(self, frame_bgr: np.ndarray) -> Optional[int]:
        """
        활성 상태에서만 호출 의미가 있음.
        - interval_sec 주기로만 샘플링
        - 직전 해시와 a/d 해밍거리 모두 tol 이하 → 동일 프레임으로 누적
        - 누적이 consecutive 도달 → on_trip 1회 호출, 비활성화(또는 쿨다운 진입)
        반환값: 누적 카운트(int) 또는 None(샘플링하지 않은 경우)
        """
        if not self._active or self.is_disabled:
            return None

        t = time.time()
        if t - self._last_sample_ts < self.cfg.interval_sec:
            return None  # 아직 샘플 주기 아님

        self._last_sample_ts = t

        try:
            ah = _ahash(frame_bgr)
            dh = _dhash(frame_bgr)
        except Exception:
            # 이미지 변환 실패 등 → 카운트 리셋
            self._last_ah = ah = None
            self._last_dh = dh = None
            self._consec = 0
            return 0

        if self._last_ah is None or self._last_dh is None:
            # 첫 샘플
            self._last_ah, self._last_dh = ah, dh
            self._consec = 1
            return self._consec

        dist_a = _hamming64(self._last_ah, ah)
        dist_d = _hamming64(self._last_dh, dh)

        if dist_a <= self.cfg.tol_ahash and dist_d <= self.cfg.tol_dhash:
            self._consec += 1
        else:
            self._consec = 1  # 다른 프레임으로 판정 → 카운트 리셋(자기 자신 포함)

        # 마지막 해시 갱신
        self._last_ah, self._last_dh = ah, dh

        if self._consec >= self.cfg.consecutive:
            # 트립
            self._active = False
            self.is_disabled = True
            if self.cfg.cooldown_sec > 0:
                self._disabled_until_ts = time.time() + self.cfg.cooldown_sec
            if self.on_trip:
                try:
                    self.on_trip(self._consec)
                except Exception:
                    pass
            return self._consec

        return self._consec
