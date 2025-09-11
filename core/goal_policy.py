# core/goal_policy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any, List
import time
import re



def _to_int_relaxed(v) -> Optional[int]:
    """
    "20", "3,439", "3 439", "3439\n" 등 문자열도 int로 보정.
    보정 실패 시 None.
    """
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        try:
            return int(v)
        except Exception:
            return None
    try:
        s = str(v).strip()
        m = re.search(r"(\d{1,3}(?:[,\.\s]\s?\d{3})*|\d+)", s)
        if not m:
            return None
        token = m.group(1)
        token = re.sub(r"[,\.\s]", "", token)
        return int(token)
    except Exception:
        return None


# ---- 데이터 모델 ----
@dataclass(frozen=True)
class GoalPreset:
    """목표 프리셋 구조 (settings.goal.presets 의 단일 항목)"""
    name: str
    mode: str  # "rank" | "points" | "keep_alive"
    # rank mode
    rank_target: Optional[int] = None
    rank_tolerance: Optional[int] = None
    # points mode
    points_target: Optional[int] = None
    points_margin: Optional[int] = None
    # 공통
    confirm_samples: int = 3
    confirm_window_ms: int = 1500


# ---- 정책 본체 ----
class GoalPolicy:
    """
    홈 화면에서만 목표 달성 여부를 판정한다.
    - on_home_enter(): 프리셋 스냅샷 고정
    - on_sample(metrics): 샘플 판정/누적 (홈 구간에서만 동작)
    - on_home_exit(): 스냅샷/버퍼 폐기
    """
    __slots__ = (
        "_preset",
        "_snapshot",             # type: Optional[GoalPreset]
        "_hits_ts",              # type: List[int]
    )

    def __init__(self, preset: GoalPreset):
        self._preset: GoalPreset = preset
        self._snapshot: Optional[GoalPreset] = None
        self._hits_ts: List[int] = []

    # ---- 라이프사이클 훅 ----
    def on_home_enter(self) -> None:
        """홈 화면으로 진입한 시점에 프리셋 스냅샷을 고정한다."""
        self._snapshot = self._preset
        self._hits_ts.clear()

    def on_home_exit(self) -> None:
        """홈 화면을 이탈하면 스냅샷/버퍼를 버린다."""
        self._snapshot = None
        self._hits_ts.clear()

    # ---- 시작 차단(keep_alive) ----
    def should_block_start(self) -> bool:
        """
        루틴 시작 직전에 호출. keep_alive 모드면 True 반환(시작 차단).
        그 외에는 False.
        """
        return self._preset.mode == "keep_alive"

    # ---- 샘플 판정 ----
    def on_sample(self, metrics: Dict[str, Any]) -> bool:
        """
        한 샘플에 대한 판정 → 윈도우 내 누적.
        True: 목표 달성(현재까지 누적 히트 수가 confirm_samples 이상)
        """
        if self._snapshot is None:
            return False

        ts = int(metrics.get("ts_ms") or _now_ms())
        mode = (self._snapshot.mode or "").strip().lower()

        # ---- 유효성 산출 ----
        valid = False

        if mode == "rank":
            # 1) 텍스트 기반 OUT_OF_RANGE는 즉시 미달
            rank_flag = str(metrics.get("rank_flag") or "").upper()
            if rank_flag == "OUT_OF_RANGE":
                valid = False
            else:
                r = _to_int_relaxed(metrics.get("rank"))
                tgt = int(self._snapshot.rank_target or 0)
                tol = int(self._snapshot.rank_tolerance or 0)
                valid = (r is not None) and (r > 0) and (r <= (tgt + tol))

        elif mode == "points":
            p = _to_int_relaxed(metrics.get("points"))
            tgt = int(self._snapshot.points_target or 0)
            mg = int(self._snapshot.points_margin or 0)
            valid = (p is not None) and (p >= max(0, tgt - mg))

        elif mode == "rank_and_points":
            # 둘 다 만족해야 함
            # rank
            rank_flag = str(metrics.get("rank_flag") or "").upper()
            r = _to_int_relaxed(metrics.get("rank")) if rank_flag != "OUT_OF_RANGE" else None
            tgt_r = int(self._snapshot.rank_target or 0)
            tol_r = int(self._snapshot.rank_tolerance or 0)
            ok_r = (r is not None) and (r > 0) and (r <= (tgt_r + tol_r))
            # points
            p = _to_int_relaxed(metrics.get("points"))
            tgt_p = int(self._snapshot.points_target or 0)
            mg_p = int(self._snapshot.points_margin or 0)
            ok_p = (p is not None) and (p >= max(0, tgt_p - mg_p))
            valid = ok_r and ok_p

        elif mode == "rank_or_points":
            # 둘 중 하나만 만족해도 됨
            rank_flag = str(metrics.get("rank_flag") or "").upper()
            r = _to_int_relaxed(metrics.get("rank")) if rank_flag != "OUT_OF_RANGE" else None
            tgt_r = int(self._snapshot.rank_target or 0)
            tol_r = int(self._snapshot.rank_tolerance or 0)
            ok_r = (r is not None) and (r > 0) and (r <= (tgt_r + tol_r))

            p = _to_int_relaxed(metrics.get("points"))
            tgt_p = int(self._snapshot.points_target or 0)
            mg_p = int(self._snapshot.points_margin or 0)
            ok_p = (p is not None) and (p >= max(0, tgt_p - mg_p))

            valid = ok_r or ok_p

        else:
            # keep_alive 등: 달성 불가
            valid = False

        # ---- 윈도우 누적 및 결정 ----
        wnd = int(self._snapshot.confirm_window_ms or 1500)
        req = int(self._snapshot.confirm_samples or 3)

        self._gc_hits(ts, wnd)  # 오래된 히트 제거
        if valid:
            self._hits_ts.append(ts)

        return len(self._hits_ts) >= req

    # ---- 프리셋 갱신 (다음 홈부터 적용) ----

    def replace_preset_for_next_home(self, new_preset: GoalPreset) -> None:
        """
        실행 중 프리셋이 바뀐 경우 호출.
        - 현재 홈 구간에서는 기존 스냅샷 유지
        - 다음 on_home_enter()에서 new_preset이 스냅샷으로 고정됨
        """
        self._preset = new_preset

    # ---- 내부 유틸 ----
    def _gc_hits(self, now_ms: int, window_ms: int) -> None:
        """
        확인 창(window) 밖의 타임스탬프 제거.
        경계값(now_ms - window_ms)와 같은 샘플은 '포함'으로 취급(보수적).
        """
        cutoff = now_ms - window_ms
        if not self._hits_ts:
            return
        # 대부분 크기가 매우 작으므로 선형 필터로 충분
        self._hits_ts = [t for t in self._hits_ts if t >= cutoff]


