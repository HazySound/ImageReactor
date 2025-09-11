from __future__ import annotations
import tkinter as tk


def show_toast(root: tk.Tk, text: str, duration_ms: int = 2200, level: str = "info"):
    win = tk.Toplevel(root)
    win.overrideredirect(True); win.attributes("-topmost", True)
    bg = {"info":"#2b2b2b","warn":"#8a6d3b","error":"#a94442"}.get(level,"#2b2b2b"); fg="#ffffff"
    frm = tk.Frame(win, bg=bg, bd=1, relief="solid")
    lbl = tk.Label(frm, text=text, bg=bg, fg=fg, padx=14, pady=8, justify="left"); lbl.pack(); frm.pack()

    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    ww, wh = lbl.winfo_reqwidth()+10, lbl.winfo_reqheight()+10
    win.geometry(f"{ww}x{wh}+{sw-ww-20}+{sh-wh-40}")
    win.after(duration_ms, win.destroy)
