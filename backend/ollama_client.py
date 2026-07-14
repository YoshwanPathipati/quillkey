"""Client wrapper for the local Ollama server (port 11434).

Model selection: prefers llama3.1:8b, then mistral, then llama3, then the
first installed model. Detected once at startup and cached.
"""

import json
import logging
import re

import httpx

OLLAMA_URL = "http://localhost:11434"
MODEL_PREFERENCE = ["llama3.1:8b", "llama3.1", "mistral", "llama3", "qwen2.5"]

log = logging.getLogger("ollama")

_model: str | None = None

MODE_GUIDANCE = {
    "academic": (
        "Formal academic tone. No contractions. Structured, citation-ready "
        "paragraphs. Precise terminology."
    ),
    "professional": (
        "Concise, action-oriented, confident business writing suitable for "
        "LinkedIn or email. Cut filler ruthlessly."
    ),
    "creative": (
        "Creative writing. Allow stylistic rule-breaking. Focus on flow, "
        "rhythm, and vivid language. Only flag issues that hurt the prose."
    ),
    "social": (
        "Social media writing. Punchy, hook-forward, conversational. Short "
        "sentences are good. Flag anything that buries the hook."
    ),
}

STYLE_PROMPT = """You are a professional writing coach. Analyze the following text and return ONLY a JSON object with no markdown, no explanation outside the JSON.

Mode: {mode} — {mode_guidance}
Text: "{paragraph}"

Return exactly this structure:
{{
  "clarity_score": <0-10>,
  "issues": [
    {{
      "type": "<wordiness|passive_voice|vague_word|weak_verb|repetition|tone>",
      "original": "<exact phrase from text>",
      "suggestion": "<improved version>",
      "explanation": "<one sentence why>"
    }}
  ],
  "rewrite": "<full improved paragraph>",
  "tone": "<Formal|Casual|Confident|Hesitant|Academic|Emotional>"
}}"""

REWRITE_PROMPT = """You are an expert editor. Rewrite the following text to be clearer, more confident, and grammatically flawless while preserving its meaning, voice, and approximate length.

Mode: {mode} — {guidance}
Text: "{text}"

Return ONLY the rewritten text. No quotes, no preamble, no explanation."""

EXPLAIN_PROMPT = """You are an English grammar teacher. A student made the following error.
Error type: {error_type}
Original: "{original}"
Correction: "{fix}"

Explain in 2-3 plain sentences WHY this is wrong and what rule it breaks.
No jargon. Write as if explaining to a smart 16-year-old. Be direct and encouraging."""


async def resolve_model() -> str | None:
    """Pick the best installed model per MODEL_PREFERENCE. Cached."""
    global _model
    if _model:
        return _model
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            installed = [m["name"] for m in resp.json().get("models", [])]
    except httpx.HTTPError:
        return None
    for pref in MODEL_PREFERENCE:
        for name in installed:
            if name == pref or name.startswith(pref + ":") or name.startswith(pref):
                _model = name
                log.info("Using Ollama model: %s", name)
                return _model
    if installed:
        _model = installed[0]
        log.warning("No preferred model found; falling back to %s", _model)
    return _model


async def _generate(prompt: str, force_json: bool, timeout: float = 90.0) -> str:
    model = await resolve_model()
    if not model:
        raise RuntimeError("No Ollama model available")
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }
    if force_json:
        payload["format"] = "json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        resp.raise_for_status()
        return resp.json().get("response", "")


def _parse_style_json(raw: str) -> dict | None:
    """Parse the model's JSON, salvaging it from stray text if needed."""
    for candidate in (raw, *re.findall(r"\{.*\}", raw, re.DOTALL)):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


async def analyze_style(paragraph: str, mode: str = "professional") -> dict:
    """Run the style-coach prompt. Returns normalized analysis dict."""
    guidance = MODE_GUIDANCE.get(mode, MODE_GUIDANCE["professional"])
    prompt = STYLE_PROMPT.format(
        mode=mode, mode_guidance=guidance, paragraph=paragraph.replace('"', "'")
    )
    raw = await _generate(prompt, force_json=True)
    data = _parse_style_json(raw)
    if data is None:
        log.warning("Ollama returned unparseable JSON: %.200s", raw)
        return {"clarity_score": None, "issues": [], "rewrite": None, "tone": None}

    issues = []
    for issue in data.get("issues", []):
        if not isinstance(issue, dict) or not issue.get("original"):
            continue
        original = str(issue["original"])
        offset = paragraph.find(original)
        issues.append(
            {
                "source": "ollama",
                "error_type": "style"
                if issue.get("type") in ("wordiness", "repetition", "weak_verb")
                else "clarity",
                "style_type": issue.get("type", "style"),
                "offset": offset if offset >= 0 else None,
                "length": len(original) if offset >= 0 else None,
                "original": original,
                "suggestion": str(issue.get("suggestion", "")),
                "alternatives": [str(issue.get("suggestion", ""))],
                "explanation": str(issue.get("explanation", "")),
                "rule_id": "OLLAMA_STYLE",
            }
        )

    score = data.get("clarity_score")
    try:
        score = max(0, min(10, int(score)))
    except (TypeError, ValueError):
        score = None

    return {
        "clarity_score": score,
        "issues": issues,
        "rewrite": data.get("rewrite"),
        "tone": data.get("tone"),
    }


async def rewrite(text: str, mode: str = "professional") -> str:
    """One-shot full rewrite of a passage (powers the rewrite hotkey)."""
    guidance = MODE_GUIDANCE.get(mode, MODE_GUIDANCE["professional"])
    prompt = REWRITE_PROMPT.format(mode=mode, guidance=guidance, text=text.replace('"', "'"))
    result = (await _generate(prompt, force_json=False)).strip()
    # Local models often prepend chatter ("Here's the rewrite:") — drop any
    # short leading line that ends with a colon.
    lines = result.split("\n")
    if len(lines) > 1 and lines[0].rstrip().endswith(":") and len(lines[0]) < 60:
        result = "\n".join(lines[1:]).strip()
    return result.strip('"').strip()


async def explain_error(error_type: str, original: str, fix: str) -> str:
    """'Explain like a teacher' — plain-English explanation of one error."""
    prompt = EXPLAIN_PROMPT.format(error_type=error_type, original=original, fix=fix)
    return (await _generate(prompt, force_json=False, timeout=45.0)).strip()


async def ping() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
