# ui/preset_panel.py
from __future__ import annotations
import tkinter as tk
import customtkinter as ctk
from tkinter import messagebox

DEFAULT_PRESET = {
    "name": "프리셋",
    "type": "rank",   # "rank" | "points"
    "rank_target": 20,
    "rank_margin": 0,
    "points_target": 0,
    "points_margin": 0,
}

UI2VAL = {"등수": "rank", "점수": "points"}
VAL2UI = {"rank": "등수", "points": "점수"}

# 테마 기본값(복구용)
try:
    THEME = ctk.ThemeManager.theme
    DEFAULT_ENTRY_FG     = THEME["CTkEntry"]["fg_color"]
    DEFAULT_ENTRY_BORDER = THEME["CTkEntry"]["border_color"]
except Exception:
    DEFAULT_ENTRY_FG     = ("#343638", "#343638")
    DEFAULT_ENTRY_BORDER = ("#565b5e", "#565b5e")

DISABLED_FG     = ("#2A2A2A", "#1E1E1E")
DISABLED_BORDER = ("#3A3A3A", "#303030")
DISABLED_TEXT   = ("#808080", "#808080")  # 배지 텍스트 색

class PresetEditorPanel(ctk.CTkFrame):
    def __init__(self, parent, settings_mgr):
        super().__init__(parent)
        self.settings = settings_mgr
        self._load_state()
        self._build_ui()

    # ----- state -----
    def _load_state(self):
        self.presets = dict(self.settings.get("goal.presets", {})) or {}
        if not self.presets: self.presets = {"p1": dict(DEFAULT_PRESET)}
        self.active_id = self.settings.get("goal.active_preset_id", next(iter(self.presets.keys())))
        if self.active_id not in self.presets:
            self.active_id = next(iter(self.presets.keys()))

    def _save_state(self):
        self.settings.set("goal.presets", self.presets)
        self.settings.set("goal.active_preset_id", self.active_id)
        try:
            self.settings.flush_debounced(immediate=True)
        except Exception:
            # 디바운스 미구현 환경 폴백
            self.settings.save()

        # --- read-back 검증 추가 ---
        try:
            rb_presets = dict(self.settings.get("goal.presets", {})) or {}
            rb_active = self.settings.get("goal.active_preset_id", None)
            ok = (rb_active == self.active_id) and (rb_presets.get(self.active_id) == self.presets.get(self.active_id))
            if not ok:
                from tkinter import messagebox
                messagebox.showwarning("저장 확인", "프리셋 일부가 저장 파일에 정확히 반영되지 않았습니다.", parent=self)
        except Exception:
            pass

    # ----- ui -----
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # left
        left = ctk.CTkFrame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 16), pady=(8, 12))
        left.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(left, text="프리셋 목록").grid(row=0, column=0, sticky="w", padx=12, pady=(4,8))
        self.listbox = tk.Listbox(left)
        self.listbox.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0,12))

        bar = ctk.CTkFrame(left)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        btn_add = ctk.CTkButton(bar, text="추가", width=76, command=self._add)
        btn_del = ctk.CTkButton(bar, text="삭제", width=76, command=self._delete)
        btn_act = ctk.CTkButton(bar, text="활성화", width=90, command=self._activate)

        # 세 버튼 모두 왼쪽 정렬 + 동일한 수평 간격
        btn_add.pack(side="left", padx=(0, 8))
        btn_del.pack(side="left", padx=8)
        btn_act.pack(side="left", padx=8)

        # right
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 0), pady=(8, 12))
        right.grid_columnconfigure(1, weight=1)
        # 아래 빈 공간이 생겨 숨 쉴 수 있게
        right.grid_rowconfigure(8, weight=1)

        r = 0
        ctk.CTkLabel(right, text="이름").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.ent_name = ctk.CTkEntry(right)
        self.ent_name.grid(row=r, column=1, sticky="we", padx=12, pady=10)
        self._register_entry(self.ent_name)

        r += 1
        ctk.CTkLabel(right, text="모드").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.type_ui = tk.StringVar(value="등수")
        self.opt_type = ctk.CTkOptionMenu(right, values=["등수","점수"], variable=self.type_ui,
                                          width=120, command=lambda *_: self._on_type_change())
        self.opt_type.grid(row=r, column=1, sticky="w", padx=12, pady=10)

        # rank
        r += 1
        ctk.CTkLabel(right, text="목표 등수").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.ent_rank_target = ctk.CTkEntry(right, width=170)
        self.ent_rank_target.grid(row=r, column=1, sticky="w", padx=12, pady=10)
        self._register_entry(self.ent_rank_target)
        self.lbl_rank_t_disabled = ctk.CTkLabel(right, text="비활성", text_color=DISABLED_TEXT)
        self.lbl_rank_t_disabled.grid(row=r, column=2, sticky="w", padx=(8,0))

        r += 1
        ctk.CTkLabel(right, text="여유치(등수)").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.ent_rank_margin = ctk.CTkEntry(right, width=170)
        self.ent_rank_margin.grid(row=r, column=1, sticky="w", padx=12, pady=10)
        self._register_entry(self.ent_rank_margin)
        self.lbl_rank_m_disabled = ctk.CTkLabel(right, text="비활성", text_color=DISABLED_TEXT)
        self.lbl_rank_m_disabled.grid(row=r, column=2, sticky="w", padx=(8,0))

        # points
        r += 1
        ctk.CTkLabel(right, text="목표 점수").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.ent_points_target = ctk.CTkEntry(right, width=170)
        self.ent_points_target.grid(row=r, column=1, sticky="w", padx=12, pady=10)
        self._register_entry(self.ent_points_target)
        self.lbl_points_t_disabled = ctk.CTkLabel(right, text="비활성", text_color=DISABLED_TEXT)
        self.lbl_points_t_disabled.grid(row=r, column=2, sticky="w", padx=(8,0))

        r += 1
        ctk.CTkLabel(right, text="여유치(점수)").grid(row=r, column=0, sticky="e", padx=12, pady=10)
        self.ent_points_margin = ctk.CTkEntry(right, width=170)
        self.ent_points_margin.grid(row=r, column=1, sticky="w", padx=12, pady=10)
        self._register_entry(self.ent_points_margin)
        self.lbl_points_m_disabled = ctk.CTkLabel(right, text="비활성", text_color=DISABLED_TEXT)
        self.lbl_points_m_disabled.grid(row=r, column=2, sticky="w", padx=(8,0))

        # bottom save
        r += 1
        bottom = ctk.CTkFrame(right); bottom.grid(row=r, column=0, columnspan=3, sticky="ew", padx=12, pady=(16,10))
        bottom.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(bottom, text="저장", width=120, command=self._save).grid(row=0, column=1, sticky="e")

        # data
        self._reload_list(); self._select(self.active_id)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

    # ---- entry style helpers ----
    def _register_entry(self, entry: ctk.CTkEntry):
        if not hasattr(entry, "_orig_fg"):
            orig_fg = entry.cget("fg_color")
            orig_border = entry.cget("border_color")
            entry._orig_fg = orig_fg if orig_fg is not None else DEFAULT_ENTRY_FG
            entry._orig_border = orig_border if orig_border is not None else DEFAULT_ENTRY_BORDER

    def _set_disabled(self, entry: ctk.CTkEntry, badge: ctk.CTkLabel, disabled: bool):
        if disabled:
            entry.configure(state="disabled", fg_color=DISABLED_FG, border_color=DISABLED_BORDER)
            badge.grid()  # 보이기
        else:
            self._register_entry(entry)
            entry.configure(state="normal", fg_color=entry._orig_fg, border_color=entry._orig_border)
            badge.grid_remove()  # 숨기기

    # ----- data / events -----
    def _reload_list(self):
        self.listbox.delete(0,"end")
        for pid, p in self.presets.items():
            name = p.get("name") or pid
            mark = "★ " if pid == self.active_id else ""
            self.listbox.insert("end", f"{mark}{name} ({pid})")

    def _select(self, pid: str):
        self.active_id = pid
        p = self.presets[pid]
        self.ent_name.delete(0,"end"); self.ent_name.insert(0, p.get("name","프리셋"))
        self.type_ui.set(VAL2UI.get(p.get("type","rank"), "등수"))
        self.ent_rank_target.delete(0,"end");   self.ent_rank_target.insert(0, str(p.get("rank_target",20)))
        self.ent_rank_margin.delete(0, "end")
        self.ent_rank_margin.insert(0, str(
            p.get("rank_margin", p.get("rank_tolerance", 0))
        ))
        self.ent_points_target.delete(0,"end"); self.ent_points_target.insert(0, str(p.get("points_target",0)))
        self.ent_points_margin.delete(0,"end"); self.ent_points_margin.insert(0, str(p.get("points_margin",0)))
        self._on_type_change()

    def _on_select(self, _evt):
        sel = self.listbox.curselection()
        if not sel: return
        text = self.listbox.get(sel[0]); pid = text.split("(")[-1].rstrip(")")
        self._select(pid)

    def _on_type_change(self):
        typ = UI2VAL.get(self.type_ui.get(), "rank")
        # 등수/점수 모드에 따라 반대쪽 입력 비활성화 + '비활성' 배지 표시
        self._set_disabled(self.ent_rank_target,   self.lbl_rank_t_disabled,   typ != "rank")
        self._set_disabled(self.ent_rank_margin,   self.lbl_rank_m_disabled,   typ != "rank")
        self._set_disabled(self.ent_points_target, self.lbl_points_t_disabled, typ != "points")
        self._set_disabled(self.ent_points_margin, self.lbl_points_m_disabled, typ != "points")

    def _save(self):
        pid = self.active_id; p = self.presets[pid]
        p["name"] = self.ent_name.get().strip() or "프리셋"
        p["type"] = UI2VAL.get(self.type_ui.get(), "rank")
        try:
            p["rank_target"]   = int(self.ent_rank_target.get() or "20")
            p["rank_margin"]   = int(self.ent_rank_margin.get() or "0")
            p["points_target"] = int(self.ent_points_target.get() or "0")
            p["points_margin"] = int(self.ent_points_margin.get() or "0")
        except ValueError:
            messagebox.showerror("오류", "숫자 항목에 정수를 입력하세요.")
            return
        # 호환성: rank_tolerance 키도 함께 기록(과거 코드와 공존)
        p["rank_tolerance"] = p.get("rank_margin", 0)
        self._save_state(); self._reload_list()
        messagebox.showinfo("저장", "프리셋이 저장되었습니다.")

    def _add(self):
        base, n = "p", 1
        while f"{base}{n}" in self.presets: n += 1
        pid = f"{base}{n}"
        self.presets[pid] = dict(DEFAULT_PRESET)
        self._reload_list(); self._select(pid)

    def _delete(self):
        sel = self.listbox.curselection()
        if not sel: return
        text = self.listbox.get(sel[0]); pid = text.split("(")[-1].rstrip(")")
        if pid not in self.presets: return
        if len(self.presets) == 1:
            messagebox.showwarning("경고", "마지막 프리셋은 삭제할 수 없습니다."); return
        del self.presets[pid]
        if self.active_id == pid: self.active_id = next(iter(self.presets.keys()))
        self._reload_list(); self._select(self.active_id)

    def _activate(self):
        sel = self.listbox.curselection()
        if not sel: return
        text = self.listbox.get(sel[0]); pid = text.split("(")[-1].rstrip(")")
        self.active_id = pid; self._save_state(); self._reload_list()
