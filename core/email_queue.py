# email_queue.py (REPLACE WHOLE FILE)

from __future__ import annotations
import smtplib, threading, time
from email.mime.text import MIMEText
from queue import Queue, Empty
from typing import Dict, Any, Optional, Tuple, List

def _split_recipients(s: str) -> List[str]:
    out: List[str] = []
    for part in (s or "").replace("\n", ",").split(","):
        p = part.strip()
        if p: out.append(p)
    return out

class EmailQueue:
    """SMTP 발송 큐(핫리로드 안전)."""
    def __init__(self):
        self._cfg: Dict[str, Any] = {}
        self._cfg_lock = threading.Lock()
        self._cfg_version: int = 0
        self._cfg_taken_at: float = 0.0

        self._q: "Queue[Tuple[str, Dict[str, Any]]]" = Queue()
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._last_sent: List[float] = []
        self._rl_lock = threading.Lock()  # rate-limit 보호

    # ---- 구성 ----
    def configure(self, cfg: Dict[str, Any]) -> None:
        with self._cfg_lock:
            self._cfg = dict(cfg or {})
            self._cfg_version += 1
            self._cfg_taken_at = time.time()

    def current_config(self) -> Dict[str, Any]:
        with self._cfg_lock:
            snap = dict(self._cfg)
            snap["version"] = int(self._cfg_version)
            snap["taken_at"] = float(self._cfg_taken_at)
            return snap

    # ---- 제어 ----
    def start(self) -> None:
        if self._th and self._th.is_alive(): return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True); self._th.start()

    def stop(self) -> None:
        self._stop.set()
        if self._th: self._th.join(timeout=2.0)

    # ---- 큐 API ----
    def enqueue(self, event_type: str, payload: Dict[str, Any]) -> None:
        cfg = self.current_config()
        if not cfg.get("enabled", False): return
        # 이벤트별 on/off
        ev_flag = ((cfg.get("events") or {}).get(event_type) or {}).get("enabled", True)
        if not ev_flag: return
        self._q.put((event_type, payload or {}))

    def test_send(self) -> bool:
        cfg = self.current_config()
        return self._send_mail("[Test] Manager", "This is a test email.", cfg)

    # ---- 내부 루프 ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                event, payload = self._q.get(timeout=0.2)
            except Empty:
                continue

            cfg = self.current_config()
            if not self._rate_ok(cfg):
                continue
            subj, body = self._render(cfg, event, payload)
            if self._send_mail(subj, body, cfg):
                with self._rl_lock:
                    now = time.time()
                    win = int(cfg.get("rate_limit_window_sec", 300))
                    self._last_sent.append(now)
                    self._last_sent = [t for t in self._last_sent if now - t <= win]

    # ---- 렌더/레이트/발송 ----
    def _rate_ok(self, cfg: Dict[str, Any]) -> bool:
        win = int(cfg.get("rate_limit_window_sec", 300))
        mx = int(cfg.get("rate_limit_max", 3))
        now = time.time()
        with self._rl_lock:
            self._last_sent = [t for t in self._last_sent if now - t <= win]
            return len(self._last_sent) < mx

    def _render(self, cfg: Dict[str, Any], event: str, p: Dict[str, Any]) -> Tuple[str, str]:
        ev_map = (cfg.get("templates") or {})
        ev = (ev_map.get(event) or {})
        st = (ev.get("subject") or cfg.get("subject_tmpl") or "[Manager] {event}")
        bt = (ev.get("body") or cfg.get("body_tmpl") or "{event}")
        subj = st.format(event=event, **p)
        body = bt.format(event=event, **p)
        # 디버깅 가시성: 버전/시각을 헤더 라인에 추가(선택)
        ver = cfg.get("version"); taken = cfg.get("taken_at")
        if ver is not None and taken:
            body = f"[cfg_v={ver} at={int(taken)}]\n{body}"
        return subj, body

    def _send_mail(self, subject: str, body: str, c: Dict[str, Any]) -> bool:
        if not c.get("enabled"): return False
        sender=c.get("sender",""); pwd=c.get("app_password",""); rcpts=_split_recipients(c.get("recipients",""))
        if not sender or not pwd or not rcpts: return False
        host=c.get("smtp_host") or ("smtp.gmail.com" if c.get("provider")=="gmail" else "")
        port=int(c.get("smtp_port",587)); use_tls=bool(c.get("use_tls",True))
        msg=MIMEText(body,_charset="utf-8"); msg["Subject"]=subject; msg["From"]=sender; msg["To"]=", ".join(rcpts)
        if use_tls:
            with smtplib.SMTP(host=host, port=port, timeout=15) as s:
                s.ehlo(); s.starttls(); s.ehlo(); s.login(sender,pwd); s.sendmail(sender, rcpts, msg.as_string())
        else:
            with smtplib.SMTP(host=host, port=port, timeout=15) as s:
                s.login(sender,pwd); s.sendmail(sender, rcpts, msg.as_string())
        return True
