# core/integration_hooks.py
from __future__ import annotations
import os, json, time
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any

import cv2
import numpy as np

# 코어 모듈들
from core.home_probe import HomeProbe, get_home_anchor_template  # 1) AwaitHome 루틴, 홈 템플릿 로더
from core.autolearn import AutoLearnTable, make_event_id          # 2) 자동학습 테이블
from core.goal_policy import build_goal_policy                    # 3) 목표달성 스냅샷/판정
from ocr.metrics_reader import read_metrics_from_home, HomeOCRConfig  # 4) 홈 OCR
from core.roi_cache import load_roi as roi_load, commit_roi as roi_commit
from core.utils_anchor import load_home_anchor_threshold
from core.image_utils import safe_crop


_HOME_TPL_PATH: str | None = None        # 템플릿 파일 절대/정규 경로
_HOME_SCREEN_WH: tuple[int, int] | None = None  # 화면 해상도(w,h), 첫 프레임 도착 시 채움
_CACHED_ROI: tuple[int, int, int, int] | None = None  # 메모리 힌트(파일 캐시 로드 후 보관)


# ------ 런타임 컨텍스트 -------

@dataclass
class RuntimeCtx:
    # 외부에서 주입
    settings_manager: Any
    image_folder: str
    routine_file: str

    # 내부 구성
    home_template_bgr: Optional[np.ndarray] = None
    goal_policy: Any = None
    autolearn: AutoLearnTable = None
    home_probe: Optional[HomeProbe] = None
    last_trigger_event_id: str = ""   # 직전 실행된 (action,image) 트리거

    # 홈 OCR ROI (프로젝트 화면 해상도에 맞게 지정 필수)
    ocr_cfg: HomeOCRConfig = None

    # 캐시된 홈 ROI (NCC bbox)
    cached_home_roi: Optional[Tuple[int, int, int, int]] = None


# ------ 초기화 (앱 시작 시 1회) -------

def init_runtime(settings_manager, image_folder: str, routine_file: str,
                 ocr_roi_rank: Tuple[int,int,int,int], ocr_roi_points: Tuple[int,int,int,int]) -> RuntimeCtx:
    # ✅ 홈 템플릿: 포인터(home_anchor.json) 우선
    #   (호환 시그니처 유지 위해 routine=[] 전달)
    home_img_path = get_home_anchor_template(image_folder, routine=[])
    global _HOME_TPL_PATH
    _HOME_TPL_PATH = home_img_path
    if not home_img_path:
        raise RuntimeError("home_anchor.json 미지정 또는 이미지 파일을 찾을 수 없습니다.")

    tpl_bgr = cv2.imread(home_img_path, cv2.IMREAD_COLOR)
    if tpl_bgr is None:
        raise RuntimeError(f"Home 템플릿 이미지를 읽을 수 없습니다: {home_img_path}")

    # 목표정책과 학습 테이블 생성
    goal_policy = build_goal_policy(settings_manager)
    autolearn = AutoLearnTable()

    # OCR ROI 구성
    ocr_cfg = HomeOCRConfig(roi_rank=ocr_roi_rank, roi_points=ocr_roi_points, bin_thresh=170, invert=True)

    return RuntimeCtx(
        settings_manager=settings_manager,
        image_folder=image_folder,
        routine_file=routine_file,
        home_template_bgr=tpl_bgr,
        goal_policy=goal_policy,
        autolearn=autolearn,
        home_probe=None,
        last_trigger_event_id="",
        ocr_cfg=ocr_cfg,
        cached_home_roi=None,
    )


# ------ (A) AWAIT_HOME 진입 훅  [Step 1] -------

def on_await_home_enter(ctx: RuntimeCtx, last_action: str, last_image: Optional[str]):
    """
    컨트롤러가 결과 트리거를 실행한 직후 호출.
    last_action/last_image: 방금 실행된 루틴 스텝의 action / image
    """
    # 1-1. 트리거 event_id
    ctx.last_trigger_event_id = make_event_id(last_action, last_image)

    # 1-2. AutoLearn의 적응 파라미터 → probe 타임아웃 설정
    settle_ms, window_ms = ctx.autolearn.get_adaptive_params(ctx.last_trigger_event_id)
    soft_to = max(window_ms, 8000)
    hard_to = max(window_ms + 4000, 12000)

    # 1-3. Probe 생성/시작
    ctx.home_probe = HomeProbe(
        ncc_threshold=load_home_anchor_threshold(),  # ✅ 포인터 임계 적용
        soft_timeout_ms=soft_to,
        hard_timeout_ms=hard_to
    )
    # settle_ms는 컨트롤러가 AWAIT_HOME으로 전이 직후 sleep(settle_ms)로 소비하는 게 좋다.
    return settle_ms


# ------ (B) AWAIT_HOME 틱 훅  [Step 2] -------

