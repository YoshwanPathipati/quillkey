"""Applies corrections to whatever app has focus, via key injection and the
clipboard. Also wraps the backend API."""

import time

import httpx
import pyperclip
from pynput.keyboard import Controller, Key

API = "http://127.0.0.1:8765"

kb = Controller()


def _levenshtein(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 3:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class Corrector:
    def __init__(self, monitor):
        self.monitor = monitor
        self.session_id = None

    # ---------- backend ----------

    def api_check(self, text: str, include_style: bool = False) -> dict | None:
        try:
            resp = httpx.post(
                f"{API}/check",
                json={
                    "text": text,
                    "mode": "professional",
                    "session_id": self.session_id,
                    "domain": "desktop",
                    "include_style": include_style,
                },
                timeout=120 if include_style else 20,
            )
            resp.raise_for_status()
            data = resp.json()
            self.session_id = data.get("session_id") or self.session_id
            return data
        except httpx.HTTPError:
            return None

    def log_acceptance(self, suggestion_id: str, accepted: bool):
        try:
            httpx.post(
                f"{API}/log-acceptance",
                json={"suggestion_id": suggestion_id, "accepted": accepted},
                timeout=5,
            )
        except httpx.HTTPError:
            pass

    def health(self) -> dict | None:
        try:
            return httpx.get(f"{API}/health", timeout=5).json()
        except httpx.HTTPError:
            return None

    def api_rewrite(self, text: str, mode: str) -> str | None:
        try:
            resp = httpx.post(
                f"{API}/rewrite", json={"text": text, "mode": mode}, timeout=180
            )
            resp.raise_for_status()
            return resp.json().get("rewrite")
        except httpx.HTTPError:
            return None

    # ---------- autocorrect (single word, fired on space) ----------

    def spelling_fix_for(self, word: str) -> dict | None:
        """Return a safe, unambiguous spelling correction for a word, or None."""
        if len(word) < 3 or not word.isalpha() or not word.islower():
            return None
        result = self.api_check(word, include_style=False)
        if not result:
            return None
        for s in result["suggestions"]:
            if (
                s["error_type"] == "spelling"
                and s["offset"] == 0
                and s["length"] == len(word)
                and s["suggestion"]
                and " " not in s["suggestion"]
                and s["suggestion"].lower() != word.lower()
                and _levenshtein(word.lower(), s["suggestion"].lower()) <= 2
            ):
                return s
        return None

    # ---------- key injection ----------

    def _tap(self, key, times=1):
        for _ in range(times):
            kb.press(key)
            kb.release(key)
            time.sleep(0.004)

    def replace_tail(self, chars_to_erase: int, new_text: str):
        """Erase the last N chars in the focused field and type new text."""
        self.monitor.suppress = True
        try:
            self._tap(Key.backspace, chars_to_erase)
            kb.type(new_text)
        finally:
            time.sleep(0.05)
            self.monitor.suppress = False

    def autocorrect_last_word(self, word: str, fix: str):
        # caret is right after "word " — erase word + space, retype fixed.
        self.replace_tail(len(word) + 1, fix + " ")

    # ---------- fix selected text (Ctrl+Alt+G) ----------

    def fix_selection(self) -> tuple[str, int]:
        """Copy the selection, fix grammar+spelling, paste it back.

        Returns (status, fixes_applied).
        """
        old_clip = None
        try:
            old_clip = pyperclip.paste()
        except pyperclip.PyperclipException:
            pass

        self.monitor.suppress = True
        try:
            pyperclip.copy("")
            with kb.pressed(Key.ctrl):
                self._tap("c")
            time.sleep(0.20)
            text = pyperclip.paste()
            if not text.strip():
                return ("no-selection", 0)

            result = self.api_check(text, include_style=False)
            if result is None:
                return ("offline", 0)

            fixes = [
                s
                for s in result["suggestions"]
                if s["offset"] is not None
                and s["suggestion"]
                and s["error_type"] in ("grammar", "spelling")
                and text[s["offset"] : s["offset"] + s["length"]] == s["original"]
            ]
            if not fixes:
                return ("clean", 0)

            fixed = text
            for s in sorted(fixes, key=lambda x: -x["offset"]):
                fixed = (
                    fixed[: s["offset"]] + s["suggestion"] + fixed[s["offset"] + s["length"] :]
                )
                self.log_acceptance(s["id"], True)

            pyperclip.copy(fixed)
            with kb.pressed(Key.ctrl):
                self._tap("v")
            time.sleep(0.20)
            self.monitor.clear()
            return ("fixed", len(fixes))
        finally:
            self.monitor.suppress = False
            if old_clip is not None:
                # restore the user's clipboard after the paste has landed
                try:
                    pyperclip.copy(old_clip)
                except pyperclip.PyperclipException:
                    pass

    # ---------- AI rewrite of selected text (Ctrl+Alt+R) ----------

    def rewrite_selection(self, mode: str = "professional") -> str:
        """Copy the selection, rewrite it with the local LLM, paste it back."""
        old_clip = None
        try:
            old_clip = pyperclip.paste()
        except pyperclip.PyperclipException:
            pass

        self.monitor.suppress = True
        try:
            pyperclip.copy("")
            with kb.pressed(Key.ctrl):
                self._tap("c")
            time.sleep(0.20)
            text = pyperclip.paste()
            if not text.strip():
                return "no-selection"
            if len(text) > 4000:
                return "too-long"

            rewritten = self.api_rewrite(text, mode)
            if not rewritten:
                return "offline"

            pyperclip.copy(rewritten)
            with kb.pressed(Key.ctrl):
                self._tap("v")
            time.sleep(0.20)
            self.monitor.clear()
            return "done"
        finally:
            self.monitor.suppress = False
            if old_clip is not None:
                try:
                    pyperclip.copy(old_clip)
                except pyperclip.PyperclipException:
                    pass
