"""QuillKey suggestion card + toast — modern dark UI, rounded Win11 corners,
never steals focus from the app you're writing in."""

import ctypes
import tkinter as tk

import win32api
import win32con
import win32gui

BG = "#17171c"
SURFACE = "#202028"
SURFACE_HOVER = "#272732"
FG = "#ecedf0"
MUTED = "#8b8b96"
GREEN = "#30a46c"
BLUE = "#4c8dff"

DOT = {"grammar": "#ef5350", "spelling": "#fbc02d", "style": "#42a5f5", "clarity": "#26a69a"}

MAX_ROWS = 5
CARD_WIDTH = 340  # logical px, scaled


def _round_corners(hwnd):
    """Windows 11 native rounded corners."""
    try:
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), 4
        )
    except Exception:
        pass


def _no_activate(win):
    try:
        hwnd = win32gui.GetParent(win.winfo_id())
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            hwnd,
            win32con.GWL_EXSTYLE,
            style | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
        )
        _round_corners(hwnd)
    except Exception:
        pass


def _work_area(near_hwnd=None) -> tuple:
    try:
        hwnd = near_hwnd or win32gui.GetForegroundWindow()
        mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        return win32api.GetMonitorInfo(mon)["Work"]
    except Exception:
        return (0, 0, 1920, 1040)


class SuggestionCard:
    def __init__(self, root: tk.Tk, scale: float):
        self.scale = scale
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        self.win.withdraw()
        self.visible = False
        self.on_apply = None       # callbacks wired by the app
        self.on_apply_all = None
        self.on_copy = None

    def f(self, px: int, bold=False):
        return ("Segoe UI", -int(px * self.scale), "bold") if bold else ("Segoe UI", -int(px * self.scale))

    def px(self, n: int) -> int:
        return int(n * self.scale)

    # ---------- content ----------

    def render(self, suggestions: list[dict], note: str | None):
        for child in self.win.winfo_children():
            child.destroy()

        head = tk.Frame(self.win, bg=BG)
        head.pack(fill="x", padx=self.px(14), pady=(self.px(12), self.px(6)))
        tk.Label(head, text="🪶 QuillKey", bg=BG, fg=MUTED, font=self.f(11, True)).pack(side="left")
        n = len(suggestions)
        pill_bg = "#3b1519" if n else "#0f2b1d"
        pill_fg = "#ff8f94" if n else "#5fd39c"
        tk.Label(
            head, text=f" {n} issue{'s' if n != 1 else ''} " if n else " ✓ clean ",
            bg=pill_bg, fg=pill_fg, font=self.f(10, True),
        ).pack(side="left", padx=(self.px(8), 0))
        tk.Button(
            head, text="✕", command=self.hide, bg=BG, fg=MUTED, bd=0,
            activebackground=BG, activeforeground=FG, font=self.f(11), cursor="hand2",
        ).pack(side="right")

        for s in suggestions[:MAX_ROWS]:
            self._row(s)

        more = n - MAX_ROWS
        if more > 0:
            tk.Label(
                self.win, text=f"…and {more} more — fix these first", bg=BG,
                fg=MUTED, font=self.f(9),
            ).pack(anchor="w", padx=self.px(16), pady=(self.px(2), 0))

        if note:
            tk.Label(
                self.win, text=note, bg="#141d2e", fg="#9cc2ff", font=self.f(10),
                wraplength=self.px(CARD_WIDTH - 40), justify="left",
                padx=self.px(10), pady=self.px(8),
            ).pack(fill="x", padx=self.px(14), pady=(self.px(8), 0))

        foot = tk.Frame(self.win, bg=BG)
        foot.pack(fill="x", padx=self.px(14), pady=self.px(12))
        fixable = [s for s in suggestions if s.get("suggestion")]
        if fixable:
            self._button(foot, f"Fix all ({len(fixable)})", GREEN, "white",
                         lambda: self.on_apply_all and self.on_apply_all()).pack(side="left")
        if suggestions:
            self._button(foot, "Copy corrected", "#1b2b45", BLUE,
                         lambda: self.on_copy and self.on_copy()).pack(side="left", padx=(self.px(8), 0))

    def _row(self, s: dict):
        row = tk.Frame(self.win, bg=SURFACE)
        row.pack(fill="x", padx=self.px(12), pady=self.px(3))
        inner = tk.Frame(row, bg=SURFACE)
        inner.pack(fill="x", padx=self.px(10), pady=self.px(8))

        top = tk.Frame(inner, bg=SURFACE)
        top.pack(fill="x")
        tk.Label(top, text="●", fg=DOT.get(s["error_type"], DOT["style"]),
                 bg=SURFACE, font=self.f(9)).pack(side="left")
        fix = s["suggestion"] or "remove"
        tk.Label(
            top, text=f' {s["original"]}  →  {fix}', bg=SURFACE, fg=FG,
            font=self.f(11, True), anchor="w",
            wraplength=self.px(CARD_WIDTH - 110), justify="left",
        ).pack(side="left", fill="x", expand=True)
        if s.get("suggestion"):
            self._button(
                top, "Fix", GREEN, "white",
                lambda s=s: self.on_apply and self.on_apply(s),
            ).pack(side="right")

        if s.get("explanation"):
            tk.Label(
                inner, text=s["explanation"], bg=SURFACE, fg=MUTED, font=self.f(9),
                anchor="w", wraplength=self.px(CARD_WIDTH - 60), justify="left",
            ).pack(fill="x", pady=(self.px(3), 0))

    def _button(self, parent, text, bg, fg, command):
        return tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg, bd=0,
            padx=self.px(12), pady=self.px(3), font=self.f(10, True),
            cursor="hand2", activebackground=bg, activeforeground=fg,
        )

    # ---------- placement ----------

    def show_near(self, anchor_xy: tuple[int, int]):
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        left, top, right, bottom = _work_area()
        x = min(anchor_xy[0] - w + self.px(34), right - w - 8)
        y = anchor_xy[1] - h - self.px(10)
        if y < top:
            y = min(anchor_xy[1] + self.px(44), bottom - h - 8)
        x = max(x, left + 8)
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        _no_activate(self.win)
        self.visible = True
        self.win.lift()

    def hide(self):
        self.win.withdraw()
        self.visible = False


class Toast:
    """Transient bottom-right notification ('✓ teh → the')."""

    def __init__(self, root: tk.Tk, scale: float):
        self.scale = scale
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)
        self.win.withdraw()
        self.label = tk.Label(
            self.win, text="", bg=BG, fg="#5fd39c",
            font=("Segoe UI", -int(12 * scale), "bold"),
            padx=int(16 * scale), pady=int(10 * scale),
        )
        self.label.pack()
        self._job = None

    def flash(self, text: str, ms: int = 2200, color: str = "#5fd39c"):
        self.label.configure(text=text, fg=color)
        self.win.update_idletasks()
        left, top, right, bottom = _work_area()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        self.win.geometry(f"+{right - w - 20}+{bottom - h - 20}")
        self.win.deiconify()
        _no_activate(self.win)
        self.win.lift()
        if self._job:
            self.win.after_cancel(self._job)
        self._job = self.win.after(ms, self.win.withdraw)
