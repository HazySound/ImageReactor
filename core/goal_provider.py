# core/goal_provider.py
import threading, time, copy

class GoalSnapshot:
    def __init__(self, enabled: bool, mode: str, thresholds: dict, version: int, taken_at_ms: int):
        self.enabled = bool(enabled)
        self.mode = str(mode)  # "rank" | "points"
        self.thresholds = dict(thresholds)  # rank_target, rank_margin, points_target, points_margin, confirm_*
        self.version = int(version)
        self.taken_at_ms = int(taken_at_ms)

class GoalConfigProvider:
    """
    - confirm_samples / confirm_window_ms 는 goal 전역에서 읽는다(프리셋에서 제거됨)
    - 프리셋 마이그레이션 호환:
        * mode -> type
        * rank_tolerance -> rank_margin
        * (구버전 프리셋에 confirm_* 있으면 전역 값이 없을 때 폴백으로 사용)
    """
    def __init__(self, settings_manager):
        self._lock = threading.Lock()
        self._version = 0
        self._enabled = False
        self._mode = "rank"
        self._thresholds = {}
        self._settings = settings_manager
        self._load_from_settings_unlocked()

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _load_from_settings_unlocked(self) -> None:
        s = self._settings.get("goal", {}) or {}
        presets = s.get("presets", {}) or {}
        active_id = s.get("active_preset_id")
        p0 = presets.get(active_id, {}) if active_id in presets else {}

        # --- 타입/필드 매핑(마이그레이션 호환) ---
        p_type = p0.get("type", p0.get("mode", "rank"))  # UI에서는 "목표 유형"
        if p_type not in ("rank", "points"):
            p_type = "rank"

        rank_target = int(p0.get("rank_target", 20))
        # 허용치 키 호환: rank_margin(신규) 우선, 없으면 rank_tolerance(구버전)
        rank_tolerance = int(p0.get("rank_margin", p0.get("rank_tolerance", 0)))
        points_target = int(p0.get("points_target", 0))
        points_margin = int(p0.get("points_margin", 0))

        # 전역 confirm_* (없으면 구버전 프리셋 값을 폴백으로 사용)
        confirm_samples = int(s.get("confirm_samples", p0.get("confirm_samples", 3)))
        confirm_window_ms = int(s.get("confirm_window_ms", p0.get("confirm_window_ms", 1500)))

        self._enabled = bool(s.get("enabled", False))
        self._mode = p_type
        self._thresholds = {
            "rank_target": rank_target,
            "rank_tolerance": rank_tolerance,
            "points_target": points_target,
            "points_margin": points_margin,
            "confirm_samples": confirm_samples,
            "confirm_window_ms": confirm_window_ms,
        }
        self._version += 1

    def reload_from_settings(self) -> None:
        with self._lock:
            self._load_from_settings_unlocked()

    def snapshot(self) -> GoalSnapshot:
        with self._lock:
            return GoalSnapshot(
                enabled=self._enabled,
                mode=self._mode,
                thresholds=copy.deepcopy(self._thresholds),
                version=self._version,
                taken_at_ms=self._now_ms(),
            )

    # GUI에서 토글/프리셋 변경 직후 호출
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._version += 1

    def apply_active_preset(self) -> None:
        with self._lock:
            self._load_from_settings_unlocked()
