// Local Grammarly content script.
// Watches editable fields, sends text to the backend (via the background
// worker), draws underline overlays, and hosts the sidebar iframe.

(() => {
  if (window.__localGrammarly) return;
  window.__localGrammarly = true;

  const GRAMMAR_DEBOUNCE = 800; // fast LanguageTool pass
  const STYLE_PAUSE = 2000;     // full Ollama pass after typing stops

  const MODE_BY_DOMAIN_DEFAULTS = [
    [/linkedin\.com|mail\.google|outlook/, 'professional'],
    [/twitter\.com|x\.com|instagram|facebook|reddit/, 'social'],
    [/scholar\.google|overleaf|notion/, 'academic'],
  ];

  let mode = 'professional';
  let sessionId = null;
  let sidebarFrame = null;
  let sidebarVisible = false;
  let activeField = null;     // the tracker currently in focus
  let lastContextTarget = null; // suggestion id under last right-click
  const trackers = new Map(); // element -> tracker

  // ---------- utilities ----------

  const send = (type, payload) =>
    new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type, payload }, (resp) => {
          void chrome.runtime.lastError;
          resolve(resp || null);
        });
      } catch {
        resolve(null);
      }
    });

  function toast(text) {
    const el = document.createElement('div');
    el.className = 'lg-toast';
    el.textContent = text;
    document.documentElement.appendChild(el);
    setTimeout(() => el.remove(), 2200);
  }

  function isEligible(el) {
    if (!el || el.closest?.('#lg-sidebar-frame')) return false;
    const tag = el.tagName;
    if (tag === 'TEXTAREA') {
      if (el.readOnly || el.disabled) return false;
    } else if (el.isContentEditable) {
      // fine
    } else {
      return false;
    }
    // Exclusions: password/search fields, code editors.
    if (el.type === 'password' || el.type === 'search') return false;
    if (el.getAttribute('role') === 'searchbox') return false;
    if ((el.getAttribute('aria-label') || '').toLowerCase().includes('search')) return false;
    if (el.closest('.CodeMirror, .monaco-editor, [class*="ace_editor"], .cm-editor')) return false;
    if (el.spellcheck === false && el.closest('[class*="code"], [class*="editor-code"]')) return false;
    return true;
  }

  function getText(el) {
    return el.tagName === 'TEXTAREA' ? el.value : el.innerText || '';
  }

  // ---------- text offset mapping for contenteditable ----------

  // Walk text nodes accumulating innerText-equivalent offsets. innerText
  // inserts "\n" at block boundaries, which we approximate by tracking
  // block-level element transitions.
  function collectTextNodes(root) {
    const nodes = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) =>
        n.parentElement?.closest('script,style') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    let offset = 0;
    let lastBlock = null;
    const BLOCK = /^(DIV|P|LI|BR|H[1-6]|BLOCKQUOTE|PRE|TR)$/;
    let node;
    while ((node = walker.nextNode())) {
      let block = node.parentElement;
      while (block && block !== root && !BLOCK.test(block.tagName)) block = block.parentElement;
      if (lastBlock && block !== lastBlock) offset += 1; // newline between blocks
      lastBlock = block;
      nodes.push({ node, start: offset, end: offset + node.data.length });
      offset += node.data.length;
    }
    return nodes;
  }

  function rangeForOffsets(root, start, end) {
    const nodes = collectTextNodes(root);
    const range = document.createRange();
    let ok = 0;
    for (const { node, start: s, end: e } of nodes) {
      if (ok === 0 && start >= s && start <= e) {
        range.setStart(node, Math.min(start - s, node.data.length));
        ok = 1;
      }
      if (ok === 1 && end >= s && end <= e) {
        range.setEnd(node, Math.min(end - s, node.data.length));
        return range;
      }
    }
    return null;
  }

  // ---------- textarea mirror (for measuring error positions) ----------

  const MIRROR_PROPS = [
    'boxSizing', 'width', 'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
    'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth',
    'fontFamily', 'fontSize', 'fontWeight', 'fontStyle', 'letterSpacing',
    'lineHeight', 'textTransform', 'wordSpacing', 'textIndent', 'whiteSpace', 'wordBreak', 'overflowWrap',
  ];

  function mirrorRects(textarea, start, end) {
    const div = document.createElement('div');
    const cs = getComputedStyle(textarea);
    for (const p of MIRROR_PROPS) div.style[p] = cs[p];
    div.style.position = 'absolute';
    div.style.visibility = 'hidden';
    div.style.whiteSpace = 'pre-wrap';
    div.style.overflow = 'hidden';
    div.style.height = 'auto';

    const value = textarea.value;
    div.appendChild(document.createTextNode(value.slice(0, start)));
    const marker = document.createElement('span');
    marker.textContent = value.slice(start, end) || '​';
    div.appendChild(marker);
    div.appendChild(document.createTextNode(value.slice(end)));
    document.body.appendChild(div);

    const divRect = div.getBoundingClientRect();
    const taRect = textarea.getBoundingClientRect();
    const rects = [...marker.getClientRects()].map((r) => ({
      left: r.left - divRect.left + taRect.left - textarea.scrollLeft,
      top: r.top - divRect.top + taRect.top - textarea.scrollTop,
      width: r.width,
      height: r.height,
    }));
    div.remove();
    return rects;
  }

  // ---------- field tracker ----------

  class Tracker {
    constructor(el) {
      this.el = el;
      this.suggestions = [];
      this.ignored = new Set();
      this.overlay = document.createElement('div');
      this.overlay.className = 'lg-overlay';
      document.documentElement.appendChild(this.overlay);
      this.grammarTimer = null;
      this.styleTimer = null;
      this.lastChecked = '';

      this.onInput = this.onInput.bind(this);
      this.reposition = this.reposition.bind(this);
      el.addEventListener('input', this.onInput);
      window.addEventListener('scroll', this.reposition, { capture: true, passive: true });
      window.addEventListener('resize', this.reposition, { passive: true });
    }

    onInput() {
      clearTimeout(this.grammarTimer);
      clearTimeout(this.styleTimer);
      this.grammarTimer = setTimeout(() => this.check(false), GRAMMAR_DEBOUNCE);
      this.styleTimer = setTimeout(() => this.check(true), STYLE_PAUSE);
      postToSidebar({ type: 'text-update', text: getText(this.el) });
    }

    async check(includeStyle) {
      const text = getText(this.el);
      if (!text.trim() || (includeStyle === false && text === this.lastChecked)) return;
      this.lastChecked = text;
      if (includeStyle) postToSidebar({ type: 'style-loading' });

      const result = await send('check', {
        text,
        mode,
        session_id: sessionId,
        domain: location.hostname,
        include_style: includeStyle,
      });
      if (!result) return;
      if (result.session_id) sessionId = result.session_id;

      // Text may have changed while the check ran; keep only still-valid spans.
      const current = getText(this.el);
      this.suggestions = (result.suggestions || []).filter(
        (s) =>
          !this.ignored.has(s.original + '|' + s.suggestion) &&
          (s.offset == null || current.slice(s.offset, s.offset + s.length) === s.original)
      );
      this.render();
      postToSidebar({
        type: 'result',
        suggestions: this.suggestions,
        clarity_score: result.clarity_score,
        rewrite: result.rewrite,
        tone: result.tone,
        warnings: result.warnings || [],
        offline: !!result.offline,
        includeStyle,
        text: current,
      });
    }

    render() {
      this.overlay.textContent = '';
      const isTa = this.el.tagName === 'TEXTAREA';
      const elRect = this.el.getBoundingClientRect();
      for (const s of this.suggestions) {
        if (s.offset == null) continue;
        let rects = [];
        if (isTa) {
          rects = mirrorRects(this.el, s.offset, s.offset + s.length);
          // Clip to the visible textarea box.
          rects = rects.filter(
            (r) => r.top >= elRect.top - 2 && r.top + r.height <= elRect.bottom + 2
          );
        } else {
          const range = rangeForOffsets(this.el, s.offset, s.offset + s.length);
          if (range) rects = [...range.getClientRects()];
        }
        for (const r of rects) {
          if (!r.width) continue;
          const u = document.createElement('div');
          u.className = 'lg-underline';
          u.dataset.kind = s.error_type;
          u.dataset.sid = s.id;
          u.style.left = r.left + scrollX + 'px';
          u.style.top = r.top + scrollY + 'px';
          u.style.width = r.width + 'px';
          u.style.height = r.height + 'px';
          u.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            showPopup(this, s, u);
          });
          u.addEventListener('contextmenu', () => { lastContextTarget = s; });
          this.overlay.appendChild(u);
        }
      }
    }

    reposition() {
      if (this.suggestions.length) this.render();
    }

    applyFix(s, replacement) {
      const el = this.el;
      const fix = replacement ?? s.suggestion;
      if (!fix && fix !== '') return;
      if (el.tagName === 'TEXTAREA') {
        const v = el.value;
        if (v.slice(s.offset, s.offset + s.length) !== s.original) return;
        el.focus();
        el.setSelectionRange(s.offset, s.offset + s.length);
        // execCommand keeps the undo stack and fires proper input events.
        if (!document.execCommand('insertText', false, fix)) {
          el.value = v.slice(0, s.offset) + fix + v.slice(s.offset + s.length);
          el.dispatchEvent(new Event('input', { bubbles: true }));
        }
      } else {
        const range = rangeForOffsets(el, s.offset, s.offset + s.length);
        if (!range || range.toString() !== s.original) return;
        el.focus();
        const sel = getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        document.execCommand('insertText', false, fix);
      }
      this.suggestions = this.suggestions.filter((x) => x.id !== s.id);
      this.render();
      send('log-acceptance', { suggestion_id: s.id, accepted: true });
      toast('✓ Fix applied');
      // Re-check soon so remaining offsets get refreshed.
      clearTimeout(this.grammarTimer);
      this.grammarTimer = setTimeout(() => this.check(false), 400);
    }

    ignore(s) {
      this.ignored.add(s.original + '|' + s.suggestion);
      this.suggestions = this.suggestions.filter((x) => x.id !== s.id);
      this.render();
      send('log-acceptance', { suggestion_id: s.id, accepted: false });
    }

    fixAll() {
      // Apply mechanical fixes (grammar + spelling) from last to first so
      // earlier offsets stay valid.
      const fixable = this.suggestions
        .filter((s) => s.offset != null && s.suggestion && ['grammar', 'spelling'].includes(s.error_type))
        .sort((a, b) => b.offset - a.offset);
      for (const s of fixable) this.applyFix(s);
      if (fixable.length) toast(`✓ Fixed ${fixable.length} issue${fixable.length > 1 ? 's' : ''}`);
    }

    destroy() {
      this.el.removeEventListener('input', this.onInput);
      window.removeEventListener('scroll', this.reposition, { capture: true });
      window.removeEventListener('resize', this.reposition);
      this.overlay.remove();
    }
  }

  // ---------- suggestion popup ----------

  let popup = null;

  function closePopup() {
    popup?.remove();
    popup = null;
  }

  function showPopup(tracker, s, anchor) {
    closePopup();
    popup = document.createElement('div');
    popup.className = 'lg-popup';
    const badgeIcon = { grammar: '🔴', spelling: '🟡', style: '🔵', clarity: '🟢' }[s.error_type] || '🔵';
    popup.innerHTML = `
      <span class="lg-badge lg-badge-${s.error_type}">${badgeIcon} ${s.style_type || s.error_type}</span>
      <div><span class="lg-original"></span> → <span class="lg-fix"></span></div>
      <div class="lg-explain"></div>
      <div class="lg-actions">
        <button class="lg-accept">Accept</button>
        <button class="lg-ignore">Ignore</button>
        <button class="lg-why">Why?</button>
      </div>`;
    popup.querySelector('.lg-original').textContent = s.original;
    popup.querySelector('.lg-fix').textContent = s.suggestion || '(remove)';
    popup.querySelector('.lg-explain').textContent = s.explanation || '';
    popup.querySelector('.lg-accept').onclick = () => { tracker.applyFix(s); closePopup(); };
    popup.querySelector('.lg-ignore').onclick = () => { tracker.ignore(s); closePopup(); };
    popup.querySelector('.lg-why').onclick = async (e) => {
      e.target.textContent = '…';
      const resp = await send('explain', {
        error_type: s.style_type || s.error_type,
        original: s.original,
        fix: s.suggestion,
      });
      const box = document.createElement('div');
      box.className = 'lg-teacher';
      box.textContent = resp?.explanation || 'Could not reach Ollama for an explanation.';
      popup?.appendChild(box);
      e.target.textContent = 'Why?';
    };

    const r = anchor.getBoundingClientRect();
    popup.style.left = Math.min(r.left + scrollX, scrollX + innerWidth - 340) + 'px';
    popup.style.top = r.bottom + scrollY + 6 + 'px';
    document.documentElement.appendChild(popup);
    setTimeout(() => {
      document.addEventListener('mousedown', (e) => {
        if (!popup?.contains(e.target)) closePopup();
      }, { once: true });
    });
  }

  // ---------- sidebar ----------

  function ensureSidebar() {
    if (sidebarFrame) return sidebarFrame;
    sidebarFrame = document.createElement('iframe');
    sidebarFrame.id = 'lg-sidebar-frame';
    sidebarFrame.className = 'lg-hidden';
    sidebarFrame.src = chrome.runtime.getURL('sidebar.html');
    document.documentElement.appendChild(sidebarFrame);
    return sidebarFrame;
  }

  function postToSidebar(msg) {
    if (!sidebarFrame?.contentWindow) return;
    sidebarFrame.contentWindow.postMessage({ __lg: true, ...msg }, '*');
  }

  function toggleSidebar(force) {
    ensureSidebar();
    sidebarVisible = force ?? !sidebarVisible;
    sidebarFrame.classList.toggle('lg-hidden', !sidebarVisible);
    if (sidebarVisible) {
      postToSidebar({ type: 'shown', mode, domain: location.hostname });
      if (activeField) postToSidebar({ type: 'text-update', text: getText(activeField.el) });
    }
  }

  // Messages coming back from the sidebar iframe.
  window.addEventListener('message', (e) => {
    const msg = e.data;
    if (!msg || !msg.__lg_sidebar) return;
    switch (msg.type) {
      case 'accept': {
        const s = activeField?.suggestions.find((x) => x.id === msg.id);
        if (s && s.offset != null) activeField.applyFix(s);
        else if (s) { // no span (e.g. Ollama couldn't locate) — just log it
          send('log-acceptance', { suggestion_id: s.id, accepted: true });
          activeField.suggestions = activeField.suggestions.filter((x) => x.id !== s.id);
          toast('✓ Logged');
        }
        break;
      }
      case 'ignore': {
        const s = activeField?.suggestions.find((x) => x.id === msg.id);
        if (s) activeField.ignore(s);
        break;
      }
      case 'fix-all':
        activeField?.fixAll();
        break;
      case 'apply-rewrite': {
        if (!activeField) break;
        const el = activeField.el;
        el.focus();
        if (el.tagName === 'TEXTAREA') {
          el.select();
        } else {
          const range = document.createRange();
          range.selectNodeContents(el);
          const sel = getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
        document.execCommand('insertText', false, msg.text);
        toast('✓ Rewrite applied');
        break;
      }
      case 'set-mode':
        mode = msg.mode;
        chrome.storage.local.set({ ['mode:' + location.hostname]: mode });
        activeField?.check(true);
        break;
      case 'close':
        toggleSidebar(false);
        break;
    }
  });

  // ---------- wiring ----------

  document.addEventListener('focusin', (e) => {
    const el = e.target;
    if (!isEligible(el)) return;
    if (!trackers.has(el)) trackers.set(el, new Tracker(el));
    activeField = trackers.get(el);
    ensureSidebar();
    if (!sidebarVisible && getText(el).trim().length > 0) toggleSidebar(true);
  });

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === 'toggle-sidebar') toggleSidebar();
    if (msg.type === 'explain-context' && lastContextTarget && activeField) {
      // Reuse the popup flow: find an underline for the suggestion.
      const u = activeField.overlay.querySelector(`[data-sid="${lastContextTarget.id}"]`);
      if (u) {
        showPopup(activeField, lastContextTarget, u);
        popup?.querySelector('.lg-why')?.click();
      }
    }
  });

  // Restore per-domain mode (or auto-pick from the URL).
  chrome.storage.local.get('mode:' + location.hostname, (data) => {
    const saved = data['mode:' + location.hostname];
    if (saved) {
      mode = saved;
    } else {
      for (const [re, m] of MODE_BY_DOMAIN_DEFAULTS) {
        if (re.test(location.hostname)) { mode = m; break; }
      }
    }
  });

  // Clean up trackers for removed elements.
  setInterval(() => {
    for (const [el, tracker] of trackers) {
      if (!el.isConnected) {
        tracker.destroy();
        trackers.delete(el);
        if (activeField === tracker) activeField = null;
      }
    }
  }, 5000);
})();
