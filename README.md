# 🪶 QuillKey

A private, system-wide writing assistant for Windows — the Grammarly experience, running **100% on your machine**. Grammar and spelling from a local LanguageTool server, style coaching and AI rewrites from a local LLM via Ollama. No subscription, no API keys, no data ever leaving localhost.

QuillKey uses the same architecture as Grammarly Desktop: **Windows UI Automation** reads the text field you're working in (in any app — Word, Notepad, browsers, Discord, Slack), so it sees your whole text including what was there before you started typing.

## What it does

| | |
|---|---|
| 🔴 **Underlines in any app** | Errors are underlined right over the host application, color-coded: red grammar · yellow spelling · blue style · green clarity |
| 🟢 **Floating dot** | A Grammarly-style dot sits in the corner of the field you're writing in: red with the issue count, green ✓ when clean. Click it to open the suggestion card |
| 🪶 **Suggestion card** | Modern dark card with each fix, its explanation, one-click **Fix** (applied surgically anywhere in the text — your cursor never moves), **Fix all**, and **Copy corrected** |
| ⚡ **Autocorrect while typing** | `teh` + space → `the`, instantly, like a phone keyboard. Capitalized words (names) are never touched |
| ✂️ **`Ctrl+Alt+G`** | Fix all grammar/spelling in the selected text, in place, in any app |
| ✨ **`Ctrl+Alt+R`** | Rewrite the selected text with the local LLM — polish a LinkedIn post or email in one keystroke, tuned to your writing mode |
| 🎛️ **Writing modes** | Professional · Academic · Creative · Social — set in the tray menu, changes what the AI optimizes for |
| 📊 **Progress tracking** | Every accepted fix is logged to SQLite: acceptance rate, writing streak, top recurring mistakes |

The tray menu also shows your running fix count and streak, and has a "Pause for 30 minutes" for screen-sharing or gaming.

## Architecture

```
[Any app on your laptop]
   ↑ underline overlay        ↑ surgical fixes (UIA range select + replace)
   |                          |
[QuillKey tray app] ── UI Automation reads the focused field's full text
   |        └─ keyboard monitor → instant autocorrect on word completion
   ↓
[FastAPI backend — localhost:8765]
     ↙          ↘
[LanguageTool]   [Ollama]
 (Docker, 8010)   grammar/spell    clarity, tone, rewrites
     ↘          ↙
   merged suggestions → SQLite (sessions, accepted fixes, stats)
```

## Prerequisites

- **Docker Desktop** (for LanguageTool)
- **Python 3.11+**
- **Ollama** with a model — `llama3.1:8b` recommended (`ollama pull llama3.1:8b`). Auto-detects what's installed: `llama3.1:8b → mistral → llama3 → qwen2.5 → first available`.

## Setup (one time)

```
pip install -r backend/requirements.txt
pip install -r desktop/requirements.txt
```

## Run

Double-click **`start.bat`** — starts LanguageTool, the backend, and puts QuillKey in your tray. Then click into any text field and write.

## Optional: Chrome extension

An in-browser variant with a full sidebar (live writing metrics, coach tab with tips and weekly stats): `chrome://extensions` → Developer mode → Load unpacked → `extension/`. Toggle with **Alt+G**. The desktop app already covers browsers, so this is for people who want the metrics dashboard.

## Privacy

- Everything runs on localhost; no external calls, ever.
- The typing monitor skips windows whose title mentions "password".
- Terminals and code editors (Windows Terminal, VS Code, JetBrains, …) are excluded — QuillKey won't touch your code.
- Text buffers live in RAM only; SQLite stores just error/fix pairs for the Coach stats.

## Known limitations

- Underlines and the dot need the app to expose UI Automation text (almost everything modern does: Office, browsers, Notepad, Discord, Slack). For apps that don't, autocorrect and both hotkeys still work everywhere.
- The AI style coach (tray toggle, off by default) adds clarity/tone/rewrite to the card but takes 5–15 s per paragraph on an RTX 4050 with an 8B model.
- Google Docs works via the desktop app (it watches the field, not the page DOM) — but Docs' canvas renderer exposes limited UIA text, so underline positions there can be approximate; the card and hotkeys are reliable.

## File structure

```
quillkey/
├── backend/
│   ├── main.py              # FastAPI: /check /rewrite /explain /stats … + WebSocket
│   ├── languagetool.py      # LanguageTool client
│   ├── ollama_client.py     # Ollama client + prompt templates
│   ├── vocabulary.py        # weak-word upgrader
│   ├── database.py          # SQLite
│   └── tips.py              # 50 writing tips
├── desktop/                 # the QuillKey app
│   ├── app.py               # entry point: tray, orchestration, hotkeys
│   ├── uia.py               # UI Automation: read fields, locate spans, select
│   ├── monitor.py           # global typing monitor (autocorrect trigger)
│   ├── corrector.py         # backend client + key/clipboard injection
│   ├── overlay.py           # click-through underline overlay
│   ├── indicator.py         # floating status dot
│   └── popup.py             # suggestion card + toasts
├── extension/               # optional Chrome extension
├── docker-compose.yml       # LanguageTool (port 8010)
├── start.bat                # one-click launcher
└── README.md
```