# ---- 빌더/팩토리 ----

def build_goal_policy(settings_manager) -> GoalPolicy:
    """
    SettingsManager 인터페이스(요구):
      - get(key: str, default=None) -> Any
    settings 구조(예):
      goal:
        active_preset_id: "p1"
        presets:
          p1: {name, mode, rank_target, rank_tolerance, points_target, points_margin, confirm_samples, confirm_window_ms}
    """
    goal = settings_manager.get("goal", {}) or {}
    presets = goal.get("presets", {}) or {}
    active_id = goal.get("active_preset_id")

    preset_dict = None
    if active_id and active_id in presets:
        preset_dict = presets[active_id]
    elif presets:
        # 사전식 첫 항목 폴백
        first_key = next(iter(presets.keys()))
        preset_dict = presets[first_key]

    if not preset_dict:
        # 안전 기본값
        preset = GoalPreset(
            name="기본(keep_alive)",
            mode="keep_alive",
            confirm_samples=3,
            confirm_window_ms=1500,
        )
        return GoalPolicy(preset)

    preset = _to_goal_preset(preset_dict)
    return GoalPolicy(preset)


# ---- 변환/검증 ----

def _to_goal_preset(d: Dict[str, Any]) -> GoalPreset:
    """
    dict → GoalPreset 변환 시 결측 기본값/타입 보정.
    (settings.json이 수동 편집되거나 이전 버전일 때 대비)
    """
    mode = str(d.get("mode") or d.get("type") or "keep_alive").strip()

    return GoalPreset(
        name=str(d.get("name", "")).strip() or "unnamed",
        mode=mode,
        rank_target=_coerce_int_or_none(d.get("rank_target")),
        rank_tolerance=_coerce_int_or_none(d.get("rank_tolerance")),
        points_target=_coerce_int_or_none(d.get("points_target")),
        points_margin=_coerce_int_or_none(d.get("points_margin")),
        confirm_samples=int(d.get("confirm_samples", 3) or 3),
        confirm_window_ms=int(d.get("confirm_window_ms", 1500) or 1500),
    )


def _coerce_int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
