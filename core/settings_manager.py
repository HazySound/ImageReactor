from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict
import threading, time, atexit

DEFAULTS: Dict[str, Any] = {
    "gui": {
        "idle_alpha": 0.35,
        "hover_alpha": 1.0,
        "topmost": True,
        "auto_compact_on_start": True,
        "remember_position": True,
        "_last_geometry": None,
    },
    "safety": {
        "stop_longpress_ms": 1200,  # 1.2s
    },
    "email": {
        "enabled": False,
        "provider": "gmail",
        "smtp_host": "",
        "smtp_port": 587,
        "use_tls": True,
        "sender": "",
        "app_password": "",
        "recipients": "",

        # 전역 폴백(비워도 저장 허용)
        "subject_tmpl": "[자동 이메일 테스트]",
        "body_tmpl": "테스트용 메일입니다",

        # 이벤트별 ON/OFF (전역 enabled 와 AND)
        "events": {
            "goal_achieved":  { "enabled": True },
            "client_crashed": { "enabled": True },
            "freeze_detected":{ "enabled": True }
        },

        # 이벤트별 '운용 템플릿'(선택): 비어 있으면 폴백 경로로
        "templates": {
            "goal_achieved":  { "subject": "", "body": "" },
            "client_crashed": { "subject": "", "body": "" },
            "freeze_detected":{ "subject": "", "body": "" }
        },

        # 사용자 정의 기본값(Defaults): 초기 생성 시 채워서 배포/복구용으로 사용
        "defaults": {
            "goal_achieved":  {
                "subject": "",
                "body":    ""
            },
            "client_crashed": {
                "subject": "",
                "body":    ""
            },
            "freeze_detected":{
                "subject": "",
                "body":    ""
            }
        },

        "rate_limit_max": 3,
        "rate_limit_window_sec": 300
    },
    "hotkeys": {
        "start": "F9",
        "stop": "F12",
        "toggle_compact": "F10",
        "calibration": "F8",
    },
    "profiles": {}
}


# === DEFAULTS 확장: goal.*, ocr.* 추가 ===
def _goal_defaults():
    return {
        "enabled": False,
        "active_preset_id": "p1",
        "presets": {
            "p1": {
                "name": "TOP 20~25",
                "mode": "rank",              # rank | points | keep_alive
                "rank_target": 20,
                "rank_tolerance": 5,
                "points_target": 0,
                "points_margin": 0,
            },
            "p2": {
                "name": "TOP 200",
                "mode": "rank",
                "rank_target": 200,
                "rank_tolerance": 0,
                "points_target": 0,
                "points_margin": 0,
            },
        },
    }


# DEFAULTS 사전 정의부 안에서 "park" 아래쪽에 이어서 다음 두 항목을 넣는다.
# (기존 DEFAULTS 선언부에서 적절한 위치에 삽입)
DEFAULTS["goal"] = _goal_defaults()
DEFAULTS["ocr"] = {"roi_rank": None, "roi_score": None}


def _deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_update(out[k], v)
        else:
            out[k] = v
    return out


