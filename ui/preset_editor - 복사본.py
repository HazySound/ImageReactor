# ui/preset_editor.py
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox

# ===== 스키마/유틸 =====
DEFAULT_PRESET = {
    "name": "프리셋",
    "type": "rank",              # "rank" | "points"
    "rank_target": 20,
    "rank_tolerance": 0,
    "points_target": 0,
    "points_margin": 0,
}


def clamp_int(v, lo, hi):
    try:
        v = int(v)
    except Exception:
        v = lo
    if v < lo: v = lo
    if v > hi: v = hi
    return v


def next_preset_id(presets: dict) -> str:
    maxn = 0
    for pid in presets.keys():
        if isinstance(pid, str) and pid.startswith("p"):
            try:
                n = int(pid[1:])
                if n > maxn: maxn = n
            except Exception:
                pass
    return f"p{maxn+1}"


def copy_preset(src: dict) -> dict:
    out = dict(DEFAULT_PRESET)
    out.update({
        "name": src.get("name", out["name"]),
        "type": src.get("type", src.get("mode", out["type"])),
        "rank_target": int(src.get("rank_target", out["rank_target"])),
        "rank_tolerance": int(src.get("rank_tolerance", out["rank_tolerance"])),
        "points_target": int(src.get("points_target", out["points_target"])),
        "points_margin": int(src.get("points_margin", out["points_margin"])),
    })
    # 타입 보정
    if out["type"] not in ("rank", "points"):
        out["type"] = "rank"
    return out


def validate_preset(p: dict) -> tuple[bool, str]:
    if not str(p.get("name", "")).strip():
        return False, "프리셋 이름을 입력하세요."
    t = p.get("type", "rank")
    if t == "rank":
        if int(p.get("rank_target", 0)) < 1:
            return False, "목표 등수는 1 이상이어야 합니다."
        if int(p.get("rank_tolerance", -1)) < 0:
            return False, "등수 허용치는 0 이상이어야 합니다."
    elif t == "points":
        if int(p.get("points_target", -1)) < 0:
            return False, "목표 점수는 0 이상이어야 합니다."
        if int(p.get("points_margin", -1)) < 0:
            return False, "점수 여유치는 0 이상이어야 합니다."
    else:
        return False, "목표 유형이 올바르지 않습니다."
    return True, ""


