# autoemail.py (REPLACE WHOLE FILE)

import smtplib, threading, time
from email.mime.text import MIMEText
from typing import Dict, Any, Tuple, Optional, List

# ---------- 내부 상태 ----------
_cfg_lock = threading.Lock()
_cfg: Dict[str, Any] = {
    # enabled: None → Legacy(txt) 모드, True/False → GUI 모드
    "enabled": None,
    "provider": "gmail",
    "smtp_host": "",
    "smtp_port": 587,
    "use_tls": True,
    "sender": "",
    "app_password": "",
    "recipients": "",
    "subject_tmpl": "[Manager] {event}",
    "body_tmpl": "{event}",
    "events": {},
    "templates": {},
    "rate_limit_max": 3,
    "rate_limit_window_sec": 300,
}
_cfg_version: int = 0
_cfg_taken_at: float = 0.0  # epoch seconds of last configure()


# ---------- 유틸 ----------
def _split_recipients(s: str) -> List[str]:
    out: List[str] = []
    for part in (s or "").replace("\n", ",").split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def configure(settings: Dict[str, Any]) -> None:
    """
    GUI가 호출: 지금 보이는 UI 값 → 런타임 구성으로 '즉시' 반영.
    """
    global _cfg_version, _cfg_taken_at
    with _cfg_lock:
        _cfg.update(settings or {})
        # GUI에서 명시적으로 True 줄 때만 동작
        if _cfg.get("enabled") is None:
            _cfg["enabled"] = False
        _cfg_version += 1
        _cfg_taken_at = time.time()


def get_snapshot() -> Dict[str, Any]:
    """
    발송·판정 직전에 '단 한 번' 호출해서 사용하는 불변 사본.
    버전/시각 메타도 포함.
    """
    with _cfg_lock:
        snap = dict(_cfg)
        snap["version"] = int(_cfg_version)
        snap["taken_at"] = float(_cfg_taken_at)
        return snap


# ---------- LEGACY(txt) 경로 ----------
def ensure_file_exists(filepath: str, default_content: str = "") -> None:
    import os
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(default_content)


def init_txts() -> None:
    import os
    os.makedirs("./resources", exist_ok=True)
    ensure_file_exists("./resources/email.txt", "example@gmail.com")
    ensure_file_exists("./resources/id.txt", "example@gmail.com")
    ensure_file_exists("./resources/pw.txt", "password")
    ensure_file_exists("./resources/mail_content.txt", "Subject: 메일 제목\n---\n메일 본문 내용")


def load_email_data(file_path: str) -> Tuple[str, str]:
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        subject_part, body_part = content.split("---", 1)
        subject = subject_part.replace("Subject:", "").strip()
        body = body_part.strip()
        return subject, body
    except ValueError:
        return "메일 제목", "메일 내용"


def _infer_provider(sender: str) -> Tuple[str, str, int, bool]:
    try:
        domain = (sender or "").split("@", 1)[1].lower()
    except Exception:
        domain = ""
    if domain == "gmail.com":
        return "gmail", "", 587, True
    if domain == "naver.com":
        return "custom", "smtp.naver.com", 587, True
    return "custom", "", 587, True


def read_legacy_config(base_dir: str) -> Optional[Dict[str, Any]]:
    from pathlib import Path
    res_dir = Path(base_dir) / "resources"
    p_email = res_dir / "email.txt"
    p_id = res_dir / "id.txt"
    p_pw = res_dir / "pw.txt"
    p_mc = res_dir / "mail_content.txt"
    if not (p_email.exists() and p_id.exists() and p_pw.exists() and p_mc.exists()):
        return None
    to_email = p_email.read_text(encoding="utf-8", errors="ignore").strip()
    sender = p_id.read_text(encoding="utf-8", errors="ignore").strip()
    app_pwd = p_pw.read_text(encoding="utf-8", errors="ignore").strip()
    subj, body = load_email_data(str(p_mc))
    provider, host, port, tls = _infer_provider(sender)
    modified = (
        to_email and sender and app_pwd
        and to_email.lower() != "example@gmail.com"
        and sender.lower() != "example@gmail.com"
        and app_pwd != "password"
        and ("@" in to_email) and ("@" in sender)
    )
    return {
        "enabled": bool(modified),
        "provider": provider,
        "smtp_host": host,
        "smtp_port": int(port),
        "use_tls": bool(tls),
        "sender": sender,
        "app_password": app_pwd,
        "recipients": to_email,
        "subject_tmpl": subj or "",
        "body_tmpl": body or "",
    }


