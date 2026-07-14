// Sidebar UI: suggestion cards, live writing metrics, and the Coach tab.
// Runs inside the extension iframe; talks to the content script via
// postMessage and to the backend via the background worker.

const $ = (sel) => document.querySelector(sel);

const FILLER_WORDS = ['very', 'really', 'just', 'that', 'thing', 'stuff', 'basically', 'literally'];
// -ly words that are NOT adverbs (or fine to keep).
const LY_WHITELIST = new Set(['only', 'family', 'early', 'likely', 'reply', 'apply', 'supply', 'italy', 'july', 'assembly', 'ally', 'belly', 'bully', 'fly', 'rally', 'monopoly', 'anomaly']);

function postToContent(msg) {
  window.parent.postMessage({ __lg_sidebar: true, ...msg }, '*');
}

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

// ---------- tabs ----------

document.querySelectorAll('.tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((b) => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach((p) => p.classList.remove('active'));
    btn.classList.add('active');
    $('#tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'coach') loadCoach();
  });
});

$('#close-btn').addEventListener('click', () => postToContent({ type: 'close' }));
$('#mode-select').addEventListener('change', (e) => postToContent({ type: 'set-mode', mode: e.target.value }));
$('#fix-all').addEventListener('click', () => postToContent({ type: 'fix-all' }));

// ---------- suggestions rendering ----------

let currentRewrite = null;