def on_await_home_tick(ctx: RuntimeCtx, frame_bgr: np.ndarray) -> str:
    """
    매 프레임 호출. 반환값: "pending" | "hit" | "miss"
    """
    assert ctx.home_probe is not None, "on_await_home_enter를 먼저 호출해야 합니다."

    # --- ROI 캐시 초기화: 화면 크기 기록 + 파일 캐시 1회 로드 ---
    global _HOME_TPL_PATH, _HOME_SCREEN_WH, _CACHED_ROI
    if _HOME_SCREEN_WH is None:
        _HOME_SCREEN_WH = (int(frame_bgr.shape[1]), int(frame_bgr.shape[0]))  # (w,h)
    if _CACHED_ROI is None and _HOME_TPL_PATH is not None:
        loaded = roi_load(_HOME_TPL_PATH, _HOME_SCREEN_WH, auto_scale=True)
        if loaded and len(loaded) == 4:
            _CACHED_ROI = (int(loaded[0]), int(loaded[1]), int(loaded[2]), int(loaded[3]))
    # ctx.cached_home_roi는 내부 힌트로만 쓰이고, 실제 매칭 ROI는 _CACHED_ROI를 우선 사용한다.

    # 매칭 함수 어댑터
    def match_home_anchor(frame: np.ndarray, _ignored_roi: Optional[Tuple[int, int, int, int]]):
        """
        파일 캐시 ROI(_CACHED_ROI)가 있으면 반드시 그 영역만 매칭(풀프레임 금지).
        파일 캐시가 없을 때만 '최초 1회' 풀프레임을 허용.
        임계(히트/미스) 판정은 HomeProbe에서만 수행한다.
        """
        tpl = ctx.home_template_bgr
        # --- ROI 선택: 파일 캐시 우선 ---
        roi_used = _CACHED_ROI  # 파일 캐시가 없으면 None → 풀프레임 1회
        if roi_used is not None:
            x, y, w, h = roi_used
            src = safe_crop(frame, (x, y, w, h))
        else:
            src = frame

        res = cv2.matchTemplate(src, tpl, cv2.TM_CCOEFF_NORMED)
        _, maxVal, _, maxLoc = cv2.minMaxLoc(res)

        # bbox 계산
        h_tpl, w_tpl = tpl.shape[:2]
        top_left = maxLoc
        if roi_used is not None:
            top_left = (top_left[0] + roi_used[0], top_left[1] + roi_used[1])
        bbox = (int(top_left[0]), int(top_left[1]), int(w_tpl), int(h_tpl))

        # 임계 판단은 HomeProbe 전담 → 여기서는 score/bbox만 반환
        hit = True
        # 커밋은 아래 호출부(히트 확정 후)에서 처리할 수 있도록 bbox를 되돌린다.
        return hit, float(maxVal), bbox

    # 파일 캐시 ROI가 있으면 그것만 탐색. 없으면 최초 1회 풀프레임 허용.
    probe_roi = _CACHED_ROI  # ctx.cached_home_roi 대신 파일 캐시 우선
    result = ctx.home_probe.step(frame_bgr, match_home_anchor, probe_roi)

    if result == "hit":
        # ---- 최초 1회 커밋: 파일 캐시가 비어 있다면 HomeProbe의 마지막 bbox로 커밋 ----
        if _CACHED_ROI is None and _HOME_TPL_PATH is not None and _HOME_SCREEN_WH is not None:
            last_bbox = ctx.home_probe.get_last_bbox() if ctx.home_probe else None
            if last_bbox and len(last_bbox) == 4:
                roi_commit(_HOME_TPL_PATH, last_bbox, _HOME_SCREEN_WH)  # 이미 있으면 내부에서 no-op
                _CACHED_ROI = last_bbox  # 메모리 힌트도 채워서 이후 프레임부터 ROI 전용 매칭
        ctx.cached_home_roi = _CACHED_ROI
    return result


# ------ (C) HOME 진입 훅  [Step 3] -------
def on_home_enter(ctx: RuntimeCtx, t_trigger_ms: Optional[int] = None):
    """
    AWAIT_HOME → HOME 전이 직후 호출.
    t_trigger_ms: 마지막 트리거(클릭/스페이스) 시각(ms). 전달되면 latency 학습에 사용.
    """
    # 3-1. 목표정책 스냅샷 고정
    ctx.goal_policy.on_home_enter()

    # 3-2. AutoLearn 성공 업데이트(있을 때)
    if ctx.last_trigger_event_id and t_trigger_ms is not None:
        ctx.autolearn.update(ctx.last_trigger_event_id, True, _now_ms() - t_trigger_ms)

    # probe 종료
    ctx.home_probe = None


# ------ (D) HOME 틱 훅  [Step 4] -------

def on_home_tick(ctx: RuntimeCtx, frame_bgr: np.ndarray) -> bool:
    """
    HOME 상태에서 주기적으로 호출.
    반환: True면 '목표 달성 확정' → 컨트롤러는 루틴을 중단/알림.
    """
    metrics = read_metrics_from_home(frame_bgr, ctx.ocr_cfg)
    reached = ctx.goal_policy.on_sample(metrics)
    return bool(reached)


# ------ (E) HOME 이탈 훅  [Step 5] -------

def on_home_exit(ctx: RuntimeCtx, await_home_failed: bool = False):
    """
    HOME → 다른 상태(예: RUNNING/IN_MATCH)로 이탈할 때 호출.
    await_home_failed=True면 직전 AWAIT_HOME이 miss였다는 의미로 학습 실패 반영.
    """
    ctx.goal_policy.on_home_exit()

    if await_home_failed and ctx.last_trigger_event_id:
        ctx.autolearn.update(ctx.last_trigger_event_id, False, None)

    # 다음 라운드 준비
    ctx.last_trigger_event_id = ""


# ------ 유틸 -------
def _now_ms() -> int:
    return int(time.time() * 1000)