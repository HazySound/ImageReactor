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
    warmup_sec: int = 120  # 활성/리셋 후 워밍업(초) ← 추가

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
        consec = cls._coerce_int(d.get("consecutive", 25), 25)
        tol_a = cls._coerce_int(d.get("tol_ahash", 2), 2)
        tol_d = cls._coerce_int(d.get("tol_dhash", 2), 2)
        cd = cls._coerce_int(d.get("cooldown_sec", 600), 600)
        wu = cls._coerce_int(d.get("warmup_sec", 120), 120)

        # 안정 범위 클램프
        interval = cls._clamp(interval, 10, 300)   # 10s ~ 5m
        consec   = cls._clamp(consec,   3, 60)
        tol_a    = cls._clamp(tol_a,    0, 8)
        tol_d    = cls._clamp(tol_d,    0, 8)
        cd       = max(0, cd)
        wu = cls._clamp(wu, 0, 600)  # 워밍업 0~600초

        return cls(interval_sec=interval, consecutive=consec,
                   tol_ahash=tol_a, tol_dhash=tol_d, cooldown_sec=cd, warmup_sec=wu)


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
        # --- 워밍업/쿨다운 ---
        self._warmup_until_ts: float = 0.0  # activate/reset 이후 120s

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
        # 쿨다운 만료 확인
        if self.is_disabled and self.cfg.cooldown_sec > 0:
            if time.time() >= self._disabled_until_ts:
                self.is_disabled = False
        if not self.is_disabled:
            self._active = True
            self._last_sample_ts = 0.0
            # 활성화 직후 워밍업 120s
            self._warmup_until_ts = time.time() + float(getattr(self.cfg, "warmup_sec", 120))

    def deactivate(self) -> None:
        self._active = False

    def reset(self) -> None:
        """
        누적 카운트·직전 해시·샘플 타임스탬프를 초기화하고
        워밍업(120s)을 다시 시작한다. 활성/비활성 상태는 유지.
        """
        self._last_sample_ts = 0.0
        self._last_ah = None
        self._last_dh = None
        self._consec = 0
        self._warmup_until_ts = time.time() + float(getattr(self.cfg, "warmup_sec", 120))

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

        now = time.time()
        # interval 보장
        if (now - self._last_sample_ts) < max(1, int(self.cfg.interval_sec)):
            return None
        self._last_sample_ts = now

        # 해시 계산
        ah = _ahash(frame_bgr)
        dh = _dhash(frame_bgr)

        # 첫 샘플/워밍업: 상태 갱신만, 누적 카운트 증가 금지
        if self._last_ah is None or self._last_dh is None or now < self._warmup_until_ts:
            self._last_ah, self._last_dh = ah, dh
            self._consec = 0
            return 0

        # 허용 해밍거리 내 → 동일 프레임으로 누적
        ha = _hamming64(ah, self._last_ah)
        hd = _hamming64(dh, self._last_dh)
        self._last_ah, self._last_dh = ah, dh

        if ha <= int(self.cfg.tol_ahash) and hd <= int(self.cfg.tol_dhash):
            self._consec += 1
        else:
            self._consec = 0

        # 트립 판정
        if self._consec >= int(self.cfg.consecutive):
            # 1회 트립 콜백
            try:
                if self.on_trip: self.on_trip(self._consec)
            except Exception:
                pass
            self._active = False
            if int(self.cfg.cooldown_sec) > 0:
                self.is_disabled = True
                self._disabled_until_ts = now + int(self.cfg.cooldown_sec)
            return self._consec

        return self._consec