class SettingsManager:
    def __init__(self, json_path: Path):
        self.json_path = json_path
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.data: Dict[str, Any] = {}
        self.load()
        # Debounced save state
        self._save_lock = threading.Lock()
        self._debounce_timer: threading.Timer | None = None
        self._debounce_due: float = 0.0        # 다음 예약 실행 시각(epoch)
        self._debounce_deadline: float = 0.0   # 최대 지연 마감 시각(epoch)
        atexit.register(lambda: self.flush_debounced(immediate=True))

    # === SettingsManager.load() 수정 ===
    def load(self) -> None:
        if self.json_path.exists():
            try:
                loaded = json.loads(self.json_path.read_text(encoding="utf-8"))
                self.data = _deep_update(DEFAULTS, loaded)
            except Exception:
                self.data = json.loads(json.dumps(DEFAULTS))
        else:
            self.data = json.loads(json.dumps(DEFAULTS))

        # ▼ 추가: goal 기본 구조 보강 + park→goal 1회 변환
        self._merge_goal_defaults(self.data)
        migrated = self._migrate_park_to_goal(self.data)

        # load() 끝부분 보강 라인 (goal 보강 직후)
        self._merge_goal_defaults(self.data)
        self._merge_email_defaults(self.data)

        # 저장은 goal.* 기준으로만 수행되게 파일을 정리
        if migrated:
            self.save()

    def save(self) -> None:
        text = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp = self.json_path.with_suffix(self.json_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.json_path)  # 원자적 교체(같은 파티션 가정)

    def queue_save(self, *, delay_ms: int = 800, max_delay_ms: int = 3000) -> None:
        """연속 set() 이후 디바운스로 저장 예약."""
        now = time.time()
        delay = max(0.05, delay_ms / 1000.0)
        maxd  = max(delay, max_delay_ms / 1000.0)

        with self._save_lock:
            # 마감(최대 지연) 갱신
            if self._debounce_timer is None:
                self._debounce_deadline = now + maxd
            # 다음 실행 예정 시각
            self._debounce_due = now + delay

            # 기존 타이머 취소 후 재예약
            if self._debounce_timer is not None:
                try:
                    self._debounce_timer.cancel()
                except Exception:
                    pass

            def _runner():
                # 최대 지연 마감 이전이면 다시 재예약(버스트 흡수)
                with self._save_lock:
                    if time.time() < self._debounce_due and time.time() < self._debounce_deadline:
                        rem = max(0.01, min(self._debounce_due, self._debounce_deadline) - time.time())
                        self._debounce_timer = threading.Timer(rem, _runner)
                        self._debounce_timer.daemon = True
                        self._debounce_timer.start()
                        return
                    # 커밋
                    self._debounce_timer = None
                # 락 밖에서 실제 디스크 쓰기
                self.save()

            # 최초 예약
            next_in = max(0.01, min(self._debounce_due, self._debounce_deadline) - now)
            self._debounce_timer = threading.Timer(next_in, _runner)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def flush_debounced(self, *, immediate: bool = False) -> None:
        """대기중 저장을 즉시 커밋. immediate=True면 예약을 취소하고 바로 저장."""
        with self._save_lock:
            t = self._debounce_timer
            self._debounce_timer = None
        if t:
            try:
                t.cancel()
            except Exception:
                pass
        if immediate:
            self.save()

    def get(self, path: str, default=None):
        node = self.data
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def set(self, path: str, value) -> None:
        node = self.data
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value

    # === SettingsManager 클래스 내부에 추가 ===
    @staticmethod
    def _merge_goal_defaults(data: dict) -> None:
        """
        goal.* 구조가 없거나 일부만 있을 때 기본값을 채운다.
        """
        if "goal" not in data or not isinstance(data["goal"], dict):
            data["goal"] = _goal_defaults()
            return
        # presets 병합(이름/필드 누락 시 기본값 보강)
        gdef = _goal_defaults()
        for k, v in gdef.items():
            if k == "presets":
                dst = data["goal"].setdefault("presets", {})
                for pid, pdef in gdef["presets"].items():
                    dst_p = dst.setdefault(pid, {})
                    for pk, pv in pdef.items():
                        dst_p.setdefault(pk, pv)
            else:
                data["goal"].setdefault(k, v)

    @staticmethod
    def _merge_email_defaults(data: dict) -> None:
        """
        email.* 구조 보강: events/templates/defaults가 없거나 일부만 있을 때 기본값 채움
        """
        if "email" not in data or not isinstance(data["email"], dict):
            data["email"] = DEFAULTS["email"].copy()
            return

        e = data["email"]
        # 단일 키들
        e.setdefault("subject_tmpl", DEFAULTS["email"]["subject_tmpl"])
        e.setdefault("body_tmpl", DEFAULTS["email"]["body_tmpl"])

        # 중첩 dict 3종
        for k in ("events", "templates", "defaults"):
            if not isinstance(e.get(k), dict):
                e[k] = {}
            for ev in ("goal_achieved", "client_crashed", "freeze_detected"):
                if not isinstance(e[k].get(ev), dict):
                    e[k][ev] = {}
                # 필드 보강
                for fld, dv in DEFAULTS["email"][k][ev].items():
                    e[k][ev].setdefault(fld, dv)

    @staticmethod
    def _migrate_park_to_goal(data: dict) -> bool:
        """
        1회성 마이그레이션:
          - park.*를 goal.*로 반영
          - 저장은 goal.*만 사용하도록 park 키 제거
        반환: 마이그레이션이 실제 수행되었는지 여부
        """
        park = data.get("park")
        if not isinstance(park, dict):
            return False

        SettingsManager._merge_goal_defaults(data)  # goal 스켈레톤 확보
        goal = data["goal"]
        presets = goal["presets"]
        p1 = presets.get("p1", {})

        # enabled 이전
        if isinstance(park.get("enabled"), bool):
            goal["enabled"] = park["enabled"]

        # 모드/타겟 이전: rank 우선, 없으면 points
        target_rank = park.get("target_rank")
        target_score = park.get("target_score")
        if isinstance(target_rank, int) and target_rank >= 1:
            p1["mode"] = "rank"
            p1["rank_target"] = target_rank
            # tolerance는 기존에 개념 없음 → 기본값 유지
        elif isinstance(target_score, (int, float)):
            p1["mode"] = "points"
            p1["points_target"] = int(target_score)
            # margin은 기본값 0 유지

        # 연속 확인/윈도우: 기존 안정판독 개념 반영
        stable_reads = park.get("stable_reads")
        if isinstance(stable_reads, int) and stable_reads > 0:
            p1["confirm_samples"] = stable_reads
        read_interval = park.get("read_interval_sec")
        if isinstance(read_interval, (int, float)) and p1.get("confirm_samples"):
            window_ms = int(max(1500, read_interval * 1000 * p1["confirm_samples"]))
            p1["confirm_window_ms"] = window_ms

        # OCR ROI 키 신설(절대좌표 미보유 → None 유지)
        if "ocr" not in data or not isinstance(data["ocr"], dict):
            data["ocr"] = {"roi_rank": None, "roi_score": None}

        # 마이그레이션 완료: park 제거
        try:
            del data["park"]
        except KeyError:
            pass
        return True

