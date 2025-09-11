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
    """간단한 SMTP 발송 큐(키체인/암호화 없음 → UI에서 마스킹만, 사용자 책임 고지)."""
    def __init__(self):
        self._cfg: Dict[str, Any] = {}
        self._q: "Queue[Tuple[str, Dict[str, Any]]]" = Queue()
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._last_sent: List[float] = []

    def configure(self, cfg: Dict[str, Any]): self._cfg = dict(cfg or {})

    def start(self):
        if self._th and self._th.is_alive(): return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, daemon=True); self._th.start()

    def stop(self):
        self._stop.set()
        if self._th: self._th.join(timeout=2.0)

    def enqueue(self, event_type: str, payload: Dict[str, Any]):
        if not self._cfg.get("enabled", False): return
        self._q.put((event_type, payload))

    def test_send(self) -> bool:
        return self._send_mail("[Test] Manager", "This is a test email.", self._cfg)

    # 내부
    def _loop(self):
        while not self._stop.is_set():
            try: event, payload = self._q.get(timeout=0.2)
            except Empty: continue
            if not self._rate_ok(): continue
            subj, body = self._render(event, payload)
            if self._send_mail(subj, body, self._cfg):
                now=time.time()
                self._last_sent.append(now); self._last_sent = [t for t in self._last_sent if now-t<= self._cfg.get("rate_limit_window_sec",300)]

    def _rate_ok(self)->bool:
        win=int(self._cfg.get("rate_limit_window_sec",300)); mx=int(self._cfg.get("rate_limit_max",3))
        now=time.time(); self._last_sent=[t for t in self._last_sent if now-t<=win]
        return len(self._last_sent)<mx

    def _render(self, event:str, p:Dict[str,Any])->tuple[str,str]:
        st=self._cfg.get("subject_tmpl","[Manager] {event}")
        bt=self._cfg.get("body_tmpl","{event}")
        return st.format(event=event, **p), bt.format(event=event, **p)

    def _send_mail(self, subject:str, body:str, c:Dict[str,Any])->bool:
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
