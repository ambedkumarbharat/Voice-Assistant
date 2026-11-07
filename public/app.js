/* VoxDesk dashboard — vanilla ES2020, no build step. */
(function () {
  'use strict';

  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  function el(tag, attrs) {
    const node = document.createElement(tag);
    const options = attrs || {};
    for (const key of Object.keys(options)) {
      const value = options[key];
      if (value === undefined || value === null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value);
    }
    for (let i = 2; i < arguments.length; i += 1) {
      const kid = arguments[i];
      if (kid === null || kid === undefined || kid === false) continue;
      if (Array.isArray(kid)) {
        for (const item of kid) if (item !== null && item !== undefined) node.append(item.nodeType ? item : document.createTextNode(String(item)));
      } else {
        node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
      }
    }
    return node;
  }

  const state = {
    token: localStorage.getItem('voxdesk_token') || '',
    view: 'overview',
    overview: null,
    providers: null,
    live: [],
    calls: [],
    callsTotal: 0,
    page: 0,
    pageSize: 20,
    filters: { q: '', status: '' },
    ws: null,
    reconnectDelay: 1000,
    drawerCallId: null,
    test: { ws: null, mic: null, player: null, callId: null, startedAt: 0, timer: null, lastBubble: '', interim: null },
    refreshTimer: null,
  };

  const TITLES = {
    overview: ['Overview', 'Live operations for your AI voice agent.'],
    calls: ['Calls', 'Every conversation, transcript and outcome.'],
    test: ['Test line', 'Talk to your agent in the browser — no phone number required.'],
    contacts: ['Contacts', 'People and numbers your agent can dial.'],
    settings: ['Agent settings', 'Persona, knowledge, providers and access.'],
  };

  // ------------------------------------------------------------------ utils ---

  const pct = (value) => Math.max(0, Math.min(100, value));

  function fmtDuration(ms) {
    if (!ms || ms < 0) return '—';
    const total = Math.round(ms / 1000);
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    if (minutes >= 60) return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
    return `${minutes}:${String(seconds).padStart(2, '0')}`;
  }

  function fmtTime(ts) {
    if (!ts) return '—';
    const date = new Date(ts);
    const today = new Date();
    const sameDay = date.toDateString() === today.toDateString();
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (sameDay) return time;
    return `${date.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
  }

  function fmtClock(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
  }

  function shortId(id) {
    return id ? id.slice(0, 8) : '';
  }

  function partyLabel(call) {
    if (call.contact_name) return call.contact_name;
    if (call.direction === 'outbound') return call.to_number || 'Unknown';
    if (call.from_number) return call.from_number;
    return 'Browser test';
  }

  const STATUS_BADGE = {
    queued: ['warn', 'Queued'],
    ringing: ['warn', 'Ringing'],
    in_progress: ['info', 'In progress'],
    completed: ['success', 'Completed'],
    failed: ['danger', 'Failed'],
    no_answer: ['neutral', 'No answer'],
  };

  function statusBadge(status) {
    const entry = STATUS_BADGE[status] || ['neutral', status || 'Unknown'];
    const badge = el('span', { class: `badge ${entry[0]}`, text: entry[1] });
    if (status === 'in_progress' || status === 'ringing') badge.prepend(el('span', { class: 'pulse' }));
    return badge;
  }

  function outcomeBadge(outcome) {
    if (!outcome) return el('span', { class: 'muted', text: '—' });
    const map = { resolved: 'success', follow_up_needed: 'warn', escalated: 'danger', abandoned: 'neutral' };
    return el('span', { class: `badge ${map[outcome] || 'neutral'}`, text: String(outcome).replace(/_/g, ' ') });
  }

  function directionLabel(direction) {
    return direction === 'outbound' ? 'Outbound' : direction === 'inbound' ? 'Inbound' : 'Web test';
  }

  function debounce(fn, delay) {
    let timer;
    return function debounced() {
      const args = arguments;
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(null, args), delay);
    };
  }

  function toast(message, kind) {
    const node = el('div', { class: `toast ${kind || ''}` }, el('span', { text: message }));
    $('#toasts').append(node);
    setTimeout(() => {
      node.style.opacity = '0';
      node.style.transform = 'translateY(6px)';
      node.style.transition = 'all .2s ease';
      setTimeout(() => node.remove(), 220);
    }, kind === 'error' ? 6500 : 3800);
  }

  // -------------------------------------------------------------------- api ---

  async function api(path, options) {
    const opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.body && !headers['content-type']) headers['content-type'] = 'application/json';
    if (state.token) headers.authorization = `Bearer ${state.token}`;
    const response = await fetch(path, Object.assign({}, opts, { headers }));
    if (response.status === 401) {
      showLogin();
      throw new Error('Unauthorized');
    }
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (error) {
      data = { error: { message: text.slice(0, 200) } };
    }
    if (!response.ok) {
      const message = (data && data.error && data.error.message) || `Request failed (${response.status})`;
      const error = new Error(message);
      error.status = response.status;
      error.code = data && data.error && data.error.code;
      throw error;
    }
    return data;
  }

  function wsUrl(path) {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    const separator = path.includes('?') ? '&' : '?';
    return `${scheme}://${location.host}${path}${state.token ? `${separator}token=${encodeURIComponent(state.token)}` : ''}`;
  }

  // ----------------------------------------------------------------- router ---

  function setView(name, options) {
    if (!TITLES[name]) name = 'overview';
    state.view = name;
    $$('.nav-item').forEach((button) => button.classList.toggle('active', button.dataset.view === name));
    $$('.view').forEach((section) => section.classList.add('hidden'));
    const target = $(`#view-${name}`);
    if (target) target.classList.remove('hidden');
    const titles = TITLES[name];
    $('#page-title').textContent = titles[0];
    $('#page-sub').textContent = titles[1];
    if (location.hash !== `#${name}`) history.replaceState(null, '', `#${name}`);

    if (name === 'calls') void loadCalls();
    if (name === 'contacts') void loadContacts();
    if (name === 'settings') void loadSettings();
    if (name === 'overview') void loadOverview();
    if (name === 'test' && state.overview) renderTestIntro();
  }

  // --------------------------------------------------------------- overview ---

  async function loadOverview() {
    try {
      const data = await api('/api/overview');
      state.overview = data;
      state.providers = data.providers;
      state.live = data.live || [];
      renderStats(data.stats);
      renderVolume(data.volume || []);
      renderLiveList(state.live);
      renderRecent(data.recent || []);
      renderProviders(data.providers);
      const banner = $('#demo-banner');
      if (data.providers && data.providers.demoMode) {
        banner.classList.remove('hidden');
        const missing = [];
        if (!data.providers.llm.live) missing.push('LLM');
        if (!data.providers.stt.live) missing.push('STT');
        if (!data.providers.tts.live) missing.push('TTS');
        $('#demo-banner-text').textContent = `Missing live ${missing.join(', ')} — set provider keys in .env and restart.`;
      } else {
        banner.classList.add('hidden');
      }
      if (!state.live.some((call) => call.id === state.drawerCallId)) {
        const hangup = $('#drawer-hangup');
        if (hangup) hangup.classList.add('hidden');
      }
    } catch (error) {
      if (error.message !== 'Unauthorized') toast(error.message, 'error');
    }
  }

  function renderStats(stats) {
    if (!stats) return;
    $('#stat-today').textContent = stats.callsToday ?? 0;
    $('#stat-total').textContent = `${stats.totalCalls ?? 0} all time`;
    $('#stat-active').textContent = stats.activeCalls ?? 0;
    $('#stat-aht').textContent = fmtDuration(stats.avgDurationMs || 0);
    $('#stat-talk').textContent = `${stats.talkMinutes ?? 0} talk minutes`;
    $('#stat-res').textContent = stats.completed > 0 ? `${stats.resolutionRate}%` : '—';
    $('#stat-completed').textContent = `${stats.completed ?? 0} completed`;
    $('#live-count').textContent = String(stats.activeCalls ?? 0);
  }

  function renderVolume(series) {
    const node = $('#volume-chart');
    node.replaceChildren();
    const max = Math.max(1, ...series.map((point) => point.calls));
    for (const point of series) {
      const height = point.calls === 0 ? 2 : Math.max(6, (point.calls / max) * 100);
      const date = new Date(`${point.date}T12:00:00`);
      node.append(
        el(
          'div',
          { class: 'chart-col', title: `${point.date}: ${point.calls} calls, ${point.minutes} min` },
          el('div', { class: 'chart-bar-wrap' }, el('div', { class: 'chart-bar', style: `height:${height}%` })),
          el('span', { class: 'chart-day', text: String(date.getDate()).padStart(2, '0') }),
        ),
      );
    }
    if (series.length === 0) node.append(el('div', { class: 'empty', text: 'No call data yet.' }));
  }

  function renderLiveList(calls) {
    const node = $('#live-list');
    node.replaceChildren();
    if (!calls || calls.length === 0) {
      node.append(el('div', { class: 'empty' }, 'No active calls.', el('br'), el('span', { text: 'Start one from the test line.' })));
      return;
    }
    for (const call of calls) {
      node.append(
        el(
          'div',
          { class: 'live-item', onclick: () => openDrawer(call.id) },
          el('span', { class: 'wave' }, el('i'), el('i'), el('i'), el('i')),
          el('span', { class: 'who', text: call.contactName || call.toNumber || call.fromNumber || 'Web caller' }),
          el('span', { class: 'meta', text: `${call.status} · ${fmtDuration(call.durationMs)}` }),
        ),
      );
    }
  }

  function renderRecent(calls) {
    const body = $('#recent-body');
    body.replaceChildren();
    if (!calls.length) {
      body.append(el('tr', {}, el('td', { colspan: '7' }, el('div', { class: 'empty', text: 'No calls yet — open the test line to make one.' }))));
      return;
    }
    for (const call of calls) {
      body.append(
        el(
          'tr',
          { onclick: () => openDrawer(call.id) },
          el('td', { class: 'cell-time', text: fmtTime(call.started_at) }),
          el('td', { text: directionLabel(call.direction) }),
          el('td', { class: 'mono', text: partyLabel(call) }),
          el('td', {}, statusBadge(call.status)),
          el('td', { text: fmtDuration(call.duration_ms) }),
          el('td', { text: String(call.turns || 0) }),
          el('td', { class: 'cell-summary', text: call.summary || '—' }),
        ),
      );
    }
  }

  function renderProviders(providers) {
    if (!providers) return;
    const rows = [
      ['dot-llm', 'lbl-llm', providers.llm, providers.llm.live ? providers.llm.model : 'demo brain'],
      ['dot-stt', 'lbl-stt', providers.stt, providers.stt.live ? providers.stt.id : 'no key'],
      ['dot-tts', 'lbl-tts', providers.tts, providers.tts.live ? providers.tts.id : 'no key'],
      ['dot-tel', 'lbl-tel', providers.telephony, providers.telephony.live ? 'twilio' : 'browser only'],
    ];
    for (const [dotId, labelId, info, text] of rows) {
      const dot = $(`#${dotId}`);
      if (!dot) continue;
      dot.className = `dot ${info.live ? 'live' : 'off'}`;
      $(`#${labelId}`).textContent = text;
    }

    const list = $('#settings-providers');
    if (!list) return;
    list.replaceChildren();
    const entries = [
      ['LLM', `${providers.llm.id} · ${providers.llm.model}`, providers.llm.live, providers.llm.keyEnv],
      ['STT', providers.stt.id, providers.stt.live, providers.stt.keyEnv],
      ['TTS', providers.tts.id, providers.tts.live, providers.tts.keyEnv],
      ['Telephony', providers.telephony.live ? `twilio · ${providers.telephony.fromNumber}` : 'browser test line only', providers.telephony.live, ''],
    ];
    for (const [role, value, live, keyEnv] of entries) {
      list.append(
        el(
          'div',
          { class: 'provider-row' },
          el('span', { class: 'role', text: role }),
          el('span', { class: 'value', text: value }),
          el('span', { class: 'state' }, el('span', { class: `badge ${live ? 'success' : 'warn'}`, text: live ? 'live' : keyEnv ? `set ${keyEnv}` : 'demo' })),
        ),
      );
    }
  }

  // ------------------------------------------------------------------ calls ---

  async function loadCalls() {
    const params = new URLSearchParams({ limit: String(state.pageSize), offset: String(state.page * state.pageSize) });
    if (state.filters.q) params.set('q', state.filters.q);
    if (state.filters.status) params.set('status', state.filters.status);
    try {
      const data = await api(`/api/calls?${params.toString()}`);
      state.calls = data.calls || [];
      state.callsTotal = data.total || 0;
      renderCalls();
    } catch (error) {
      if (error.message !== 'Unauthorized') toast(error.message, 'error');
    }
  }

  function renderCalls() {
    const body = $('#calls-body');
    body.replaceChildren();
    if (!state.calls.length) {
      body.append(el('tr', {}, el('td', { colspan: '8' }, el('div', { class: 'empty', text: 'No calls match this filter.' }))));
    }
    for (const call of state.calls) {
      body.append(
        el(
          'tr',
          { onclick: () => openDrawer(call.id) },
          el('td', { class: 'cell-time', text: fmtTime(call.started_at) }),
          el('td', { text: directionLabel(call.direction) }),
          el('td', { class: 'mono', text: partyLabel(call) }),
          el('td', {}, statusBadge(call.status)),
          el('td', { text: fmtDuration(call.duration_ms) }),
          el('td', { text: String(call.turns || 0) }),
          el('td', {}, outcomeBadge(call.outcome)),
          el('td', { class: 'cell-summary', text: call.summary || '—' }),
        ),
      );
    }
    $('#calls-count').textContent = `${state.callsTotal} ${state.callsTotal === 1 ? 'call' : 'calls'}`;
    const pages = Math.max(1, Math.ceil(state.callsTotal / state.pageSize));
    $('#page-label').textContent = `Page ${state.page + 1} of ${pages}`;
    $('#page-prev').disabled = state.page === 0;
    $('#page-next').disabled = state.page + 1 >= pages;
  }

  // ----------------------------------------------------------------- drawer ---

  async function openDrawer(id) {
    state.drawerCallId = id;
    $('#drawer').classList.remove('hidden');
    $('#drawer-backdrop').classList.remove('hidden');
    try {
      const call = await api(`/api/calls/${id}`);
      renderDrawer(call);
    } catch (error) {
      if (error.message !== 'Unauthorized') toast(error.message, 'error');
      closeDrawer();
    }
  }

  function closeDrawer() {
    state.drawerCallId = null;
    $('#drawer').classList.add('hidden');
    $('#drawer-backdrop').classList.add('hidden');
  }

  function renderDrawer(call) {
    $('#drawer-eyebrow').textContent = `${directionLabel(call.direction)} · ${shortId(call.id)}`;
    $('#drawer-title').textContent = partyLabel(call);
    $('#drawer-sub').textContent = `${call.transport} · ${fmtTime(call.started_at)}`;
    $('#drawer-summary').textContent = call.summary || 'No summary yet. It is generated within a few seconds of the call ending.';

    const meta = $('#drawer-meta');
    meta.replaceChildren(
      metaItem('Status', statusBadge(call.status)),
      metaItem('Duration', fmtDuration(call.duration_ms)),
      metaItem('Turns', String(call.turns || 0)),
      metaItem('Outcome', outcomeBadge(call.outcome)),
      metaItem('Sentiment', call.sentiment ? el('strong', { text: call.sentiment }) : el('strong', { text: '—' })),
      metaItem('Ended', call.end_reason ? el('strong', { text: call.end_reason.replace(/_/g, ' ') }) : el('strong', { text: '—' })),
    );

    const actions = $('#drawer-actions');
    actions.replaceChildren();
    for (const item of call.action_items || []) actions.append(el('li', { text: item }));

    const transcript = $('#drawer-transcript');
    transcript.replaceChildren();
    if (!call.messages || call.messages.length === 0) transcript.append(el('div', { class: 'empty', text: 'No messages recorded.' }));
    for (const message of call.messages || []) {
      if (message.role === 'system') continue;
      transcript.append(bubble(message.role === 'user' ? 'user' : 'assistant', message.content, message.latency_ms ? `${(message.latency_ms / 1000).toFixed(2)}s` : ''));
    }
    transcript.scrollTop = transcript.scrollHeight;

    const events = $('#drawer-events');
    events.replaceChildren();
    for (const event of call.events || []) {
      events.append(el('div', { class: 'event-row' }, el('b', { text: new Date(event.at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }), el('span', { text: event.type })));
    }

    const hangup = $('#drawer-hangup');
    hangup.classList.toggle('hidden', !call.live);
    $('#drawer-export').onclick = (event) => {
      event.preventDefault();
      downloadTranscript(call.id);
    };
    $('#drawer-recall').onclick = () => {
      closeDrawer();
      const number = call.direction === 'outbound' ? call.to_number : call.from_number;
      openNewCall(number || '', call.contact_name || '');
    };
    hangup.onclick = async () => {
      hangup.disabled = true;
      try {
        await api(`/api/calls/${call.id}/hangup`, { method: 'POST' });
        toast('Call ended', 'success');
        await openDrawer(call.id);
        void loadOverview();
      } catch (error) {
        toast(error.message, 'error');
      } finally {
        hangup.disabled = false;
      }
    };
  }

  function metaItem(label, value) {
    return el('div', { class: 'meta-item' }, el('span', { text: label }), el('strong', {}, value));
  }

  function bubble(role, text, meta) {
    const classNames = `bubble ${role === 'user' ? 'user' : role === 'system' ? 'system' : 'assistant'}`;
    const node = el('div', { class: classNames }, el('span', { text }));
    if (meta) node.append(el('span', { class: 'bubble-meta', text: meta }));
    return node;
  }

  async function downloadTranscript(id) {
    try {
      const response = await fetch(`/api/calls/${id}/export`, { headers: state.token ? { authorization: `Bearer ${state.token}` } : {} });
      if (!response.ok) throw new Error(`Export failed (${response.status})`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = el('a', { href: url, download: `call-${shortId(id)}.txt` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (error) {
      toast(error.message, 'error');
    }
  }

  // -------------------------------------------------------------- test line ---

  function renderTestIntro() {
    const settings = state.overview && state.overview.settings;
    if (settings) $('#test-agent-name').textContent = `${settings.name} · ${settings.company}`;
    const supports = window.VoxAudio && window.VoxAudio.MicCapture.supported;
    $('#test-hint').textContent = supports
      ? 'Microphone audio streams to your STT provider. Typing works even without one.'
      : 'This browser has no microphone API here. Use the text box to drive the call.';
  }

  function setTestStatus(text, mode) {
    $('#test-status').textContent = text;
    const avatar = $('#avatar');
    avatar.classList.toggle('speaking', mode === 'speaking');
    avatar.classList.toggle('listening', mode === 'listening');
  }

  async function startTestCall() {
    if (state.test.ws) return;
    const button = $('#btn-start-test');
    button.disabled = true;
    try {
      const player = new window.VoxAudio.Player();
      await player.resume();
      state.test.player = player;

      const transcript = $('#test-transcript');
      transcript.replaceChildren();
      $('#test-events').replaceChildren();

      const socket = new WebSocket(wsUrl('/ws/test-call'));
      state.test.ws = socket;
      state.test.startedAt = Date.now();

      socket.onopen = () => {
        setTestStatus('Connecting…', 'listening');
      };

      socket.onmessage = async (event) => {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (message.type === 'ready') {
          state.test.callId = message.callId;
          $('#test-call-id').textContent = `call ${shortId(message.callId)}`;
          $('#btn-start-test').classList.add('hidden');
          $('#btn-end-test').classList.remove('hidden');
          $('#say-input').disabled = false;
          $('#say-send').disabled = false;
          setTestStatus('Connecting…', 'listening');
          state.test.timer = setInterval(() => {
            $('#test-timer').textContent = fmtClock(Date.now() - state.test.startedAt);
          }, 500);
          if (window.VoxAudio.MicCapture.supported) {
            try {
              const mic = new window.VoxAudio.MicCapture({
                onChunk: (payload) => {
                  const speaking = state.test.player && state.test.player.playing > 0;
                  const talkOver = $('#test-barge') && $('#test-barge').checked;
                  if (speaking && !talkOver) return; // avoid echo-triggered barge-in
                  if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'audio', payload }));
                },
                onLevel: (level) => {
                  $('#mic-meter').style.width = `${Math.round(level * 100)}%`;
                },
              });
              await mic.start();
              state.test.mic = mic;
            } catch (error) {
              toast('Microphone blocked — use the text box instead', 'error');
              setTestStatus('Text mode', 'listening');
            }
          }
          return;
        }
        if (message.type === 'audio') {
          player.schedule(message.payload);
          return;
        }
        if (message.type === 'clear') {
          player.clear();
          return;
        }
        if (message.type === 'call.ended') {
          endTestCall(true);
          return;
        }
        if (message.type === 'event') {
          handleTestEvent(message.event);
          return;
        }
        if (message.type === 'error') {
          toast(message.message, 'error');
        }
      };

      socket.onclose = () => {
        if (state.test.ws === socket) endTestCall(true);
      };
      socket.onerror = () => {
        toast('Test line socket error', 'error');
      };
    } catch (error) {
      toast(error.message || 'Could not start the test call', 'error');
      endTestCall(true);
    } finally {
      button.disabled = false;
    }
  }

  function handleTestEvent(event) {
    if (!event) return;
    const log = $('#test-events');
    if (event.type !== 'caller.level') {
      log.append(
        el(
          'div',
          { class: 'event-row' },
          el('b', { text: new Date(event.at || Date.now()).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) }),
          el('span', { text: event.type + (event.data && event.data.status ? ` · ${event.data.status}` : '') }),
        ),
      );
      while (log.children.length > 120) log.firstChild.remove();
      log.scrollTop = log.scrollHeight;
    }

    const transcript = $('#test-transcript');
    switch (event.type) {
      case 'call.status': {
        const status = event.data && event.data.status;
        if (status === 'thinking') setTestStatus('Thinking…', 'listening');
        else if (status === 'speaking') setTestStatus('Speaking', 'speaking');
        else if (status === 'listening') setTestStatus('Listening', 'listening');
        else if (status === 'ended') setTestStatus('Call ended', null);
        if (event.data && typeof event.data.turns === 'number') $('#test-turn-pill').textContent = `${event.data.turns} turn${event.data.turns === 1 ? '' : 's'}`;
        break;
      }
      case 'caller.speaking':
        setTestStatus('Hearing you…', 'listening');
        break;
      case 'transcript.partial': {
        if (!state.test.interim) {
          state.test.interim = el('div', { class: 'bubble user interim' });
          transcript.append(state.test.interim);
        }
        state.test.interim.textContent = event.data.text;
        transcript.scrollTop = transcript.scrollHeight;
        break;
      }
      case 'transcript.message': {
        clearInterim();
        const text = event.data.text;
        if (text && text !== state.test.lastBubble) {
          transcript.append(bubble('user', text, 'caller'));
          state.test.lastBubble = text;
        }
        break;
      }
      case 'assistant.message': {
        clearInterim();
        const text = event.data.text;
        if (text) {
          const meta = event.data.interrupted ? 'interrupted' : event.data.latencyMs ? `first audio ${(event.data.latencyMs / 1000).toFixed(2)}s` : 'agent';
          transcript.append(bubble('assistant', text, meta));
        }
        break;
      }
      case 'stt.error':
        if (event.data && event.data.message) setTestStatus('Listening (text mode)', 'listening');
        break;
      case 'llm.error':
      case 'tts.error':
        toast(`${event.type}: ${event.data && event.data.message}`, 'error');
        break;
      case 'call.ended':
        setTestStatus('Call ended', null);
        break;
      default:
        break;
    }
    if (transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 140) {
      transcript.scrollTop = transcript.scrollHeight;
    }
  }

  function clearInterim() {
    if (state.test.interim) {
      state.test.interim.remove();
      state.test.interim = null;
    }
  }

  function endTestCall(silent) {
    const socket = state.test.ws;
    state.test.ws = null;
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        socket.send(JSON.stringify({ type: 'end' }));
      } catch (error) {
        /* ignore */
      }
      socket.close();
    }
    if (state.test.mic) {
      state.test.mic.stop();
      state.test.mic = null;
    }
    if (state.test.player) {
      state.test.player.stop();
      state.test.player = null;
    }
    if (state.test.timer) {
      clearInterval(state.test.timer);
      state.test.timer = null;
    }
    clearInterim();
    $('#mic-meter').style.width = '0%';
    $('#btn-start-test').classList.remove('hidden');
    $('#btn-end-test').classList.add('hidden');
    $('#say-input').disabled = true;
    $('#say-send').disabled = true;
    $('#say-input').value = '';
    setTestStatus(silent ? 'Call ended' : 'Not connected', null);
    state.test.callId = null;
    void loadOverview();
  }

  // --------------------------------------------------------------- contacts ---

  async function loadContacts() {
    try {
      const data = await api('/api/contacts');
      const body = $('#contacts-body');
      body.replaceChildren();
      const contacts = data.contacts || [];
      $('#contacts-count').textContent = `${contacts.length} saved`;
      if (!contacts.length) {
        body.append(el('tr', {}, el('td', { colspan: '4' }, el('div', { class: 'empty', text: 'No contacts yet.' }))));
      }
      for (const contact of contacts) {
        body.append(
          el(
            'tr',
            {},
            el('td', { text: contact.name }),
            el('td', { class: 'mono', text: contact.phone }),
            el('td', { text: contact.company || '—' }),
            el(
              'td',
              {},
              el('button', {
                class: 'btn ghost sm',
                text: 'Delete',
                onclick: async (event) => {
                  event.stopPropagation();
                  try {
                    await api(`/api/contacts/${contact.id}`, { method: 'DELETE' });
                    await loadContacts();
                  } catch (error) {
                    toast(error.message, 'error');
                  }
                },
              }),
            ),
          ),
        );
      }
    } catch (error) {
      if (error.message !== 'Unauthorized') toast(error.message, 'error');
    }
  }

  // --------------------------------------------------------------- settings ---

  async function loadSettings() {
    try {
      const data = await api('/api/settings');
      const form = $('#settings-form');
      const settings = data.settings;
      for (const key of ['name', 'company', 'greeting', 'persona', 'knowledge', 'voice', 'language', 'fallback', 'transferNumber', 'maxTurns']) {
        if (form.elements[key]) form.elements[key].value = settings[key] === undefined || settings[key] === null ? '' : String(settings[key]);
      }
      form.elements.bargeIn.checked = Boolean(settings.bargeIn);
      state.providers = data.providers;
      renderProviders(data.providers);
      $('#token-input').value = state.token ? '••••••••••••' : '';
    } catch (error) {
      if (error.message !== 'Unauthorized') toast(error.message, 'error');
    }
  }

  // ------------------------------------------------------------ new call UI ---

  function openNewCall(prefillNumber, prefillName) {
    const modal = $('#new-call-modal');
    modal.classList.remove('hidden');
    const form = $('#new-call-form');
    form.reset();
    if (prefillNumber) form.elements.to.value = prefillNumber;
    if (prefillName) form.elements.contactName.value = prefillName;
    const live = state.overview && state.overview.liveConfigured;
    $('#call-capability').textContent = live
      ? 'The agent will call this number, greet the person and handle the conversation autonomously.'
      : 'Telephony is not configured yet. Add Twilio credentials to .env, or use the browser test line.';
  }

  function closeNewCall() {
    $('#new-call-modal').classList.add('hidden');
  }

  // ------------------------------------------------------------------- auth ---

  function showLogin() {
    $('#login-overlay').classList.remove('hidden');
  }

  // ---------------------------------------------------------------- live ws ---

  function connectLive() {
    if (state.ws) {
      try {
        state.ws.close();
      } catch (error) {
        /* ignore */
      }
    }
    const socket = new WebSocket(wsUrl('/ws/live'));
    state.ws = socket;
    const pill = $('#conn-pill');
    const text = $('#conn-text');

    socket.onopen = () => {
      state.reconnectDelay = 1000;
      pill.classList.add('online');
      pill.classList.remove('offline');
      text.textContent = 'live';
      socket.send(JSON.stringify({ type: 'list' }));
    };

    socket.onmessage = (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (message.type === 'hello') {
        state.live = message.live || [];
        if (state.view === 'overview') renderLiveList(state.live);
        return;
      }
      if (message.type === 'live') {
        state.live = message.live || [];
        if (state.view === 'overview') renderLiveList(state.live);
        return;
      }
      if (message.type === 'event') {
        handleGlobalEvent(message.event);
      }
    };

    socket.onclose = () => {
      if (state.ws !== socket) return;
      pill.classList.remove('online');
      pill.classList.add('offline');
      text.textContent = 'reconnecting';
      const delay = Math.min(15000, state.reconnectDelay);
      state.reconnectDelay = Math.min(15000, state.reconnectDelay * 1.8);
      setTimeout(() => {
        if (state.ws === socket) connectLive();
      }, delay);
    };

    socket.onerror = () => socket.close();
  }

  const refreshOverviewDebounced = debounce(() => {
    if (state.view === 'overview') void loadOverview();
    else if (state.overview) void refreshStatsOnly();
  }, 700);

  async function refreshStatsOnly() {
    try {
      const data = await api('/api/overview');
      state.overview = data;
      state.live = data.live || [];
      renderStats(data.stats);
      renderLiveList(state.live);
      if (state.view === 'overview') renderRecent(data.recent || []);
    } catch (error) {
      /* silent */
    }
  }

  function handleGlobalEvent(event) {
    if (!event) return;
    if (['call.started', 'call.status', 'call.ended', 'call.summary'].includes(event.type)) {
      refreshOverviewDebounced();
    }
    if (state.drawerCallId && event.callId === state.drawerCallId) {
      if (event.type === 'call.ended') {
        void openDrawer(state.drawerCallId);
        return;
      }
      if (event.type === 'call.status' && event.data && event.data.status) {
        const badge = $('.meta-item .badge');
        if (badge) badge.replaceWith(statusBadge(event.data.status));
        const hangup = $('#drawer-hangup');
        hangup.classList.toggle('hidden', event.data.status === 'ended');
      }
      if ((event.type === 'transcript.message' || event.type === 'assistant.message') && event.data && event.data.text) {
        const container = $('#drawer-transcript');
        const role = event.type === 'transcript.message' ? 'user' : 'assistant';
        const last = container.lastElementChild;
        const lastText = last ? last.textContent : '';
        if (!lastText.startsWith(event.data.text)) {
          container.append(bubble(role, event.data.text, role === 'user' ? 'caller' : 'agent'));
          container.scrollTop = container.scrollHeight;
        }
      }
    }
  }

  // ------------------------------------------------------------------- wire ---

  function wire() {
    $$('.nav-item').forEach((button) => button.addEventListener('click', () => setView(button.dataset.view)));
    $$('[data-goto]').forEach((button) => button.addEventListener('click', () => setView(button.dataset.goto)));

    $('#btn-new-call').addEventListener('click', () => openNewCall('', ''));
    $$('[data-close-modal]').forEach((button) => button.addEventListener('click', closeNewCall));
    $('#new-call-modal').addEventListener('click', (event) => {
      if (event.target === $('#new-call-modal')) closeNewCall();
    });

    $('#new-call-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = {
        to: form.elements.to.value.trim(),
        contactName: form.elements.contactName.value.trim() || undefined,
        greeting: form.elements.greeting.value.trim() || undefined,
      };
      try {
        const data = await api('/api/calls', { method: 'POST', body: JSON.stringify(payload) });
        closeNewCall();
        toast('Call placed', 'success');
        await loadOverview();
        if (data.call && data.call.id) openDrawer(data.call.id);
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    $('#calls-search').addEventListener(
      'input',
      debounce((event) => {
        state.filters.q = event.target.value.trim();
        state.page = 0;
        void loadCalls();
      }, 320),
    );
    $('#calls-status').addEventListener('change', (event) => {
      state.filters.status = event.target.value;
      state.page = 0;
      void loadCalls();
    });
    $('#calls-refresh').addEventListener('click', () => void loadCalls());
    $('#page-prev').addEventListener('click', () => {
      if (state.page > 0) {
        state.page -= 1;
        void loadCalls();
      }
    });
    $('#page-next').addEventListener('click', () => {
      if ((state.page + 1) * state.pageSize < state.callsTotal) {
        state.page += 1;
        void loadCalls();
      }
    });

    $('#drawer-close').addEventListener('click', closeDrawer);
    $('#drawer-backdrop').addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeDrawer();
        closeNewCall();
      }
    });

    $('#btn-start-test').addEventListener('click', () => void startTestCall());
    $('#btn-end-test').addEventListener('click', () => endTestCall(false));
    $('#say-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = $('#say-input');
      const text = input.value.trim();
      if (!text) return;
      if (state.test.ws && state.test.ws.readyState === WebSocket.OPEN) {
        state.test.ws.send(JSON.stringify({ type: 'text', text }));
        input.value = '';
      } else {
        toast('Start a call first', 'error');
      }
    });

    $('#contact-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = {
        name: form.elements.name.value.trim(),
        phone: form.elements.phone.value.trim(),
        email: form.elements.email.value.trim() || undefined,
        company: form.elements.company.value.trim() || undefined,
        notes: form.elements.notes.value.trim() || undefined,
      };
      try {
        await api('/api/contacts', { method: 'POST', body: JSON.stringify(payload) });
        form.reset();
        toast('Contact saved', 'success');
        await loadContacts();
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    $('#settings-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = {
        name: form.elements.name.value.trim(),
        company: form.elements.company.value.trim(),
        greeting: form.elements.greeting.value.trim(),
        persona: form.elements.persona.value.trim(),
        knowledge: form.elements.knowledge.value,
        voice: form.elements.voice.value.trim(),
        language: form.elements.language.value.trim(),
        fallback: form.elements.fallback.value.trim(),
        transferNumber: form.elements.transferNumber.value.trim(),
        maxTurns: Number(form.elements.maxTurns.value) || 40,
        bargeIn: form.elements.bargeIn.checked,
      };
      try {
        const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
        toast('Settings saved', 'success');
        if (state.overview) state.overview.settings = data.settings;
        renderTestIntro();
      } catch (error) {
        toast(error.message, 'error');
      }
    });

    for (const target of ['llm', 'tts']) {
      const button = $(`#btn-test-${target}`);
      if (!button) continue;
      button.addEventListener('click', async () => {
        button.disabled = true;
        const label = $('#provider-test-result');
        label.textContent = 'Testing…';
        try {
          const result = await api('/api/providers/test', { method: 'POST', body: JSON.stringify({ target }) });
          const sample = result.sample ? ` · “${result.sample}”` : '';
          const audio = result.audioSeconds ? ` · ${result.audioSeconds}s audio` : '';
          label.textContent = `${target.toUpperCase()} ok · ${result.provider}${result.model ? ` (${result.model})` : ''} · ${result.latencyMs}ms${sample}${audio}`;
          toast(`${target.toUpperCase()} provider responded`, 'success');
        } catch (error) {
          label.textContent = `${target.toUpperCase()} failed: ${error.message}`;
          toast(error.message, 'error');
        } finally {
          button.disabled = false;
        }
      });
    }

    $('#token-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = $('#token-input');
      const value = input.value.trim();
      if (!value || value.startsWith('••')) {
        toast('Paste a real API key', 'error');
        return;
      }
      state.token = value;
      localStorage.setItem('voxdesk_token', value);
      toast('API key saved', 'success');
      $('#login-overlay').classList.add('hidden');
      connectLive();
      void loadOverview();
      void loadSettings();
    });

    $('#btn-clear-token').addEventListener('click', () => {
      state.token = '';
      localStorage.removeItem('voxdesk_token');
      $('#token-input').value = '';
      toast('API key cleared');
      connectLive();
    });

    $('#login-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const value = $('#login-input').value.trim();
      if (!value) return;
      state.token = value;
      localStorage.setItem('voxdesk_token', value);
      $('#login-overlay').classList.add('hidden');
      $('#login-error').textContent = '';
      connectLive();
      void loadOverview();
    });

    window.addEventListener('hashchange', () => setView(location.hash.replace('#', '') || 'overview'));

    setInterval(() => {
      if (document.hidden) return;
      if (state.view === 'overview') void refreshStatsOnly();
      if (state.view === 'calls' && !state.filters.q) void loadCalls();
    }, 20000);
  }

  // ------------------------------------------------------------------- boot ---

  async function boot() {
    wire();
    try {
      const health = await api('/api/health');
      $('#build-line').textContent = `v${health.version} · ${health.uptimeSeconds}s uptime`;
    } catch (error) {
      if (error.message !== 'Unauthorized') {
        $('#conn-text').textContent = 'offline';
      }
    }
    await loadOverview();
    if (state.overview && state.overview.settings) {
      $('#test-agent-name').textContent = `${state.overview.settings.name} · ${state.overview.settings.company}`;
    }
    renderTestIntro();
    connectLive();
    const initial = location.hash.replace('#', '') || 'overview';
    setView(initial === 'overview' ? 'overview' : initial);
  }

  document.addEventListener('DOMContentLoaded', () => {
    void boot();
  });
})();