# ===== 다이얼로그 =====
class PresetEditorDialog(ctk.CTkToplevel):
    """
    좌측: 입력(넓게) / 우측: 프리셋 목록(좁게)
    confirm_samples / confirm_window_ms 는 편집창에 노출하지 않음(전역 설정).
    """
    def __init__(self, parent_app, settings):
        super().__init__(parent_app)
        self.title("목표달성 프리셋 편집")
        self.geometry("720x400")
        self.resizable(False, False)
        self.grab_set()

        self.parent_app = parent_app
        self.settings = settings

        # ▼ 메인 앱의 알파 트래킹 일시 중지
        try:
            if hasattr(self.parent_app, "set_alpha_tracking_enabled"):
                self.parent_app.set_alpha_tracking_enabled(False)
        except Exception:
            pass

        # 다이얼로그 닫기 복구 훅
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # 데이터 로드 + 마이그레이션(전역 confirm 승격, 키 치환)
        g = self.settings.get("goal", {}) or {}
        presets = (g.get("presets") or {}).copy()
        if not presets:
            presets = {"p1": dict(DEFAULT_PRESET) | {"name": "프리셋 1"}}
            g["active_preset_id"] = "p1"

        # 전역 confirm 없으면 구 프리셋에서 폴백
        if "confirm_samples" not in g:
            g["confirm_samples"] = int(next((p.get("confirm_samples") for p in presets.values() if "confirm_samples" in p), 3))
        if "confirm_window_ms" not in g:
            g["confirm_window_ms"] = int(next((p.get("confirm_window_ms") for p in presets.values() if "confirm_window_ms" in p), 1500))

        # 개별 프리셋 키 마이그레이션
        new_presets = {}
        for pid, p in presets.items():
            np = copy_preset(p)  # mode->type, rank_tolerance->rank_margin 등 처리
            new_presets[pid] = np

        # 상태 보관
        self.presets: dict = new_presets
        self.active_id: str = g.get("active_preset_id") or list(new_presets.keys())[0]

        # 저장(마이그레이션 결과 즉시 반영)
        self.settings.set("goal.presets", self.presets)
        self.settings.set("goal.active_preset_id", self.active_id)
        self.settings.set("goal.confirm_samples", g.get("confirm_samples", 3))
        self.settings.set("goal.confirm_window_ms", g.get("confirm_window_ms", 1500))
        self.settings.save()

        # 숫자 입력 바인딩 변수
        self.var_rank_target = tk.StringVar(value="20")
        self.var_rank_margin = tk.StringVar(value="0")
        self.var_points_target = tk.StringVar(value="0")
        self.var_points_margin = tk.StringVar(value="0")

        # UI
        self._build_ui()
        self._reload_list()
        self._select_id(self.active_id)

    # ----- UI -----
    def _build_ui(self):
        # 루트 그리드: 좌(입력)=3, 우(목록)=2
        root = ctk.CTkFrame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(0, weight=3)
        root.grid_columnconfigure(1, weight=2)

        self.frm_left = ctk.CTkFrame(root)
        self.frm_left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        self.frm_right = ctk.CTkFrame(root, width=220)
        self.frm_right.grid(row=0, column=1, sticky="nsew")

        # 좌측: 입력 폼(넓게)
        form = self.frm_left
        form.grid_columnconfigure(1, weight=1)

        r = 0
        ctk.CTkLabel(form, text="이름").grid(row=r, column=0, sticky="e", padx=6, pady=8)
        self.e_name = ctk.CTkEntry(form, width=260)
        self.e_name.grid(row=r, column=1, sticky="we", padx=6, pady=8)

        r += 1
        ctk.CTkLabel(form, text="모드").grid(row=r, column=0, sticky="e", padx=6, pady=8)
        self.var_type = tk.StringVar(value="rank")
        self.rb_rank = ctk.CTkRadioButton(form, text="등수", variable=self.var_type, value="rank", command=self._on_type_changed)
        self.rb_points = ctk.CTkRadioButton(form, text="점수", variable=self.var_type, value="points", command=self._on_type_changed)
        self.rb_rank.grid(row=r, column=1, sticky="w", padx=6, pady=8)
        self.rb_points.grid(row=r, column=1, sticky="e", padx=6, pady=8)

        # rank 필드
        # _build_ui() 안, Spinbox 만들기 직전에 검증 콜백 준비
        vcmd = (self.register(self._validate_int), "%P")

        r += 1
        ctk.CTkLabel(form, text="목표 등수").grid(row=r, column=0, sticky="e", padx=6, pady=6)
        self.sb_rank_target = tk.Spinbox(form, from_=1, to=9999, width=12,
                                         textvariable=self.var_rank_target,
                                         validate="key", validatecommand=vcmd)
        self.sb_rank_target.grid(row=r, column=1, sticky="w", padx=6, pady=6)

        r += 1
        ctk.CTkLabel(form, text="등수 허용치(±)").grid(row=r, column=0, sticky="e", padx=6, pady=6)
        self.sb_rank_margin = tk.Spinbox(form, from_=0, to=9999, width=12,
                                         textvariable=self.var_rank_margin,
                                         validate="key", validatecommand=vcmd)
        self.sb_rank_margin.grid(row=r, column=1, sticky="w", padx=6, pady=6)

        # points 필드
        r += 1
        ctk.CTkLabel(form, text="목표 점수").grid(row=r, column=0, sticky="e", padx=6, pady=6)
        self.sb_points_target = tk.Spinbox(form, from_=0, to=99999999, width=12,
                                           textvariable=self.var_points_target,
                                           validate="key", validatecommand=vcmd)
        self.sb_points_target.grid(row=r, column=1, sticky="w", padx=6, pady=6)

        r += 1
        ctk.CTkLabel(form, text="점수 여유치(±)").grid(row=r, column=0, sticky="e", padx=6, pady=6)
        self.sb_points_margin = tk.Spinbox(form, from_=0, to=99999999, width=12,
                                           textvariable=self.var_points_margin,
                                           validate="key", validatecommand=vcmd)
        self.sb_points_margin.grid(row=r, column=1, sticky="w", padx=6, pady=6)

        # 하단: 저장/닫기
        bar = ctk.CTkFrame(form)
        bar.grid(row=r+1, column=0, columnspan=2, sticky="we", padx=6, pady=(10, 0))
        ctk.CTkButton(bar, text="저장/적용", command=self._on_save).pack(side="left", padx=(0, 8))
        ctk.CTkButton(bar, text="닫기", command=self.destroy).pack(side="left")

        # 우측: 목록(좁게) + 버튼
        self.listbox = tk.Listbox(self.frm_right, height=12, exportselection=False)
        self.listbox.pack(fill="both", expand=True, padx=8, pady=(8, 6))
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        btns = ctk.CTkFrame(self.frm_right)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkButton(btns, text="추가", command=self._on_add).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(btns, text="복제", command=self._on_dup).pack(side="left", fill="x", expand=True, padx=4)
        ctk.CTkButton(btns, text="삭제", fg_color="#8e3a3a", hover_color="#7a2f2f", command=self._on_del).pack(side="left", fill="x", expand=True, padx=(4, 0))

    # ----- 목록/폼 동기화 -----
    def _reload_list(self):
        self.listbox.delete(0, "end")
        for pid, p in sorted(self.presets.items(), key=lambda kv: int(kv[0][1:]) if str(kv[0]).startswith("p") else 0):
            self.listbox.insert("end", f"{pid} · {p.get('name', pid)}")

    def _ordered_ids(self):
        return [k for k,_ in sorted(self.presets.items(), key=lambda kv: int(kv[0][1:]) if str(kv[0]).startswith("p") else 0)]

    def _select_id(self, pid: str):
        ordered = self._ordered_ids()
        try:
            idx = ordered.index(pid)
        except ValueError:
            idx = 0
        self.listbox.select_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._load_form(ordered[idx])

    def _current_pid(self) -> str | None:
        sel = self.listbox.curselection()
        if not sel:
            return None
        ordered = self._ordered_ids()
        return ordered[sel[0]]

    def _load_form(self, pid: str):
        p = self.presets.get(pid, dict(DEFAULT_PRESET))
        self.e_name.delete(0, "end")
        self.e_name.insert(0, str(p.get("name", "")))
        self.var_type.set(str(p.get("type", "rank")))

        self.var_rank_target.set(str(int(p.get("rank_target", 20))))
        self.var_rank_margin.set(str(int(p.get("rank_tolerance", 0))))
        self.var_points_target.set(str(int(p.get("points_target", 0))))
        self.var_points_margin.set(str(int(p.get("points_margin", 0))))

        self._apply_type_enable()

    def _validate_int(self, P: str) -> bool:
        # Spinbox 타이핑 중 빈 문자열 허용, 그 외에는 숫자만
        return P.isdigit() or P == ""

    def _apply_type_enable(self):
        t = self.var_type.get()
        st_rank = "normal" if t=="rank" else "disabled"
        st_pts  = "normal" if t=="points" else "disabled"
        self.sb_rank_target.config(state=st_rank)
        self.sb_rank_margin.config(state=st_rank)
        self.sb_points_target.config(state=st_pts)
        self.sb_points_margin.config(state=st_pts)

    # ----- 이벤트 -----
    def _on_close(self, *args):
        try:
            if hasattr(self.parent_app, "set_alpha_tracking_enabled"):
                self.parent_app.set_alpha_tracking_enabled(True)
        except Exception:
            pass
        self.destroy()

    def _on_list_select(self, _evt):
        pid = self._current_pid()
        if pid: self._load_form(pid)

    def _on_type_changed(self):
        self._apply_type_enable()
        # 유형별 기본 포커스
        if self.var_type.get() == "points":
            self.sb_points_target.focus_set()
        else:
            self.sb_rank_target.focus_set()

    def _on_add(self):
        pid = next_preset_id(self.presets)
        self.presets[pid] = dict(DEFAULT_PRESET) | {"name": f"프리셋 {pid[1:]}"}
        if not self.active_id:
            self.active_id = pid
        self._reload_list()
        self._select_id(pid)

    def _on_dup(self):
        src = self._current_pid()
        if not src: return
        pid = next_preset_id(self.presets)
        self.presets[pid] = copy_preset(self.presets[src]) | {"name": f"{self.presets[src].get('name','프리셋')} - 복사본"}
        self._reload_list()
        self._select_id(pid)

    def _on_del(self):
        pid = self._current_pid()
        if not pid: return
        if len(self.presets) <= 1:
            messagebox.showwarning("삭제 불가", "프리셋은 최소 1개 이상이어야 합니다.")
            return
        if messagebox.askyesno("삭제 확인", f"{pid} 프리셋을 삭제할까요?"):
            self.presets.pop(pid, None)
            if self.active_id == pid:
                self.active_id = self._ordered_ids()[0]
            self._reload_list()
            self._select_id(self.active_id)

    def _collect_form(self) -> dict:
        t = self.var_type.get()
        p = {
            "name": self.e_name.get().strip(),
            "type": t,
            "mode": t,  # ← 추가: GUI 토글/기존 코드와 호환
            "rank_target": clamp_int(self.var_rank_target.get(), 1, 9999),
            "rank_tolerance": clamp_int(self.var_rank_margin.get(), 0, 9999),
            "points_target": clamp_int(self.var_points_target.get(), 0, 99999999),
            "points_margin": clamp_int(self.var_points_margin.get(), 0, 99999999),
        }

        ok, msg = validate_preset(p)
        if not ok:
            messagebox.showerror("입력 오류", msg)
            return {}
        return p

    def _on_save(self):
        pid = self._current_pid()
        if not pid:
            messagebox.showerror("저장 실패", "선택된 프리셋이 없습니다.")
            return
        p = self._collect_form()
        if not p:
            return

        self.presets[pid] = p

        # settings 반영
        self.settings.set("goal.presets", self.presets)
        g = self.settings.get("goal", {}) or {}
        self.active_id = g.get("active_preset_id", self.active_id) or pid
        if self.active_id not in self.presets:
            self.active_id = pid
        self.settings.set("goal.active_preset_id", self.active_id)
        self.settings.save()

        # provider 갱신 + 콤보 새로고침
        try:
            if hasattr(self.parent_app, "goal_provider"):
                self.parent_app.goal_provider.reload_from_settings()
        except Exception:
            pass
        try:
            if hasattr(self.parent_app, "_refresh_goal_combo"):
                self.parent_app._refresh_goal_combo()
        except Exception:
            pass

        # ★ 목록 즉시 갱신 (이 줄들이 없어서 반영이 안 됐음)
        self._reload_list()
        self._select_id(pid)   # ← 기존 self._select_id(self.active_id) 를 pid로 교체

        messagebox.showinfo("저장 완료", "프리셋이 저장되었습니다.")

