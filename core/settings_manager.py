from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict
import threading, time, atexit
import os, tempfile

DEFAULTS: Dict[str, Any] = {
    "perf": {
        "loop_interval_sec": 0.45,
        "idle_sleep_sec": 0.016,
        "await_step_min_ms": 140,
        "busy_watchdog_ms": 2000,
        "busy_throttle_sec": 0.35
    },
    "gui": {
        "idle_alpha": 0.35,
        "hover_alpha": 1.0,
        "topmost": True,
        "auto_compact_on_start": True,
        "remember_position": True,
        "_last_geometry": None,
        "low_spec": False,
        "tick_ms": 250,         # GUI 폴러 주기(ms)
        "log_max_lines": 800,   # 로그 최대 라인
        "idle_opacity": 0.85
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
    "schedule": {
        "enabled": False,
        "entries": [],  # [{"weekday":"thu","time":"02:40"}] 형태
        "shutdown_delay_min": 20,  # 분
        "check_interval_sec": 15,  # 초
        "auto_poweroff": False  # PC 자동 종료 여부
    },
    "profiles": {},

    # --- [ADD] in-game S키 연타 모드 기본 설정 ---
    "spam_mode": {
        "enabled": True,
        "enter_images": [],  # 스팸 모드 진입 트리거 이미지들
        "exit_images":  [],                   # 스팸 모드 종료(루틴 복귀) 트리거 이미지들
        "press_key": "s",                              # 연타 키
        "interval_ms": 60,                             # 연타 간격(ms)
        "conf": 0.80                                   # 매칭 신뢰도
    },
    # --- [ADD] Freeze monitor 기본 설정 ---
    "freeze": {
        "interval_sec": 60,      # 샘플링 간격(초) — 기본 1분
        "consecutive": 25,       # 연속 동일 프레임 임계치 — 기본 25회
        "tol_ahash": 2,          # aHash 허용오차
        "tol_dhash": 2,          # dHash 허용오차
        "cooldown_sec": 0,        # 트립 후 쿨다운(사용 시)
        "warmup_sec": 0
    }
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


def _spam_defaults():
    return {
        "enabled": True,
        "enter_images": ["", ""],
        "exit_images":  [""],
        "press_key": "s",
        "interval_ms": 60,
        "conf": 0.80,
    }


# DEFAULTS 사전 정의부 안에서 "park" 아래쪽에 이어서 다음 두 항목을 넣는다.
# (기존 DEFAULTS 선언부에서 적절한 위치에 삽입)
DEFAULTS["goal"] = _goal_defaults()
DEFAULTS["ocr"] = {"roi_rank": None, "roi_points": None}


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
        self._dirty: bool = False  # ← [ADD] 더티 플래그
        self.load()
        # Debounced save state
        self._save_lock = threading.RLock()
        self._debounce_timer: threading.Timer | None = None
        self._debounce_due: float = 0.0        # 다음 예약 실행 시각(epoch)
        self._debounce_deadline: float = 0.0   # 최대 지연 마감 시각(epoch)

        # [CHANGE] atexit: 조건부로만 저장 시도
        def _atexit_flush():
            try:
                # 타이머가 잡혀 있거나 더티일 때만 플러시
                has_timer = (self._debounce_timer is not None)
                if has_timer or self._dirty:
                    self.flush_debounced(immediate=True)
            except Exception:
                pass

        atexit.register(_atexit_flush)

    def save(self) -> None:
        """호환용(원자 보장은 덜함). 새 코드에서는 save_strict() 권장."""
        with self._save_lock:  # ← [ADD]
            text = json.dumps(self.data, ensure_ascii=False, indent=2)
            tmp = self.json_path.with_suffix(self.json_path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(self.json_path)
            self._dirty = False

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
        migrated = self._migrate_park_to_goal(self.data)

        # load() 끝부분 보강 라인 (goal 보강 직후)
        self._merge_goal_defaults(self.data)
        self._merge_email_defaults(self.data)

        # --- [ADD] spam_mode 기본값 병합 및 디스크 동기화 플래그 ---
        spam_changed = self._merge_spam_defaults(self.data)

        freeze_changed = self._merge_freeze_defaults(self.data)  # ← 추가

        # 저장은 goal 마이그레이션/스팸모드 보강 발생 시 수행
        if migrated or spam_changed or freeze_changed:
            self.save_strict()

    def save_strict(self) -> None:
        """
        원자 저장: tmp 파일에 쓰고 fsync 후 replace.
        """
        with self._save_lock:
            tmp_json = json.dumps(self.data, ensure_ascii=False, indent=2)
            d = self.json_path.parent
            d.mkdir(parents=True, exist_ok=True)
            # 임시 파일 생성
            fd, tmppath = tempfile.mkstemp(prefix=".settings_", suffix=".json", dir=str(d))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(tmp_json)
                    f.flush()
                    os.fsync(f.fileno())
                # 원자 치환
                os.replace(tmppath, self.json_path)
                self._dirty = False
            finally:
                try:
                    if os.path.exists(tmppath):
                        os.remove(tmppath)
                except Exception:
                    pass

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
                self.save_strict()

            # 최초 예약
            next_in = max(0.01, min(self._debounce_due, self._debounce_deadline) - now)
            self._debounce_timer = threading.Timer(next_in, _runner)
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def flush_debounced(self, immediate: bool = False) -> None:
        if not immediate:
            # 즉시 커밋이 아니면 기존 예약 로직을 그대로 사용(생략)
            now = time.time()
            with self._save_lock:
                self._debounce_due = now + 0.25
                self._debounce_deadline = now + 1.50
            return

        # ─ immediate 경로: 락을 잡은 채로 save_strict()를 호출하지 않는다 ─
        with self._save_lock:
            had_timer = self._debounce_timer is not None
            if self._debounce_timer:
                try:
                    self._debounce_timer.cancel()
                except Exception:
                    pass
                self._debounce_timer = None
            need_save = (self._dirty or had_timer)

        if need_save:
            try:
                self.save_strict()  # save_strict 안에서 락을 다시 획득
            except Exception:
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
        self._dirty = True

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
    def _merge_spam_defaults(data: dict) -> bool:
        """
        spam_mode.* 구조가 없거나 일부만 있을 때 기본값을 채워 넣고,
        실제로 보강이 일어났다면 True를 반환한다.
        """
        changed = False
        if "spam_mode" not in data or not isinstance(data["spam_mode"], dict):
            data["spam_mode"] = _spam_defaults()
            return True

        sdef = _spam_defaults()
        sm = data["spam_mode"]
        for k, v in sdef.items():
            if k not in sm:
                sm[k] = v
                changed = True
        return changed

    @staticmethod
    def _merge_freeze_defaults(data: dict) -> bool:
        """
        data["freeze"]에 누락된 키를 DEFAULTS로 채운다.
        실제 보강이 있으면 True.
        """
        changed = False
        frz = data.get("freeze")
        if not isinstance(frz, dict):
            data["freeze"] = frz = dict(DEFAULTS["freeze"])
            return True
        for k, v in DEFAULTS["freeze"].items():
            if k not in frz:
                frz[k] = v
                changed = True
        return changed

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
            data["ocr"] = {"roi_rank": None, "roi_points": None}

        # 마이그레이션 완료: park 제거
        try:
            del data["park"]
        except KeyError:
            pass
        return True

