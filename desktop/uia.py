"""UI Automation layer — the same architecture Grammarly Desktop uses.

Reads the *entire* text of the focused field in any application (including
text that was there before QuillKey started), resolves on-screen rectangles
for error spans so underlines can be drawn over the host app, and applies
fixes surgically by selecting the exact error range and typing over it.

All calls must run inside a thread wrapped in
`uiautomation.UIAutomationInitializerInThread()` (COM apartment).
"""

import os
import logging

import uiautomation as auto
import win32api
import win32con
import win32process

log = logging.getLogger("quillkey.uia")

MAX_TEXT = 6000  # chars — larger docs are checked on their tail only

# Apps we must never grammar-check: terminals and code editors.
BLOCKED_EXES = {
    "windowsterminal.exe", "wt.exe", "conhost.exe", "cmd.exe",
    "powershell.exe", "pwsh.exe", "openconsole.exe",
    "code.exe", "cursor.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
    "clion64.exe", "webstorm64.exe", "sublime_text.exe", "windbg.exe",
}

_exe_cache: dict[int, str] = {}


def _exe_for_pid(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            name = os.path.basename(win32process.GetModuleFileNameEx(handle, 0)).lower()
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        pass
    if len(_exe_cache) > 256:
        _exe_cache.clear()
    _exe_cache[pid] = name
    return name


class TextField:
    """A focused, editable text control reachable through UIA."""

    def __init__(self, control, text_pattern):
        self.control = control
        self.pattern = text_pattern
        self.runtime_key = tuple(control.GetRuntimeId() or ()) or (
            control.NativeWindowHandle, control.ProcessId
        )

    # ---------- reading ----------

    def text(self) -> str | None:
        try:
            return self.pattern.DocumentRange.GetText(MAX_TEXT)
        except Exception:
            return None

    def bounding_rect(self):
        """(left, top, right, bottom) of the control, physical pixels."""
        try:
            r = self.control.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    # ---------- locating error spans ----------

    def _occurrence_ordinal(self, full_text: str, original: str, offset: int) -> int:
        """How many times `original` appears before `offset` in our text."""
        count, start = 0, 0
        while True:
            i = full_text.find(original, start)
            if i < 0 or i >= offset:
                break
            count += 1
            start = i + 1
        return count

    def _find_range(self, full_text: str, original: str, offset: int):
        """Locate the UIA text range for the occurrence of `original` that
        sits at `offset` in our snapshot. Ordinal-based so it's immune to
        \\r\\n-vs-\\n differences between UIA and Python strings."""
        if not original or "\n" in original or "\r" in original:
            return None
        ordinal = self._occurrence_ordinal(full_text, original, offset)
        try:
            search = self.pattern.DocumentRange
            found = None
            for _ in range(ordinal + 1):
                found = search.FindText(original, False, False)
                if found is None:
                    return None
                # continue searching after this match
                search.MoveEndpointByRange(
                    auto.TextPatternRangeEndpoint.Start,
                    found,
                    auto.TextPatternRangeEndpoint.End,
                )
            return found
        except Exception:
            return None

    def rects_for(self, full_text: str, original: str, offset: int) -> list[tuple]:
        """Screen rectangles of an error span, for underline drawing."""
        rng = self._find_range(full_text, original, offset)
        if rng is None:
            return []
        try:
            rects = rng.GetBoundingRectangles() or []
            out = []
            for r in rects:
                # uiautomation returns Rect objects; be tolerant of tuples
                if hasattr(r, "left"):
                    out.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
                elif len(r) == 4:
                    left, top, w, h = r
                    out.append((int(left), int(top), int(left + w), int(top + h)))
            return out
        except Exception:
            return []

    def select_span(self, full_text: str, original: str, offset: int) -> bool:
        """Select the error span in the host app so typing replaces it."""
        rng = self._find_range(full_text, original, offset)
        if rng is None:
            return False
        try:
            rng.Select()
            return True
        except Exception:
            return False


def focused_text_field() -> TextField | None:
    """Return the currently focused editable text control, or None.

    Filters: our own process, terminals/code editors, password fields,
    non-editable documents (e.g. a web page body).
    """
    try:
        control = auto.GetFocusedControl()
    except Exception:
        return None
    if control is None or control.ProcessId == os.getpid():
        return None
    if _exe_for_pid(control.ProcessId) in BLOCKED_EXES:
        return None

    try:
        if control.IsPassword:
            return None
    except Exception:
        pass

    try:
        text_pattern = control.GetPattern(auto.PatternId.TextPattern)
    except Exception:
        text_pattern = None
    if not text_pattern:
        return None

    # Editability check: Edit controls are editable by nature; Documents
    # (browsers, Word) must expose a writable ValuePattern or TextEdit.
    try:
        ctype = control.ControlType
    except Exception:
        return None
    editable = ctype == auto.ControlType.EditControl
    if not editable:
        try:
            vp = control.GetPattern(auto.PatternId.ValuePattern)
            editable = vp is not None and not vp.IsReadOnly
        except Exception:
            editable = False
    if not editable:
        try:
            editable = control.GetPattern(auto.PatternId.TextEditPattern) is not None
        except Exception:
            editable = False
    if not editable:
        return None

    return TextField(control, text_pattern)
