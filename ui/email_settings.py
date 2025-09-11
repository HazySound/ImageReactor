# ui/email_settings.py
from __future__ import annotations
import customtkinter as ctk
from tkinter import messagebox
import autoemail


class EmailSettingsDialog(ctk.CTkToplevel):
    """
    이메일 설정 입력/저장/테스트 다이얼로그(CTk).
    - 저장: settings.json의 email 섹션 저장 + autoemail/emailq에 실시간 반영
    - 테스트: 현재 입력값으로 즉시 1통 발송 (event="test")
    """
    def __init__(self, parent, settings_mgr, email_queue):
        super().__init__(parent)
        self.title("설정 - 이메일")
        self.geometry("520x560")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)

        self.settings = settings_mgr
        self.emailq = email_queue

        # 현재 값 로드
        sget = self.settings.get
        enabled = bool(sget("email.enabled", False))
        provider = sget("email.provider", "gmail")
        smtp_host = sget("email.smtp_host", "")
        smtp_port = int(sget("email.smtp_port", 587))
        use_tls = bool(sget("email.use_tls", True))
        sender = sget("email.sender", "")
        app_password = sget("email.app_password", "")
        recipients = sget("email.recipients", "")

        subject_tmpl = sget("email.subject_tmpl", "")
        body_tmpl = sget("email.body_tmpl", "")
        # ※ 과거 빌드에서 하드코딩 기본값이 저장돼 버린 경우 정리(1회 보정)
        if subject_tmpl in ("[Manager] {event}", "[ImageReactor] ${event}"):
            subject_tmpl = ""
        if body_tmpl in (
                "Event: {event}\nTime: {timestamp}\nHost: {hostname}",
                "Event: ${event}\nAt: ${timestamp}\nHost: ${hostname}",
        ):
            body_tmpl = ""

        rate_min = int(sget("email.rate_limit_min_interval", 600))
        rate_burst = int(sget("email.rate_limit_burst", 1))
        rate_win = int(sget("email.rate_limit_window_sec", 300))  # EmailQueue 용

        pad = {"padx": 10, "pady": 6}

        row = 0
        self.chk_enabled = ctk.CTkCheckBox(self, text="이메일 알림 사용")
        self.chk_enabled.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        if enabled: self.chk_enabled.select()

        row += 1
        ctk.CTkLabel(self, text="프로바이더").grid(row=row, column=0, sticky="e", **pad)
        self.cbo_provider = ctk.CTkOptionMenu(self, values=["gmail", "custom"])
        self.cbo_provider.grid(row=row, column=1, sticky="w", **pad)
        self.cbo_provider.set(provider if provider in ("gmail","custom") else "gmail")

        row += 1
        ctk.CTkLabel(self, text="SMTP 호스트").grid(row=row, column=0, sticky="e", **pad)
        self.ent_host = ctk.CTkEntry(self, width=280)
        self.ent_host.grid(row=row, column=1, sticky="w", **pad)
        self.ent_host.insert(0, smtp_host)

        row += 1
        ctk.CTkLabel(self, text="SMTP 포트").grid(row=row, column=0, sticky="e", **pad)
        self.ent_port = ctk.CTkEntry(self, width=120)
        self.ent_port.grid(row=row, column=1, sticky="w", **pad)
        self.ent_port.insert(0, str(smtp_port))

        row += 1
        self.chk_tls = ctk.CTkCheckBox(self, text="TLS 사용")
        self.chk_tls.grid(row=row, column=1, sticky="w", **pad)
        if use_tls: self.chk_tls.select()

        row += 1
        ctk.CTkLabel(self, text="발신 이메일").grid(row=row, column=0, sticky="e", **pad)
        self.ent_sender = ctk.CTkEntry(self, width=280)
        self.ent_sender.grid(row=row, column=1, sticky="w", **pad)
        self.ent_sender.insert(0, sender)

        row += 1
        ctk.CTkLabel(self, text="앱 비밀번호").grid(row=row, column=0, sticky="e", **pad)
        self.ent_pwd = ctk.CTkEntry(self, width=280, show="•")
        self.ent_pwd.grid(row=row, column=1, sticky="w", **pad)
        self.ent_pwd.insert(0, app_password)

        row += 1
        ctk.CTkLabel(self, text="수신자(쉼표/개행 구분)").grid(row=row, column=0, sticky="e", **pad)
        self.ent_rcpt = ctk.CTkEntry(self, width=280)
        self.ent_rcpt.grid(row=row, column=1, sticky="w", **pad)
        self.ent_rcpt.insert(0, recipients)

        row += 1
        ctk.CTkLabel(self, text="제목 템플릿").grid(row=row, column=0, sticky="e", **pad)
        self.ent_subj = ctk.CTkEntry(self, width=280)
        self.ent_subj.grid(row=row, column=1, sticky="w", **pad)
        self.ent_subj.insert(0, subject_tmpl)

        row += 1
        ctk.CTkLabel(self, text="본문 템플릿").grid(row=row, column=0, sticky="ne", **pad)
        self.txt_body = ctk.CTkTextbox(self, width=280, height=120)
        self.txt_body.grid(row=row, column=1, sticky="w", **pad)
        self.txt_body.insert("1.0", body_tmpl)

        row += 1
        ctk.CTkLabel(self, text="최소 간격(초)").grid(row=row, column=0, sticky="e", **pad)
        self.ent_rate_min = ctk.CTkEntry(self, width=120)
        self.ent_rate_min.grid(row=row, column=1, sticky="w", **pad)
        self.ent_rate_min.insert(0, str(rate_min))

        row += 1
        ctk.CTkLabel(self, text="버스트 허용").grid(row=row, column=0, sticky="e", **pad)
        self.ent_rate_burst = ctk.CTkEntry(self, width=120)
        self.ent_rate_burst.grid(row=row, column=1, sticky="w", **pad)
        self.ent_rate_burst.insert(0, str(rate_burst))

        row += 1
        ctk.CTkLabel(self, text="윈도(초, 큐용)").grid(row=row, column=0, sticky="e", **pad)
        self.ent_rate_win = ctk.CTkEntry(self, width=120)
        self.ent_rate_win.grid(row=row, column=1, sticky="w", **pad)
        self.ent_rate_win.insert(0, str(rate_win))

        # 버튼 바
        row += 1
        bar = ctk.CTkFrame(self)
        bar.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=(12,10))
        bar.grid_columnconfigure(0, weight=1)
        btn_test = ctk.CTkButton(bar, text="테스트 메일", command=self._on_test)
        btn_test.grid(row=0, column=0, sticky="w")
        btn_cancel = ctk.CTkButton(bar, text="취소", command=self.destroy)
        btn_cancel.grid(row=0, column=2, sticky="e", padx=(6,0))
        btn_save = ctk.CTkButton(bar, text="저장", command=self._on_save)
        btn_save.grid(row=0, column=1, sticky="e", padx=(6,0))

    def _collect(self) -> dict | None:
        # 숫자 변환/필수값 검증
        try:
            port = int((self.ent_port.get() or "587").strip())
            rate_min = int((self.ent_rate_min.get() or "600").strip())
            rate_burst = int((self.ent_rate_burst.get() or "1").strip())
            rate_win = int((self.ent_rate_win.get() or "300").strip())
        except ValueError:
            messagebox.showerror("오류", "포트/레이트리밋 값은 정수여야 합니다.", parent=self)
            return None

        cfg = {
            "enabled": bool(self.chk_enabled.get()),
            "provider": self.cbo_provider.get().strip() or "gmail",
            "smtp_host": self.ent_host.get().strip(),
            "smtp_port": port,
            "use_tls": bool(self.chk_tls.get()),
            "sender": self.ent_sender.get().strip(),
            "app_password": self.ent_pwd.get(),
            "recipients": self.ent_rcpt.get().strip(),  # autoemail/queue가 쉼표/개행 split
            "subject_tmpl": self.ent_subj.get(),
            "body_tmpl": self.txt_body.get("1.0", "end").rstrip("\n"),
            "rate_limit_min_interval": rate_min,
            "rate_limit_burst": rate_burst,
            "rate_limit_window_sec": rate_win,  # EmailQueue용
        }

        if cfg["enabled"]:
            missing = []
            if not cfg["sender"]: missing.append("발신 이메일")
            if not cfg["app_password"]: missing.append("앱 비밀번호")
            if not cfg["recipients"]: missing.append("수신자")
            # custom일 때 호스트/포트 필요
            if cfg["provider"] == "custom" and not cfg["smtp_host"]:
                missing.append("SMTP 호스트")
            if missing:
                messagebox.showwarning("부족한 입력", ", ".join(missing) + " 입력 필요", parent=self)
                return None
        return cfg

    def _apply_live(self, cfg: dict) -> None:
        # autoemail(프리즈 등 개별발송) + emailq(큐 발송) 동시 반영
        try:
            autoemail.configure(cfg)
        except Exception:
            pass
        try:
            if self.emailq is not None:
                self.emailq.configure(cfg)
        except Exception:
            pass

    def _on_save(self):
        cfg = self._collect()
        if cfg is None: return
        # 저장
        self.settings.set("email", cfg)
        self.settings.save()
        self._apply_live(cfg)
        messagebox.showinfo("완료", "이메일 설정이 저장되었고 즉시 반영되었습니다.", parent=self)
        self.destroy()

    def _on_test(self):
        cfg = self._collect()
        if cfg is None: return
        # 설정 임시 반영 후 테스트
        self._apply_live(cfg)
        try:
            ok = autoemail.send_email(event="test", payload=None)
            if ok:
                messagebox.showinfo("성공", "테스트 메일을 보냈습니다.", parent=self)
            else:
                messagebox.showwarning("실패", "테스트 메일 전송 실패.", parent=self)
        except Exception as e:
            messagebox.showerror("에러", f"테스트 중 오류: {e}", parent=self)
