'use strict';

// ── Select-all checkbox & bulk bar ────────────────────────────────────────────
const selectAll = document.getElementById('selectAll');
const bulkBar   = document.getElementById('bulkBar');
const selCount  = document.getElementById('selCount');

function updateBulk() {
  const checks = Array.from(document.querySelectorAll('.row-check:checked'));
  if (selCount) selCount.textContent = checks.length;
  if (bulkBar)  bulkBar.style.display = checks.length ? 'flex' : 'none';

  const dlSingle = document.getElementById('dlSingle');
  const dlZip    = document.getElementById('dlZip');
  if (dlSingle && dlZip) {
    const paths = checks.map(c => encodeURIComponent(c.value));
    if (paths.length === 1) {
      dlSingle.href = '/download?path=' + paths[0];
      dlSingle.style.display = '';
      dlZip.style.display    = 'none';
    } else if (paths.length > 1) {
      dlZip.href = '/download-zip?' + paths.map(p => 'path=' + p).join('&');
      dlZip.style.display    = '';
      dlSingle.style.display = 'none';
    }
  }
}

function bindRowChecks() {
  document.querySelectorAll('.row-check').forEach(c => {
    c.addEventListener('change', () => {
      if (selectAll && !c.checked) selectAll.checked = false;
      updateBulk();
    });
  });
}

if (selectAll) {
  selectAll.addEventListener('change', () => {
    document.querySelectorAll('.row-check').forEach(c => { c.checked = selectAll.checked; });
    updateBulk();
  });
}
bindRowChecks();



// ── Live tail (viewer page) ───────────────────────────────────────────────────
let _tailEs    = null;
let _tailLines = 0;

function toggleTail(encodedPath) {
  const btn        = document.getElementById('tailBtn');
  const staticView = document.getElementById('staticView');
  const tailView   = document.getElementById('tailView');

  if (_tailEs) {
    // Stop
    _tailEs.close();
    _tailEs = null;
    btn.textContent = '🔴 Live Tail';
    btn.classList.remove('btn-active');
    tailView.style.display  = 'none';
    staticView.style.display = '';
    return;
  }

  // Start
  btn.textContent = '⏹ Stop Tail';
  btn.classList.add('btn-active');
  staticView.style.display = 'none';
  tailView.style.display   = '';

  const pre    = document.getElementById('tailPre');
  const status = document.getElementById('tailStatus');
  const count  = document.getElementById('tailLineCount');
  _tailLines   = 0;
  pre.textContent = '';

  _tailEs = new EventSource('/tail?path=' + encodedPath);

  _tailEs.onopen = () => { status.textContent = 'Connected — following ' + decodeURIComponent(encodedPath).split('\\').pop(); };

  _tailEs.onmessage = (e) => {
    _tailLines++;
    if (count) count.textContent = `(${_tailLines} lines)`;
    const line = document.createTextNode(e.data + '\n');
    pre.appendChild(line);
    if (document.getElementById('tailAutoScroll')?.checked) {
      pre.parentElement.scrollTop = pre.parentElement.scrollHeight;
    }
  };

  _tailEs.addEventListener('reset', () => {
    status.textContent = 'File was truncated — restarted';
    pre.textContent = '';
    _tailLines = 0;
  });

  _tailEs.addEventListener('error', (e) => {
    status.textContent = 'Error: ' + (e.data || 'connection lost');
    _tailEs.close();
    _tailEs = null;
    btn.textContent = '🔴 Live Tail';
    btn.classList.remove('btn-active');
  });

  _tailEs.onerror = () => {
    if (_tailEs && _tailEs.readyState === EventSource.CLOSED) {
      status.textContent = 'Connection closed';
    }
  };
}

function applyWrap() {
  const pre = document.getElementById('tailPre');
  if (!pre) return;
  pre.style.whiteSpace = document.getElementById('tailWrap')?.checked ? 'pre-wrap' : 'pre';
}

// ── Find-in-file (viewer page) ────────────────────────────────────────────────
const logPre = document.getElementById('logPre');
let _origHTML = null;   // saved before any highlight mutations
let _findIdx  = 0;

function _saveOrig() {
  if (logPre && _origHTML === null) _origHTML = logPre.innerHTML;
}

function _escapeTerm(t) {
  return t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function _applyHighlight(term) {
  if (!logPre) return 0;
  _saveOrig();
  if (!term) { logPre.innerHTML = _origHTML; return 0; }
  logPre.innerHTML = _origHTML.replace(
    new RegExp(`(${_escapeTerm(term)})`, 'gi'),
    '<mark>$1</mark>'
  );
  return logPre.querySelectorAll('mark').length;
}

function findInFile() {
  const term  = document.getElementById('findInput')?.value || '';
  const count = _applyHighlight(term);
  const info  = document.getElementById('findCount');
  _findIdx    = 0;
  if (info) info.textContent = count ? `1 / ${count}` : (term ? 'No matches' : '');
  const marks = logPre?.querySelectorAll('mark') || [];
  marks.forEach(m => m.classList.remove('mark-current'));
  if (marks[0]) { marks[0].classList.add('mark-current'); marks[0].scrollIntoView({ block: 'nearest' }); }

  const link = document.getElementById('findSearchAll');
  if (link) link.href = `/search?q=${encodeURIComponent(term)}&group=1`;
}

function findNav(dir) {
  const marks = logPre?.querySelectorAll('mark') || [];
  if (!marks.length) return;
  marks[_findIdx]?.classList.remove('mark-current');
  _findIdx = (_findIdx + dir + marks.length) % marks.length;
  marks[_findIdx].classList.add('mark-current');
  marks[_findIdx].scrollIntoView({ block: 'nearest' });
  const info = document.getElementById('findCount');
  if (info) info.textContent = `${_findIdx + 1} / ${marks.length}`;
}

function findBarKey(e) {
  if (e.key === 'Enter') { e.preventDefault(); findNav(e.shiftKey ? -1 : 1); }
  if (e.key === 'Escape') closeFindBar();
}

function openFindBar() {
  const bar = document.getElementById('findBar');
  if (!bar) return;
  bar.style.display = 'flex';
  const inp = document.getElementById('findInput');
  if (inp) { inp.focus(); inp.select(); }
}

function closeFindBar() {
  const bar = document.getElementById('findBar');
  if (bar) bar.style.display = 'none';
  if (logPre && _origHTML !== null) logPre.innerHTML = _origHTML;
  const info = document.getElementById('findCount');
  if (info) info.textContent = '';
}

// Intercept Ctrl+F on the viewer page
if (logPre) {
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
      const bar = document.getElementById('findBar');
      if (bar) { e.preventDefault(); openFindBar(); }
    }
  });
}

