# autoemail.py
import smtplib
from email.mime.text import MIMEText
import os

# 내부 설정 캐시 (GUI에서 configure()로 덮어씌우면 txt 대신 이 값 사용)
_cfg = {
    "enabled": None,  # None이면 legacy(txt) 모드, True/False면 GUI 모드
    "provider": "gmail",
    "smtp_host": "",
    "smtp_port": 587,
    "use_tls": True,
    "sender": "",
    "app_password": "",
    "recipients": "",
    "subject_tmpl": "[Manager] {event}",
    "body_tmpl": "{event}",
}


def ensure_file_exists(filepath, default_content=""):
    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(default_content)


def init_txts():
    os.makedirs("./resources", exist_ok=True)
    ensure_file_exists("./resources/email.txt", "example@gmail.com")
    ensure_file_exists("./resources/id.txt", "example@gmail.com")
    ensure_file_exists("./resources/pw.txt", "password")
    ensure_file_exists(
        "./resources/mail_content.txt", "Subject: 메일 제목\n---\n메일 본문 내용"
    )


def load_email_data(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        subject_part, body_part = content.split("---", 1)
        subject = subject_part.replace("Subject:", "").strip()
        body = body_part.strip()
        return subject, body
    except ValueError:
        return "메일 제목", "메일 내용"


# GUI에서 호출할 수 있는 진입점
def configure(settings: dict):
    """GUI 설정을 dict로 주입"""
    global _cfg
    _cfg.update(settings or {})
    if _cfg.get("enabled") is None:
        _cfg["enabled"] = False  # GUI에서 명시적으로 True 줄 때만 동작


def send_email(event="default", payload=None):
    """
    - GUI 모드(enabled != None) → _cfg 기반으로 발송
    - Legacy 모드(enabled == None) → txt 기반으로 발송
    """
    if _cfg["enabled"] is None:
        return _send_email_legacy()
    else:
        if not _cfg.get("enabled", False):
            print("[info] 자동 이메일 비활성화 상태입니다.")
            return False
        return _send_email_cfg(_cfg, event=event, payload=(payload or {}))


# --- Legacy 설정 읽기/이관 유틸 ---
def _infer_provider(sender: str) -> tuple[str, str, int, bool]:
    """
    sender 도메인으로 provider/smtp_host/smtp_port/use_tls 추정
    - gmail.com → ("gmail", "", 587, True)  # host 비우면 내부 기본 smtp.gmail.com 사용
    - naver.com → ("custom", "smtp.naver.com", 587, True)
    - 그 외     → ("custom", "", 587, True)
    """
    try:
        domain = (sender or "").split("@", 1)[1].lower()
    except Exception:
        domain = ""
    if domain == "gmail.com":
        return "gmail", "", 587, True
    if domain == "naver.com":
        return "custom", "smtp.naver.com", 587, True
    return "custom", "", 587, True


def read_legacy_config(base_dir) -> dict | None:
    """
    BASE_DIR/resources 하위 txt 4종을 읽어 settings.email 스키마로 반환.
    - 필수 파일 중 하나라도 없으면 None
    - placeholders(예: example@gmail.com, password) 그대로면 enabled=False
    """
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
    subj, body = load_email_data(str(p_mc))  # 기존 파서 재사용

    provider, host, port, tls = _infer_provider(sender)

    # 사용자가 실제로 설정했는지(기본값 탈출) 판별
    # - 수신/발신이 example@gmail.com이 아니고
    # - 비밀번호가 "password"가 아니면 → 사용자가 설정했다고 간주
    modified = (
        to_email and sender and app_pwd
        and to_email.lower() != "example@gmail.com"
        and sender.lower() != "example@gmail.com"
        and app_pwd != "password"
        and ("@" in to_email) and ("@" in sender)
    )

    cfg = {
        "enabled": bool(modified),  # 수정돼 있으면 즉시 ON
        "provider": provider,
        "smtp_host": host,
        "smtp_port": int(port),
        "use_tls": bool(tls),
        "sender": sender,
        "app_password": app_pwd,
        "recipients": to_email,      # 줄바꿈/쉼표 혼용은 EmailQueue에서 처리
        "subject_tmpl": subj or "",
        "body_tmpl": body or "",
        # 레이트 리밋은 settings.DEFAULTS 사용(빈 값이면 SM 쪽에서 병합)
    }
    return cfg


# ===== Legacy 구현 (지금 쓰고 있는 방식 그대로) =====
def _send_email_legacy():
    email_file = open("./resources/email.txt", "r")
    id_file = open("./resources/id.txt", "r")
    pw_file = open("./resources/pw.txt", "r")

    to_email = email_file.readline().strip()
    from_email = id_file.readline().strip()
    password = pw_file.readline().strip()

    email_file.close()
    id_file.close()
    pw_file.close()

    if to_email == "example@gmail.com" or from_email == "example@gmail.com":
        print("이메일 설정을 하지 않아 메일을 발송하지 않습니다.")
        return False

    if "@" not in to_email:
        print("수신 이메일 주소 오류: email.txt 확인 필요")
        return False

    check_email = from_email.find("@")
    check_domain = from_email[check_email + 1 :]

    if check_email == -1:
        print("발신 이메일 주소 오류: id.txt 확인 필요")
        return False
    elif check_domain not in ("gmail.com", "naver.com"):
        print("지원되지 않는 메일 도메인:", check_domain)
        return False
    else:
        subject, body = load_email_data("./resources/mail_content.txt")
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email

        smtp_server = "smtp.gmail.com"
        port = 587
        if check_domain == "naver.com":
            smtp_server = "smtp.naver.com"

        conn = smtplib.SMTP(smtp_server, port)
        conn.starttls()
        try:
            conn.login(user=from_email, password=password)
        except Exception:
            print("발신 메일 계정 로그인 실패. id.txt / pw.txt 확인 필요.")
            return False

        conn.sendmail(from_email, to_email, msg.as_string())
        conn.close()
        return True


# ===== GUI 모드 구현 =====
def _send_email_cfg(c, *, event: str = "default", payload: dict | None = None):
    raw_recipients = c.get("recipients", "")
    if isinstance(raw_recipients, (list, tuple, set)):
        joined = ",".join(str(x) for x in raw_recipients)
    else:
        joined = str(raw_recipients)
    to_list = [addr.strip() for addr in joined.replace("\n", ",").split(",") if addr.strip()]
    sender = c.get("sender", "")
    pwd = c.get("app_password", "")
    if not sender or not pwd or not to_list:
        print("[warn] 이메일 설정이 불완전 → 발송 안 함")
        return False

    payload = dict(payload or {})
    # 안전 치환용 공통 변수
    fmt_vars = {"event": event, **payload}

    # 1) 이벤트별 템플릿 우선
    tmap = c.get("templates") or {}
    tev = tmap.get(event) or {}

    subj_src = (tev.get("subject") or c.get("subject_tmpl") or "").strip()
    body_src = (tev.get("body") or c.get("body_tmpl") or "").strip()

    # 2) 포맷 치환 ({event}, payload 키들) — 키 없으면 원문 유지
    def _fmt(s: str) -> str:
        if not s:
            return ""
        try:
            return s.format(**fmt_vars)
        except Exception:
            return s  # 치환 실패 시 원문 사용

    subject = _fmt(subj_src) or "메일 제목을 입력해주세요"
    body = _fmt(body_src) or "본문 내용을 입력해주세요"

    msg = MIMEText(body, _charset="utf-8")

    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)

    host = c.get("smtp_host") or ("smtp.gmail.com" if c.get("provider") == "gmail" else "")
    port = int(c.get("smtp_port", 587))
    use_tls = bool(c.get("use_tls", True))

    # 추가: 커스텀인데 호스트 없음 → 실패 처리
    if c.get("provider") != "gmail" and not host:
        print("[warn] 커스텀 SMTP 사용이지만 smtp_host가 비어 있습니다. 발송을 중단합니다.")
        return False

    try:
        if use_tls:
            with smtplib.SMTP(host=host, port=port, timeout=15) as s:
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(sender, pwd)
                s.sendmail(sender, to_list, msg.as_string())
        else:
            with smtplib.SMTP(host=host, port=port, timeout=15) as s:
                s.login(sender, pwd)
                s.sendmail(sender, to_list, msg.as_string())
        return True
    except Exception as e:
        print(f"[warn] SMTP 전송 실패(GUI 모드): {e}")
        return False

