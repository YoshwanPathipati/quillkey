"""QuillKey backend - FastAPI on localhost:8765.

Coordinates LanguageTool (grammar/spelling) and Ollama (style coaching),
merges results, and persists everything to SQLite.
"""

import asyncio
import logging

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database
import languagetool
import ollama_client
import vocabulary
from tips import TIPS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("quillkey")

app = FastAPI(title="QuillKey")

# The Chrome extension calls from a chrome-extension:// origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_MODES = {"academic", "professional", "creative", "social"}


class CheckRequest(BaseModel):
    text: str
    mode: str = "professional"
    session_id: int | None = None
    domain: str = ""
    include_style: bool = True  # False = fast grammar-only pass


class AcceptanceRequest(BaseModel):
    suggestion_id: str
    accepted: bool


class ExplainRequest(BaseModel):
    error_type: str
    original: str
    fix: str


class RewriteRequest(BaseModel):
    text: str
    mode: str = "professional"


@app.on_event("startup")
async def startup() -> None:
    database.init(TIPS)
    lt_ok, ollama_ok = await asyncio.gather(languagetool.ping(), ollama_client.ping())
    log.info("LanguageTool: %s | Ollama: %s", "up" if lt_ok else "DOWN", "up" if ollama_ok else "DOWN")
    if ollama_ok:
        await ollama_client.resolve_model()


async def run_check(req: CheckRequest) -> dict:
    """Shared by POST /check and the WebSocket: run engines, merge, log."""
    mode = req.mode if req.mode in VALID_MODES else "professional"
    session_id = req.session_id or database.create_session(mode, req.domain)

    warnings = []
    style: dict = {"clarity_score": None, "issues": [], "rewrite": None, "tone": None}

    async def grammar_task():
        try:
            return await languagetool.check(req.text)
        except httpx.HTTPError as exc:
            warnings.append("LanguageTool is unreachable - grammar checks are off. Is Docker running?")
            log.warning("LanguageTool error: %s", exc)
            return []

    async def style_task():
        if not req.include_style or len(req.text.split()) < 5:
            return style
        try:
            return await ollama_client.analyze_style(req.text, mode)
        except (httpx.HTTPError, RuntimeError) as exc:
            warnings.append("Ollama is unreachable - style coaching is off. Is Ollama running?")
            log.warning("Ollama error: %s", exc)
            return style

    grammar, style = await asyncio.gather(grammar_task(), style_task())

    # Merge: LanguageTool wins over style/vocab when they flag the same span.
    taken: list[tuple[int, int]] = [
        (s["offset"], s["offset"] + s["length"]) for s in grammar if s["offset"] is not None
    ]
    merged = list(grammar)
    for s in style["issues"] + vocabulary.find_weak_words(req.text):
        if s.get("offset") is not None and any(
            s["offset"] < end and s["offset"] + s["length"] > start for start, end in taken
        ):
            continue
        merged.append(s)
    suggestions = merged

    database.log_suggestions(session_id, suggestions)
    database.update_session(session_id, len(req.text.split()))

    return {
        "session_id": session_id,
        "suggestions": suggestions,
        "clarity_score": style["clarity_score"],
        "rewrite": style["rewrite"],
        "tone": style["tone"],
        "warnings": warnings,
    }


@app.post("/check")
async def check(req: CheckRequest) -> dict:
    return await run_check(req)


@app.post("/log-acceptance")
async def log_acceptance(req: AcceptanceRequest) -> dict:
    found = database.log_acceptance(req.suggestion_id, req.accepted)
    return {"ok": found}


@app.get("/stats")
async def stats() -> dict:
    return database.get_stats()


@app.get("/coach-tip")
async def coach_tip() -> dict:
    return database.get_daily_tip()


@app.get("/history")
async def history() -> dict:
    return {
        "sessions": database.get_history(7),
        "top_mistakes": database.get_top_mistakes(7),
        "weekly_summary": database.get_weekly_summary(),
    }


@app.post("/rewrite")
async def rewrite(req: RewriteRequest) -> dict:
    """Full LLM rewrite of a passage (desktop Ctrl+Alt+R hotkey)."""
    mode = req.mode if req.mode in VALID_MODES else "professional"
    try:
        text = await ollama_client.rewrite(req.text, mode)
        return {"rewrite": text}
    except (httpx.HTTPError, RuntimeError):
        return {"rewrite": None, "error": "Ollama is unreachable."}


@app.post("/explain")
async def explain(req: ExplainRequest) -> dict:
    """'Explain like a teacher' mode."""
    try:
        text = await ollama_client.explain_error(req.error_type, req.original, req.fix)
        return {"explanation": text}
    except (httpx.HTTPError, RuntimeError):
        return {"explanation": None, "error": "Ollama is unreachable."}


@app.get("/health")
async def health() -> dict:
    lt_ok, ollama_ok = await asyncio.gather(languagetool.ping(), ollama_client.ping())
    return {
        "languagetool": lt_ok,
        "ollama": ollama_ok,
        "ollama_model": await ollama_client.resolve_model() if ollama_ok else None,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    """Real-time checking. Client sends CheckRequest-shaped JSON; each
    message supersedes the previous one (stale checks are cancelled)."""
    await ws.accept()
    pending: asyncio.Task | None = None

    async def check_and_reply(payload: dict) -> None:
        try:
            result = await run_check(CheckRequest(**payload))
            result["request_id"] = payload.get("request_id")
            await ws.send_json({"type": "result", **result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never crash the socket loop
            log.exception("WS check failed")
            await ws.send_json({"type": "error", "message": str(exc)})

    try:
        while True:
            payload = await ws.receive_json()
            if pending and not pending.done():
                pending.cancel()
            pending = asyncio.create_task(check_and_reply(payload))
    except WebSocketDisconnect:
        if pending and not pending.done():
            pending.cancel()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
