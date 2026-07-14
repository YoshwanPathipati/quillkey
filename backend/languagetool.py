"""Client wrapper for the local LanguageTool server (Docker, port 8010)."""

import logging

import httpx

LT_URL = "http://localhost:8010/v2/check"

log = logging.getLogger("languagetool")

# Map LanguageTool issue categories to our four suggestion buckets.
_CATEGORY_MAP = {
    "TYPOS": "spelling",
    "MISSPELLING": "spelling",
    "GRAMMAR": "grammar",
    "PUNCTUATION": "grammar",
    "CASING": "grammar",
    "TYPOGRAPHY": "grammar",
    "CONFUSED_WORDS": "grammar",
    "REDUNDANCY": "clarity",
    "PLAIN_ENGLISH": "clarity",
    "STYLE": "style",
    "COLLOQUIALISMS": "style",
    "MISC": "style",
}


def _bucket(match: dict) -> str:
    cat = (match.get("rule", {}).get("category", {}).get("id") or "").upper()
    issue_type = (match.get("rule", {}).get("issueType") or "").lower()
    if cat in _CATEGORY_MAP:
        return _CATEGORY_MAP[cat]
    if issue_type == "misspelling":
        return "spelling"
    if issue_type in ("style", "register", "locale-violation"):
        return "style"
    return "grammar"


async def check(text: str, language: str = "en-US") -> list[dict]:
    """Run text through LanguageTool. Returns normalized suggestion dicts.

    Raises httpx.HTTPError if the server is unreachable so the caller can
    surface a degraded-mode warning instead of failing silently.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            LT_URL,
            data={"text": text, "language": language, "enabledOnly": "false"},
        )
        resp.raise_for_status()
        matches = resp.json().get("matches", [])

    suggestions = []
    for m in matches:
        offset, length = m["offset"], m["length"]
        replacements = [r["value"] for r in m.get("replacements", [])[:3]]
        suggestions.append(
            {
                "source": "languagetool",
                "error_type": _bucket(m),
                "offset": offset,
                "length": length,
                "original": text[offset : offset + length],
                "suggestion": replacements[0] if replacements else "",
                "alternatives": replacements,
                "explanation": m.get("message", ""),
                "rule_id": m.get("rule", {}).get("id", ""),
            }
        )
    return suggestions


async def ping() -> bool:
    """True if the LanguageTool server answers."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get("http://localhost:8010/v2/languages")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
