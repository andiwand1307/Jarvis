(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const AREA_LABELS = {
    todoist: 'Todoist', mail: 'Mail', email: 'Mail', homeassistant: 'Home Assistant', home_assistant: 'Home Assistant',
    ha: 'Home Assistant', memory: 'Gedächtnis', web: 'Websuche', calendar: 'Kalender',
  };
  const KIND_LABELS = {
    mail_send: 'Mail senden', mail: 'Mail senden', ha_service: 'Home-Assistant-Aktion', phone_call: 'Anruf',
  };
  const FIELD_LABELS = {
    to: 'An', cc: 'Cc', bcc: 'Bcc', from: 'Von', subject: 'Betreff', body: 'Text', text: 'Text', message: 'Nachricht',
    domain: 'Bereich', service: 'Dienst', entity_id: 'Gerät', data: 'Daten', number: 'Nummer', in_reply_to: 'Antwort auf',
  };
  const MAX_LEN = 20000;
  const KEY_BANNER = 'Kein Claude-Schlüssel eingetragen. Trag ihn in der Add-on-Konfiguration ein.';
  // Protokoll-Status laut jarvis_core (agent.py): Kennzeichen immer mit Symbol und Text, nicht nur Farbe.
  const LOG_STATUS = {
    ok: { label: 'Erfolgreich', cls: 'good', sym: '✓' },
    approved: { label: 'Freigegeben', cls: 'good', sym: '✓' },
    rejected: { label: 'Verworfen', cls: 'off', sym: '–' },
    pending: { label: 'Wartet auf Freigabe', cls: 'off', sym: '…' },
    cancelled: { label: 'Abgebrochen', cls: 'off', sym: '■' },
    interrupted: { label: 'Unterbrochen (Neustart)', cls: 'warn', sym: '!' },
    error: { label: 'Fehler', cls: 'bad', sym: '✕' },
    invalid: { label: 'Ungültige Eingabe', cls: 'bad', sym: '✕' },
    timeout: { label: 'Zeitüberschreitung', cls: 'bad', sym: '✕' },
    approved_failed: { label: 'Freigegeben, fehlgeschlagen', cls: 'bad', sym: '✕' },
    approved_unknown: { label: 'Freigegeben, Ergebnis unbekannt', cls: 'warn', sym: '!' },
  };
  const RISK_LABELS = { read: 'lesend', write: 'schreibend', confirm: 'mit Freigabe' };
  const LONG_FIELDS = new Set(['body', 'text', 'message']);

  const state = {
    conversationId: null,
    streaming: false,
    controller: null,
    pending: new Map(), // id -> {node, ...}
    areasConfigured: null,
    keyMissing: false,
    notice: null,
    run: null,
    runningIds: new Set(),
    pollTimer: 0,
    syncSeq: 0,
    tooLong: false,
  };

  /* ---------- Helfer ---------- */
  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { Accept: 'application/json' }, cache: 'no-store' }, opts));
  }
  async function jsonOrThrow(res) {
    if (!res.ok) {
      let msg = 'Fehler ' + res.status;
      try {
        const j = await res.json();
        if (j) {
          const d = j.detail != null ? j.detail : (j.message || j.error);
          if (typeof d === 'string') msg = d;
          else if (Array.isArray(d)) msg = d.map((x) => (x && x.msg) || '').filter(Boolean).join(' ') || msg;
        }
      } catch (_) { /* ignore */ }
      if (res.status === 401) msg = 'Kein Zugang. Öffne Jarvis über den Eintrag in der Home-Assistant-Seitenleiste (Ingress) oder über den Link mit Zugangsschlüssel.';
      else if (res.status === 403) msg = 'Zugriff verweigert.';
      const e = new Error(msg); e.status = res.status; throw e;
    }
    return res.json();
  }
  function netMessage(e) {
    if (e && e.name === 'AbortError') return 'Abgebrochen.';
    if (e && e.status) return e.message;
    return 'Jarvis ist nicht erreichbar. Prüfe, ob das Add-on läuft.';
  }
  function parseTime(v) {
    if (v == null || v === '') return null;
    const d = typeof v === 'number' ? new Date(v < 1e11 ? v * 1000 : v) : new Date(v);
    return isNaN(d) ? null : d;
  }
  function fmtTime(v) {
    const d = parseTime(v);
    if (!d) return '';
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const opts = sameDay ? { hour: '2-digit', minute: '2-digit' }
      : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' };
    return new Intl.DateTimeFormat('de-DE', opts).format(d);
  }
  function areaLabel(a) { return AREA_LABELS[String(a).toLowerCase()] || String(a); }
  function stringify(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    if (Array.isArray(v) && v.every((x) => typeof x !== 'object')) return v.join(', ');
    try { return JSON.stringify(v, null, 2); } catch (_) { return String(v); }
  }

  /* ---------- Markdown (DOM-basiert, ohne innerHTML) ---------- */
  function safeUrl(u) { return /^(https?:\/\/|mailto:)/i.test(u) ? u : null; }
  function inline(parent, text) {
    const re = /(`+)([\s\S]+?)\1|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|(?<![\w*])\*([^\s*][^*]*?)\*(?![\w*])|(?<![\w_])_([^\s_][^_]*?)_(?![\w_])|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      if (m[2] != null) parent.appendChild(el('code', null, m[2].trim()));
      else if (m[3] != null || m[4] != null) { const s = el('strong'); inline(s, m[3] != null ? m[3] : m[4]); parent.appendChild(s); }
      else if (m[5] != null || m[6] != null) { const s = el('em'); inline(s, m[5] != null ? m[5] : m[6]); parent.appendChild(s); }
      else if (m[7] != null) {
        const href = safeUrl(m[8]);
        if (href) parent.appendChild(link(href, m[7]));
        else parent.appendChild(document.createTextNode(m[0]));
      } else if (m[9] != null) parent.appendChild(link(m[9], m[9]));
      last = re.lastIndex;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }
  function link(href, label) {
    const a = el('a', null, label);
    a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer nofollow';
    return a;
  }
  function renderMarkdown(src, into) {
    into.textContent = '';
    into.classList.add('md');
    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    let i = 0;
    const isBlank = (l) => /^\s*$/.test(l);
    const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
    while (i < lines.length) {
      const line = lines[i];
      if (isBlank(line)) { i++; continue; }
      let m;
      if ((m = /^```+\s*(\S*)\s*$/.exec(line))) {
        const buf = []; i++;
        while (i < lines.length && !/^```+\s*$/.test(lines[i])) buf.push(lines[i++]);
        i++;
        const pre = el('pre'); pre.appendChild(el('code', null, buf.join('\n'))); into.appendChild(pre);
        continue;
      }
      if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
        const h = el(m[1].length <= 2 ? 'h4' : 'h5'); inline(h, m[2]); into.appendChild(h); i++; continue;
      }
      if (/^\s*([-*_])\1\1+\s*$/.test(line)) { into.appendChild(el('hr')); i++; continue; }
      if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
        const q = el('blockquote'); inline(q, buf.join('\n')); into.appendChild(q); continue;
      }
      if ((m = listRe.exec(line))) {
        const ordered = /\d/.test(m[2]);
        const list = el(ordered ? 'ol' : 'ul');
        while (i < lines.length && (m = listRe.exec(lines[i])) && /\d/.test(m[2]) === ordered) {
          const li = el('li'); let text = m[3]; i++;
          while (i < lines.length && !isBlank(lines[i]) && !listRe.test(lines[i]) && /^\s{2,}\S/.test(lines[i])) text += '\n' + lines[i++].trim();
          inline(li, text); list.appendChild(li);
          while (i < lines.length && isBlank(lines[i]) && i + 1 < lines.length && listRe.test(lines[i + 1])) i++;
        }
        into.appendChild(list); continue;
      }
      const buf = [];
      while (i < lines.length && !isBlank(lines[i]) && !/^```/.test(lines[i]) && !/^#{1,6}\s/.test(lines[i]) && !/^>\s?/.test(lines[i]) && !listRe.test(lines[i])) buf.push(lines[i++]);
      if (!buf.length) { buf.push(lines[i++]); }
      const p = el('p'); inline(p, buf.join('\n')); p.style.whiteSpace = 'pre-line'; into.appendChild(p);
    }
  }

  /* ---------- Icons ---------- */
  const NS = 'http://www.w3.org/2000/svg';
  function icon(d) {
    const s = document.createElementNS(NS, 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true');
    const p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); s.appendChild(p); return s;
  }
  const ICONS = { running: 'M12 3a9 9 0 1 0 9 9', done: 'M5 12.5l4.5 4.5L19 7.5', error: 'M6 6l12 12M18 6L6 18' };

  /* ---------- Gespräch ---------- */
  const scroller = $('scroller'), messagesEl = $('messages'), input = $('input'), sendBtn = $('sendBtn');
  function nearBottom() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 140; }
  function toBottom(force) { if (force || nearBottom()) scroller.scrollTop = scroller.scrollHeight; }

  function updateEmpty() {
    $('empty').hidden = messagesEl.children.length > 0;
    $('noAreas').hidden = state.areasConfigured !== false;
  }
  function addUser(text, when) {
    const m = el('div', 'msg user');
    m.appendChild(el('div', 'bubble', text));
    const t = fmtTime(when); if (t) m.appendChild(el('span', 'time', t));
    messagesEl.appendChild(m); updateEmpty(); return m;
  }
  function addAssistant(text, when) {
    const m = el('div', 'msg assistant');
    const tools = el('div', 'tools'); tools.hidden = true;
    const body = el('div', 'body');
    m.append(tools, body);
    m._tools = tools; m._body = body; m._text = '';
    if (text) { m._text = text; renderMarkdown(text, body); }
    const t = fmtTime(when); if (t) m.appendChild(el('span', 'time', t));
    messagesEl.appendChild(m); updateEmpty(); return m;
  }
  function setTool(m, ev) {
    m._chips = m._chips || [];
    let chip = null;
    if (ev.status !== 'running') chip = m._chips.find((c) => c._name === ev.name && c.classList.contains('running'));
    if (!chip) { chip = el('span', 'chip'); chip._name = ev.name; m._chips.push(chip); m._tools.appendChild(chip); m._tools.hidden = false; }
    const status = ev.status === 'done' || ev.status === 'error' ? ev.status : 'running';
    chip.className = 'chip ' + status;
    chip.textContent = '';
    chip.append(icon(ICONS[status]), el('span', null, ev.label || ev.name || 'Werkzeug'));
    const sr = el('span', 'sr-only', status === 'running' ? ' (läuft)' : status === 'done' ? ' (fertig)' : ' (Fehler)');
    chip.appendChild(sr);
  }
  function showError(m, message, retryText) {
    const box = el('div', 'errbox');
    box.appendChild(el('span', null, message));
    if (retryText) {
      const b = el('button', 'btn small', 'Erneut senden'); b.type = 'button';
      b.addEventListener('click', () => { if (state.conversationId !== retryText.cid) return; box.remove(); send(retryText.text, { retry: true, cid: retryText.cid }); });
      box.appendChild(b);
    }
    m.appendChild(box);
  }

  function updateSend() {
    const blocked = !state.streaming && (state.keyMissing || state.tooLong);
    sendBtn.disabled = blocked;
    sendBtn.setAttribute('aria-label', state.streaming ? 'Stoppen' : 'Senden');
  }
  function setBusy(busy) {
    state.streaming = busy;
    $('mark').classList.toggle('busy', busy);
    sendBtn.classList.toggle('stop', busy);
    const svg = $('sendIcon'); svg.firstElementChild.setAttribute('d', busy ? 'M7 7h10v10H7z' : 'M12 19V5M5.5 11.5L12 5l6.5 6.5');
    updateSend();
  }
  let hintTimer = 0;
  function busyHint() {
    const h = $('busyHint'); h.hidden = false; h.textContent = 'Jarvis antwortet noch. Mit dem Stopp-Knopf kannst du abbrechen.';
    clearTimeout(hintTimer); hintTimer = setTimeout(() => { h.hidden = true; }, 3500);
  }
  function rememberConversation() {
    try { if (state.conversationId) sessionStorage.setItem('jarvis.cid', state.conversationId); else sessionStorage.removeItem('jarvis.cid'); } catch (_) { /* ignore */ }
  }

  async function* sseEvents(stream) {
    const reader = stream.getReader(); const dec = new TextDecoder('utf-8'); let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      buf += dec.decode(value || new Uint8Array(), { stream: !done });
      buf = buf.replace(/\r\n?/g, '\n');
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        let type = 'message'; const data = [];
        for (const l of block.split('\n')) {
          if (l.startsWith(':')) continue;
          if (l.startsWith('event:')) type = l.slice(6).trim();
          else if (l.startsWith('data:')) data.push(l.slice(5).replace(/^ /, ''));
        }
        if (data.length) { let j = null; try { j = JSON.parse(data.join('\n')); } catch (_) { j = { raw: data.join('\n') }; } yield { type, data: j }; }
      }
      if (done) return;
    }
  }

  /* Gespräch vom Server anzeigen (die Wahrheit liegt im Backend, nicht im Stream). */
  function clearNotice() { if (state.notice) { state.notice.remove(); state.notice = null; } }
  function stopPolling() { clearTimeout(state.pollTimer); state.pollTimer = 0; state.syncSeq++; }
  function renderConversation(c) {
    clearNotice();
    messagesEl.textContent = '';
    for (const msg of c.messages || []) {
      if (msg.role === 'user') addUser(msg.text, msg.created_at);
      else if (msg.role === 'assistant') addAssistant(msg.text, msg.created_at);
    }
    updateEmpty();
  }
  function addNotice(kind, text, buttons) {
    clearNotice();
    const box = el('div', 'errbox notice-box ' + kind);
    box.setAttribute('role', 'status');
    box.appendChild(el('span', null, text));
    for (const [label, fn, cls] of buttons || []) {
      const b = el('button', 'btn small' + (cls ? ' ' + cls : ''), label); b.type = 'button';
      b.addEventListener('click', fn); box.appendChild(b);
    }
    messagesEl.appendChild(box); state.notice = box; return box;
  }
  async function cancelConversation(cid) {
    if (!cid) return null;
    try { return await jsonOrThrow(await api('api/conversations/' + encodeURIComponent(cid) + '/cancel', { method: 'POST' })); }
    catch (e) { return { error: netMessage(e) }; }
  }
  async function stopAndSync(cid) { await cancelConversation(cid); await syncConversation(cid, { stopped: true }); }

  // Lädt Gespräch `cid` und zeigt den echten Stand. Läuft es noch, wird alle 2 s nachgeladen, bis es fertig ist.
  // Wirkt nur, solange `cid` das angezeigte Gespräch ist und kein neuerer Abgleich gestartet wurde.
  // opts.sent: gesendeter Text (Prüfung, ob er ankam); opts.stopped / opts.busy: Texte.
  async function syncConversation(cid, opts) {
    opts = opts || {};
    clearTimeout(state.pollTimer);
    const token = opts.token || ++state.syncSeq;
    const alive = () => state.syncSeq === token && state.conversationId === cid;
    if (!cid) {
      if (state.conversationId) return null;
      addNotice('warn', 'Die Verbindung brach ab, bevor Jarvis geantwortet hat. Ob die Nachricht angekommen ist, ist unklar. Sieh in der Gesprächsliste nach, bevor du sie erneut sendest.',
        [['Gesprächsliste aktualisieren', loadConversations]]);
      return null;
    }
    const stick = !opts.token || nearBottom();
    let c;
    try { c = await jsonOrThrow(await api('api/conversations/' + encodeURIComponent(cid))); }
    catch (e) {
      if (e.status === 404 && alive()) { state.conversationId = null; rememberConversation(); messagesEl.textContent = ''; updateEmpty(); return null; }
      if (alive()) addNotice('warn', 'Der Stand konnte nicht geladen werden: ' + netMessage(e),
        [['Stand aktualisieren', () => syncConversation(cid, opts)]]);
      return null;
    }
    if (!alive()) return null;
    renderConversation(c);
    const msgs = c.messages || [];
    const lastUser = [...msgs].reverse().find((m) => m.role === 'user');
    const arrived = !opts.sent || !!(lastUser && lastUser.text.trim() === opts.sent.trim());
    if (c.running) {
      state.runningIds.add(cid);
      const text = opts.busy ? 'In diesem Gespräch läuft gerade schon eine Anfrage.'
        : opts.stopped ? 'Wird angehalten. Ein laufendes Werkzeug wird noch beendet.' : 'Jarvis arbeitet noch an dieser Anfrage.';
      addNotice('warn', text, [
        ['Stand aktualisieren', () => syncConversation(cid, opts)],
        ['Stoppen', () => stopAndSync(cid), 'danger'],
      ]);
      state.pollTimer = setTimeout(() => { if (alive()) syncConversation(cid, Object.assign({}, opts, { token })); }, 2000);
    } else {
      state.runningIds.delete(cid);
      if (opts.stopped) addNotice('info', 'Angehalten. Das ist der tatsächliche Stand.');
      else if (!arrived) {
        addNotice('warn', opts.busy ? 'Die vorherige Anfrage ist fertig. Deine Nachricht wurde nicht verarbeitet.' : 'Die Nachricht ist nicht angekommen.',
          [['Erneut senden', () => { if (state.conversationId === cid) { clearNotice(); send(opts.sent, { retry: true, cid }); } }]]);
      }
    }
    if (stick) toBottom(true);
    loadConversations(); loadPending();
    return c;
  }
  // Ein vom Bildschirm gelöster Lauf läuft serverseitig weiter; im Hintergrund nur das Kennzeichen „läuft“ pflegen.
  function watchBackground(cid, n) {
    n = n || 0;
    if (!cid || n > 300) return;
    setTimeout(async () => {
      try {
        const c = await jsonOrThrow(await api('api/conversations/' + encodeURIComponent(cid)));
        if (c.running) { state.runningIds.add(cid); watchBackground(cid, n + 1); return; }
      } catch (_) { watchBackground(cid, n + 1); return; }
      state.runningIds.delete(cid); loadConversations();
    }, 2000);
  }
  function detachRun() {
    const run = state.run;
    if (!run) return;
    run.detached = true; state.run = null;
    try { run.ctrl.abort(); } catch (_) { /* ignore */ }
    setBusy(false);
    if (run.cid) { state.runningIds.add(run.cid); watchBackground(run.cid); }
  }
  async function stopRun() {
    const run = state.run; if (!run) return;
    run.userStopped = true;
    if (run.cid) await cancelConversation(run.cid);
    run.ctrl.abort();
  }

  async function send(text, o) {
    o = o || {};
    text = text.trim();
    if (!text || state.streaming) return;
    if ([...text].length > MAX_LEN) return;
    stopPolling();
    setBanner(null); if (state.keyMissing) setBanner(KEY_BANNER);
    clearNotice();
    if (!o.retry) addUser(text, new Date());
    const m = addAssistant('', null);
    m.classList.add('streaming');
    toBottom(true);
    const ctrl = new AbortController();
    const run = { cid: o.retry ? o.cid : state.conversationId, ctrl, userStopped: false, detached: false };
    state.run = run;
    setBusy(true);
    const live = () => state.run === run;
    let acc = '', raf = 0, gotDone = false, gotError = false, connected = false, busyMsg = false;
    const paint = () => { raf = 0; if (!live()) return; const stick = nearBottom(); renderMarkdown(acc, m._body); if (stick) toBottom(true); };
    let after = null;
    try {
      const res = await fetch('api/chat', {
        method: 'POST', signal: ctrl.signal, cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
        body: JSON.stringify(run.cid ? { message: text, conversation_id: run.cid } : { message: text }),
      });
      if (!res.ok || !res.body) { await jsonOrThrow(res); throw new Error('Leere Antwort'); }
      connected = true;
      for await (const ev of sseEvents(res.body)) {
        if (!live()) break;
        const d = ev.data || {};
        if (ev.type === 'start') {
          run.cid = d.conversation_id || run.cid; state.conversationId = run.cid; state.runningIds.add(run.cid); rememberConversation();
        }
        else if (ev.type === 'text') { acc += d.delta || ''; if (!raf) raf = requestAnimationFrame(paint); }
        else if (ev.type === 'tool') { setTool(m, d); toBottom(); }
        else if (ev.type === 'pending') { upsertPending(d, true); }
        else if (ev.type === 'error') {
          gotError = true;
          const msg = d.message || 'Unbekannter Fehler.';
          if (/läuft gerade schon eine Anfrage/.test(msg)) busyMsg = true;
          else showError(m, msg, null);
        }
        else if (ev.type === 'done') { gotDone = true; }
      }
      if (!live()) { /* gelöst: nichts mehr anzeigen */ }
      else if (busyMsg) after = () => syncConversation(run.cid, { sent: text, busy: true });
      else if (!gotDone && !gotError) { const st = run.userStopped; after = () => syncConversation(run.cid, st ? { stopped: true } : { sent: text }); }
      else if (gotDone && run.cid) state.runningIds.delete(run.cid);
    } catch (e) {
      if (!live()) { /* gelöst */ }
      else if (e.name === 'AbortError' && run.userStopped) after = () => syncConversation(run.cid, { stopped: true });
      else if (e.status) showError(m, netMessage(e), null);
      else if (connected || run.cid) after = () => syncConversation(run.cid, { sent: text });
      else { showError(m, netMessage(e), text ? { text, cid: run.cid } : null); setBanner('Jarvis ist nicht erreichbar.'); }
    } finally {
      if (raf) cancelAnimationFrame(raf);
      if (live()) {
        if (acc) renderMarkdown(acc, m._body);
        m._text = acc;
        m.classList.remove('streaming');
        if (!after && !acc && !m._tools.children.length && !m.querySelector('.errbox')) m._body.appendChild(el('p', 'muted', 'Keine Antwort erhalten.'));
        for (const c of m._chips || []) if (c.classList.contains('running')) { c.className = 'chip error'; }
        state.run = null; setBusy(false);
        if (after) await after();
        toBottom();
        input.focus();
      }
      loadConversations();
    }
  }

  /* ---------- Freigaben ---------- */
  // Verschachtelte Werte lesbar einrückt darstellen, nie als eine JSON-Zeile.
  function fmtValue(v, depth) {
    if (v == null) return '';
    if (typeof v !== 'object') return String(v);
    const pad = '  '.repeat(depth);
    if (Array.isArray(v)) {
      if (v.every((x) => x === null || typeof x !== 'object')) return v.join(', ');
      return v.map((x) => pad + '- ' + fmtValue(x, depth + 1).replace(/^\s+/, '')).join('\n');
    }
    return Object.entries(v).map(([k, x]) => {
      if (x != null && typeof x === 'object' && !(Array.isArray(x) && x.every((y) => y === null || typeof y !== 'object'))) {
        return pad + k + ':\n' + fmtValue(x, depth + 1);
      }
      return pad + k + ': ' + fmtValue(x, depth + 1);
    }).join('\n');
  }
  function renderFields(details) {
    const wrap = el('dl', 'fields');
    let entries = [];
    if (details && typeof details === 'object' && !Array.isArray(details)) entries = Object.entries(details);
    else if (details != null && details !== '') entries = [['details', details]];
    if (!entries.length) { wrap.appendChild(el('p', 'muted', 'Keine weiteren Details mitgeliefert.')); return wrap; }
    for (const [k, v] of entries) {
      const f = el('div', 'field' + (LONG_FIELDS.has(k) ? ' body' : ''));
      f.appendChild(el('dt', null, FIELD_LABELS[k] || k));
      const structured = v != null && typeof v === 'object' && !(Array.isArray(v) && v.every((x) => x === null || typeof x !== 'object'));
      f.appendChild(el('dd', structured ? 'struct' : null, fmtValue(v, 0)));
      wrap.appendChild(f);
    }
    return wrap;
  }
  function relAge(v) {
    const d = parseTime(v); if (!d) return '';
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return 'gerade eben angelegt';
    if (mins < 60) return 'angelegt vor ' + mins + (mins === 1 ? ' Minute' : ' Minuten');
    const h = Math.floor(mins / 60);
    if (h < 24) return 'angelegt vor ' + h + (h === 1 ? ' Stunde' : ' Stunden');
    return 'angelegt am ' + fmtTime(v);
  }
  function refreshAges() { for (const rec of state.pending.values()) if (rec.age) rec.age.textContent = relAge(rec.created); }
  setInterval(refreshAges, 30000);
  function hintFor(kind) {
    if (kind === 'mail_send' || kind === 'mail') return 'Prüfe Empfänger, Betreff und Text. Eine gesendete Mail lässt sich nicht zurückholen.';
    if (kind === 'ha_service') return 'Prüfe Dienst und Ziel. Die Aktion wirkt sofort im Haus.';
    return 'Erst nach Freigabe wird etwas ausgeführt. Prüfe die Details.';
  }

  function upsertPending(p, scroll) {
    if (!p || !p.id) return;
    const box = $('pendingBox'); box.hidden = false;
    let rec = state.pending.get(p.id);
    const reveal = (node) => { (state.pending.size <= 1 ? box : node).scrollIntoView({ block: 'start' }); };
    if (rec) { if (scroll) reveal(rec.node); return; }
    const card = el('article', 'card'); card.setAttribute('aria-label', 'Freigabe: ' + (p.summary || p.kind || ''));
    const kind = el('div', 'kind', KIND_LABELS[p.kind] || 'Freigabe nötig');
    const summary = el('div', 'summary', p.summary || p.kind || 'Offene Aktion');
    const created = p.created_at || new Date().toISOString();
    const age = el('div', 'age', relAge(created));
    const when = parseTime(created); if (when) age.title = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' }).format(when);
    const det = el('details'); det.open = true;
    det.append(el('summary', null, 'Details: genau das wird ausgeführt'), renderFields(p.details));
    const hint = el('p', 'hint', hintFor(p.kind));
    const actions = el('div', 'actions');
    const ok = el('button', 'btn primary', 'Freigeben'); ok.type = 'button';
    const no = el('button', 'btn danger', 'Verwerfen'); no.type = 'button';
    actions.append(ok, no);
    const result = el('div', 'result'); result.hidden = true; result.setAttribute('role', 'status');
    card.append(kind, summary, age, det, hint, actions, result);
    $('pendingList').appendChild(card);
    rec = { node: card, age, created };
    state.pending.set(p.id, rec);

    const finish = (cls, label, text) => {
      card.classList.remove('approved', 'rejected', 'failed', 'unknown'); card.classList.add(cls);
      kind.textContent = label; result.textContent = text; result.hidden = false;
      hint.hidden = true; actions.hidden = true;
      if (card.querySelector('.dismiss')) return;
      const close = el('button', 'btn small dismiss', 'Ausblenden'); close.type = 'button';
      close.addEventListener('click', () => { card.remove(); state.pending.delete(p.id); if (!state.pending.size) box.hidden = true; });
      card.appendChild(close); close.focus();
    };
    const UNKNOWN_HINT = 'Ob die Aktion ausgeführt wurde, ist unklar. Sieh im Postfach nach (bei Mails im Ordner „Gesendet“) und sende nicht erneut.';
    const act = async (which) => {
      ok.disabled = no.disabled = true;
      const btn = which === 'approve' ? ok : no; const orig = btn.textContent;
      btn.textContent = which === 'approve' ? 'Wird ausgeführt …' : 'Wird verworfen …';
      try {
        const j = await jsonOrThrow(await api('api/pending/' + encodeURIComponent(p.id) + '/' + which, { method: 'POST' }));
        if (which === 'approve') {
          const txt = stringify(j.result);
          if (j.status === 'done') finish('approved', 'Ausgeführt', txt || 'Erledigt.');
          else if (j.status === 'failed') finish('failed', 'Fehlgeschlagen', txt || 'Die Aktion ist fehlgeschlagen.');
          else finish('unknown', 'Ergebnis unbekannt', UNKNOWN_HINT + (txt ? '\n\n' + txt : ''));
        } else finish('rejected', 'Verworfen', 'Nichts wurde ausgeführt.');
      } catch (e) {
        btn.textContent = orig;
        if (e.status === 404 || e.status === 409) {
          finish('failed', 'Nicht mehr offen', 'Diese Aktion ist nicht mehr offen. Sie wurde schon bearbeitet oder ist abgelaufen.');
          return;
        }
        if (!e.status && which === 'approve') {
          // Verbindung weg: die Aktion kann trotzdem gelaufen sein. Nachsehen, ob sie noch offen ist.
          try {
            const list = await jsonOrThrow(await api('api/pending'));
            const still = (Array.isArray(list) ? list : []).some((x) => x.id === p.id);
            if (!still) { finish('unknown', 'Ergebnis unbekannt', 'Die Verbindung brach während der Freigabe ab, die Aktion ist nicht mehr offen. ' + UNKNOWN_HINT); return; }
          } catch (_) {
            finish('unknown', 'Ergebnis unbekannt', 'Die Verbindung brach während der Freigabe ab. ' + UNKNOWN_HINT); return;
          }
        }
        ok.disabled = no.disabled = false;
        result.hidden = false; card.classList.add('failed'); result.textContent = netMessage(e);
      }
    };
    ok.addEventListener('click', () => act('approve'));
    no.addEventListener('click', () => act('reject'));
    if (scroll) reveal(card);
  }
  async function loadPending() {
    try {
      const list = await jsonOrThrow(await api('api/pending'));
      (Array.isArray(list) ? list : list.items || []).forEach((p) => upsertPending(p, false));
    } catch (_) { /* Banner kommt über Status */ }
  }

  /* ---------- Gesprächsliste ---------- */
  async function loadConversations() {
    const ul = $('convList'), note = $('convNote');
    try {
      let list = await jsonOrThrow(await api('api/conversations'));
      if (!Array.isArray(list)) list = list.items || [];
      ul.textContent = '';
      note.hidden = list.length > 0; note.textContent = 'Noch keine Gespräche.';
      for (const c of list) {
        const li = el('li'); const b = el('button'); b.type = 'button';
        const t = el('span', 'conv-time', fmtTime(c.updated_at));
        if (c.running || state.runningIds.has(c.id)) { t.appendChild(el('span', 'run-flag', ' · ● läuft')); }
        b.append(el('span', 'conv-title', c.title || 'Ohne Titel'), t);
        if (c.id === state.conversationId) b.setAttribute('aria-current', 'true');
        b.addEventListener('click', () => openConversation(c.id));
        li.appendChild(b); ul.appendChild(li);
      }
    } catch (e) { ul.textContent = ''; note.hidden = false; note.textContent = 'Gespräche konnten nicht geladen werden.'; }
  }
  async function openConversation(id) {
    detachRun(); stopPolling(); closeSide();
    state.conversationId = id; rememberConversation();
    clearNotice(); messagesEl.textContent = ''; updateEmpty();
    const c = await syncConversation(id, {});
    if (!c) { if (state.conversationId === id && !state.notice) setBanner('Gespräch konnte nicht geladen werden.'); }
    else setBanner(state.keyMissing ? KEY_BANNER : null);
    toBottom(true);
  }
  function newConversation() {
    detachRun(); stopPolling();
    clearNotice();
    state.conversationId = null; rememberConversation(); messagesEl.textContent = ''; updateEmpty(); closeSide(); loadConversations(); input.focus();
  }

  /* ---------- Status / Protokoll ---------- */
  function setBanner(text) {
    $('banner').hidden = !text; $('bannerText').textContent = text || '';
  }
  async function loadStatus() {
    const dot = $('statusDot'), label = $('statusLabel');
    try {
      const s = await jsonOrThrow(await api('api/status'));
      const tools = Array.isArray(s.tools) ? s.tools : [];
      // Gedächtnis und Websuche brauchen keine Zugangsdaten und zählen nicht als "eingerichtet".
      const n = tools.filter((t) => t.configured && !['memory', 'web'].includes(String(t.area).toLowerCase())).length;
      state.areasConfigured = n > 0;
      state.keyMissing = s.api_key_configured === false;
      const ready = n > 0 && !state.keyMissing;
      dot.className = 'dot ' + (ready ? 'ok' : 'warn');
      label.textContent = ready ? 'Bereit' : 'Nicht eingerichtet';
      updateSend();
      $('modelName').textContent = s.model || '–';
      const ul = $('areaList'); ul.textContent = '';
      for (const t of tools) {
        const li = el('li'); li.appendChild(el('span', null, areaLabel(t.area)));
        li.appendChild(el('span', 'badge' + (t.configured ? '' : ' off'), t.configured ? 'eingerichtet' : 'nicht eingerichtet'));
        ul.appendChild(li);
      }
      if (!tools.length) ul.appendChild(el('li', null, 'Keine Bereiche gemeldet.'));
      setBanner(state.keyMissing ? KEY_BANNER : null);
    } catch (e) {
      state.areasConfigured = null;
      dot.className = 'dot bad'; label.textContent = e.status === 401 ? 'Kein Zugang' : (e.status ? 'Fehler' : 'Offline');
      $('modelName').textContent = '–';
      setBanner(e.status ? e.message : 'Jarvis ist nicht erreichbar. Prüfe, ob das Add-on läuft.');
    }
    updateEmpty();
  }
  async function loadLog() {
    const st = $('logState'), ol = $('logList'); ol.textContent = ''; st.hidden = false; st.textContent = 'Wird geladen …';
    try {
      const list = await jsonOrThrow(await api('api/log?limit=50'));
      if (!Array.isArray(list) || !list.length) { st.textContent = 'Noch keine Einträge.'; return; }
      st.hidden = true;
      for (const e of list) {
        const sv = LOG_STATUS[e.status] || { label: e.status ? String(e.status) : 'Unbekannt', cls: 'warn', sym: '?' };
        const li = el('li', 'st-' + sv.cls);
        const head = el('div', 'l-head');
        head.appendChild(el('span', 'l-tool', e.tool || 'Eintrag'));
        head.appendChild(el('span', 'badge ' + sv.cls, sv.sym + ' ' + sv.label));
        if (e.risk) head.appendChild(el('span', 'badge off', RISK_LABELS[e.risk] || String(e.risk)));
        const t = fmtTime(e.created_at); if (t) head.appendChild(el('span', 'l-time', t));
        li.appendChild(head);
        if (e.result) li.appendChild(el('div', 'l-sum', stringify(e.result)));
        if (e.input != null && e.input !== '') {
          const d = el('details'); d.append(el('summary', null, 'Eingabe'), el('pre', null, stringify(e.input))); li.appendChild(d);
        }
        ol.appendChild(li);
      }
    } catch (e) { st.textContent = 'Protokoll konnte nicht geladen werden: ' + netMessage(e); }
  }

  /* ---------- Seitenleiste ---------- */
  const side = $('sidebar'), scrim = $('scrim'), openBtn = $('openSide');
  function openSide() { side.classList.add('open'); scrim.hidden = false; openBtn.setAttribute('aria-expanded', 'true'); $('closeSide').focus(); }
  function closeSide() { side.classList.remove('open'); scrim.hidden = true; openBtn.setAttribute('aria-expanded', 'false'); }
  openBtn.addEventListener('click', openSide);
  $('closeSide').addEventListener('click', () => { closeSide(); openBtn.focus(); });
  scrim.addEventListener('click', closeSide);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && side.classList.contains('open')) { closeSide(); openBtn.focus(); } });

  /* ---------- Verdrahtung ---------- */
  const dlg = $('statusDlg');
  $('statusBtn').addEventListener('click', () => { dlg.showModal(); loadStatus(); loadLog(); });
  $('dlgClose').addEventListener('click', () => dlg.close());
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
  $('logReload').addEventListener('click', loadLog);
  $('bannerRetry').addEventListener('click', () => { loadStatus(); loadPending(); loadConversations(); });
  $('newChat').addEventListener('click', newConversation);

  const SUGGESTIONS = ['Was steht heute an?', 'Neue Mails zusammenfassen', 'Setz Milch auf die Einkaufsliste'];
  for (const s of SUGGESTIONS) {
    const b = el('button', null, s); b.type = 'button';
    b.addEventListener('click', () => send(s));
    $('suggest').appendChild(b);
  }

  function grow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, window.innerHeight * 0.4) + 'px'; }
  function checkLen() {
    const raw = input.value; let n = raw.length;
    if (n > MAX_LEN - 2000) n = [...raw].length;
    state.tooLong = n > MAX_LEN;
    const h = $('limitHint');
    h.hidden = n <= MAX_LEN - 1000;
    if (!h.hidden) h.textContent = state.tooLong
      ? 'Die Nachricht ist ' + (n - MAX_LEN).toLocaleString('de-DE') + ' Zeichen zu lang. Erlaubt sind ' + MAX_LEN.toLocaleString('de-DE') + ' Zeichen. Kürze sie oder teile sie auf.'
      : 'Noch ' + (MAX_LEN - n).toLocaleString('de-DE') + ' Zeichen frei (höchstens ' + MAX_LEN.toLocaleString('de-DE') + ').';
    h.classList.toggle('bad', state.tooLong);
    updateSend();
  }
  input.addEventListener('input', () => { grow(); checkLen(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && window.matchMedia('(pointer: fine)').matches) {
      e.preventDefault();
      if (state.streaming) { if (input.value.trim()) busyHint(); return; }
      $('composer').requestSubmit();
    }
  });
  $('composer').addEventListener('submit', (e) => {
    e.preventDefault();
    if (state.streaming) { stopRun(); return; }
    if (state.keyMissing || state.tooLong) return;
    const t = input.value; if (!t.trim()) return;
    input.value = ''; grow(); checkLen(); send(t);
  });

  updateEmpty();
  loadStatus(); loadPending(); loadConversations();
  try { const saved = sessionStorage.getItem('jarvis.cid'); if (saved) openConversation(saved); } catch (_) { /* ignore */ }
  window.addEventListener('online', () => { loadStatus(); loadPending(); });
})();
