"""QuillKey — private, system-wide writing assistant for Windows.

Grammarly-style architecture: UI Automation reads the focused text field in
any application (including pre-existing text), underlines are drawn over the
host app at real screen positions, a floating dot shows issue count, and a
click opens a card that applies fixes surgically anywhere in the text.

Plus: instant autocorrect while typing, Ctrl+Alt+G (fix selection), and
Ctrl+Alt+R (AI rewrite of selection). Everything stays on this machine.
"""

import ctypes
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path

import pyperclip
import pystray
import uiautomation as auto
from PIL import Image, ImageDraw
from pynput import keyboard

import uia
from corrector import Corrector, kb
from monitor import TypingMonitor
from overlay import UnderlineOverlay
from indicator import Indicator
from popup import SuggestionCard, Toast

CONFIG_PATH = Path(__file__).parent / "config.json"
DEFAULT_CONFIG = {
    "autocorrect": True,
    "assist": True,        # underlines + indicator + card
    "style_coach": False,  # Ollama clarity/tone/rewrite in the card
    "mode": "professional",
}
MODES = ["professional", "academic", "creative", "social"]

IDLE_BEFORE_CHECK = 1.0
MIN_WORDS = 3
POLL_SECONDS = 0.45


def make_dpi_aware() -> float:
    """Per-monitor DPI awareness so tk coords match UIA's physical pixels.
    Returns the UI scale factor for font/padding sizing."""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PMv2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def load_config() -> dict:
    try:
        cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text())}
    except (OSError, json.JSONDecodeError):
        cfg = dict(DEFAULT_CONFIG)
    return {k: cfg[k] for k in DEFAULT_CONFIG}