function renderSuggestions(suggestions) {
  const list = $('#suggestion-list');
  list.textContent = '';
  const count = suggestions.length;
  $('#sugg-count').textContent = count
    ? `${count} suggestion${count > 1 ? 's' : ''}`
    : 'No issues found ✨';
  $('#fix-all').hidden = !suggestions.some(
    (s) => s.offset != null && s.suggestion && ['grammar', 'spelling'].includes(s.error_type)
  );

  const icons = { grammar: '🔴', spelling: '🟡', style: '🔵', clarity: '🟢' };
  for (const s of suggestions) {
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <span class="badge badge-${s.error_type}">${icons[s.error_type] || '🔵'} ${s.style_type || s.error_type}</span>
      <div><span class="orig"></span>${s.suggestion ? ' → ' : ''}<span class="fix"></span></div>
      <div class="why"></div>
      <div class="row">
        <button class="accept">Accept</button>
        <button class="ignore">Ignore</button>
      </div>`;
    card.querySelector('.orig').textContent = s.original;
    card.querySelector('.fix').textContent = s.suggestion || '';
    card.querySelector('.why').textContent = s.explanation || '';
    card.querySelector('.accept').addEventListener('click', () => {
      postToContent({ type: 'accept', id: s.id });
      card.remove();
      bumpCount(-1);
    });
    card.querySelector('.ignore').addEventListener('click', () => {
      postToContent({ type: 'ignore', id: s.id });
      card.remove();
      bumpCount(-1);
    });
    list.appendChild(card);
  }
}

function bumpCount(delta) {
  const cards = document.querySelectorAll('#suggestion-list .card').length;
  $('#sugg-count').textContent = cards ? `${cards} suggestion${cards > 1 ? 's' : ''}` : 'No issues found ✨';
}

function renderClarity(score) {
  const row = $('#clarity-row');
  if (score == null) { row.hidden = true; return; }
  row.hidden = false;
  const pct = score * 10;
  const fill = $('#clarity-fill');
  fill.style.width = pct + '%';
  fill.style.background = score >= 7 ? '#30a46c' : score >= 4 ? '#f5b301' : '#e5484d';
  $('#clarity-num').textContent = score + '/10';
}

function renderTone(tone) {
  const badge = $('#tone-badge');
  if (!tone) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.textContent = tone;
  badge.className = 'tone-badge ' + tone.toLowerCase();
  if (tone.toLowerCase() === 'hesitant') {
    badge.title = 'Your writing sounds hesitant — cut hedges like "I think" and "maybe".';
  }
}

function renderRewrite(text) {
  currentRewrite = text;
  const box = $('#rewrite-box');
  box.hidden = !text;
  if (text) $('#rewrite-text').textContent = text;
}

$('#rewrite-toggle').addEventListener('click', () => {
  const t = $('#rewrite-text');
  const showing = !t.hidden;
  t.hidden = showing;
  $('#rewrite-apply').hidden = showing;
  $('#rewrite-toggle').textContent = showing ? 'Show full rewrite ▾' : 'Hide rewrite ▴';
});
$('#rewrite-apply').addEventListener('click', () => {
  if (currentRewrite) postToContent({ type: 'apply-rewrite', text: currentRewrite });
});

function renderWarnings(warnings, offline) {
  const box = $('#warnings');
  box.textContent = '';
  const all = [...(warnings || [])];
  if (offline) all.unshift('⚠ Backend offline — run start.bat to enable checking.');
  for (const w of all) {
    const div = document.createElement('div');
    div.className = 'warn';
    div.textContent = '⚠ ' + w.replace(/^⚠ /, '');
    box.appendChild(div);
  }
}

// ---------- writing metrics ----------

function countSyllables(word) {
  word = word.toLowerCase().replace(/[^a-z]/g, '');
  if (!word) return 0;
  if (word.length <= 3) return 1;
  word = word.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, '').replace(/^y/, '');
  return Math.max(1, (word.match(/[aeiouy]{1,2}/g) || []).length);
}

function computeMetrics(text) {
  const words = text.match(/[A-Za-z''-]+/g) || [];
  const sentences = text.split(/[.!?]+["')\]]*\s/).filter((s) => s.trim().split(/\s+/).length > 1);
  const sentenceCount = Math.max(1, sentences.length);
  const wordCount = words.length;
  if (!wordCount) return null;

  const avgSentence = wordCount / sentenceCount;
  const longSentences = sentences.filter((s) => s.trim().split(/\s+/).length > 25).length;

  // Passive voice heuristic: be-verb + past participle.
  const passiveRe = /\b(am|is|are|was|were|be|been|being)\s+(\w+ed|\w+en|born|made|done|said|seen|known|given|taken|found|held|kept|left|lost|paid|sent|sold|told|won|built|shown)\b/gi;
  const passiveHits = (text.match(passiveRe) || []).length;
  const passivePct = Math.round((passiveHits / sentenceCount) * 100);

  const adverbs = words.filter(
    (w) => /ly$/i.test(w) && w.length > 4 && !LY_WHITELIST.has(w.toLowerCase())
  ).length;

  const syllables = words.reduce((n, w) => n + countSyllables(w), 0);
  const grade = 0.39 * (wordCount / sentenceCount) + 11.8 * (syllables / wordCount) - 15.59;

  const unique = new Set(words.map((w) => w.toLowerCase())).size;
  const uniqueRatio = Math.round((unique / wordCount) * 100);

  const fillers = {};
  for (const f of FILLER_WORDS) {
    const n = (text.match(new RegExp(`\\b${f}\\b`, 'gi')) || []).length;
    if (n) fillers[f] = n;
  }

  return {
    wordCount, avgSentence: Math.round(avgSentence * 10) / 10, longSentences,
    passivePct, adverbs, grade: Math.max(0, Math.round(grade * 10) / 10),
    uniqueRatio, fillers,
  };
}

function renderMetrics(text) {
  const m = computeMetrics(text || '');
  $('#metrics-empty').hidden = !!m;
  $('#metrics-list').hidden = !m;
  if (!m) return;

  const fillerTotal = Object.values(m.fillers).reduce((a, b) => a + b, 0);
  const fillerDetail = Object.entries(m.fillers).map(([w, n]) => `${w}×${n}`).join(', ');

  const rows = [
    ['Words', m.wordCount, '', ''],
    ['Avg sentence length', m.avgSentence + ' words', m.avgSentence > 25 ? 'bad' : '', m.longSentences ? `${m.longSentences} sentence(s) over 25 words` : 'Target: under 25'],
    ['Passive voice', m.passivePct + '%', m.passivePct > 15 ? 'flag' : '', 'Target: under 15%'],
    ['Adverbs', m.adverbs, m.adverbs > Math.max(3, m.wordCount / 50) ? 'flag' : '', 'Adverbs often weaken writing'],
    ['Reading grade level', m.grade, m.grade > 14 ? 'flag' : '', 'Flesch-Kincaid'],
    ['Unique word ratio', m.uniqueRatio + '%', m.uniqueRatio < 40 ? 'flag' : '', 'Vocabulary diversity'],
    ['Filler words', fillerTotal, fillerTotal > 2 ? 'flag' : '', fillerDetail || 'None found 🎉'],
  ];

  const list = $('#metrics-list');
  list.textContent = '';
  for (const [name, val, cls, sub] of rows) {
    const div = document.createElement('div');
    div.className = 'metric';
    div.innerHTML = `<span class="name"></span><span class="val ${cls}"><span class="v"></span><span class="sub"></span></span>`;
    div.querySelector('.name').textContent = name;
    div.querySelector('.v').textContent = val;
    div.querySelector('.sub').textContent = sub;
    list.appendChild(div);
  }
}

// ---------- coach tab ----------

let coachLoaded = false;

async function loadCoach(force = false) {
  if (coachLoaded && !force) return;
  const [stats, history, tip] = await Promise.all([
    send('stats'), send('history'), coachLoaded ? null : send('coach-tip'),
  ]);
  coachLoaded = true;

  if (stats && !stats.offline) {
    $('#improve-score').textContent = Math.round(stats.improvement_score);
    $('#score-detail').textContent =
      `${stats.accepted} fixes accepted · ${Math.round(stats.acceptance_rate * 100)}% acceptance · ${stats.streak_days}-day streak`;
  }
  if (tip && tip.tip) $('#coach-tip').textContent = tip.tip;

  const ol = $('#top-mistakes');
  ol.textContent = '';
  const mistakes = history?.top_mistakes || [];
  if (!mistakes.length) {
    ol.innerHTML = '<li class="muted">No data yet — keep writing!</li>';
  } else {
    for (const m of mistakes) {
      const li = document.createElement('li');
      li.innerHTML = `<span class="type"></span> — <span class="count"></span> times`;
      li.querySelector('.type').textContent = m.error_type;
      li.querySelector('.count').textContent = m.n;
      ol.appendChild(li);
    }
  }

  const ws = history?.weekly_summary;
  if (ws) {
    $('#weekly-summary').textContent = ws.fixed_this_week
      ? `This week you fixed ${ws.fixed_this_week} issue${ws.fixed_this_week > 1 ? 's' : ''}. Your most common error: ${ws.most_common_error}.`
      : 'No fixes recorded this week yet. Accept a few suggestions to start tracking.';
  }
}

// ---------- messages from content script ----------

window.addEventListener('message', (e) => {
  const msg = e.data;
  if (!msg || !msg.__lg) return;
  switch (msg.type) {
    case 'shown':
      $('#mode-select').value = msg.mode;
      break;
    case 'style-loading':
      $('#style-loading').hidden = false;
      break;
    case 'result':
      $('#style-loading').hidden = true;
      renderSuggestions(msg.suggestions || []);
      renderWarnings(msg.warnings, msg.offline);
      if (msg.includeStyle) {
        renderClarity(msg.clarity_score);
        renderTone(msg.tone);
        renderRewrite(msg.rewrite);
      }
      renderMetrics(msg.text);
      coachLoaded = false; // stats changed; refresh next time Coach opens
      break;
    case 'text-update':
      renderMetrics(msg.text);
      break;
  }
});

// Initial health check so the user sees engine status immediately.
send('health').then((h) => {
  if (!h || h.offline) {
    renderWarnings([], true);
  } else {
    const warns = [];
    if (!h.languagetool) warns.push('LanguageTool is down — grammar checks off. Is Docker running?');
    if (!h.ollama) warns.push('Ollama is down — style coaching off.');
    renderWarnings(warns, false);
  }
});
