# ui/settings_dialog.py
from __future__ import annotations
import threading
import customtkinter as ctk
from tkinter import messagebox
import autoemail
from path_manager import BASE_DIR  # ← 추가

GMAIL = "@gmail.com"
NAVER = "@naver.com"

SMTP_BY_DOMAIN = {
    GMAIL: {"provider": "gmail",  "smtp_host": "",               "smtp_port": 587, "use_tls": True},
    NAVER: {"provider": "custom", "smtp_host": "smtp.naver.com", "smtp_port": 587, "use_tls": True},
}

# === Disabled palette for card OFF state ===
DISABLED_TEXT          = ("#7A7A7A", "#5C5C5C")  # entry/textarea 본문
DISABLED_PLACEHOLDER   = ("#9A9A9A", "#6E6E6E")  # ★ placeholder(Entry/Textbox 공용)
DISABLED_LABEL_TEXT    = ("#A0A0A0", "#6A6A6A")  # 좌측 라벨(예: "메일 제목")
DISABLED_FG            = ("#2A2A2A", "#1E1E1E")  # 배경(너가 쓰던 값)
DISABLED_BORDER        = ("#3A3A3A", "#303030")  # 테두리(너가 쓰던 값)


# ---------- CTkTextbox placeholder (Entry와 동일 UX) ----------
class PlaceholderText(ctk.CTkTextbox):
    def __init__(
        self,
        master=None,
        placeholder_text="",
        placeholder_color=("gray60", "gray40"),
        initial_text: str | None = None,  # ← 추가
        **kwargs
    ):
        super().__init__(master, **kwargs)
        self._ph_text = placeholder_text
        self._ph_color = placeholder_color
        self._ph_label = ctk.CTkLabel(self, text=self._ph_text, text_color=self._ph_color)
        self._ph_x, self._ph_y = 8, 6
        self._text = getattr(self, "_textbox", None)

        # ---- 초기 내용 주입: 내용이 있으면 placeholder 자체를 안 띄움 ----
        initial = (initial_text or "").rstrip("\n")
        if initial.strip():
            super().insert("1.0", initial)
            self._hide_placeholder()  # ← 명시적으로 숨김
        else:
            self._show_placeholder_if_needed()

        # 이벤트 바인딩
        targets = [t for t in (self, self._text) if t is not None]
        for t in targets:
            t.bind("<Button-1>",    self._on_click,     add="+")
            t.bind("<FocusIn>",     self._on_focus_in,  add="+")
            t.bind("<FocusOut>",    self._on_focus_out, add="+")
            t.bind("<KeyRelease>",  self._on_key,       add="+")
            t.bind("<Configure>",   self._reposition_placeholder, add="+")

        # [ADD] 보이자마자(맵/가시화)도 본문 유무로 강제 동기화
        self.bind("<Map>", self._sync_placeholder_strict, add="+")
        self.bind("<Visibility>", self._sync_placeholder_strict, add="+")
        self.after_idle(self._sync_placeholder_strict)

    # ---- disabled 상태 가드 ----
    def _is_disabled(self) -> bool:
        """
        래퍼(CTkTextbox)와 내부 tkinter.Text 둘 다 확인.
        _ui_disabled 플래그가 있으면 그 값을 최우선으로 사용.
        """
        # 1) 토글 시 우리가 직접 세팅하는 플래그
        if hasattr(self, "_ui_disabled"):
            return bool(getattr(self, "_ui_disabled"))

        # 2) 래퍼 state
        try:
            s = str(self.cget("state"))
            if s == "disabled":
                return True
        except Exception:
            pass

        # 3) 내부 텍스트 state
        try:
            if getattr(self, "_textbox", None) is not None:
                s2 = str(self._textbox.cget("state"))
                if s2 == "disabled":
                    return True
        except Exception:
            pass
        return False

    def _break_if_disabled(self):
        # 바인딩 핸들러에서 호출: disabled면 이벤트 소비
        if self._is_disabled():
            return "break"
        return None

    # 외부에서 세팅/조회
    # ---- 외부에서 스타일/포커스 제어 (추가) ----
    def set_text_color(self, color):
        try: self.configure(text_color=color)
        except Exception: pass

    def set_placeholder_color(self, color):
        try:
            self._ph_color = color
            self._ph_label.configure(text_color=color)
        except Exception:
            pass

    def set_takefocus(self, on: bool):
        val = 1 if on else 0
        try: self.configure(takefocus=val)
        except Exception: pass
        try:
            if self._text is not None:
                self._text.configure(takefocus=val)
        except Exception:
            pass

    def set_text(self, text: str):
        super().delete("1.0", "end")
        if text and text.strip():
            super().insert("1.0", text.rstrip("\n"))
            self._hide_placeholder()
        else:
            self._show_placeholder_if_needed()

    def get_text(self) -> str:
        return self.get("1.0", "end").rstrip("\n")

    # insert/delete 오버라이드
    def insert(self, index, text, *args, **kwargs):
        super().insert(index, text, *args, **kwargs)
        self._on_key()

    def delete(self, index1, index2=None):
        super().delete(index1, index2)
        self._on_key()

    # 이벤트
    def _on_click(self, *_):
        if self._is_disabled():  # ★ 비활성 시 placeholder 유지 + 이벤트 차단
            return "break"
        self._hide_placeholder()

    def _on_focus_in(self, *_):
        if self._is_disabled():  # ★ disabled면 포커스/커서 이동도 금지
            return "break"
        self._hide_placeholder()
        try:
            if self._text is not None:
                self._text.mark_set("insert", "1.0");
                self._text.focus_set()
        except Exception:
            pass

    def _on_focus_out(self, *_):
        if not self.get("1.0","end").strip():
            self._show_placeholder_if_needed()

    def _on_key(self, *_):
        if self._is_disabled():  # ★ 프로그램적 호출/바인딩 이벤트가 와도 무시
            return "break"
        if self.get("1.0", "end").strip():
            self._hide_placeholder()
        else:
            has_focus = (self.focus_get() in (self, self._text))
            self._hide_placeholder() if has_focus else self._show_placeholder_if_needed()

    # 내부 유틸
    def _show_placeholder_if_needed(self):
        if not self._ph_text: return
        if not self.get("1.0","end").strip() and not self._ph_label.winfo_ismapped():
            self._ph_label.place(relx=0.0, rely=0.0, x=self._ph_x, y=self._ph_y, anchor="nw")

    def _hide_placeholder(self):
        if self._ph_label.winfo_ismapped(): self._ph_label.place_forget()

    def _reposition_placeholder(self, *_):
        if self._ph_label.winfo_ismapped():
            self._ph_label.place_configure(x=self._ph_x, y=self._ph_y)

    def _sync_placeholder_strict(self, *_):
        """레이아웃/맵 완료 타이밍에도 본문이 있으면 placeholder를 무조건 숨긴다."""
        try:
            if self.get("1.0", "end").strip():
                self._hide_placeholder()
            else:
                self._show_placeholder_if_needed()
        except Exception:
            pass


