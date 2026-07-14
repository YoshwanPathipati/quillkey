"""Click-through transparent overlay that draws Grammarly-style underlines
over any application's text, at real screen coordinates from UIA."""

import tkinter as tk

import win32api
import win32con
import win32gui

TRANS = "#010101"  # magic transparency key — everything this color is see-through

COLORS = {
    "grammar": "#ef5350",
    "spelling": "#fbc02d",
    "style": "#42a5f5",
    "clarity": "#26a69a",
}


class UnderlineOverlay:
    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", TRANS)
        self.win.configure(bg=TRANS)

        # Cover the whole virtual desktop (all monitors).
        self.ox = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        self.oy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        self.win.geometry(f"{w}x{h}+{self.ox}+{self.oy}")

        self.canvas = tk.Canvas(self.win, bg=TRANS, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.win.withdraw()
        self.visible = False

    def _apply_styles(self):
        """Click-through + never-activate + hidden from alt-tab."""
        try:
            hwnd = win32gui.GetParent(self.win.winfo_id())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style
                | win32con.WS_EX_TRANSPARENT
                | win32con.WS_EX_NOACTIVATE
                | win32con.WS_EX_TOOLWINDOW,
            )
        except Exception:
            pass

    def draw(self, spans: list[tuple]):
        """spans: [(left, top, right, bottom, error_type), ...] screen px."""
        self.canvas.delete("all")
        if not spans:
            self.clear()
            return
        for left, top, right, bottom, kind in spans:
            if right - left < 2:
                continue
            color = COLORS.get(kind, COLORS["style"])
            y = bottom - self.oy - 2
            self.canvas.create_rectangle(
                left - self.ox, y, right - self.ox, y + 3,
                fill=color, outline="",
            )
        if not self.visible:
            self.win.deiconify()
            self._apply_styles()
            self.visible = True
        self.win.lift()

    def clear(self):
        if self.visible:
            self.canvas.delete("all")
            self.win.withdraw()
            self.visible = False