class App:
    def __init__(self, scale: float):
        self.config = load_config()
        self.paused_until = 0.0
        self.running = True

        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = UnderlineOverlay(self.root)
        self.indicator = Indicator(self.root, scale, on_click=self.toggle_card)
        self.card = SuggestionCard(self.root, scale)
        self.toast = Toast(self.root, scale)

        self.ui_queue: queue.Queue = queue.Queue()
        self.jobs: queue.Queue = queue.Queue()
        self.monitor = TypingMonitor(on_word=self.on_word_completed)
        self.corrector = Corrector(self.monitor)

        # last result shown (owned by the UI thread)
        self.shown: dict = {"suggestions": [], "note": None, "rect": None}
        self.card.on_apply = lambda s: self.jobs.put(("apply", [s]))
        self.card.on_apply_all = lambda: self.jobs.put(
            ("apply", [s for s in self.shown["suggestions"] if s.get("suggestion")])
        )
        self.card.on_copy = self.copy_corrected

        self.icon = None
        self._stats_cache = (0.0, None)

    def save_config(self):
        try:
            CONFIG_PATH.write_text(json.dumps(self.config, indent=2))
        except OSError:
            pass

    def paused(self) -> bool:
        return time.time() < self.paused_until

    # ================= autocorrect (unchanged core from v1) =================

    def on_word_completed(self, word: str):
        if not self.config["autocorrect"] or self.paused():
            return
        snapshot = self.monitor.keystrokes
        hwnd = self.monitor.hwnd
        threading.Thread(
            target=self._autocorrect_worker, args=(word, snapshot, hwnd), daemon=True
        ).start()

    def _autocorrect_worker(self, word: str, snapshot: int, hwnd):
        fix = self.corrector.spelling_fix_for(word)
        if not fix:
            return
        if self.monitor.keystrokes != snapshot or self.monitor.hwnd != hwnd:
            return  # user kept typing — backspace surgery would mangle text
        self.corrector.autocorrect_last_word(word, fix["suggestion"])
        text = self.monitor.text()
        if text.endswith(word + " "):
            self.monitor.set_text(text[: -len(word) - 1] + fix["suggestion"] + " ")
        self.corrector.log_acceptance(fix["id"], True)
        self.ui(("toast", f'✓ {word} → {fix["suggestion"]}'))

    # ================= UIA worker (the Grammarly architecture) ==============

    def uia_worker(self):
        with auto.UIAutomationInitializerInThread():
            state = None
            while self.running:
                try:
                    job = self.jobs.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    job = None

                if not self.config["assist"] or self.paused():
                    if state:
                        self.ui(("clear",))
                        state = None
                    continue

                field = uia.focused_text_field()
                if field is None:
                    if state:
                        self.ui(("clear",))
                        state = None
                    continue

                if job and state:
                    if job[0] == "apply" and job[1]:
                        self._apply_fixes(field, job[1])
                        state["checked"] = None  # force re-check
                    elif job[0] == "copy":
                        self._copy_corrected(field, job[1])
                    continue

                text = field.text()
                rect = field.bounding_rect()
                if text is None or rect is None:
                    continue

                if state is None or state["key"] != field.runtime_key:
                    state = {"key": field.runtime_key, "checked": None, "sugg": []}

                if text == state["checked"]:
                    if state["sugg"]:  # keep underlines glued to scrolling text
                        spans = self._resolve_spans(field, text, state["sugg"])
                        self.ui(("reposition", rect, spans))
                    continue

                idle = time.time() - self.monitor.last_key_time
                if idle < IDLE_BEFORE_CHECK and state["checked"] is not None:
                    self.ui(("checking", rect))
                    continue
                if len(text.split()) < MIN_WORDS:
                    state["checked"] = text
                    state["sugg"] = []
                    self.ui(("result", rect, [], [], None))
                    continue

                self.ui(("checking", rect))
                tail = text[-uia.MAX_TEXT :]
                base = len(text) - len(tail)
                result = self.corrector.api_check(
                    tail, include_style=self.config["style_coach"]
                )
                if result is None:
                    self.ui(("offline", rect))
                    state["checked"] = text
                    continue

                suggestions = []
                for s in result["suggestions"]:
                    if s["offset"] is None or not s["original"]:
                        continue
                    if tail[s["offset"] : s["offset"] + s["length"]] != s["original"]:
                        continue
                    s["offset"] += base
                    suggestions.append(s)

                state["checked"] = text
                state["sugg"] = suggestions
                spans = self._resolve_spans(field, text, suggestions)
                note = None
                if self.config["style_coach"] and result.get("clarity_score") is not None:
                    note = f'Clarity {result["clarity_score"]}/10'
                    if result.get("tone"):
                        note += f' · {result["tone"]}'
                    if result.get("rewrite"):
                        note += f'\n💡 {result["rewrite"]}'
                self.ui(("result", rect, suggestions, spans, note))

    def _resolve_spans(self, field, text: str, suggestions) -> list[tuple]:
        spans = []
        for s in suggestions[:20]:
            for left, top, right, bottom in field.rects_for(text, s["original"], s["offset"]):
                spans.append((left, top, right, bottom, s["error_type"]))
        return spans

    def _apply_fixes(self, field, fixes: list[dict]):
        applied = 0
        for s in fixes:
            text = field.text()
            if text is None:
                break
            # relocate: nearest occurrence to the recorded offset
            positions, start = [], 0
            while True:
                i = text.find(s["original"], start)
                if i < 0:
                    break
                positions.append(i)
                start = i + 1
            if not positions:
                continue
            offset = min(positions, key=lambda p: abs(p - s["offset"]))
            if not field.select_span(text, s["original"], offset):
                continue
            self.monitor.suppress = True
            try:
                kb.type(s["suggestion"])
            finally:
                time.sleep(0.06)
                self.monitor.suppress = False
            self.monitor.clear()
            self.corrector.log_acceptance(s["id"], True)
            applied += 1
            time.sleep(0.08)
        if applied:
            self.ui(("toast", f"✓ {applied} fix{'es' if applied != 1 else ''} applied"))
        else:
            self.ui(("toast", "⚠ Couldn't apply — text may have changed"))

    def _copy_corrected(self, field, suggestions: list[dict]):
        text = field.text()
        if text is None:
            return
        fixed = text
        for s in sorted(
            (s for s in suggestions if s.get("suggestion")),
            key=lambda x: -x["offset"],
        ):
            if fixed[s["offset"] : s["offset"] + s["length"]] == s["original"]:
                fixed = (
                    fixed[: s["offset"]] + s["suggestion"] + fixed[s["offset"] + s["length"] :]
                )
        try:
            pyperclip.copy(fixed)
            self.ui(("toast", "✓ Corrected text copied"))
        except pyperclip.PyperclipException:
            self.ui(("toast", "⚠ Clipboard unavailable"))

    # ================= UI thread =================

    def ui(self, msg):
        self.ui_queue.put(msg)

    def pump(self):
        try:
            while True:
                self._dispatch(self.ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def _dispatch(self, msg):
        kind = msg[0]
        if kind == "toast":
            self.toast.flash(msg[1])
        elif kind == "checking":
            self.indicator.show("checking", 0, msg[1])
        elif kind == "offline":
            self.indicator.show("offline", 0, msg[1])
        elif kind == "clear":
            self.overlay.clear()
            self.indicator.hide()
            self.card.hide()
        elif kind == "reposition":
            _, rect, spans = msg
            self.overlay.draw(spans)
            self.indicator.show(self.indicator.state or "clean", self.indicator.count, rect)
        elif kind == "result":
            _, rect, suggestions, spans, note = msg
            self.shown = {"suggestions": suggestions, "note": note, "rect": rect}
            self.overlay.draw(spans)
            n = len(suggestions)
            self.indicator.show("issues" if n else "clean", n, rect)
            if self.card.visible:
                if n or note:
                    self.card.render(suggestions, note)
                    self.card.show_near(self.indicator.position())
                else:
                    self.card.hide()

    def toggle_card(self):
        if self.card.visible:
            self.card.hide()
            return
        self.card.render(self.shown["suggestions"], self.shown["note"])
        self.card.show_near(self.indicator.position())

    def copy_corrected(self):
        # UIA access must happen on the worker thread — hand it a job.
        self.jobs.put(("copy", list(self.shown["suggestions"])))

    # ================= hotkeys =================

    def fix_selection_hotkey(self):
        threading.Thread(target=self._fix_selection_worker, daemon=True).start()

    def _fix_selection_worker(self):
        status, n = self.corrector.fix_selection()
        messages = {
            "no-selection": ("Select text first, then Ctrl+Alt+G", "#f5b301"),
            "offline": ("⚠ Backend offline — run start.bat", "#f5b301"),
            "clean": ("✓ No errors found", "#5fd39c"),
            "fixed": (f"✓ {n} fix{'es' if n != 1 else ''} applied", "#5fd39c"),
        }
        text, color = messages[status]
        self.ui(("toast", text))

    def rewrite_hotkey(self):
        threading.Thread(target=self._rewrite_worker, daemon=True).start()

    def _rewrite_worker(self):
        self.ui(("toast", "✨ Rewriting with local AI…"))
        status = self.corrector.rewrite_selection(self.config["mode"])
        messages = {
            "no-selection": "Select text first, then Ctrl+Alt+R",
            "too-long": "⚠ Selection too long (max ~4000 chars)",
            "offline": "⚠ Ollama unavailable for rewriting",
            "done": "✨ Rewritten",
        }
        self.ui(("toast", messages[status]))

    # ================= tray =================

    def _tray_image(self) -> Image.Image:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill="#30a46c")
        d.text((21, 12), "Q", fill="white", font_size=36)
        return img

    def _toggle(self, key):
        def handler(icon, item):
            self.config[key] = not self.config[key]
            self.save_config()
        return handler

    def _checked(self, key):
        return lambda item: self.config[key]

    def _set_mode(self, mode):
        def handler(icon, item):
            self.config["mode"] = mode
            self.save_config()
        return handler

    def _today_line(self, item) -> str:
        now = time.time()
        ts, cached = self._stats_cache
        if now - ts > 60:
            try:
                import httpx
                cached = httpx.get("http://127.0.0.1:8765/stats", timeout=3).json()
            except Exception:
                cached = None
            self._stats_cache = (now, cached)
        if not cached:
            return "Stats unavailable"
        return (
            f"Fixes accepted: {cached['accepted']}  ·  "
            f"Streak: {cached['streak_days']} day{'s' if cached['streak_days'] != 1 else ''}"
        )

    def build_tray(self):
        mode_items = [
            pystray.MenuItem(
                m.capitalize(), self._set_mode(m),
                checked=lambda item, m=m: self.config["mode"] == m, radio=True,
            )
            for m in MODES
        ]
        menu = pystray.Menu(
            pystray.MenuItem(self._today_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Autocorrect while typing", self._toggle("autocorrect"),
                             checked=self._checked("autocorrect")),
            pystray.MenuItem("Writing assistant (underlines + card)", self._toggle("assist"),
                             checked=self._checked("assist")),
            pystray.MenuItem("AI style coach in card (slower)", self._toggle("style_coach"),
                             checked=self._checked("style_coach")),
            pystray.MenuItem("Writing mode", pystray.Menu(*mode_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Fix selection\tCtrl+Alt+G",
                             lambda icon, item: self.fix_selection_hotkey()),
            pystray.MenuItem("Rewrite selection\tCtrl+Alt+R",
                             lambda icon, item: self.rewrite_hotkey()),
            pystray.MenuItem("Pause for 30 minutes", self._pause),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit QuillKey", self.quit),
        )
        self.icon = pystray.Icon("quillkey", self._tray_image(), "QuillKey", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _pause(self, icon, item):
        self.paused_until = time.time() + 30 * 60
        self.ui(("toast", "⏸ QuillKey paused for 30 minutes"))

    def quit(self, icon=None, item=None):
        self.running = False
        if self.icon:
            self.icon.stop()
        self.monitor.stop()
        self.root.after(0, self.root.destroy)

    # ================= startup =================

    def run(self):
        self.monitor.start()
        self.build_tray()
        keyboard.GlobalHotKeys(
            {
                "<ctrl>+<alt>+g": self.fix_selection_hotkey,
                "<ctrl>+<alt>+r": self.rewrite_hotkey,
            }
        ).start()
        threading.Thread(target=self.uia_worker, daemon=True).start()

        def health_check():
            h = self.corrector.health()
            if h is None:
                self.ui(("toast", "⚠ Backend offline — run start.bat first"))
            else:
                model = h.get("ollama_model") or "no LLM"
                self.ui(("toast", f"🪶 QuillKey running — grammar + {model}"))

        threading.Thread(target=health_check, daemon=True).start()

        self.root.after(100, self.pump)
        self.root.mainloop()


if __name__ == "__main__":
    ui_scale = make_dpi_aware()
    App(ui_scale).run()