// ── FAIL line highlighting & summary (viewer page) ────────────────────────────
(function () {
  if (!logPre) return;

  function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  const lines    = logPre.textContent.split('\n');
  const failLines = [];
  const html = lines.map((line, i) => {
    const esc = escHtml(line);
    if (/fail/i.test(line)) {
      failLines.push({ num: i + 1, text: line.trim() });
      return `<span class="line-fail">${esc}</span>`;
    }
    if (/pass/i.test(line)) {
      return `<span class="line-pass">${esc}</span>`;
    }
    return esc;
  }).join('\n');

  logPre.innerHTML = html;
  _origHTML = logPre.innerHTML; // seed so find-in-file restores to highlighted state

  if (!failLines.length) return;

  const MAX = 30;
  const shown = failLines.slice(0, MAX);
  const extra = failLines.length - shown.length;

  const panel = document.createElement('div');
  panel.className = 'fail-summary';
  panel.innerHTML =
    `<div class="fail-summary-hdr">` +
    `❌ <strong>${failLines.length}</strong> FAIL line${failLines.length !== 1 ? 's' : ''} on this page` +
    `<button class="fail-summary-toggle" onclick="this.closest('.fail-summary').classList.toggle('collapsed')">▲</button>` +
    `</div>` +
    `<ul class="fail-summary-list">` +
    shown.map(f =>
      `<li><span class="fail-linenum">L${f.num}</span><span class="fail-line-text">${escHtml(f.text)}</span></li>`
    ).join('') +
    (extra ? `<li class="fail-more">…and ${extra} more on this page</li>` : '') +
    `</ul>`;

  const logWrap = logPre.closest('.log-wrap');
  if (logWrap) logWrap.before(panel);
})();

// Auto-open find bar if hl param set (from search/grep links)
if (logPre) {
  const hlTerm = logPre.dataset.hl || '';
  if (hlTerm) {
    openFindBar();
    const inp = document.getElementById('findInput');
    if (inp) { inp.value = hlTerm; findInFile(); }
  }
}

// ── Infinite scroll (browse page) ────────────────────────────────────────────
(function () {
  const sentinel = document.getElementById('scrollSentinel');
  if (!sentinel) return;

  let currentPage = 1;
  let totalPages  = parseInt(sentinel.dataset.total || '1', 10);
  let loading     = false;
  let observer    = null;

  function makeObserver() {
    return new IntersectionObserver(
      entries => { if (entries[0].isIntersecting) loadNextPage(); },
      { rootMargin: '300px' }
    );
  }

  function stopObserving() {
    if (observer) { observer.disconnect(); observer = null; }
  }

  async function loadNextPage() {
    if (loading || currentPage >= totalPages) { stopObserving(); return; }
    loading = true;
    document.getElementById('loadingMore').style.display = '';

    const params = new URLSearchParams(window.location.search);
    params.set('page', ++currentPage);

    try {
      const resp = await fetch('/browse/rows?' + params.toString());
      if (!resp.ok) throw new Error(resp.status);
      const html  = await resp.text();
      const tbody = document.getElementById('fileTableBody');
      if (tbody) {
        tbody.insertAdjacentHTML('beforeend', html);
        bindRowChecks();
        // If select-all is active, check newly loaded rows too
        if (selectAll && selectAll.checked) {
          tbody.querySelectorAll('.row-check').forEach(c => { c.checked = true; });
          updateBulk();
        }
      }
    } catch (_) {
      currentPage--; // back off; next scroll will retry
    }

    document.getElementById('loadingMore').style.display = 'none';
    loading = false;
    if (currentPage >= totalPages) stopObserving();
  }

  window._resetInfiniteScroll = function () {
    currentPage = 1;
    totalPages  = parseInt(sentinel.dataset.total || '1', 10);
    stopObserving();
    if (currentPage < totalPages) {
      observer = makeObserver();
      observer.observe(sentinel);
    }
  };

  observer = makeObserver();
  observer.observe(sentinel);
})();

// ── Grep: search within selected files ────────────────────────────────────────
function submitGrep() {
  const q = document.getElementById('grepQ')?.value?.trim();
  if (!q) return;
  const checks = Array.from(document.querySelectorAll('.row-check:checked'));
  if (!checks.length) { alert('Select at least one file first.'); return; }
  const form = document.createElement('form');
  form.method = 'POST';
  form.action = '/grep';
  const qi = document.createElement('input');
  qi.type = 'hidden'; qi.name = 'q'; qi.value = q;
  form.appendChild(qi);
  // Send all paths as a single JSON field to avoid multipart field-count limits
  const pj = document.createElement('input');
  pj.type = 'hidden'; pj.name = 'paths_json';
  pj.value = JSON.stringify(checks.map(c => c.value));
  form.appendChild(pj);
  document.body.appendChild(form);
  form.submit();
}
