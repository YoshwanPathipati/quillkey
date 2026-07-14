// Background service worker: owns all network I/O to the local backend.
// Content scripts can't reliably reach localhost (page CSP on Gmail etc.),
// so every request is routed through here. A WebSocket is kept for
// real-time checks, with a plain fetch fallback when it's down.

const API = 'http://127.0.0.1:8765';
const WS_URL = 'ws://127.0.0.1:8765/ws';

let ws = null;
let wsReady = false;
let requestSeq = 0;
const pendingWs = new Map(); // request_id -> {resolve, timer}

function connectWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    ws = null;
    return;
  }
  ws.onopen = () => { wsReady = true; };
  ws.onclose = ws.onerror = () => {
    wsReady = false;
    ws = null;
    for (const [, p] of pendingWs) { clearTimeout(p.timer); p.resolve(null); }
    pendingWs.clear();
  };
  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch { return; }
    const p = pendingWs.get(msg.request_id);
    if (p) {
      clearTimeout(p.timer);
      pendingWs.delete(msg.request_id);
      p.resolve(msg.type === 'result' ? msg : null);
    }
  };
}

function checkViaWs(payload) {
  return new Promise((resolve) => {
    if (!wsReady || !ws) return resolve(null);
    const request_id = 'r' + (++requestSeq);
    const timer = setTimeout(() => {
      pendingWs.delete(request_id);
      resolve(null);
    }, 120000);
    pendingWs.set(request_id, { resolve, timer });
    try {
      ws.send(JSON.stringify({ ...payload, request_id }));
    } catch {
      clearTimeout(timer);
      pendingWs.delete(request_id);
      resolve(null);
    }
  });
}

async function apiFetch(path, options = {}) {
  const resp = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!resp.ok) throw new Error(`Backend returned ${resp.status}`);
  return resp.json();
}

async function handleCheck(payload) {
  connectWs();
  let result = await checkViaWs(payload);
  if (!result) {
    try {
      result = await apiFetch('/check', { method: 'POST', body: JSON.stringify(payload) });
    } catch (e) {
      return { offline: true, suggestions: [], warnings: ['Backend is unreachable — is start.bat running?'] };
    }
  }
  return result;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg.type) {
        case 'check':
          sendResponse(await handleCheck(msg.payload));
          break;
        case 'log-acceptance':
          sendResponse(await apiFetch('/log-acceptance', { method: 'POST', body: JSON.stringify(msg.payload) }));
          break;
        case 'explain':
          sendResponse(await apiFetch('/explain', { method: 'POST', body: JSON.stringify(msg.payload) }));
          break;
        case 'stats':
          sendResponse(await apiFetch('/stats'));
          break;
        case 'history':
          sendResponse(await apiFetch('/history'));
          break;
        case 'coach-tip':
          sendResponse(await apiFetch('/coach-tip'));
          break;
        case 'health':
          sendResponse(await apiFetch('/health'));
          break;
        default:
          sendResponse({ error: 'unknown message type' });
      }
    } catch (e) {
      sendResponse({ offline: true, error: String(e) });
    }
  })();
  return true; // keep the message channel open for the async response
});

// Alt+G — forward the toggle to the active tab's content script.
chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'toggle-sidebar') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab?.id) chrome.tabs.sendMessage(tab.id, { type: 'toggle-sidebar' }).catch(() => {});
});

// "Why is this wrong?" context menu (Explain Like a Teacher mode).
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'lg-explain',
    title: 'Why is this wrong?',
    contexts: ['all'],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'lg-explain' && tab?.id) {
    chrome.tabs.sendMessage(tab.id, { type: 'explain-context' }).catch(() => {});
  }
});

connectWs();
