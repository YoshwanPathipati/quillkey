"""Global typing monitor.

Buffers what the user types in the currently focused window so the app can
check it. The buffer resets whenever the caret plausibly moved somewhere we
can't track: window switch, mouse click, Enter, arrows, Ctrl-shortcuts.

Privacy: nothing ever leaves the machine, the buffer only lives in RAM, and
windows whose title mentions "password" are never captured.
"""

import threading
import time

import win32gui
from pynput import keyboard, mouse

Key = keyboard.Key

CLEAR_KEYS = {
    Key.enter, Key.tab, Key.esc,
    Key.left, Key.right, Key.up, Key.down,
    Key.home, Key.end, Key.page_up, Key.page_down, Key.delete,
}

MAX_BUFFER = 1500  # chars — roughly a long paragraph


class TypingMonitor:
    def __init__(self, on_word=None):
        self.on_word = on_word  # callback(word) fired when a word is completed
        self.suppress = False   # True while the app itself is injecting keys
        self.last_key_time = 0.0
        self.keystrokes = 0     # monotonic counter: "has the user typed since X?"
        self.hwnd = None
        self.blocked = False
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._kb = keyboard.Listener(on_press=self._on_press)
        self._mouse = mouse.Listener(on_click=self._on_click)

    def start(self):
        self._kb.start()
        self._mouse.start()

    def stop(self):
        self._kb.stop()
        self._mouse.stop()

    def text(self) -> str:
        with self._lock:
            return "".join(self._buffer)

    def set_text(self, text: str):
        """Resync the buffer after the app itself edited the target field."""
        with self._lock:
            self._buffer = list(text[-MAX_BUFFER:])

    def clear(self):
        with self._lock:
            self._buffer.clear()

    # ---------- listeners ----------

    def _on_click(self, *args):
        if not self.suppress:
            self.clear()

    def _on_press(self, key):
        if self.suppress:
            return
        self.last_key_time = time.time()
        self.keystrokes += 1

        try:
            hwnd = win32gui.GetForegroundWindow()
        except Exception:
            hwnd = None
        if hwnd != self.hwnd:
            self.hwnd = hwnd
            self.clear()
            try:
                title = (win32gui.GetWindowText(hwnd) or "").lower()
            except Exception:
                title = ""
            self.blocked = "password" in title
        if self.blocked:
            return

        if key in CLEAR_KEYS:
            self.clear()
            return
        if key == Key.backspace:
            with self._lock:
                if self._buffer:
                    self._buffer.pop()
            return
        if key == Key.space:
            with self._lock:
                text = "".join(self._buffer)
                self._buffer.append(" ")
            if text and not text.endswith(" "):
                word = text.split()[-1]
                if word and self.on_word:
                    self.on_word(word)
            return

        ch = getattr(key, "char", None)
        if ch is None:
            return  # shift/ctrl/etc. on their own
        if ord(ch) < 32:
            # Ctrl+<letter> — a shortcut probably moved/changed things.
            self.clear()
            return
        with self._lock:
            self._buffer.append(ch)
            if len(self._buffer) > MAX_BUFFER:
                del self._buffer[: len(self._buffer) - MAX_BUFFER]