# ---------- 발송 ----------
def send_email(event: str = "default", payload: Optional[Dict[str, Any]] = None) -> bool:
    """
    - GUI 모드(enabled != None): 스냅샷 기반 발송
    - Legacy 모드(enabled == None): txt 기반 발송
    """
    payload = payload or {}
    snap = get_snapshot()
    if snap.get("enabled") is None:
        # legacy
        return _send_email_legacy()
    if not snap.get("enabled", False):
        print("[info] 자동 이메일 비활성화 상태입니다.")
        return False
    subj, body = _render_from_snap(snap, event, payload)
    ok = _send_mail(subject=subj, body=body, c=snap)
    # 로깅 시 version/taken_at을 남길 수 있도록 호출부에서 snap["version"] 사용
    return ok


def _render_from_snap(c: Dict[str, Any], event: str, p: Dict[str, Any]) -> Tuple[str, str]:
    # 이벤트별 템플릿이 있으면 우선, 없으면 전역 템플릿 사용
    ev_map = (c.get("templates") or {})
    ev = (ev_map.get(event) or {})
    st = (ev.get("subject") or c.get("subject_tmpl") or "[Manager] {event}")
    bt = (ev.get("body") or c.get("body_tmpl") or "{event}")
    return st.format(event=event, **p), bt.format(event=event, **p)


def _send_mail(subject: str, body: str, c: Dict[str, Any]) -> bool:
    sender = c.get("sender", "")
    pwd = c.get("app_password", "")
    rcpts = _split_recipients(c.get("recipients", ""))
    if not sender or not pwd or not rcpts:
        return False
    host = c.get("smtp_host") or ("smtp.gmail.com" if c.get("provider") == "gmail" else "")
    port = int(c.get("smtp_port", 587))
    use_tls = bool(c.get("use_tls", True))

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(rcpts)

    if use_tls:
        with smtplib.SMTP(host=host, port=port, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo(); s.login(sender, pwd); s.sendmail(sender, rcpts, msg.as_string())
    else:
        with smtplib.SMTP(host=host, port=port, timeout=15) as s:
            s.login(sender, pwd); s.sendmail(sender, rcpts, msg.as_string())
    return True


# ---------- Legacy 구현 ----------
def _send_email_legacy() -> bool:
    # (기존 파일 기반 로직 유지)
    email_file = open("./resources/email.txt", "r", encoding="utf-8", errors="ignore")
    id_file = open("./resources/id.txt", "r", encoding="utf-8", errors="ignore")
    pw_file = open("./resources/pw.txt", "r", encoding="utf-8", errors="ignore")
    to_email = email_file.readline().strip()
    from_email = id_file.readline().strip()
    password = pw_file.readline().strip()
    email_file.close(); id_file.close(); pw_file.close()

    if to_email == "example@gmail.com" or from_email == "example@gmail.com":
        print("이메일 설정을 하지 않아 메일을 발송하지 않습니다.")
        return False
    if "@" not in to_email or "@" not in from_email:
        print("이메일 주소 오류: email.txt / id.txt 확인 필요")
        return False

    domain = from_email.split("@", 1)[1].lower()
    if domain not in ("gmail.com", "naver.com"):
        print("지원되지 않는 메일 도메인:", domain)
        return False

    subject, body = load_email_data("./resources/mail_content.txt")
    host = "smtp.gmail.com" if domain == "gmail.com" else "smtp.naver.com"
    port = 587; use_tls = True

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject; msg["From"] = from_email; msg["To"] = to_email

    with smtplib.SMTP(host=host, port=port, timeout=15) as s:
        s.ehlo()
        if use_tls:
            s.starttls(); s.ehlo()
        s.login(from_email, password)
        s.sendmail(from_email, [to_email], msg.as_string())
    return True
