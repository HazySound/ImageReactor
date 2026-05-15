# ui/settings_dialog.py
from __future__ import annotations
import threading
import webbrowser
import customtkinter as ctk
from tkinter import messagebox
from version import APP_VERSION, DEV_MODE
from core import updater as _updater
import autoemail
from path_manager import BASE_DIR, get_img_path  # ← 추가
from PIL import Image, ImageEnhance
from pathlib import Path

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

# 스팸 모드에서 허용할 키 목록 (pyautogui/keyboard에 매핑 가능한 이름으로 유지)
ALLOWED_SPAM_KEYS: list[str] = [
    # 문자/숫자
    "s","a","d","f","q","w","e","r","1","2","3","4","5","6","7","8","9","0",
    # 특수/제어
    "space","enter","esc","tab","backspace",
    # 화살표
    "up","down","left","right",
]

# 세로(height) 기준 축소, 가로는 비율로 계산 (너비 상한만 둠)
SPAM_THUMB_H      = 72
SPAM_THUMB_MAX_W  = 140
SPAM_THUMB_DIM    = 0.45


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
        self.owner = parent
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

        self.tabbar = ctk.CTkFrame(root, width=220)
        self.tabbar.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.content = ctk.CTkFrame(root)
        self.content.grid(row=0, column=1, sticky="nsew")

        # ─ 탭 버튼들 ─
        ctk.CTkButton(self.tabbar, text="이메일 설정", command=self._show_email_tab).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(self.tabbar, text="성능(저사양)", command=self._show_perf_tab).pack(fill="x", pady=(0, 6))
        # [ADD] 예약 종료 탭
        ctk.CTkButton(self.tabbar, text="예약 종료", command=self._show_schedule_tab).pack(fill="x", pady=(0, 6))
        # [ADD] 스팸 모드 탭
        ctk.CTkButton(self.tabbar, text="스팸 모드", command=self._show_spam_tab).pack(fill="x", pady=(0, 6))
        # [ADD] 업데이트 탭
        ctk.CTkButton(self.tabbar, text="업데이트", command=self._show_update_tab).pack(fill="x", pady=(0, 6))

        # ─ 페이지들 ─
        self.page_email = ctk.CTkFrame(self.content)
        self.page_email.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.page_perf = ctk.CTkFrame(self.content)
        self.page_perf.place(relx=0, rely=0, relwidth=1, relheight=1)
        # [ADD] 예약 종료 페이지
        self.page_schedule = ctk.CTkFrame(self.content)
        self.page_schedule.place(relx=0, rely=0, relwidth=1, relheight=1)
        # [ADD] 스팸 모드 페이지
        self.page_spam = ctk.CTkFrame(self.content)
        self.page_spam.place(relx=0, rely=0, relwidth=1, relheight=1)
        # [ADD] 업데이트 페이지
        self.page_update = ctk.CTkFrame(self.content)
        self.page_update.place(relx=0, rely=0, relwidth=1, relheight=1)

        # 스팸 탭에서 쓸 이미지 리스트 선로딩
        self._ensure_spam_choices_loaded()  # 없으면 새로 추가될 함수

        # ─ 빌드 ─
        self._build_email_page(self.page_email)
        self._build_perf_page(self.page_perf)
        # [ADD]
        self._build_schedule_page(self.page_schedule)
        # [ADD] 스팸 모드 빌드
        self._build_spam_page(self.page_spam)
        # [ADD] 업데이트 탭 빌드
        self._build_update_page(self.page_update)

        # (안전) 초기 프리뷰 동기화
        try:
            self._update_spam_preview_all()
        except Exception:
            pass

        # 기본 표시 탭은 기존과 동일
        self._show_email_tab()

        # (프리셋 페이지/빌더 호출 제거)

        self._parent_app = parent
        try:
            if hasattr(self._parent_app, "set_alpha_tracking_enabled"):
                self._parent_app.set_alpha_tracking_enabled(False)
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._maybe_close)

    # --- Topmost 계층 관리 유틸 ---
    def _push_topmost(self):
        """현재 창이 topmost면 스택에 추가하고 일시적으로 해제."""
        try:
            if not hasattr(self, "_topmost_stack"):
                self._topmost_stack = []
            was_top = bool(int(self.wm_attributes("-topmost")))
            self._topmost_stack.append(was_top)
            if was_top:
                self.attributes("-topmost", False)
            return was_top
        except Exception:
            return False

    def _pop_topmost(self):
        """스택에 있던 topmost 상태를 복원."""
        try:
            if hasattr(self, "_topmost_stack") and self._topmost_stack:
                prev = self._topmost_stack.pop()
                if prev:
                    self.attributes("-topmost", True)
                    self.lift()
                    self.focus_force()
        except Exception:
            pass

    def _force_front(self):
        """설정창을 확실히 앞으로 끌어올린다(윈도우/CTk 테마별 이슈 방지용)."""
        try:
            self.attributes("-topmost", True)
            self.lift()
            self.focus_force()
            # 메시지 큐 타이밍 차이 대비 더블 리프트
            self.after(0, lambda: (self.lift(), self.focus_force()))
            self.after(60, lambda: (self.lift(), self.focus_force()))
        except Exception:
            pass

    def _raise_self_front(self):
        """모달 종료 후 '메인 설정' 창을 확실히 최상단/포커스로 복귀."""
        try:
            # 1) 즉시 앞으로 끌어올리고 포커스/모달 그랩 복구
            self.lift()
            self.focus_force()
            self.grab_set()  # SettingsDialog가 다시 모달처럼 동작

            # 2) topmost 디더링으로 z-order 재배열
            self.attributes("-topmost", False)
            # WM에 반영될 틱을 약간 준 뒤 다시 ON
            self.after(10, lambda: self.attributes("-topmost", True))

            # 3) 마지막으로 한 번 더 lift (안전핀)
            self.after(15, self.lift)
        except Exception:
            pass

    def _lower_main_owner_once(self):
        """메인 GUI가 topmost면 잠시 내려서 순서를 확정해 준다(잠깐 내렸다가 복원)."""
        try:
            owner = getattr(self, "owner", None) or getattr(self, "master", None)
            if owner is None:
                return
            t = owner.wm_attributes("-topmost")
            owner_is_top = (int(t) == 1) if isinstance(t, (int, str)) else bool(t)
            if owner_is_top:
                owner.attributes("-topmost", False)
                # 우리가 앞으로 온 뒤 원복
                self.after(100, lambda: owner.attributes("-topmost", True))
        except Exception:
            pass

    def _restore_after_child_close(self):
        """
        자식 모달이 닫힌 ‘직후’ 호출.
        메인 GUI가 always-on-top 여도, 설정창을 즉시 최상단/포커스로 복귀시킨다.
        """
        try:
            owner = getattr(self, "owner", None) or getattr(self, "master", None)
            owner_is_top = False
            if owner is not None:
                try:
                    t = owner.wm_attributes("-topmost")
                    owner_is_top = (int(t) == 1) if isinstance(t, (int, str)) else bool(t)
                except Exception:
                    owner_is_top = False

            # 1) 메인을 잠시 내리고
            if owner_is_top:
                try:
                    owner.attributes("-topmost", False)
                except Exception:
                    pass

            # 2) 설정창을 강제로 최상단 + 포커스 + grab
            try:
                self.attributes("-topmost", True)
                self.lift()
                self.focus_force()
                self.grab_set()
            except Exception:
                pass

            # 3) 메시지 큐가 정리된 뒤 한 번 더 올려 확정
            try:
                self.after(30, lambda: (self.lift(), self.focus_force()))
            except Exception:
                pass

            # 4) 메인을 원복(있던 사람만), 그리고 마지막으로 설정창을 한 번 더 올려 순서를 고정
            if owner_is_top:
                try:
                    self.after(80, lambda: owner.attributes("-topmost", True))
                    self.after(90, lambda: (self.lift(), self.focus_force()))
                except Exception:
                    pass
        except Exception:
            pass

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

    def _build_perf_page(self, parent):
        pad = {"padx": 8, "pady": 6}

        # 상단 고정 바(옵션 설명)
        top = ctk.CTkFrame(parent)
        top.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 4))
        ctk.CTkLabel(top, text="저사양 장치 대응 옵션 (UI 부하/로그량 조절)").grid(row=0, column=0, sticky="w", padx=8, pady=6)

        # 스크롤러 (본문)
        container = ctk.CTkScrollableFrame(parent)
        container.grid(row=1, column=0, sticky="nsew")
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        # 현재 설정값 로드
        low_spec = bool(self.settings.get("gui.low_spec", False))
        tick_ms = int(self.settings.get("gui.tick_ms", 250))
        max_lines = int(self.settings.get("gui.log_max_lines", 800))

        # 위젯들
        self.var_low_spec = ctk.BooleanVar(value=low_spec)
        self.chk_low_spec = ctk.CTkCheckBox(container, text="저사양 모드 활성화", variable=self.var_low_spec,
                                            command=self._on_perf_change)
        self.chk_low_spec.grid(row=0, column=0, sticky="w", **pad)

        row = 1
        ctk.CTkLabel(container, text="GUI 폴링 주기(ms) : [숫자가 높을수록 저사양]").grid(row=row, column=0, sticky="w", **pad)
        row += 1
        self.ent_tick_ms = ctk.CTkEntry(container, width=120)
        self.ent_tick_ms.insert(0, str(tick_ms))
        self.ent_tick_ms.grid(row=row, column=0, sticky="w", **pad)
        self.ent_tick_ms.bind("<KeyRelease>", self._on_perf_change)
        row += 1

        ctk.CTkLabel(container, text="로그 최대 라인 수").grid(row=row, column=0, sticky="w", **pad)
        row += 1
        self.ent_log_lines = ctk.CTkEntry(container, width=120)
        self.ent_log_lines.insert(0, str(max_lines))
        self.ent_log_lines.grid(row=row, column=0, sticky="w", **pad)
        self.ent_log_lines.bind("<KeyRelease>", self._on_perf_change)
        row += 1

        ctk.CTkLabel(container, text="권장값: 폴링 250~350ms / 로그 800~2000 라인").grid(row=row, column=0, sticky="w", **pad)
        row += 1

        # 하단 고정 바(버튼들)
        bar = ctk.CTkFrame(parent)
        bar.grid(row=2, column=0, sticky="ew", padx=0, pady=(4, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_perf_status = ctk.CTkLabel(bar, text="", text_color=("gray70", "gray40"))
        self.lbl_perf_status.grid(row=0, column=0, sticky="w", padx=(8, 0))

        self.btn_perf_save = ctk.CTkButton(bar, text="저장", width=120, command=self._on_save_perf, state="disabled")
        self.btn_perf_close = ctk.CTkButton(bar, text="닫기", width=120, command=self._maybe_close)
        self.btn_perf_save.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.btn_perf_close.grid(row=0, column=2, sticky="e", padx=(0, 8))

        # 베이스라인 스냅샷 저장
        self._perf_baseline = self._snapshot_perf_ui()

    def _build_schedule_page(self, parent):
        pad = {"padx": 8, "pady": 6}
        top = ctk.CTkFrame(parent)
        top.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 4))
        ctk.CTkLabel(top, text="예약 종료 설정 (요일/시간 1건 + 경기 종료 후 지연 종료)").grid(row=0, column=0, sticky="w", padx=8, pady=6)

        container = ctk.CTkFrame(parent);
        container.grid(row=1, column=0, sticky="nsew")
        parent.grid_rowconfigure(1, weight=1);
        parent.grid_columnconfigure(0, weight=1)

        # 현재 설정 로드
        sched = self.settings.get("schedule", {}) or {}
        enabled = bool(sched.get("enabled", False))
        entries = (sched.get("entries") or [])
        entry = (entries[0] if entries else {"weekday": "mon", "hour": 3, "min": 0})
        delay_min = int(sched.get("shutdown_delay_min", 20))
        auto_off = bool(sched.get("auto_poweroff", True))

        # 한글↔키 매핑
        self._wday_to_key = {"월": "mon", "화": "tue", "수": "wed", "목": "thu", "금": "fri", "토": "sat", "일": "sun"}
        self._key_to_wday = {v: k for k, v in self._wday_to_key.items()}
        wday_label = self._key_to_wday.get(str(entry.get("weekday", "mon")), "월")

        # 위젯
        row = 0
        self.var_sched_enabled = ctk.BooleanVar(value=enabled)
        # 토글 시: (1) UI enable/disable 적용 → (2) 변경 감지
        self.chk_sched_enabled = ctk.CTkCheckBox(
            container, text="예약 종료 활성화", variable=self.var_sched_enabled,
            command=lambda: (self._apply_schedule_enabled_state(), self._on_schedule_change())
        )
        self.chk_sched_enabled.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        # 요일/시/분
        # 라벨 참조 보관(비활성화 시 디밍용)
        self.lbl_wday_time = ctk.CTkLabel(container, text="요일 / 시각")
        self.lbl_wday_time.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        self.cbo_wday = ctk.CTkOptionMenu(container, values=["월", "화", "수", "목", "금", "토", "일"],
                                          command=lambda *_: self._on_schedule_change())
        self.cbo_wday.set(wday_label)
        self.cbo_wday.grid(row=row, column=0, sticky="w", **pad)

        hh_vals = [f"{i:02d}" for i in range(24)]
        self.cbo_hh = ctk.CTkOptionMenu(container, values=hh_vals, command=lambda *_: self._on_schedule_change())
        self.cbo_hh.set(f"{int(entry.get('hour', 3)):02d}")
        self.cbo_hh.grid(row=row, column=1, sticky="w", **pad)

        # 0~55까지 5분 간격
        mm_vals = [f"{i:02d}" for i in range(0, 60, 5)]
        self.cbo_mm = ctk.CTkOptionMenu(container, values=mm_vals, command=lambda *_: self._on_schedule_change())
        _mm0 = int(entry.get('min', 0))
        _mm0 = max(0, min(55, _mm0 - (_mm0 % 5)))  # 5분 배수 스냅
        self.cbo_mm.set(f"{_mm0:02d}")
        self.cbo_mm.grid(row=row, column=2, sticky="w", **pad)
        row += 1

        self.lbl_auto_off = ctk.CTkLabel(container, text="예약 종료 후 자동 전원 끄기")
        self.lbl_auto_off.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        self.var_auto_off = ctk.BooleanVar(value=auto_off)
        self.chk_auto_off = ctk.CTkCheckBox(container, text="PC 자동 종료",
                                            variable=self.var_auto_off, command=self._on_schedule_change)
        self.chk_auto_off.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        self.lbl_delay = ctk.CTkLabel(container, text="지연 종료 대기(분)")
        self.lbl_delay.grid(row=row, column=0, sticky="w", **pad)
        row += 1

        self.ent_delay = ctk.CTkEntry(container, width=120)
        self.ent_delay.insert(0, str(delay_min))
        self.ent_delay.bind("<KeyRelease>", self._on_schedule_change)
        self.ent_delay.grid(row=row - 1, column=1, sticky="w", **pad)

        # [ADD] 라벨 기본 색상 스냅샷(활성화 시 복원용)
        self._sched_label_baseline = {
            "lbl_wday_time": getattr(self, "lbl_wday_time").cget("text_color"),
            "lbl_auto_off": self.lbl_auto_off.cget("text_color"),
            "lbl_delay": getattr(self, "lbl_delay").cget("text_color"),
        }

        # 하단 바
        bar = ctk.CTkFrame(parent)
        bar.grid(row=2, column=0, sticky="ew", padx=0, pady=(4, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_sched_status = ctk.CTkLabel(bar, text="", text_color=("gray70", "gray40"))
        self.lbl_sched_status.grid(row=0, column=0, sticky="w", padx=(8, 0))
        self.btn_sched_save = ctk.CTkButton(bar, text="저장", width=120, command=self._on_save_schedule, state="disabled")
        self.btn_sched_close = ctk.CTkButton(bar, text="닫기", width=120, command=self._maybe_close)
        self.btn_sched_save.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.btn_sched_close.grid(row=0, column=2, sticky="e", padx=(0, 8))

        # 초기 렌더 상태를 즉시 반영(꺼져 있으면 하위 전부 disable)
        self._apply_schedule_enabled_state()

    def _snapshot_perf_ui(self) -> dict:
        def _i(x, lo, hi, default):
            try:
                v = int(str(x).strip())
                return max(lo, min(v, hi))
            except Exception:
                return default

        return {
            "low_spec": bool(self.var_low_spec.get()),
            "tick_ms": _i(self.ent_tick_ms.get(), 200, 1000, int(self.settings.get("gui.tick_ms", 250))),
            "lines": _i(self.ent_log_lines.get(), 200, 10000, int(self.settings.get("gui.log_max_lines", 800))),
        }

    def _on_perf_change(self, *_):
        snap = self._snapshot_perf_ui()
        is_dirty = (snap != getattr(self, "_perf_baseline", {}))
        # 전역 dirty 플래그도 함께 갱신(닫기 경고 일관성 확보)
        self._mark_dirty(is_dirty)
        try:
            self.btn_perf_save.configure(state=("normal" if is_dirty else "disabled"))
            self.lbl_perf_status.configure(text=("변경 사항 있음" if is_dirty else ""))
        except Exception:
            pass

    def _collect_perf_changes(self) -> dict:
        """성능(저사양) 페이지 위젯에서 변경사항을 사전으로 수집."""
        changes = {}
        # 페이지가 아직 안 만들어졌으면 스킵
        if not hasattr(self, "var_low_spec"):
            return changes

        # 스냅샷 재사용 (유효성 포함)
        snap = self._snapshot_perf_ui()  # 이미 있는 함수
        if bool(self.settings.get("gui.low_spec", False)) != snap["low_spec"]:
            changes["gui.low_spec"] = snap["low_spec"]
        if int(self.settings.get("gui.tick_ms", 250)) != snap["tick_ms"]:
            changes["gui.tick_ms"] = snap["tick_ms"]
        if int(self.settings.get("gui.log_max_lines", 800)) != snap["lines"]:
            changes["gui.log_max_lines"] = snap["lines"]
        return changes

    def _save_perf_if_dirty(self) -> bool:
        """
        성능(저사양) 페이지에 변경이 있으면 settings에 반영하고 저장까지 수행.
        저장 성공시 True 반환.
        """
        changes = self._collect_perf_changes()
        if not changes:
            return False

        try:
            for k, v in changes.items():
                self.settings.set(k, v)
            self.settings.save_strict()

            # read-back 검증
            ok = all(self.settings.get(k) == v for k, v in changes.items())
            if not ok:
                messagebox.showwarning("저장 경고", "일부 성능 설정이 정상 반영되지 않았습니다.", parent=self)
            # 베이스라인 갱신(닫기 직후 dirty 경고 재발 방지)
            if hasattr(self, "_perf_baseline"):
                self._perf_baseline = self._snapshot_perf_ui()
            # 즉시 적용(메인 GUI에 반영)
            try:
                tgt = getattr(self, "owner", None) or getattr(self, "master", None)
                if tgt and hasattr(tgt, "apply_runtime_perf_settings"):
                    tgt.apply_runtime_perf_settings()
            except Exception:
                pass
            return True
        except Exception as e:
            messagebox.showerror("저장 실패", f"{e}", parent=self)
            return False

    def _on_save_perf(self):
        # 스냅샷으로부터 검증된 값 확보
        snap = self._snapshot_perf_ui()
        changed = {}
        if bool(self.settings.get("gui.low_spec", False)) != snap["low_spec"]:
            changed["gui.low_spec"] = snap["low_spec"]
        if int(self.settings.get("gui.tick_ms", 250)) != snap["tick_ms"]:
            changed["gui.tick_ms"] = snap["tick_ms"]
        if int(self.settings.get("gui.log_max_lines", 800)) != snap["lines"]:
            changed["gui.log_max_lines"] = snap["lines"]

        if not changed:
            try:
                self.lbl_perf_status.configure(text="변경 사항 없음")
                self.btn_perf_save.configure(state="disabled")
            except Exception:
                pass
            self._mark_dirty(False)
            return

        # 저장
        try:
            for k, v in changed.items():
                self.settings.set(k, v)
            self.settings.save_strict()
            # read-back 검증(필요 최소)
            ok = all(self.settings.get(k) == v for k, v in changed.items())
            if not ok:
                messagebox.showwarning("저장 경고", "일부 설정이 정상 반영되지 않았습니다.", parent=self)
            # 베이스라인 갱신
            self._perf_baseline = self._snapshot_perf_ui()
            self._mark_dirty(False)
            self.btn_perf_save.configure(state="disabled")
            self.lbl_perf_status.configure(text="저장 완료")
        except Exception as e:
            messagebox.showerror("저장 실패", f"{e}", parent=self)

        # 런타임 즉시 반영
        try:
            target = getattr(self, "owner", None) or getattr(self, "master", None)
            if target and hasattr(target, "apply_runtime_perf_settings"):
                target.apply_runtime_perf_settings()
        except Exception:
            pass

    # --- [NEW] 스팸 모드 탭/위젯 구성 ---
    def _build_spam_page(self, parent):
        # 레이아웃: 상단 안내 / 본문 / 하단 바
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)  # 본문 확장

        cur = self.settings.get("spam_mode", {}) or {}
        enabled = bool(cur.get("enabled", False))
        enter0 = (cur.get("enter_images") or [""])[0] if isinstance(cur.get("enter_images"), list) else ""
        exit0 = (cur.get("exit_images") or [""])[0] if isinstance(cur.get("exit_images"), list) else ""
        key0 = str(cur.get("press_key", "s")).strip().lower()
        itv0 = int(cur.get("interval_ms", 60))

        # [ADD] 다중 선택 상태(초기값: settings의 리스트)
        self._spam_enter_selected = list(cur.get("enter_images") or [])
        self._spam_exit_selected = list(cur.get("exit_images") or [])

        # ─ 상단 안내
        top = ctk.CTkFrame(parent)
        top.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 4))
        ctk.CTkLabel(top, text="스팸 모드(진입 이미지 감지→키 연타 / 탈출 이미지 감지→복귀)").grid(row=0, column=0, sticky="w", padx=10,
                                                                            pady=8)

        # ─ 본문
        body = ctk.CTkFrame(parent)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=0)  # 라벨
        body.grid_columnconfigure(1, weight=1)  # 입력행 컨테이너(콤보+썸네일)
        body.grid_columnconfigure(2, weight=0)  # 우측 버튼 열

        self.var_spam_enabled = ctk.BooleanVar(value=enabled)
        self.chk_spam_enabled = ctk.CTkCheckBox(body, text="스팸 모드 활성화",
                                                variable=self.var_spam_enabled, command=self._on_spam_change)
        self.chk_spam_enabled.grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))

        self.lbl_spam_enter = ctk.CTkLabel(body, text="진입 이미지")
        self.lbl_spam_enter.grid(row=1, column=0, sticky="w", padx=10, pady=(4, 2))

        # 컨테이너: 썸네일(좌) + 버튼(우)
        row_enter = ctk.CTkFrame(body, fg_color="transparent")
        row_enter.grid(row=1, column=1, sticky="w", padx=6, pady=(4, 2))
        row_enter.grid_columnconfigure(0, weight=0)  # 썸네일
        row_enter.grid_columnconfigure(1, weight=0)  # 버튼

        # 썸네일(라벨)
        self.lbl_spam_enter_prev = ctk.CTkLabel(row_enter, text="미리보기 없음", takefocus=0)
        self.lbl_spam_enter_prev.grid(row=0, column=0, sticky="w")

        # 다중 선택 모달 버튼
        self.btn_spam_enter_pick = ctk.CTkButton(row_enter, text="선택…", width=70,
                                                 command=lambda: self._open_spam_picker("enter"))
        self.btn_spam_enter_pick.grid(row=0, column=1, sticky="w", padx=(6, 0))

        # 콤보 제거
        self.cbo_spam_enter = None

        self.lbl_spam_exit = ctk.CTkLabel(body, text="탈출 이미지")
        self.lbl_spam_exit.grid(row=2, column=0, sticky="w", padx=10, pady=(2, 2))

        # 컨테이너: 썸네일(좌) + 버튼(우)
        row_exit = ctk.CTkFrame(body, fg_color="transparent")
        row_exit.grid(row=2, column=1, sticky="w", padx=6, pady=(2, 2))
        row_exit.grid_columnconfigure(0, weight=0)  # 썸네일
        row_exit.grid_columnconfigure(1, weight=0)  # 버튼

        # 썸네일(라벨)
        self.lbl_spam_exit_prev = ctk.CTkLabel(row_exit, text="미리보기 없음", takefocus=0)
        self.lbl_spam_exit_prev.grid(row=0, column=0, sticky="w")

        # 다중 선택 모달 버튼
        self.btn_spam_exit_pick = ctk.CTkButton(row_exit, text="선택…", width=70,
                                                command=lambda: self._open_spam_picker("exit"))
        self.btn_spam_exit_pick.grid(row=0, column=1, sticky="w", padx=(6, 0))

        # 콤보 제거
        self.cbo_spam_exit = None

        # 버튼은 우측 별도 열(column=2)에 — 썸네일 '왼쪽'이 됨
        self.btn_spam_refresh = ctk.CTkButton(body, text="이미지 새로고침", width=120,
                                              command=self._reload_spam_image_choices)
        self.btn_spam_refresh.grid(row=2, column=2, sticky="w", padx=6, pady=(2, 2))

        self.lbl_spam_key   = ctk.CTkLabel(body, text="연타 키")
        self.lbl_spam_key.grid(row=3, column=0, sticky="w", padx=10, pady=(10,2))
        self.cbo_spam_key = ctk.CTkComboBox(body, values=ALLOWED_SPAM_KEYS, width=120,
                                            command=lambda *_: self._on_spam_change())
        self.cbo_spam_key.set(key0 if key0 in ALLOWED_SPAM_KEYS else "s")
        self.cbo_spam_key.grid(row=3, column=1, sticky="w", padx=6, pady=(10, 2))

        self.lbl_spam_itv   = ctk.CTkLabel(body, text="간격(ms)")
        self.lbl_spam_itv.grid(row=4, column=0, sticky="w", padx=10, pady=(2,2))
        self.ent_spam_itv = ctk.CTkEntry(body, width=120)
        self.ent_spam_itv.insert(0, str(itv0))
        self.ent_spam_itv.grid(row=4, column=1, sticky="w", padx=6, pady=(2, 2))
        self.ent_spam_itv.bind("<KeyRelease>", self._on_spam_change)

        # ─ 하단 바(다른 탭과 동일)
        bar = ctk.CTkFrame(parent)
        bar.grid(row=2, column=0, sticky="ew", padx=0, pady=(4, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.lbl_spam_status = ctk.CTkLabel(bar, text="", text_color=("gray70", "gray40"))
        self.lbl_spam_status.grid(row=0, column=0, sticky="w", padx=(8, 0))
        self.btn_spam_save = ctk.CTkButton(bar, text="저장", width=120, command=self._on_save_spam, state="disabled")
        self.btn_spam_close = ctk.CTkButton(bar, text="닫기", width=120, command=self._maybe_close)
        self.btn_spam_save.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.btn_spam_close.grid(row=0, column=2, sticky="e", padx=(0, 8))

        # baseline 이후 초기 상태/미리보기 반영
        self._apply_spam_enabled_state()
        self._update_spam_preview_all()

        # 베이스라인 스냅샷 (dirty 비교 기준)
        self._spam_baseline = self._snapshot_spam_ui()

    def _ensure_spam_choices_loaded(self):
        """self._spam_img_choices를 준비한다. 실패해도 [] 보장."""
        if getattr(self, "_spam_img_choices", None) is not None:
            return self._spam_img_choices

        try:
            from path_manager import BASE_DIR
            p = (BASE_DIR / "routine.json")
        except Exception:
            from pathlib import Path
            p = Path("routine.json")

        imgs = []
        try:
            if p.exists():
                import json
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    # 최상위 배열 [{image:...}, ...]
                    for it in data:
                        x = str((it or {}).get("image", "")).strip()
                        if x: imgs.append(x)
                elif isinstance(data, dict):
                    # 객체형 {"routine_items":[...]}
                    for it in (data.get("routine_items") or []):
                        x = str((it or {}).get("image", "")).strip()
                        if x: imgs.append(x)
        except Exception:
            imgs = []

        self._spam_img_choices = sorted(set(imgs))
        return self._spam_img_choices

    def _reload_spam_image_choices(self):
        # [ADD] 썸네일 캐시 비우기(경로/파일 교체 반영)
        self._spam_thumb_cache = {}
        self._spam_thumb_dim_cache = {}

        self._spam_img_choices = self._scan_routine_images()

        # 선택 모달은 self._spam_img_choices를 직접 사용하므로 별도 갱신 불필요
        self._on_spam_change()
        self._update_spam_preview_all()
        # [ADD] 현재 토글 상태에 맞춰 최종 표시/숨김 확정
        self._show_spam_previews(bool(self.var_spam_enabled.get()))

    def _scan_routine_images(self) -> list[str]:
        from pathlib import Path
        import json
        try:
            from path_manager import BASE_DIR
            rjson_path = (BASE_DIR / "routine.json")
        except Exception:
            rjson_path = Path("routine.json")

        imgs: list[str] = []
        try:
            if rjson_path.exists():
                data = json.loads(rjson_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for it in data:
                        img = str((it or {}).get("image", "")).strip()
                        if img: imgs.append(img)
                elif isinstance(data, dict):
                    for it in (data.get("routine_items") or []):
                        img = str((it or {}).get("image", "")).strip()
                        if img: imgs.append(img)
        except Exception:
            pass

        names = sorted(set(imgs))
        # [ADD] 경로 캐시 미리워밍(선택)
        try:
            for n in names:
                _ = self._resolve_spam_image_path(n)
        except Exception:
            pass
        return names

    def _snapshot_spam_ui(self) -> dict:
        def _i(s, lo, hi, default):
            try:
                v = int(str(s).strip())
                return max(lo, min(hi, v))
            except Exception:
                return default

        key = (self.cbo_spam_key.get().strip().lower() if hasattr(self, "cbo_spam_key") else "s")
        if key not in ALLOWED_SPAM_KEYS: key = "s"
        # 콤보는 “빠른 추가” 용으로만 유지하고, 실제 저장은 다중 선택 리스트를 사용
        enter_list = list(dict.fromkeys(x for x in (self._spam_enter_selected or []) if x))  # 중복 제거/원순서 유지
        exit_list = list(dict.fromkeys(x for x in (self._spam_exit_selected or []) if x))
        itv = _i(self.ent_spam_itv.get(), 10, 500, 60) if hasattr(self, "ent_spam_itv") else 60
        return {
            "enabled": bool(self.var_spam_enabled.get()) if hasattr(self, "var_spam_enabled") else False,
            "enter_images": enter_list,
            "exit_images": exit_list,
            "press_key": key,
            "interval_ms": itv,
        }

    def _apply_spam_enabled_state(self):
        """스팸 모드 on/off에 따라 입력/색/썸네일 디밍까지 일괄 적용."""
        enabled = bool(self.var_spam_enabled.get())
        state = "normal" if enabled else "disabled"

        # 위젯 상태
        widgets = [self.cbo_spam_key, self.ent_spam_itv, self.btn_spam_refresh,
                   self.btn_spam_enter_pick, self.btn_spam_exit_pick]
        for w in (self.cbo_spam_enter, self.cbo_spam_exit):
            if w is not None:
                widgets.append(w)
        for w in widgets:
            try:
                w.configure(state=state)
            except Exception:
                pass

        # 라벨 색상(초기 색 저장)
        if not hasattr(self, "_spam_label_baseline"):
            self._spam_label_baseline = {
                "enter": self.lbl_spam_enter.cget("text_color"),
                "exit": self.lbl_spam_exit.cget("text_color"),
                "key": self.lbl_spam_key.cget("text_color"),
                "itv": self.lbl_spam_itv.cget("text_color"),
            }
        dim = ("gray70", "gray40")
        try:
            self.lbl_spam_enter.configure(text_color=(self._spam_label_baseline["enter"] if enabled else dim))
        except Exception:
            pass
        try:
            self.lbl_spam_exit.configure(text_color=(self._spam_label_baseline["exit"] if enabled else dim))
        except Exception:
            pass
        try:
            self.lbl_spam_key.configure(text_color=(self._spam_label_baseline["key"] if enabled else dim))
        except Exception:
            pass
        try:
            self.lbl_spam_itv.configure(text_color=(self._spam_label_baseline["itv"] if enabled else dim))
        except Exception:
            pass

        # 미리보기 처리
        if enabled:
            # 1) 그림 먼저 갱신
            self._update_spam_preview_all()
            # 2) 선택 유무 기준으로 보이기/숨기기 결정
            self._sync_preview_visibility()
        else:
            # 비활성이면 전부 숨김
            self._show_spam_previews(False)

    def _on_spam_change(self, *_):
        try:
            # 1) 상태/색/썸네일 즉시 반영
            self._apply_spam_enabled_state()
            # 1.5) 가시화 최종 보정(선택이 0개면 숨김)
            self._sync_preview_visibility()

            # 2) dirty 판정 + 상태 텍스트
            snap = self._snapshot_spam_ui()
            is_dirty = (snap != getattr(self, "_spam_baseline", {}))
            if hasattr(self, "_mark_dirty"):
                self._mark_dirty(is_dirty)
            self.btn_spam_save.configure(state=("normal" if is_dirty else "disabled"))
            self.lbl_spam_status.configure(text=("변경 사항 있음" if is_dirty else ""))
        except Exception:
            pass

    def _collect_spam_changes(self) -> dict:
        """현재 UI와 settings의 spam_mode를 비교해 바뀐 항목만 반환."""
        snap = self._snapshot_spam_ui()
        cur = self.settings.get("spam_mode", {}) or {}
        normalized_cur = {
            "enabled": bool(cur.get("enabled", False)),
            "enter_images": list(cur.get("enter_images") or []),
            "exit_images": list(cur.get("exit_images") or []),
            "press_key": str(cur.get("press_key", "s")).strip().lower(),
            "interval_ms": int(cur.get("interval_ms", 60)),
        }
        return {"spam_mode": snap} if (snap != normalized_cur) else {}

    def _save_spam_if_dirty(self) -> bool:
        changed = self._collect_spam_changes()
        if not changed:
            try:
                self.lbl_spam_status.configure(text="변경 사항 없음")
                self.btn_spam_save.configure(state="disabled")
            except Exception:
                pass
            # [ADD] 전역 dirty 강제 해제 (저장 버튼이 비활성이어도 _dirty가 True일 수 있음)
            try:
                self._mark_dirty(False)
            except Exception:
                pass
            return False

        try:
            for k, v in changed.items():
                self.settings.set(k, v)
            # 디스크 저장
            try:
                self.settings.flush_debounced(immediate=True)
            except Exception:
                self.settings.save()

            # 런타임 반영
            try:
                import main as _main
                if hasattr(_main, "reload_spam_mode_from_settings"):
                    _main.reload_spam_mode_from_settings()
            except Exception:
                pass

            # 베이스라인 갱신 + 버튼 상태
            self._spam_baseline = self._snapshot_spam_ui()
            self.btn_spam_save.configure(state="disabled")
            self.lbl_spam_status.configure(text="저장 완료")
            # [ADD] 전역 dirty 해제 → 닫기 시 추가 확인 창 방지
            try:
                self._mark_dirty(False)
            except Exception:
                pass
            return True
        except Exception as e:
            messagebox.showerror("저장 실패", f"{e}", parent=self)
            return False

    def _on_save_spam(self):
        self._save_spam_if_dirty()

    # [ADD] --- spam image helpers ---
    def _resolve_existing_path(self, p: Path) -> Path | None:
        try:
            return p if p.exists() else None
        except Exception:
            return None

    def _resolve_spam_image_path(self, name: str) -> Path | None:
        """
        썸네일 원본 파일의 실제 경로를 해석한다.
        탐색 우선순위:
          1) 절대경로 그대로
          2) BASE_DIR / get_img_path() / name     (예: resources/1920x1080/s2.png)
          3) BASE_DIR / "resources" / name
          4) BASE_DIR / name
          5) BASE_DIR 내 case-insensitive rglob(name)
        """
        if not name:
            return None

        from pathlib import Path

        def _exists(p: Path) -> Path | None:
            try:
                return p if p.exists() else None
            except Exception:
                return None

        # 1) 절대경로
        try:
            p = Path(name)
            if p.is_absolute():
                hit = _exists(p)
                if hit:
                    return hit
        except Exception:
            pass

        # 2) 해상도 폴더(최우선)
        try:
            # get_img_path()는 "resources/{WxH}/" 같은 상대경로 문자열을 반환
            res_dir = BASE_DIR / Path(get_img_path())
            hit = _exists(res_dir / name)
            if hit:
                return hit
        except Exception:
            pass

        # 3) resources 바로 아래
        hit = _exists(BASE_DIR / "resources" / name)
        if hit:
            return hit

        # 4) 프로젝트 루트 바로 아래
        hit = _exists(BASE_DIR / name)
        if hit:
            return hit

        # 5) 마지막 수단: 대소문자 무시 전체 검색
        try:
            target_lower = name.lower()
            for p in BASE_DIR.rglob("*"):
                # 너무 큰 트리에서 비용 줄이기: 파일만 검사
                if not p.is_file():
                    continue
                if p.name.lower() == target_lower:
                    return p
        except Exception:
            pass

        return None

    def _get_pil_thumb(self, name: str, side: int = 48, dim: bool = False):
        """
        PIL 이미지 썸네일 반환(투명 유지). 기존 _get_ctk_thumb와 동일 경로해석/캐시 사용.
        """
        try:
            p = self._resolve_spam_image_path(name)
            if not p: return None
            from PIL import Image, ImageEnhance
            im = Image.open(p).convert("RGBA")
            im.thumbnail((side, side), Image.Resampling.LANCZOS)
            if dim:
                # 살짝 디밍
                r, g, b, a = im.split()
                base = Image.new("RGBA", im.size, (0, 0, 0, 0))
                base.paste(im, mask=a)
                im = Image.blend(base, Image.new("RGBA", im.size, (60, 60, 60, 0)), 0.25)
            return im
        except Exception:
            return None

    def _get_ctk_thumb(self, name: str, dim: bool = False):
        """원본보다 키우지 않는다(Downscale-only). 비율 유지. 높이/너비 상한을 동시에 만족."""
        if not hasattr(self, "_spam_thumb_cache"):
            self._spam_thumb_cache = {}
            self._spam_thumb_dim_cache = {}

        cache = self._spam_thumb_dim_cache if dim else self._spam_thumb_cache
        if name in cache:
            return cache[name]

        p = self._resolve_spam_image_path(name)
        if not p:
            cache[name] = None
            return None

        try:
            im = Image.open(p).convert("RGBA")
            w0, h0 = im.width, im.height
            if w0 <= 0 or h0 <= 0:
                cache[name] = None
                return None

            # 축소 비율(업스케일 금지: 1.0보다 커지지 않음)
            r_h = SPAM_THUMB_H / h0
            r_w = SPAM_THUMB_MAX_W / w0
            r = min(r_h, r_w, 1.0)  # <- 핵심

            w = max(1, int(round(w0 * r)))
            h = max(1, int(round(h0 * r)))

            if r < 1.0:
                im = im.resize((w, h), Image.LANCZOS)  # 축소만 수행

            if dim:
                im = ImageEnhance.Brightness(im).enhance(SPAM_THUMB_DIM)

            cimg = ctk.CTkImage(light_image=im, dark_image=im, size=(w, h))
            cache[name] = cimg
            return cimg
        except Exception:
            cache[name] = None
            return None

    def _update_spam_preview(self, which: str):
        try:
            lbl = self.lbl_spam_enter_prev if which == "enter" else self.lbl_spam_exit_prev
            enabled = bool(self.var_spam_enabled.get())
            names = (self._spam_enter_selected if which == "enter" else self._spam_exit_selected) or []

            # 라벨이 숨겨졌다면 먼저 보이게
            try:
                lbl.grid()
            except Exception:
                pass

            if not names:
                self._clear_preview_image(lbl)
                # 비어 있을 때는 라벨 자체를 숨김 (완전 제거)
                try:
                    lbl.grid_remove()
                except Exception:
                    pass
                return
            else:
                # 다시 선택되었으면 썸네일 영역 복원
                try:
                    if not lbl.winfo_ismapped():
                        lbl.grid()
                except Exception:
                    pass

            # --- 항상 이전 이미지 완전 제거 후 시작 ---
            self._clear_preview_image(lbl)

            # 모자이크 합성
            tile, pad, max_cols = 48, 2, 6
            cols = min(max_cols, max(1, len(names)))
            rows = (len(names) + cols - 1) // cols
            from PIL import Image
            W = cols * tile + (cols - 1) * pad
            H = rows * tile + (rows - 1) * pad
            canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))

            for idx, nm in enumerate(names):
                col, row = (idx % cols), (idx // cols)
                x, y = col * (tile + pad), row * (tile + pad)
                thumb = self._get_pil_thumb(nm, side=tile, dim=(not enabled))
                if thumb is not None:
                    canvas.paste(thumb, (x, y), mask=thumb if thumb.mode == "RGBA" else None)

            # 새 이미지 구성 + 다중 레퍼런스 보관
            cimg = ctk.CTkImage(light_image=canvas, dark_image=canvas, size=(W, H))
            lbl.configure(image=cimg, text="")
            lbl._image = cimg  # CTk 내부용(관례)
            lbl.image = cimg  # Tkinter 관례까지 함께 유지

            # 클래스 레벨 캐시에도 저장(가비지콜렉션 방지 > 드문 케이스 커버)
            if not hasattr(self, "_spam_prev_cache"):
                self._spam_prev_cache = {}
            self._spam_prev_cache[which] = cimg

            # 즉시 리페인트
            try:
                lbl.update_idletasks()
            except Exception:
                pass
        except Exception:
            pass

    def _update_spam_preview_all(self):
        self._update_spam_preview("enter")
        self._update_spam_preview("exit")

    # [ADD] 프리뷰 라벨 강제 해제/숨김/표시
    def _clear_preview_image(self, lbl):
        try:
            lbl.configure(image=None, text="")  # 텍스트/이미지 초기화
            for attr in ("_image", "image"):
                if hasattr(lbl, attr):
                    setattr(lbl, attr, None)
        except Exception:
            pass

    def _show_spam_previews(self, show: bool):
        targets = [self.lbl_spam_enter_prev, self.lbl_spam_exit_prev]
        for lbl in targets:
            try:
                if show:
                    # 숨겨져 있었으면 다시 표시
                    if not lbl.winfo_ismapped():
                        lbl.grid()
                else:
                    # 완전 제거 (텍스트/이미지 초기화 + 숨김)
                    self._clear_preview_image(lbl)
                    lbl.grid_remove()
            except Exception:
                pass

    def _sync_preview_visibility(self):
        """선택 유무와 스팸 활성 상태에 맞춰 라벨을 보이거나 숨긴다."""
        enabled = bool(self.var_spam_enabled.get())
        pairs = (("enter", self.lbl_spam_enter_prev), ("exit", self.lbl_spam_exit_prev))
        for which, lbl in pairs:
            has = bool(self._spam_enter_selected) if which == "enter" else bool(self._spam_exit_selected)
            try:
                if enabled and has:
                    if not lbl.winfo_ismapped():
                        lbl.grid()
                else:
                    self._clear_preview_image(lbl)
                    if lbl.winfo_ismapped():
                        lbl.grid_remove()
            except Exception:
                pass

    def _open_spam_picker(self, which: str):
        """
        which in {'enter','exit'}
        체크박스 리스트 + 썸네일(옵션) 모달을 띄워 다중 선택.
        """
        import tkinter as tk
        # 부모 topmost 해제 push
        self._push_topmost()

        # (1) 시작부: topmost 스택/토글 불필요. 아래 3줄만 보장
        win = ctk.CTkToplevel(self)
        win.title("스팸 이미지 선택")
        win.transient(self)
        win.grab_set()
        try:
            win.attributes("-topmost", True)
            win.lift()
            win.focus_force()
        except Exception:
            pass

        # 검색
        search_var = tk.StringVar()
        frm_top = ctk.CTkFrame(win)
        frm_top.pack(fill="x", padx=8, pady=(8, 4))
        ent_search = ctk.CTkEntry(frm_top, placeholder_text="이름 검색", textvariable=search_var)
        ent_search.pack(side="left", fill="x", expand=True, padx=(0, 6))
        btn_clear = ctk.CTkButton(frm_top, text="지우기", width=70, command=lambda: search_var.set(""))
        btn_clear.pack(side="left")

        # 스크롤 영역
        frm_list = ctk.CTkScrollableFrame(win, width=480, height=420)
        frm_list.pack(fill="both", expand=True, padx=8, pady=4)

        # 현재 선택 상태
        selected = set(self._spam_enter_selected if which == "enter" else self._spam_exit_selected)

        # 체크박스 렌더
        rows = []

        def rebuild():
            for r in rows:
                try:
                    r.destroy()
                except Exception:
                    pass
            rows.clear()
            kw = (search_var.get() or "").strip().lower()
            for name in self._spam_img_choices:
                if kw and (kw not in name.lower()):
                    continue
                row = ctk.CTkFrame(frm_list, fg_color="transparent")
                row.pack(fill="x", padx=2, pady=2)
                var = tk.BooleanVar(value=(name in selected))
                chk = ctk.CTkCheckBox(row, text=name, variable=var)
                chk.pack(side="left", padx=(4, 6))
                # (옵션) 작은 미리보기
                try:
                    img = self._get_ctk_thumb(name, dim=False)
                    if img:
                        prev = ctk.CTkLabel(row, image=img, text="")
                        prev._image = img
                        prev.pack(side="left")
                except Exception:
                    pass

                # 선택 토글 동기화
                def _on_toggle(n=name, v=var):
                    if v.get():
                        selected.add(n)
                    else:
                        selected.discard(n)

                chk.configure(command=_on_toggle)
                rows.append(row)

        rebuild()
        ent_search.bind("<KeyRelease>", lambda *_: rebuild())

        # 하단 버튼
        frm_bot = ctk.CTkFrame(win)
        frm_bot.pack(fill="x", padx=8, pady=(4, 8))

        selected = set(self._spam_enter_selected if which == "enter" else self._spam_exit_selected)

        def on_ok():
            lst = sorted(selected)
            if which == "enter":
                self._spam_enter_selected = lst
            else:
                self._spam_exit_selected = lst
            # 프리뷰를 모달 닫기 전에 즉시 갱신(시각적으로 확실)
            self._update_spam_preview(which)
            win.destroy()

        ctk.CTkButton(frm_bot, text="확인", width=100, command=on_ok).pack(side="right", padx=(6, 0))
        ctk.CTkButton(frm_bot, text="취소", width=100, command=win.destroy).pack(side="right")

        # X/ESC도 동일 동작
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.bind("<Escape>", lambda *_: win.destroy(), add="+")

        # (2) 닫힘 대기
        win.wait_window()

        # (3) 단 한 번의 복귀 루틴만 호출
        self._restore_after_child_close()

        # (4) 프리뷰/상태 동기화
        self._on_spam_change()

    # ---------- 스팸 모드 관련 끝 ----------

    def _snapshot_schedule_ui(self) -> dict:
        def _i(txt, lo, hi, default):
            try:
                v = int(str(txt).strip())
                return max(lo, min(v, hi))
            except Exception:
                return default

        weekday_key = self._wday_to_key.get(self.cbo_wday.get(), "mon")
        hh = _i(self.cbo_hh.get(), 0, 23, 3)
        mm = _i(self.cbo_mm.get(), 0, 59, 0)
        delay = _i(self.ent_delay.get(), 0, 600, 20)  # 최대 10시간 가드
        snap = {
            "schedule.enabled": bool(self.var_sched_enabled.get()),
            "schedule.entries": [{"weekday": weekday_key, "time": f"{hh:02d}:{mm:02d}"}],
            "schedule.shutdown_delay_min": delay,
            "schedule.auto_poweroff": bool(self.var_auto_off.get()),
        }
        return snap

    def _on_schedule_change(self, *_):
        try:
            snap = self._snapshot_schedule_ui()
            # 현재 settings와 비교
            cur = self.settings.get("schedule", {}) or {}
            cur_ent = (cur.get("entries") or [{"weekday": "mon", "time": "03:00"}])[0]
            cur_key = {
                "schedule.enabled": bool(cur.get("enabled", False)),
                "schedule.entries": [
                    {"weekday": str(cur_ent.get("weekday", "mon")), "time": str(cur_ent.get("time", "03:00"))}],
                "schedule.shutdown_delay_min": int(cur.get("shutdown_delay_min", 20)),
                "schedule.auto_poweroff": bool(cur.get("auto_poweroff", True)),
            }

            is_dirty = (snap != cur_key)
            self._mark_dirty(is_dirty)
            self.btn_sched_save.configure(state=("normal" if is_dirty else "disabled"))
            self.lbl_sched_status.configure(text=("변경 사항 있음" if is_dirty else ""))
        except Exception:
            # UI 에러는 치명적이지 않음
            pass

    def _collect_schedule_changes(self) -> dict:
        changes = {}
        snap = self._snapshot_schedule_ui()
        cur = self.settings.get("schedule", {}) or {}
        # enabled
        if bool(cur.get("enabled", False)) != snap["schedule.enabled"]:
            changes["schedule.enabled"] = snap["schedule.enabled"]
        # entries (단일 엔트리 비교)
        cur_ent = (cur.get("entries") or [{"weekday": "mon", "time": "03:00"}])[0]
        new_ent = snap["schedule.entries"][0]
        if (str(cur_ent.get("weekday", "mon")) != new_ent["weekday"] or
                str(cur_ent.get("time", "03:00")) != new_ent["time"]):
            changes["schedule.entries"] = [new_ent]
        # delay
        if int(cur.get("shutdown_delay_min", 20)) != int(snap["schedule.shutdown_delay_min"]):
            changes["schedule.shutdown_delay_min"] = int(snap["schedule.shutdown_delay_min"])
        # auto_off
        if bool(cur.get("auto_poweroff", True)) != bool(snap["schedule.auto_poweroff"]):
            changes["schedule.auto_poweroff"] = bool(snap["schedule.auto_poweroff"])
        return changes

    def _on_save_schedule(self):
        self._save_schedule_if_dirty()

    def _save_schedule_if_dirty(self) -> bool:
        changed = self._collect_schedule_changes()
        if not changed:
            try:
                self.lbl_sched_status.configure(text="변경 사항 없음")
                self.btn_sched_save.configure(state="disabled")
            except Exception:
                pass
            return False

        # settings 반영
        for k, v in changed.items():
            self.settings.set(k, v)
        # 디스크 즉시 저장
        try:
            self.settings.flush_debounced(immediate=True)
        except Exception:
            self.settings.save()

        # 런타임 재적용 + 상태 로그
        try:
            import main as _mainmod
            # 1) 런타임 재적용
            _mainmod.reload_scheduled_shutdown()  # ← 인자 없음

            # 2) 상태 로그 출력(첫 엔트리만 표기)
            s = self.settings.get("schedule", {}) or {}
            ent = (s.get("entries") or [])
            weekday_ko = ""
            hhmm = ""
            if ent:
                wd_key = str(ent[0].get("weekday", "mon"))
                hhmm = str(ent[0].get("time", "03:00"))
                _KO = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}
                weekday_ko = _KO.get(wd_key, wd_key)

            try:
                _mainmod.log_scheduled_shutdown_state(
                    enabled=bool(s.get("enabled", False)),
                    weekday_ko=weekday_ko,
                    hhmm=hhmm,
                    auto_poweroff=bool(s.get("auto_poweroff", False)),
                    delay_min=int(s.get("shutdown_delay_min", 20)),
                )
            except Exception:
                pass

            # 3) 예약 기능을 끄면 보류된 OS 종료 타이머를 명시적으로 해제
            try:
                if "schedule.enabled" in changed and changed["schedule.enabled"] is False:
                    # NOTE: main에 래퍼가 없으므로 컨트롤러 직접 접근
                    getattr(_mainmod, "_SCHED_SD", None) and _mainmod._SCHED_SD.cancel_pending_shutdown()
            except Exception:
                pass

        except Exception:
            pass

        try:
            self.lbl_sched_status.configure(text="저장 완료")
            self.btn_sched_save.configure(state="disabled")
        except Exception:
            pass
        # 전체 닫기 경고 일관성
        self._mark_dirty(False)
        return True

    def _apply_schedule_enabled_state(self):
        """예약 종료 활성/비활성에 따라 하위 위젯 일괄 제어 + 시각적 디밍."""
        enabled = bool(self.var_sched_enabled.get())
        state = "normal" if enabled else "disabled"
        # 입력 위젯 상태 변경
        for w in (self.cbo_wday, self.cbo_hh, self.cbo_mm, self.chk_auto_off, self.ent_delay):
            try:
                w.configure(state=state)
            except Exception:
                pass

        # 라벨 디밍(켜질 때는 원복)
        dim = ("gray70", "gray40")
        if not hasattr(self, "_sched_label_baseline"):
            # 방어적 초기화: 혹시 빌드 순서 이슈가 있어도 NPE 방지
            self._sched_label_baseline = {
                "lbl_wday_time": self.lbl_wday_time.cget("text_color"),
                "lbl_auto_off": self.lbl_auto_off.cget("text_color"),
                "lbl_delay": self.lbl_delay.cget("text_color"),
            }

        if enabled:
            # 원래 색으로 복원(기본값이면 None일 수 있음)
            try:
                self.lbl_wday_time.configure(text_color=self._sched_label_baseline.get("lbl_wday_time"))
            except Exception:
                pass
            try:
                self.lbl_auto_off.configure(text_color=self._sched_label_baseline.get("lbl_auto_off"))
            except Exception:
                pass
            try:
                self.lbl_delay.configure(text_color=self._sched_label_baseline.get("lbl_delay"))
            except Exception:
                pass
        else:
            # 명시적 디밍
            try:
                self.lbl_wday_time.configure(text_color=dim)
            except Exception:
                pass
            try:
                self.lbl_auto_off.configure(text_color=dim)
            except Exception:
                pass
            try:
                self.lbl_delay.configure(text_color=dim)
            except Exception:
                pass

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
            # 1) 성능(저사양) 탭 변경사항 먼저 반영+저장 (+ 런타임 즉시 적용까지 내부에서 수행)
            try:
                self._save_perf_if_dirty()
            except Exception:
                pass

            # [ADD] 1.5) 예약 종료 저장(변경 없으면 영향 없음)
            try:
                self._save_schedule_if_dirty()
            except Exception:
                pass

            # [ADD] 1.7) 스팸 모드 저장(변경 없으면 영향 없음)
            try:
                self._save_spam_if_dirty()
            except Exception:
                pass

            # 2) 이메일 탭 저장(변경 없으면 영향 없음)
            self._on_save_email()

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
        if getattr(self, "_save_in_progress", False):
            return
        cfg = self._collect_email_cfg()
        # 검증 실패 시 return 하므로 여기서 버튼 잠그기
        self._save_in_progress = True
        try:
            self.btn_save.configure(state="disabled", text="저장 중…")
            try:
                self.progress.grid()  # 인디케이터 보이기
                self.progress.start()
                self.lbl_status.configure(text="디스크에 저장하는 중…")
            except Exception:
                pass

            def _work():
                try:
                    self.settings.set("email", cfg)
                    try:
                        self.settings.flush_debounced(immediate=True)
                    except Exception:
                        self.settings.save()
                    # 라이브 반영 (autoemail/emailq)
                    try:
                        import autoemail
                        autoemail.configure(cfg)
                        if self.emailq is not None:
                            self.emailq.configure(cfg)
                    except Exception:
                        pass

                    # UI 스레드: 베이스라인 갱신 + 상태 리셋
                    def _ui_done():
                        try:
                            self._baseline = self._snapshot_email_ui()
                            self._mark_dirty(False)  # 저장 버튼 비활성화 유지
                            self.lbl_status.configure(text="저장됨 ✓")
                        except Exception:
                            pass

                    self.after(0, _ui_done)
                finally:
                    def _ui_cleanup():
                        try:
                            self.progress.stop()
                            self.progress.grid_remove()
                            self.btn_save.configure(text="저장")
                        except Exception:
                            pass
                        setattr(self, "_save_in_progress", False)

                    self.after(0, _ui_cleanup)

            import threading
            threading.Thread(target=_work, daemon=True).start()
        except Exception:
            # 예외 시 복구
            try:
                self.btn_save.configure(text="저장", state="normal")
            except Exception:
                pass
            self._save_in_progress = False

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

    def _show_perf_tab(self):
        self.page_perf.lift()

    def _show_schedule_tab(self):
        self.page_schedule.lift()

    def _show_spam_tab(self):
        try:
            self._lift_page(self.page_spam)
        except Exception:
            # _lift_page가 없다면 최소 동작 보장
            try:
                self.page_spam.lift()
            except Exception:
                pass

    def _show_update_tab(self):
        try:
            self._lift_page(self.page_update)
        except Exception:
            try:
                self.page_update.lift()
            except Exception:
                pass

    def _lift_page(self, target):
        # content 아래에 얹힌 모든 page_*를 한 번에 관리
        pages = []
        for name in ("page_email", "page_perf", "page_schedule", "page_spam", "page_update"):
            pg = getattr(self, name, None)
            if pg is not None:
                pages.append(pg)
        # 타겟 올리고 나머지 내림
        try:
            target.lift()
            for p in pages:
                if p is not target:
                    p.lower()
        except Exception:
            pass

    # ───────────────────────────────────────────────────────────────
    # 업데이트 탭
    # ───────────────────────────────────────────────────────────────

    def _build_update_page(self, parent):
        parent.grid_columnconfigure(0, weight=1)

        pad = {"padx": 24, "pady": 6}

        # 현재 버전
        ctk.CTkLabel(
            parent,
            text="업데이트",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(20, 4))

        ctk.CTkLabel(
            parent,
            text=f"현재 버전:  v{APP_VERSION}",
            text_color="gray",
        ).grid(row=1, column=0, sticky="w", **pad)

        # 상태 표시 라벨
        self._upd_status_lbl = ctk.CTkLabel(parent, text="", text_color="gray")
        self._upd_status_lbl.grid(row=2, column=0, sticky="w", **pad)

        # 최신 버전 라벨 (체크 후 채워짐)
        self._upd_latest_lbl = ctk.CTkLabel(parent, text="")
        self._upd_latest_lbl.grid(row=3, column=0, sticky="w", **pad)

        # 변경사항 박스
        self._upd_body_box = ctk.CTkTextbox(parent, height=120, state="disabled", wrap="word")
        self._upd_body_box.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 8))

        # 진행 바 (숨겨둠)
        self._upd_progress = ctk.CTkProgressBar(parent)
        self._upd_progress.set(0)
        self._upd_progress_lbl = ctk.CTkLabel(parent, text="", text_color="gray", font=ctk.CTkFont(size=11))

        # 버튼 행
        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(row=7, column=0, sticky="w", padx=24, pady=(4, 0))

        self._upd_check_btn = ctk.CTkButton(
            btn_frame, text="업데이트 확인",
            command=self._upd_check_now,
        )
        self._upd_check_btn.pack(side="left")

        self._upd_open_btn = ctk.CTkButton(
            btn_frame, text="릴리즈 페이지",
            fg_color="transparent", border_width=1,
            command=lambda: webbrowser.open(_updater.RELEASES_URL),
        )
        self._upd_open_btn.pack(side="left", padx=(8, 0))

        self._upd_dl_btn = ctk.CTkButton(
            btn_frame, text="지금 업데이트",
            fg_color="#2563eb",
            command=self._upd_start_download,
        )
        # 처음엔 숨김 — 새 버전이 있을 때만 표시

        self._upd_release_info: dict | None = None
        self._upd_downloading = False

        # ── 개발자 모드 전용 UI ─────────────────────────────────────
        if DEV_MODE:
            sep = ctk.CTkFrame(parent, height=1, fg_color="#333")
            sep.grid(row=8, column=0, sticky="ew", padx=24, pady=(16, 8))

            ctk.CTkLabel(
                parent,
                text="개발자 모드 — Pre-release 포함 버전 선택",
                text_color="#facc15",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=9, column=0, sticky="w", padx=24, pady=(0, 4))

            dev_btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
            dev_btn_frame.grid(row=10, column=0, sticky="w", padx=24, pady=(0, 4))

            self._dev_load_btn = ctk.CTkButton(
                dev_btn_frame, text="릴리즈 목록 불러오기",
                fg_color="#78350f", hover_color="#92400e",
                command=self._dev_load_releases,
            )
            self._dev_load_btn.pack(side="left")

            self._dev_version_var = ctk.StringVar(value="")
            self._dev_version_menu = ctk.CTkOptionMenu(
                dev_btn_frame,
                variable=self._dev_version_var,
                values=["목록을 먼저 불러오세요"],
                command=self._dev_on_version_select,
                width=200,
            )
            self._dev_version_menu.pack(side="left", padx=(8, 0))

            self._dev_status_lbl = ctk.CTkLabel(parent, text="", text_color="gray", font=ctk.CTkFont(size=11))
            self._dev_status_lbl.grid(row=11, column=0, sticky="w", padx=24)

            self._dev_body_box = ctk.CTkTextbox(parent, height=80, state="disabled", wrap="word")
            self._dev_body_box.grid(row=12, column=0, sticky="ew", padx=24, pady=(4, 4))

            self._dev_dl_btn = ctk.CTkButton(
                parent, text="이 버전으로 교체",
                fg_color="#7c3aed", hover_color="#6d28d9",
                state="disabled",
                command=self._dev_start_download,
            )
            self._dev_dl_btn.grid(row=13, column=0, sticky="w", padx=24, pady=(0, 8))

            self._dev_progress = ctk.CTkProgressBar(parent)
            self._dev_progress.set(0)
            self._dev_progress_lbl = ctk.CTkLabel(parent, text="", text_color="gray", font=ctk.CTkFont(size=11))

            self._dev_all_releases: list[dict] = []
            self._dev_selected_release: dict | None = None

    def _upd_check_now(self):
        """업데이트 확인 버튼 핸들러 — 백그라운드 스레드에서 API 호출."""
        self._upd_check_btn.configure(state="disabled", text="확인 중...")
        self._upd_status_lbl.configure(text="GitHub에서 버전 정보를 가져오는 중...", text_color="gray")
        self._upd_latest_lbl.configure(text="")
        self._upd_body_box.configure(state="normal")
        self._upd_body_box.delete("1.0", "end")
        self._upd_body_box.configure(state="disabled")
        try:
            self._upd_dl_btn.pack_forget()
        except Exception:
            pass

        def _run():
            info = _updater.check_latest_release()
            err = _updater.check_latest_release.last_error
            self.after(0, lambda: self._upd_on_result(info, err))

        threading.Thread(target=_run, daemon=True).start()

    def _upd_on_result(self, info: dict | None, err: str = ""):
        self._upd_check_btn.configure(state="normal", text="업데이트 확인")
        if info is None:
            if err:
                msg = f"버전 정보를 가져오지 못했습니다.\n{err}"
            else:
                msg = "버전 정보를 가져오지 못했습니다. (네트워크 확인)"
            self._upd_status_lbl.configure(text=msg, text_color="#f87171")
            return

        self._upd_release_info = info
        tag = info.get("tag_name", "")
        body = (info.get("body") or "").strip()
        exe_asset = info.get("exe_asset")

        if _updater.is_newer(tag):
            self._upd_status_lbl.configure(text="새 버전이 있습니다!", text_color="#4ade80")
            self._upd_latest_lbl.configure(text=f"최신 버전:  {tag}", text_color="#4ade80")
            if exe_asset:
                self._upd_dl_btn.pack(side="left", padx=(8, 0))
        else:
            self._upd_status_lbl.configure(text="최신 버전을 사용 중입니다.", text_color="#4ade80")
            self._upd_latest_lbl.configure(text=f"최신 버전:  {tag}", text_color="gray")

        if body:
            self._upd_body_box.configure(state="normal")
            self._upd_body_box.delete("1.0", "end")
            self._upd_body_box.insert("1.0", body)
            self._upd_body_box.configure(state="disabled")

    def _upd_start_download(self):
        if self._upd_downloading or self._upd_release_info is None:
            return
        exe_asset = self._upd_release_info.get("exe_asset")
        if not exe_asset:
            return

        from path_manager import BASE_DIR
        dest = BASE_DIR / exe_asset["name"]

        self._upd_downloading = True
        self._upd_dl_btn.configure(state="disabled", text="다운로드 중...")
        self._upd_check_btn.configure(state="disabled")

        # 진행 바 표시
        self._upd_progress.grid(row=5, column=0, sticky="ew", padx=24, pady=(0, 2))
        self._upd_progress_lbl.grid(row=6, column=0, sticky="w", padx=24)

        def _run():
            try:
                _updater.download_exe(
                    exe_asset["url"],
                    dest,
                    progress_cb=self._upd_on_progress,
                )
                self.after(0, self._upd_on_download_done)
            except Exception as e:
                self.after(0, lambda: self._upd_on_download_error(str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _upd_on_progress(self, done: int, total: int):
        if total > 0:
            ratio = done / total
            label = f"{done/1_048_576:.1f} / {total/1_048_576:.1f} MB"
        else:
            ratio = 0
            label = f"{done/1_048_576:.1f} MB"
        self.after(0, lambda r=ratio, l=label: self._upd_progress_ui(r, l))

    def _upd_progress_ui(self, ratio: float, label: str):
        try:
            self._upd_progress.set(ratio)
            self._upd_progress_lbl.configure(text=label)
        except Exception:
            pass

    def _upd_on_download_done(self):
        try:
            self._upd_progress.set(1.0)
            self._upd_progress_lbl.configure(text="다운로드 완료!")
            self._upd_dl_btn.configure(
                state="normal", text="재시작하여 업데이트 적용",
                fg_color="#16a34a",
                command=self._upd_apply,
            )
            self._upd_check_btn.configure(state="normal")
            self._upd_downloading = False
        except Exception:
            pass

    def _upd_apply(self):
        if self._upd_release_info is None:
            return
        exe_asset = self._upd_release_info.get("exe_asset")
        if not exe_asset:
            return

        # 즉시 비활성화 — 중복 클릭 방지
        self._upd_dl_btn.configure(state="disabled", text="적용 중...")

        from path_manager import BASE_DIR
        dest = BASE_DIR / exe_asset["name"]

        if not dest.exists():
            messagebox.showerror("오류", f"다운로드된 파일을 찾을 수 없습니다.\n{dest}", parent=self)
            self._upd_dl_btn.configure(state="normal", text="재시작하여 업데이트 적용", fg_color="#16a34a")
            return

        # 설정창을 먼저 닫은 뒤 부모 위젯을 통해 apply_update 실행
        # (sys.exit가 tkinter 콜백 내에서 catch되는 문제 방지)
        master = self.master
        self.destroy()
        master.after(200, lambda: _updater.apply_update(dest))

    def _upd_on_download_error(self, msg: str):
        try:
            self._upd_downloading = False
            self._upd_dl_btn.configure(state="normal", text="다시 시도", fg_color="#2563eb")
            self._upd_check_btn.configure(state="normal")
            self._upd_progress_lbl.configure(text=f"오류: {msg}", text_color="#f87171")
        except Exception:
            pass

    # ───────────────────────────────────────────────────────────────
    # 개발자 모드 — 전체 릴리즈 목록 / 버전 선택 교체
    # ───────────────────────────────────────────────────────────────

    def _dev_load_releases(self):
        if not DEV_MODE:
            return
        self._dev_load_btn.configure(state="disabled", text="불러오는 중...")
        self._dev_status_lbl.configure(text="GitHub에서 전체 릴리즈 목록을 가져오는 중...", text_color="gray")

        def _run():
            releases = _updater.check_all_releases()
            err = _updater.check_all_releases.last_error
            self.after(0, lambda: self._dev_on_releases_loaded(releases, err))

        threading.Thread(target=_run, daemon=True).start()

    def _dev_on_releases_loaded(self, releases: list[dict] | None, err: str):
        self._dev_load_btn.configure(state="normal", text="릴리즈 목록 불러오기")
        if releases is None:
            self._dev_status_lbl.configure(
                text=f"목록 로드 실패: {err}" if err else "목록 로드 실패 (네트워크 확인)",
                text_color="#f87171",
            )
            return

        self._dev_all_releases = releases
        if not releases:
            self._dev_status_lbl.configure(text="릴리즈가 없습니다.", text_color="gray")
            return

        labels = []
        for r in releases:
            tag = r.get("tag_name", "(unknown)")
            pre = "  [pre]" if r.get("prerelease") else ""
            exe = "  ✓exe" if r.get("exe_asset") else "  ✗exe없음"
            labels.append(f"{tag}{pre}{exe}")

        self._dev_version_menu.configure(values=labels)
        self._dev_version_var.set(labels[0])
        self._dev_on_version_select(labels[0])
        self._dev_status_lbl.configure(text=f"총 {len(releases)}개 릴리즈", text_color="gray")

    def _dev_on_version_select(self, label: str):
        if not self._dev_all_releases:
            return
        labels = self._dev_version_menu.cget("values")
        try:
            idx = list(labels).index(label)
        except ValueError:
            return
        rel = self._dev_all_releases[idx]
        self._dev_selected_release = rel

        body = (rel.get("body") or "").strip()
        self._dev_body_box.configure(state="normal")
        self._dev_body_box.delete("1.0", "end")
        self._dev_body_box.insert("1.0", body or "(변경사항 없음)")
        self._dev_body_box.configure(state="disabled")

        has_exe = rel.get("exe_asset") is not None
        self._dev_dl_btn.configure(state="normal" if has_exe else "disabled")

    def _dev_start_download(self):
        if not DEV_MODE or self._upd_downloading:
            return
        rel = self._dev_selected_release
        if rel is None:
            return
        exe_asset = rel.get("exe_asset")
        if not exe_asset:
            return

        from path_manager import BASE_DIR
        dest = BASE_DIR / exe_asset["name"]

        self._upd_downloading = True
        self._dev_dl_btn.configure(state="disabled", text="다운로드 중...")
        self._dev_load_btn.configure(state="disabled")

        self._dev_progress.grid(row=14, column=0, sticky="ew", padx=24, pady=(0, 2))
        self._dev_progress_lbl.grid(row=15, column=0, sticky="w", padx=24)

        def _progress(done, total):
            if total > 0:
                ratio = done / total
                label = f"{done/1_048_576:.1f} / {total/1_048_576:.1f} MB"
            else:
                ratio = 0
                label = f"{done/1_048_576:.1f} MB"
            self.after(0, lambda r=ratio, l=label: self._dev_progress_ui(r, l))

        def _run():
            try:
                _updater.download_exe(exe_asset["url"], dest, progress_cb=_progress)
                self.after(0, lambda: self._dev_on_download_done(dest, rel))
            except Exception as e:
                self.after(0, lambda: self._dev_on_download_error(str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _dev_progress_ui(self, ratio: float, label: str):
        try:
            self._dev_progress.set(ratio)
            self._dev_progress_lbl.configure(text=label)
        except Exception:
            pass

    def _dev_on_download_done(self, dest, rel: dict):
        try:
            self._dev_progress.set(1.0)
            self._dev_progress_lbl.configure(text="다운로드 완료!")
            tag = rel.get("tag_name", "")
            self._dev_dl_btn.configure(
                state="normal",
                text=f"재시작하여 {tag} 적용",
                fg_color="#16a34a",
                command=lambda: self._dev_apply(dest),
            )
            self._dev_load_btn.configure(state="normal")
            self._upd_downloading = False
        except Exception:
            pass

    def _dev_apply(self, dest):
        # 즉시 비활성화 — 중복 클릭 방지
        try:
            self._dev_dl_btn.configure(state="disabled", text="적용 중...")
        except Exception:
            pass

        if not dest.exists():
            messagebox.showerror("오류", f"다운로드된 파일을 찾을 수 없습니다.\n{dest}", parent=self)
            try:
                self._dev_dl_btn.configure(state="normal", text="재시작하여 적용", fg_color="#16a34a")
            except Exception:
                pass
            return

        # 설정창 먼저 닫고 부모 위젯을 통해 apply_update 실행
        master = self.master
        self.destroy()
        master.after(200, lambda: _updater.apply_update(dest))

    def _dev_on_download_error(self, msg: str):
        try:
            self._upd_downloading = False
            self._dev_dl_btn.configure(state="normal", text="이 버전으로 교체", fg_color="#7c3aed")
            self._dev_load_btn.configure(state="normal")
            self._dev_progress_lbl.configure(text=f"오류: {msg}", text_color="#f87171")
        except Exception:
            pass