# ---------- SettingsDialog ----------
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, settings_manager, email_queue):
        super().__init__(parent)
        self.title("설정")
        self.geometry("980x620")
        self.minsize(960, 600)
        self.resizable(False, False)   # 리사이즈 금지
        self.grab_set()
        self.transient(parent)
        self.settings = settings_manager
        self.emailq = email_queue

        root = ctk.CTkFrame(self); root.pack(fill="both", expand=True, padx=10, pady=10)
        root.grid_columnconfigure(1, weight=1); root.grid_rowconfigure(0, weight=1)

        self.tabbar = ctk.CTkFrame(root, width=220); self.tabbar.grid(row=0, column=0, sticky="nsw", padx=(0,10))
        self.content = ctk.CTkFrame(root);           self.content.grid(row=0, column=1, sticky="nsew")

        ctk.CTkButton(self.tabbar, text="이메일 설정", command=self._show_email_tab).pack(fill="x", pady=(0,6))

        self.page_email = ctk.CTkFrame(self.content)
        self.page_email.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_email_page(self.page_email)
        self._show_email_tab()
        # (프리셋 페이지/빌더 호출 제거)

        self._parent_app = parent
        try:
            if hasattr(self._parent_app, "set_alpha_tracking_enabled"):
                self._parent_app.set_alpha_tracking_enabled(False)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._maybe_close)

    # ------- settings helpers -------
    def _refresh_entry_placeholder(self, entry_widget):
        """
        제목 Entry가 빈 값인데 placeholder가 숨겨진 상태로 남아있는 경우,
        포커스를 치우고, state를 잠깐 normal로 풀어 redraw를 유도한 다음 다시 disabled로 돌린다.
        """
        try:
            # 텍스트가 있으면 손대지 않음
            if (entry_widget.get() or "").strip():
                return

            # 현재 상태/포커스 저장
            cur_state = str(entry_widget.cget("state"))
            had_focus = (entry_widget.focus_get() == entry_widget)

            # 포커스를 다른 위젯으로 잠시 이동(placeholder가 focus-out 조건을 요구하는 테마에서 필요)
            if had_focus:
                try:
                    self.focus_set()  # 다이얼로그로 포커스 이동
                except Exception:
                    pass

            # normal로 풀어서 placeholder 라벨을 다시 붙일 수 있게 만든다
            if cur_state == "disabled":
                entry_widget.configure(state="normal")

            # 같은 값을 두 번 토글해서 내부 redraw 트리거
            ph = entry_widget.cget("placeholder_text")
            entry_widget.configure(placeholder_text="")
            entry_widget.configure(placeholder_text=ph)

            # 원래 상태로 복귀
            if cur_state == "disabled":
                entry_widget.configure(state="disabled")

        except Exception:
            pass

    # [ADD] 모든 이벤트 카드의 제목/본문 placeholder를 현재 값에 맞춰 동기화
    def _sync_event_placeholders(self):
        try:
            for ev, h in getattr(self, "_ev_widgets", {}).items():
                # 제목 Entry: 비어 있으면 placeholder가 보이도록 강제 리프레시
                self._refresh_entry_placeholder(h["ent_subject"])

                # 본문 Textbox: 내용 유무에 따라 placeholder 숨김/표시
                tb = h["txt_body"]
                body_now = tb.get_text().strip() if hasattr(tb, "get_text") else tb.get("1.0", "end").strip()
                if body_now:
                    tb._hide_placeholder()
                else:
                    tb._show_placeholder_if_needed()
        except Exception:
            pass

    def _refresh_scroll_after_toggle(self, to_top: bool = False) -> None:
        """
        제목/본문 섹션을 보이거나 숨긴 직후 scrollregion 재계산과 뷰 위치 보정을 수행한다.
        - to_top=True : 맨 위로 스크롤(숨김 시 권장)
        - to_top=False: 현 뷰 위치 유지(보임 시 권장)
        """
        try:
            # msg_section가 스크롤러(CTkScrollableFrame) 안에 있으므로 그 캔버스를 찾는다.
            container = getattr(self, "msg_section", None)
            if container is None:
                return
            parent = container.master
            canvas = None

            # 1) CTkScrollableFrame의 내부 Canvas는 통상 _parent_canvas 속성으로 노출된다.
            try:
                canvas = getattr(parent, "_parent_canvas", None)
            except Exception:
                canvas = None

            # 2) 혹시 위 방식이 실패하면 부모/자식 위젯들 중 Canvas를 탐색(보수적 가드)
            if canvas is None:
                try:
                    for child in parent.winfo_children():
                        if str(child.winfo_class()).lower() == "canvas":
                            canvas = child
                            break
                except Exception:
                    pass

            if canvas is None:
                return

            # 레이아웃을 먼저 확정시킨 뒤 scrollregion을 재계산한다.
            try:
                self.update_idletasks()
            except Exception:
                pass

            try:
                bbox = canvas.bbox("all")
                if bbox is not None:
                    canvas.configure(scrollregion=bbox)
            except Exception:
                pass

            # 숨김 전환 등으로 콘텐츠 높이가 크게 줄었을 때 안전하게 맨 위로 올린다.
            if to_top:
                try:
                    canvas.yview_moveto(0.0)
                except Exception:
                    pass

        except Exception:
            pass

    def _apply_subject_section_visibility(self):
        """전역 '이메일 알림 사용' 상태에 따라 제목/내용 섹션을 숨기거나 보인다."""
        try:
            if bool(self.var_enabled.get()):
                # 보이기
                self.msg_section.grid()
                # 레이아웃 settle 후 placeholder + scrollregion 동기화 (뷰 위치는 유지)
                try:
                    self.after_idle(self._sync_event_placeholders)
                except Exception:
                    pass
                try:
                    # 숨김→보임 전환에서도 scrollregion은 재계산해 둔다
                    self.after_idle(lambda: self._refresh_scroll_after_toggle(to_top=False))
                except Exception:
                    pass
            else:
                # 숨기기
                self.msg_section.grid_remove()
                # 레이아웃 반영 후 scrollregion 재계산 + 안전하게 맨 위로 스크롤
                try:
                    self.after_idle(lambda: self._refresh_scroll_after_toggle(to_top=True))
                except Exception:
                    pass
        except Exception:
            pass

    def _apply_event_card_state(self, ev_key: str, enabled: bool):
        """이벤트 카드의 enable/disable 시각/입력 상태를 일괄 적용."""
        h = getattr(self, "_ev_widgets", {}).get(ev_key)
        if not h: return

        ent  = h["ent_subject"]
        txt  = h["txt_body"]
        lblt = h["lbl_title"]
        lbls = h["lbl_subject"]
        lblb = h["lbl_body"]

        # 원상 복구에 쓸 오리지널 팔레트 스냅샷을 최초 1회 저장
        if "orig" not in h:
            h["orig"] = {
                "ent": {
                    "fg": ent.cget("fg_color"),
                    "bd": ent.cget("border_color"),
                    "tc": ent.cget("text_color"),
                    "ph": ent.cget("placeholder_text_color"),  # ★ 추가
                },
                "txt": {
                    "fg": txt.cget("fg_color"),
                    "bd": txt.cget("border_color"),
                    "tc": txt.cget("text_color"),
                    "ph": getattr(txt, "_ph_label", None).cget("text_color") if getattr(txt, "_ph_label", None) else None,
                },
                "labels": {
                    "t": lblt.cget("text_color"),
                    "s": lbls.cget("text_color"),
                    "b": lblb.cget("text_color"),
                },
            }

        if enabled:
            # 입력 가능 + 원래 팔레트 복구
            try:
                ent.configure(state="normal",
                              fg_color=h["orig"]["ent"]["fg"],
                              border_color=h["orig"]["ent"]["bd"],
                              text_color=h["orig"]["ent"]["tc"],
                              takefocus=1)
            except Exception: pass

            # ★ 활성화 시에도 빈 값이면 placeholder가 보이도록 보정
            self._refresh_entry_placeholder(ent)

            try:
                setattr(txt, "_ui_disabled", False)  # ★ 토글 플래그 off
            except Exception:
                pass

            # ★ placeholder 색 복구
            try:
                ent.configure(placeholder_text_color=h["orig"]["ent"]["ph"])
            except Exception:
                pass

            try:
                txt.configure(state="normal",
                              fg_color=h["orig"]["txt"]["fg"],
                              border_color=h["orig"]["txt"]["bd"])
                txt.set_text_color(h["orig"]["txt"]["tc"])
                if h["orig"]["txt"]["ph"] is not None:
                    txt.set_placeholder_color(h["orig"]["txt"]["ph"])
                txt.set_takefocus(True)
            except Exception: pass

            # (ent/txt 팔레트 복구, takefocus 설정까지 끝난 직후)
            # --- 본문 placeholder 동기화 ---
            try:
                body_now = txt.get_text().strip() if hasattr(txt, "get_text") else txt.get("1.0", "end").strip()
                if body_now:
                    txt._hide_placeholder()
                else:
                    txt._show_placeholder_if_needed()
            except Exception:
                pass

            # (선택) 제목 Entry도 비어 있으면 placeholder 재그리기 보정
            try:
                self._refresh_entry_placeholder(ent)
            except Exception:
                pass

            for lb, key in ((lblt, "t"), (lbls, "s"), (lblb, "b")):
                try: lb.configure(text_color=h["orig"]["labels"][key])
                except Exception: pass

        else:
            # 입력 차단 + 톤 다운
            try:
                ent.configure(state="disabled",
                              fg_color=DISABLED_FG,
                              border_color=DISABLED_BORDER,
                              text_color=DISABLED_TEXT,
                              takefocus=0)
            except Exception: pass

            # ★ Entry가 빈 값인데 placeholder가 사라져 있으면 강제로 다시 그리기
            self._refresh_entry_placeholder(ent)

            try:
                setattr(txt, "_ui_disabled", True)   # ★ 토글 플래그 on
            except Exception:
                pass

            # ★ placeholder 색을 더 연하게
            try:
                ent.configure(placeholder_text_color=DISABLED_PLACEHOLDER)
            except Exception:
                pass

            try:
                txt.configure(state="disabled",
                              fg_color=DISABLED_FG,
                              border_color=DISABLED_BORDER)
                txt.set_text_color(DISABLED_TEXT)
                txt.set_placeholder_color(DISABLED_PLACEHOLDER)
                txt.set_takefocus(False)
            except Exception: pass

            # ★ 본문이 비어 있으면 바로 placeholder 다시 노출
            if not txt.get("1.0", "end").strip():
                try:
                    txt._show_placeholder_if_needed()
                except Exception:
                    pass

            for lb in (lblt, lbls, lblb):
                try: lb.configure(text_color=DISABLED_LABEL_TEXT)
                except Exception: pass

    def _sget(self, dotted_key: str, default=None):
        v = self.settings.get(dotted_key, None)
        if v is not None: return v
        if dotted_key.startswith("email."):
            legacy = self.settings.get("email", None)
            if isinstance(legacy, dict):
                return legacy.get(dotted_key.split(".",1)[1], default)
        return default

    def _ensure_email_section(self):
        sec = self.settings.get("email", None)
        if not isinstance(sec, dict):
            self.settings.set("email", {}); self.settings.save()

    def _save_partial_email(self, kv: dict):
        self._ensure_email_section()
        for k, v in kv.items():
            self.settings.set(f"email.{k}", v)
        self.settings.save()

    # ------- email tab -------
    def _build_email_page(self, parent):
        row = 0
        pad = {"padx": 8, "pady": 6}

        # ─ 고정 상단 / 스크롤 본문 / 고정 하단 3분할
        top = ctk.CTkFrame(parent)
        top.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 4))

        # 상단 한 줄: 좌(체크박스) / 가운데(스페이서) / 우(버튼)
        top.grid_columnconfigure(0, weight=0)  # 체크박스
        top.grid_columnconfigure(1, weight=1)  # ← 스페이서(가변)
        top.grid_columnconfigure(2, weight=0)  # 버튼

        # ─ 전체 스크롤러(이 페이지의 모든 위젯 부모) ─
        container = ctk.CTkScrollableFrame(parent)
        container.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        try:
            parent.grid_rowconfigure(0, weight=0)  # top
            parent.grid_rowconfigure(1, weight=1)  # sc
            parent.grid_columnconfigure(0, weight=1)
        except Exception:
            pass

        container.grid_columnconfigure(0, weight=1)  # label
        container.grid_columnconfigure(1, weight=0)  # entry
        container.grid_columnconfigure(2, weight=0)  # domain
        container.grid_columnconfigure(3, weight=0)  # to-self
        container.grid_columnconfigure(4, weight=1)  # spacer

        # load
        email_enabled  = bool(self._sget("email.enabled", False))
        sender_full    = self._sget("email.sender", "") or ""
        app_password   = self._sget("email.app_password", "") or ""
        recipients_val = self._sget("email.recipients", "") or ""
        if isinstance(recipients_val, (list,tuple)):
            recipients_val = ",".join(map(str, recipients_val))

        self.var_enabled = ctk.BooleanVar(value=email_enabled)
        self.chk_enabled = ctk.CTkCheckBox(
            top,
            text="이메일 알림 사용",
            variable=self.var_enabled,
            command=self._on_toggle_enabled
        )
        self.chk_enabled.grid(row=0, column=0, sticky="w", padx=8, pady=6)

        # [기존 설정 가져오기] 버튼 (우상단)
        self.btn_import_legacy = ctk.CTkButton(
            top, text="기존 설정 가져오기", width=150, command=self._on_import_legacy
        )
        self.btn_import_legacy.grid(row=0, column=2, sticky="e", padx=8, pady=6)

        # sender (ID + domain)
        row += 1
        ctk.CTkLabel(container, text="발신 이메일").grid(row=row, column=0, sticky="e", **pad)
        sid, sdomain = self._split_sender(sender_full)
        self.ent_sender_id = ctk.CTkEntry(container, width=260); self.ent_sender_id.grid(row=row, column=1, sticky="w", padx=(8,4), pady=6)
        self.cbo_domain    = ctk.CTkOptionMenu(container, values=[GMAIL,NAVER], width=140); self.cbo_domain.grid(row=row, column=2, sticky="w", padx=(4,0), pady=6)
        self.ent_sender_id.insert(0, sid or ""); self.cbo_domain.set(sdomain if sdomain in (GMAIL,NAVER) else GMAIL)

        # preview
        row += 1
        self.lbl_sender_preview = ctk.CTkLabel(container, text=f"= {(self.ent_sender_id.get().strip() or '')}{self.cbo_domain.get()}",
                                               text_color=("gray70","gray40"))
        self.lbl_sender_preview.grid(row=row, column=1, columnspan=3, sticky="w", padx=8, pady=(0,6))

        # app pwd
        row += 1
        ctk.CTkLabel(container, text="앱 비밀번호").grid(row=row, column=0, sticky="e", **pad)
        self.ent_app_pwd = ctk.CTkEntry(container, width=260, show="•"); self.ent_app_pwd.grid(row=row, column=1, sticky="w", padx=8, pady=6)
        self.ent_app_pwd.insert(0, app_password or "")

        # ← 추가: 비밀번호 표시 토글
        self.var_show_pwd = ctk.BooleanVar(value=False)
        self.chk_show_pwd = ctk.CTkCheckBox(
            container,
            text="비밀번호 표시",
            variable=self.var_show_pwd,
            command=self._on_toggle_show_pwd
        )
        self.chk_show_pwd.grid(row=row, column=2, sticky="w", padx=(4, 0), pady=6)

        # recipients + to-self
        row += 1
        ctk.CTkLabel(container, text="수신자").grid(row=row, column=0, sticky="e", **pad)
        self.ent_recipients = ctk.CTkEntry(container, width=420); self.ent_recipients.grid(row=row, column=1, columnspan=2, sticky="w", padx=(8,4), pady=6)
        self.ent_recipients.insert(0, recipients_val or "")
        # 교체
        self.var_to_self = ctk.BooleanVar(value=False)
        self.chk_to_self = ctk.CTkCheckBox(container, text="내게 보내기", variable=self.var_to_self,
                                           command=self._on_to_self_toggle)  # ← 콜백 교체
        self.chk_to_self.grid(row=row, column=3, sticky="w", padx=(4, 0), pady=6)

        # 이 줄을 추가 (수신자 백업 저장용)
        self._rcpt_saved_before_to_self = None

        # ───────── 메일 제목/내용 설정 섹션(토글 대상) ─────────
        row += 1
        self.msg_section = ctk.CTkFrame(container, fg_color="transparent")
        self.msg_section.grid(row=row, column=0, columnspan=4, sticky="nsew", padx=0, pady=0)

        # 컬럼 가중치 재정의: 0=라벨, 1=입력부(늘어남), 2=보조, 3=버튼
        self.msg_section.grid_columnconfigure(0, weight=0)
        self.msg_section.grid_columnconfigure(1, weight=1)  # ← 여기만 늘어나게

        # (섹션 내부 로컬 row)
        r2 = 0
        hdr = ctk.CTkFrame(self.msg_section, fg_color="transparent")
        hdr.grid(row=r2, column=0, columnspan=4, sticky="ew", padx=8, pady=(0, 10))
        hdr.grid_columnconfigure(0, weight=1)
        hdr.grid_columnconfigure(1, weight=0)
        hdr.grid_columnconfigure(2, weight=1)

        line_l = ctk.CTkFrame(hdr, height=2, fg_color=("gray70", "gray30"), corner_radius=0)
        line_l.grid(row=0, column=0, sticky="ew", padx=(0, 12), pady=(18, 0))
        lbl = ctk.CTkLabel(hdr, text="메일 제목/내용 설정", font=ctk.CTkFont(size=16, weight="bold"))
        lbl.grid(row=0, column=1, sticky="n", pady=(18, 0))
        line_r = ctk.CTkFrame(hdr, height=2, fg_color=("gray55", "gray25"), corner_radius=0)
        line_r.grid(row=0, column=2, sticky="ew", padx=(12, 0), pady=(18, 0))

        # 이벤트 카드 빌더 (로컬 함수)
        def _build_event_card(container, base_row: int, ev_key: str, ev_title: str) -> int:
            # 설정값 로드
            ev_enabled = bool(self._sget(f"email.events.{ev_key}.enabled", True))
            ev_subj = self._sget(f"email.templates.{ev_key}.subject", "") or ""
            ev_body = self._sget(f"email.templates.{ev_key}.body", "") or ""
            def_subj = self._sget(f"email.defaults.{ev_key}.subject", "") or ""
            def_body = self._sget(f"email.defaults.{ev_key}.body", "") or ""

            r = base_row

            # 헤더(타이틀 + 사용 토글)
            lbl_title = ctk.CTkLabel(container, text=ev_title)
            lbl_title.grid(row=r, column=0, sticky="e", padx=(8, 0), pady=(25, 6))

            var_en = ctk.BooleanVar(value=ev_enabled)
            chk = ctk.CTkCheckBox(container, text="알림 사용", variable=var_en, command=self._on_any_change)
            chk.grid(row=r, column=1, sticky="w", padx=8, pady=(25, 6))

            # 제목
            r += 1
            lbl_subject = ctk.CTkLabel(container, text="메일 제목")
            lbl_subject.grid(row=r, column=0, sticky="e", padx=8, pady=(6, 4))
            ent_subj = ctk.CTkEntry(container, width=420, placeholder_text="메일 제목을 입력해주세요")
            ent_subj.grid(row=r, column=1, columnspan=3, sticky="ew", padx=8, pady=(6, 4))
            if ev_subj: ent_subj.insert(0, ev_subj)

            # 본문
            r += 1
            lbl_body = ctk.CTkLabel(container, text="메일 내용")
            lbl_body.grid(row=r, column=0, sticky="e", padx=8, pady=(6, 4))
            txt_body = PlaceholderText(container,
                                       placeholder_text="메일 본문 내용을 입력해주세요",
                                       initial_text=ev_body, width=420, height=120)
            txt_body.grid(row=r, column=1, columnspan=3, sticky="nsew", padx=8, pady=(4, 8))
            try:
                container.grid_rowconfigure(r, weight=0)
            except Exception:
                pass

            # 액션 영역 row 예약
            r += 1

            # 핸들 저장
            if not hasattr(self, "_ev_widgets"):
                self._ev_widgets = {}
            self._ev_widgets[ev_key] = {
                "var_enabled": var_en,
                "chk_enabled": chk,
                "ent_subject": ent_subj,
                "txt_body": txt_body,
                "lbl_title": lbl_title,
                "lbl_subject": lbl_subject,
                "lbl_body": lbl_body,
                "def_subject": def_subj,
                "def_body": def_body,
            }

            # 상태 반영(초기)
            self._apply_event_card_state(ev_key, ev_enabled)

            # 상태 변경 트레이스 → 시각/입력 상태 즉시 반영
            try:
                var_en.trace_add("write", lambda *_, key=ev_key: self._apply_event_card_state(key, bool(
                    self._ev_widgets[key]["var_enabled"].get())))
            except Exception:
                pass

            return r

        # 카드 생성(순서 고정) - 부모를 스크롤프레임(ev_parent)로 지정
        r2 = _build_event_card(self.msg_section, r2 + 1, "goal_achieved", "점수/등수 목표 달성")
        r2 = _build_event_card(self.msg_section, r2 + 1, "client_crashed", "클라이언트 튕김")
        r2 = _build_event_card(self.msg_section, r2 + 1, "freeze_detected", "게임 멈춤 감지")
        # 생성 직후 모든 카드 상태 한 번 더 동기화(안전망)
        for _key, _h in getattr(self, "_ev_widgets", {}).items():
            self._apply_event_card_state(_key, bool(_h["var_enabled"].get()))

        # --- 불러오기 초기화: 카드마다 값 주입 직후 placeholder 강제 정리 ---
        for _key, _h in getattr(self, "_ev_widgets", {}).items():
            ent = _h["ent_subject"]
            txt = _h["txt_body"]

            # 1) 카드 상태(팔레트/포커스) 재적용
            self._apply_event_card_state(_key, bool(_h["var_enabled"].get()))

            # 2) 본문: 내용이 있으면 즉시 placeholder 숨김, 없으면 표시
            if txt.get("1.0", "end").strip():
                try: txt._hide_placeholder()
                except Exception: pass
            else:
                try: txt._show_placeholder_if_needed()
                except Exception: pass

            # 3) 제목: 빈 값이면 placeholder가 보이도록 강제 리프레시
            self._refresh_entry_placeholder(ent)

        # ───────── 이벤트별 이메일 설정 섹션 끝 ─────────

        # bottom bar + status + spinner
        row += 1
        bar = ctk.CTkFrame(parent)
        bar.grid(row=2, column=0, sticky="ew", padx=8, pady=(14, 8))  # 고정 하단(상단:0, 스크롤:1, 하단:2)
        bar.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bar, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.progress.grid_remove()  # 필요할 때만 보이게
        self.lbl_status = ctk.CTkLabel(bar, text="", text_color=("gray70", "gray40"))
        self.lbl_status.grid(row=0, column=0, sticky="w", padx=(160, 0))

        self.btn_test  = ctk.CTkButton(bar, text="테스트 메일", width=140, command=self._on_test_email)
        self.btn_save  = ctk.CTkButton(bar, text="저장",       width=120, command=self._on_save_email)
        self.btn_cancel = ctk.CTkButton(bar, text="닫기", width=120, command=self._maybe_close)
        self.btn_test.grid(row=0, column=1, sticky="e")
        self.btn_save.grid(row=0, column=2, sticky="e", padx=(12,0))
        self.btn_cancel.grid(row=0, column=3, sticky="e", padx=(12,0))

        # bindings
        def _bind_update(*_):
            if bool(self.var_to_self.get()): self._apply_send_to_self()
            self._update_sender_preview()
        self.ent_sender_id.bind("<KeyRelease>", _bind_update)
        self.cbo_domain.configure(command=lambda *_: _bind_update())
        # --- 변경감지/저장버튼 상태 초기화 ---
        self._setup_dirty_tracking()  # 이벤트 바인딩 + baseline 세팅
        self._mark_dirty(False)  # 저장 버튼 비활성화로 시작
        self._apply_subject_section_visibility()

    def _snapshot_email_ui(self) -> dict:
        """현재 UI 상태를 dict로 스냅샷."""
        snap = {
            "enabled": bool(self.var_enabled.get()),
            "sender_id": (self.ent_sender_id.get() or "").strip(),
            "domain": self.cbo_domain.get(),
            "app_pwd": self.ent_app_pwd.get(),
            "rcpt": (self.ent_recipients.get() or "").strip(),
        }

        # ─ 새로 추가: 이벤트 스냅샷 ─
        events_snap = {}
        templates_snap = {}
        for ev, h in getattr(self, "_ev_widgets", {}).items():
            events_snap[ev] = bool(h["var_enabled"].get())
            templates_snap[ev] = {
                "subject": (h["ent_subject"].get() or "").strip(),
                "body": h["txt_body"].get_text(),
            }
        snap["events"] = events_snap
        snap["templates"] = templates_snap
        return snap

    def _mark_dirty(self, is_dirty: bool | None = None):
        """dirty 상태 토글 및 저장 버튼 상태 갱신."""
        if is_dirty is None:
            is_dirty = (self._snapshot_email_ui() != getattr(self, "_baseline", {}))
        self._dirty = bool(is_dirty)
        self.btn_save.configure(state=("normal" if self._dirty else "disabled"))

    def _on_any_change(self, *_):
        """어떤 필드라도 변하면 호출."""
        self._mark_dirty()

    def _setup_dirty_tracking(self):
        """변경감지 이벤트 바인딩 + baseline 설정."""
        # baseline: 위젯 값 세팅이 모두 끝난 시점에서 캡처
        self._baseline = self._snapshot_email_ui()

        # Entry류 (전역 제목 엔트리 제거)
        for w in (self.ent_sender_id, self.ent_app_pwd, self.ent_recipients):
            w.bind("<KeyRelease>", self._on_any_change)

        # ─ 새로 추가: 이벤트별 위젯 바인딩 ─
        try:
            for ev, h in getattr(self, "_ev_widgets", {}).items():
                # 제목 Entry
                h["ent_subject"].bind("<KeyRelease>", self._on_any_change, add="+")
                # 본문 Textbox(래퍼와 실제 텍스트 박스 모두)
                for t in (h["txt_body"], getattr(h["txt_body"], "_textbox", None)):
                    if t is not None:
                        t.bind("<KeyRelease>", self._on_any_change, add="+")
                # 사용 토글
                try:
                    h["var_enabled"].trace_add("write", lambda *_: self._on_any_change())
                except Exception:
                    pass
        except Exception:
            pass

        # 도메인 콤보
        old_cmd = getattr(self, "_cbo_domain_old_cmd", None)

        def _domain_changed(*_):
            # 기존에 등록해 둔 프리뷰 갱신 콜백이 있으면 호출
            try:
                self._update_sender_preview()
            except Exception:
                pass
            self._on_any_change()

        self.cbo_domain.configure(command=_domain_changed)
        self._cbo_domain_old_cmd = _domain_changed  # 보관(필요시)

        # 체크박스(알림 on/off)
        def _enabled_trace(*_):
            self._on_any_change()

        try:
            self.var_enabled.trace_add("write", _enabled_trace)
        except Exception:
            pass
        # '내게 보내기'는 저장값이 아니지만, 수신자 자동입력으로 이어질 수 있으므로 여기선 제외

    # 모든 종료 경로에서 트래킹 ON 보장
    def _maybe_close(self):
        """미저장 변경이 있으면 저장 여부 묻고 닫기."""
        if not getattr(self, "_dirty", False):
            try:
                if hasattr(self, "_parent_app") and hasattr(self._parent_app, "set_alpha_tracking_enabled"):
                    self._parent_app.set_alpha_tracking_enabled(True)
            except Exception:
                pass
            self.destroy()
            return

        res = messagebox.askyesnocancel("변경 사항 저장", "변경사항을 저장하시겠습니까?", parent=self)
        if res is None:
            return  # 취소

        if res is True:
            self._on_save_email()  # 검증/저장
            try:
                if hasattr(self, "_parent_app") and hasattr(self._parent_app, "set_alpha_tracking_enabled"):
                    self._parent_app.set_alpha_tracking_enabled(True)
            except Exception:
                pass
            self.destroy()
        else:
            try:
                if hasattr(self, "_parent_app") and hasattr(self._parent_app, "set_alpha_tracking_enabled"):
                    self._parent_app.set_alpha_tracking_enabled(True)
            except Exception:
                pass
            self.destroy()

    def _split_sender(self, sender_full: str):
        if not sender_full or "@" not in sender_full: return "", GMAIL
        local, domain = sender_full.split("@", 1)
        domain = "@" + domain.lower()
        if domain not in (GMAIL, NAVER): domain = GMAIL
        return local, domain

    def _compose_sender(self) -> str:
        return (self.ent_sender_id.get().strip() or "") + self.cbo_domain.get()

    def _update_sender_preview(self):
        self.lbl_sender_preview.configure(text=f"= {(self.ent_sender_id.get().strip() or '')}{self.cbo_domain.get()}")

    def _on_toggle_show_pwd(self):
        """'비밀번호 표시' 체크박스 토글 시, 앱 비밀번호 입력칸 마스킹 on/off."""
        try:
            if bool(self.var_show_pwd.get()):
                # 표시: 마스킹 해제
                self.ent_app_pwd.configure(show="")
            else:
                # 숨김: 다시 마스킹
                self.ent_app_pwd.configure(show="•")
        except Exception as e:
            # UI 에러는 치명적이지 않으므로 로그 정도만
            print(f"[ui] show-password toggle error: {e}")

    def _on_to_self_toggle(self):
        """체크/해제 토글 시 호출."""
        self._apply_send_to_self()
        # 토글 자체도 변경으로 간주해 저장 버튼 활성화
        try:
            self._on_any_change()
        except Exception:
            pass

    def _apply_send_to_self(self):
        """'내게 보내기' 체크 상태에 따라 수신자 필드를 자동 관리."""
        if self.var_to_self.get():
            # 1) 현재 수신자 값을 처음 한 번만 백업
            if self._rcpt_saved_before_to_self is None:
                self._rcpt_saved_before_to_self = self.ent_recipients.get()
            # 2) 발신자 이메일로 덮어쓰기(비어 있든 말든 무조건 반영)
            self.ent_recipients.delete(0, "end")
            self.ent_recipients.insert(0, self._compose_sender())
        else:
            # 해제 시 백업해둔 원래 수신자 값으로 복원
            if self._rcpt_saved_before_to_self is not None:
                self.ent_recipients.delete(0, "end")
                self.ent_recipients.insert(0, self._rcpt_saved_before_to_self)
            self._rcpt_saved_before_to_self = None

    def _on_toggle_enabled(self):
        if self.var_enabled.get():
            sender_ok = bool(self.ent_sender_id.get().strip())
            pwd_ok    = bool(self.ent_app_pwd.get())
            rcpt_ok   = bool(self.ent_recipients.get().strip() or self.var_to_self.get())
            if not (sender_ok and pwd_ok and rcpt_ok):
                messagebox.showwarning("부족한 입력", "필수 정보를 모두 입력해야 이메일 알림을 켤 수 있습니다.", parent=self)
                self.var_enabled.set(False)
        self._apply_subject_section_visibility()
        # 토글 핸들러의 마지막에:
        self.after_idle(lambda: self._refresh_scroll_after_toggle(to_top=not self.var_enabled.get()))

    def _collect_email_cfg(self) -> dict:
        sender_id = (self.ent_sender_id.get() or "").strip()
        domain    = self.cbo_domain.get()
        sender    = (sender_id + domain) if sender_id else ""
        app_pwd   = self.ent_app_pwd.get()
        rcpt      = (self.ent_recipients.get() or "").strip()
        if self.var_to_self.get() and not rcpt and sender: rcpt = sender
        smtp = SMTP_BY_DOMAIN.get(domain, SMTP_BY_DOMAIN[GMAIL])

        cfg: dict = {}
        # enabled 저장(부족 시 False로 강등)
        if self.var_enabled.get():
            miss = []
            if not sender_id: miss.append("발신 이메일 ID")
            if not app_pwd:   miss.append("앱 비밀번호")
            if not rcpt:      miss.append("수신자")
            cfg["enabled"] = False if miss else True
            if miss:
                messagebox.showwarning("부족한 입력", ", ".join(miss) + " 입력 필요. 알림은 비활성화로 저장합니다.", parent=self)
        else:
            cfg["enabled"] = False

        # 부분 저장이 아니라 현재 값 그대로 저장 (본문/제목 포함)
        cfg["sender"]        = sender
        cfg["app_password"]  = app_pwd
        cfg["recipients"]    = rcpt
        # 상단 제목/본문 제거됨. 전역 템플릿은 기존 설정값을 유지하려면 아래처럼 보존:
        cfg["subject_tmpl"] = self.settings.get("email", {}).get("subject_tmpl", "")
        cfg["body_tmpl"] = self.settings.get("email", {}).get("body_tmpl", "")

        # SMTP 정보는 항상 동기화
        cfg["provider"]  = smtp["provider"]
        cfg["smtp_host"] = smtp["smtp_host"]
        cfg["smtp_port"] = smtp["smtp_port"]
        cfg["use_tls"]   = smtp["use_tls"]

        # ─ 새로 추가: 이벤트별 저장값 수집 ─
        ev_out = {}
        tmpl_out = {}
        for ev, h in getattr(self, "_ev_widgets", {}).items():
            ev_out[ev] = {"enabled": bool(h["var_enabled"].get())}
            tmpl_out[ev] = {
                "subject": (h["ent_subject"].get() or "").strip(),
                "body": h["txt_body"].get_text(),
            }
        cfg["events"] = ev_out
        cfg["templates"] = tmpl_out

        return cfg

    def _apply_live(self, cfg: dict):
        try: autoemail.configure(cfg)
        except Exception: pass
        try:
            if self.emailq is not None: self.emailq.configure(cfg)
        except Exception: pass

    def _on_save_email(self):
        cfg = self._collect_email_cfg()
        self._save_partial_email(cfg)
        self._apply_live(cfg)

        # UI 재적용(저장된 값이 바로 보이게)
        sid, sdomain = self._split_sender(cfg.get("sender", ""))
        self.ent_sender_id.delete(0, "end")
        self.ent_sender_id.insert(0, sid or "")
        self.cbo_domain.set(sdomain if sdomain in (GMAIL,NAVER) else GMAIL)
        self._update_sender_preview()

        self.ent_recipients.delete(0, "end")
        self.ent_recipients.insert(0, cfg.get("recipients", ""))

        messagebox.showinfo("완료", "이메일 설정을 저장했습니다.", parent=self)
        # 저장 완료 → 현재 상태를 baseline으로, 저장 버튼 비활성화
        self._baseline = self._snapshot_email_ui()
        self._mark_dirty(False)
        try: self.focus_force()
        except Exception: pass

    # 테스트 메일: 알림 사용 여부와 무관. 진행 상태/스피너 표시.
    def _on_test_email(self):
        # 필수만 확인
        sid_ok = bool(self.ent_sender_id.get().strip())
        pwd_ok = bool(self.ent_app_pwd.get())
        rcpt   = (self.ent_recipients.get() or "").strip()
        if self.var_to_self.get() and not rcpt:
            rcpt = self._compose_sender()
            self.ent_recipients.delete(0,"end"); self.ent_recipients.insert(0, rcpt)

        if not (sid_ok and pwd_ok and rcpt):
            messagebox.showwarning("부족한 입력", "발신 이메일/앱 비밀번호/수신자를 입력해 주세요.", parent=self)
            return

        # 진행 UI
        self.progress.grid()              # 스피너 보이기
        self.progress.start()
        self.lbl_status.configure(text="테스트 메일 전송 중…")
        for b in (self.btn_test, self.btn_save, self.btn_cancel): b.configure(state="disabled")
        self.configure(cursor="watch")

        # 백그라운드 스레드
        cfg = self._collect_email_cfg()
        cfg["enabled"] = True  # 테스트는 강제 활성화

        def _worker():
            ok, err = False, None
            try:
                autoemail.configure(cfg)
                ok = autoemail.send_email(event="test")
            except Exception as e:
                err = e
            self.after(0, self._on_test_done, ok, err)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_test_event_email(self, ev_key: str):
        """이벤트 키로 테스트 메일 발송. UI는 변경하지 않는다."""
        # 필수만 체크(수신자는 '내게 보내기' 시 가상 계산)
        sid_ok = bool(self.ent_sender_id.get().strip())
        pwd_ok = bool(self.ent_app_pwd.get())
        rcpt = (self.ent_recipients.get() or "").strip()
        if self.var_to_self.get() and not rcpt:
            rcpt = self._compose_sender()
        if not (sid_ok and pwd_ok and rcpt):
            messagebox.showwarning("부족한 입력", "발신 이메일/앱 비밀번호/수신자를 입력해 주세요.", parent=self)
            return

        # 진행 UI
        self.progress.grid();
        self.progress.start()
        for b in (self.btn_test, self.btn_save, self.btn_cancel): b.configure(state="disabled")
        self.configure(cursor="watch")
        self.lbl_status.configure(text=f"테스트 발송 중… ({ev_key})")

        # 현재 UI 스냅샷으로 cfg 구성(이벤트 템플릿 포함)
        cfg = self._collect_email_cfg()

        # 테스트는 강제 enabled 로 간주하지 않고, 사용자가 켠 상태에서만 정상 발송됨

        def _worker():
            ok, err = False, None
            try:
                autoemail.configure(cfg)
                # 백엔드가 event를 인식한다는 전제. 미구현이면 내부에서 기본 템플릿으로 나갈 수 있음.
                ok = autoemail.send_email(event=ev_key)
            except Exception as e:
                err = e
            self.after(0, self._on_test_done, ok, err)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_test_done(self, ok: bool, err: Exception | None):
        self.progress.stop()
        self.progress.grid_remove()

        # 테스트/닫기 버튼만 즉시 복구
        try:
            self.btn_test.configure(state="normal")
            self.btn_cancel.configure(state="normal")
        except Exception:
            pass

        # 저장 버튼은 dirty 여부로 상태 결정
        self._mark_dirty()  # 스냅샷 비교로 dirty 재산출 → 내부에서 btn_save state 결정

        self.configure(cursor="")

        if ok:
            self.lbl_status.configure(text="테스트 메일 전송 완료")
            messagebox.showinfo("알림", "테스트 메일을 보냈습니다.", parent=self)
        else:
            self.lbl_status.configure(text="테스트 메일 전송 실패")
            msg = f"테스트 중 오류: {err}" if err else "테스트 메일 전송 실패"
            messagebox.showerror("에러", msg, parent=self)

    def _on_import_legacy(self):
        """
        resources/의 txt 4종을 읽어서 항목별로 가져온다.
        - 기본값(placeholder)은 공백("")으로 치환
        - enabled는 강제로 False (자동 활성화 금지)
        - settings.json에는 저장하지 않음 (UI만 채움)
        """
        # 1) 읽기
        try:
            legacy = autoemail.read_legacy_config(BASE_DIR)
        except Exception as e:
            messagebox.showerror("에러", f"레거시 설정 읽기 실패: {e}", parent=self)
            return

        if not legacy:
            messagebox.showinfo(
                "안내",
                "불러올 설정이 없습니다.\n\n"
                "아래 파일들을 '프로젝트 루트/resources/'에 덮어쓴 뒤 다시 시도하세요.\n"
                "- id.txt (발신 이메일)\n- pw.txt (앱 비밀번호)\n- email.txt (수신자)\n- mail_content.txt (제목/본문)",
                parent=self
            )
            return

        # 2) 항목별 placeholder → 공백 치환
        def _is_placeholder_email(v: str) -> bool:
            v = (v or "").strip().lower()
            return (not v) or (v == "example@gmail.com")

        def _clean_recipients(v: str) -> str:
            raw = (v or "").replace("\n", ",")
            parts = [x.strip() for x in raw.split(",") if x.strip()]
            parts = [p for p in parts if p.lower() != "example@gmail.com"]
            return ",".join(parts)

        sender = legacy.get("sender", "") or ""
        app_password = legacy.get("app_password", "") or ""
        recipients = legacy.get("recipients", "") or ""
        subject = legacy.get("subject_tmpl", "") or ""
        body = legacy.get("body_tmpl", "") or ""

        if _is_placeholder_email(sender):          sender = ""
        recipients = _clean_recipients(recipients)
        if (app_password or "").strip() == "password":
            app_password = ""

        # 제목/본문 기본값 치환
        if (subject or "").strip() in ("", "메일 제목"):
            subject = ""
        # ★ 여기서 본문 기본값도 확장 처리 (아래 패치 2와 맞물림)
        if (body or "").strip() in ("", "메일 내용", "메일 본문 내용"):
            body = ""

        # 3) 모두 빈 값이면 안내만
        if not any([sender, app_password, recipients, subject, body]):
            messagebox.showinfo(
                "안내",
                "불러올 설정이 없습니다.\n\n"
                "아래 파일들을 '프로젝트 루트/resources/'에 덮어쓴 뒤 다시 시도하세요.\n"
                "- id.txt (발신 이메일)\n- pw.txt (앱 비밀번호)\n- email.txt (수신자)\n- mail_content.txt (제목/본문)",
                parent=self
            )
            return

        # 4) UI 필드 갱신 (저장은 하지 않음)
        try:
            sid, sdomain = self._split_sender(sender)
            self.ent_sender_id.delete(0, "end")
            self.ent_sender_id.insert(0, sid or "")
            if sdomain in (GMAIL, NAVER):
                self.cbo_domain.set(sdomain)
            else:
                self.cbo_domain.set(GMAIL)
            self._update_sender_preview()

            self.ent_app_pwd.delete(0, "end")
            self.ent_app_pwd.insert(0, app_password or "")
            self.ent_recipients.delete(0, "end")
            self.ent_recipients.insert(0, recipients or "")
            # 이벤트 카드(클라이언트 아이콘 소실)로 매핑
            h = getattr(self, "_ev_widgets", {}).get("client_crashed")
            if h:
                h["ent_subject"].delete(0, "end")
                h["ent_subject"].insert(0, subject or "")
                h["txt_body"].set_text(body or "")
                # 필요하면 가져오자마자 이 이벤트만 사용 토글을 켤 수도 있음(선택):
                # h["var_enabled"].set(True)

                # --- placeholder/팔레트 보정 시작 ---
                # 1) 카드 enable/disable 팔레트/포커스 상태 재적용
                self._apply_event_card_state("client_crashed", bool(h["var_enabled"].get()))  # 함수 정의는 위에 있음

                # 2) 본문 placeholder 가시성 정리
                if h["txt_body"].get("1.0", "end").strip():
                    # 내용이 있으면 즉시 숨김 (비활성이라도 시각적으로 placeholder가 남지 않게)
                    try:
                        h["txt_body"]._hide_placeholder()
                    except Exception:
                        pass
                else:
                    # 내용 비었으면 비활성/활성 상태와 무관하게 placeholder 노출
                    try:
                        h["txt_body"]._show_placeholder_if_needed()
                    except Exception:
                        pass

                # 3) 제목 Entry placeholder 강제 리프레시(빈 값일 때만 내부 redraw 트리거)
                self._refresh_entry_placeholder(h["ent_subject"])
                # --- placeholder/팔레트 보정 끝 ---

                # --- placeholder 동기화(제목/본문 모두) ---
                for ev, hh in getattr(self, "_ev_widgets", {}).items():
                    # Entry placeholder redraw (비어 있고 disabled였다가 풀린 경우 대비)
                    self._refresh_entry_placeholder(hh["ent_subject"])

                    # Textbox placeholder: 내용이 있으면 숨김, 없으면 보임
                    tb = hh["txt_body"]
                    body_now = tb.get_text().strip() if hasattr(tb, "get_text") else tb.get("1.0", "end").strip()
                    if body_now:
                        tb._hide_placeholder()
                    else:
                        tb._show_placeholder_if_needed()

            else:
                # 이벤트 카드가 아직 없다면 안내만
                messagebox.showwarning("안내", "이벤트 입력란(클라이언트 아이콘 소실)을 찾지 못했습니다.", parent=self)

            # 저장/취소 동작과 싱크: dirty 플래그 ON → 저장 버튼 활성화
            self._on_any_change()
            # 가져온 값 반영 후, 섹션 표시/숨김 상태 재적용
            self._apply_subject_section_visibility()
            # 레이아웃이 다시 열린 경우를 대비해 idle 시점에 placeholder 최종 동기화
            try:
                self.after_idle(self._sync_event_placeholders)
            except Exception:
                pass

            messagebox.showinfo("완료", "기존 설정을 불러왔습니다.\n\n설정을 저장하시려면 저장 버튼을 눌러주세요.", parent=self)
        except Exception as e:
            messagebox.showerror("에러", f"UI 갱신 중 오류: {e}", parent=self)

    # ------- preset tab -------
    def _build_preset_page(self, parent):
        parent.grid_columnconfigure(0, weight=1); parent.grid_rowconfigure(0, weight=1)

    # ------- page switch -------
    def _show_email_tab(self):  self.page_email.lift()
