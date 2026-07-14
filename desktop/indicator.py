"""Floating status dot pinned to the corner of the field you're writing in —
QuillKey's version of Grammarly's green dot. Click it to open the card."""

import tkinter as tk

import win32con
import win32gui

TRANS = "#010101"
SIZE = 34

STATE_COLORS = {
    "issues": "#e5484d",
    "clean": "#30a46c",
    "checking": "#8f8f9a",
    "offline": "#5a5a64",
}


class Indicator:
    def __init__(self, root: tk.Tk, scale: float, on_click):
        self.scale = scale
        self.size = int(SIZE * scale)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", TRANS)
        self.win.configure(bg=TRANS)
        self.win.geometry(f"{self.size}x{self.size}+0+0")
        self.canvas = tk.Canvas(
            self.win, bg=TRANS, highlightthickness=0, bd=0,
            width=self.size, height=self.size, cursor="hand2",
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", lambda e: on_click())
        self.win.withdraw()
        self.visible = False
        self.state = None
        self.count = 0

    def _apply_styles(self):
        try:
            hwnd = win32gui.GetParent(self.win.winfo_id())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
            )
        except Exception:
            pass

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        pad = int(3 * self.scale)
        color = STATE_COLORS.get(self.state, STATE_COLORS["checking"])
        c.create_oval(
            pad, pad, self.size - pad, self.size - pad,
            fill=color, outline="#0e0e12", width=2,
        )
        label = (
            "…" if self.state == "checking"
            else "!" if self.state == "offline"
            else "✓" if self.state == "clean"
            else str(min(self.count, 9)) + ("+" if self.count > 9 else "")
        )
        c.create_text(
            self.size // 2, self.size // 2, text=label, fill="white",
            font=("Segoe UI", -int(15 * self.scale), "bold"),
        )

    def show(self, state: str, count: int, field_rect: tuple | None):
        changed = (state, count) != (self.state, self.count)
        self.state, self.count = state, count
        if changed:
            self._redraw()
        if field_rect:
            left, top, right, bottom = field_rect
            x = right - self.size - int(10 * self.scale)
            y = bottom - self.size - int(6 * self.scale)
            # keep it inside the field for tiny fields
            x = max(x, left)
            y = max(y, top)
            self.win.geometry(f"+{x}+{y}")
        if not self.visible:
            self.win.deiconify()
            self._apply_styles()
            self.visible = True
        self.win.lift()

    def position(self) -> tuple[int, int]:
        return (self.win.winfo_x(), self.win.winfo_y())

    def hide(self):
        if self.visible:
            self.win.withdraw()
            self.visible = False
