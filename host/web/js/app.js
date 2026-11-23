/* CamDeck: interface web. Sem dependências; fala com o camd por fetch (/api/*),
   recebe o estado ao vivo por Server-Sent Events (/api/events) e mostra o vídeo
   como MJPEG num <img> (/stream.mjpg), que qualquer browser suporta. */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const S = {
  camera: null,        // estado da câmara (public_state do camd)
  stat: {},
  rec: { recording: false, elapsed: 0, processing: [] },
  disk: null,
  tab: 'live',
  gallery: [],
  galKind: '',
  viewerIdx: -1,
  builtFor: '',        // assinatura dos controlos já construídos
  dragging: null,      // controlo a ser mexido (não o atualizar por baixo do dedo)
  recTick: null,
  version: '',
};

// ── utilitários ──────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const o = { method: opts.method || (opts.body ? 'POST' : 'GET'), headers: {} };
  if (opts.body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.body); }
  const r = await fetch(path, o);
  let j = {};
  try { j = await r.json(); } catch (_) { /* resposta sem JSON */ }
  if (r.status === 401 && !path.endsWith('/login')) { showLogin(); throw new Error('sessão expirada'); }
  if (!r.ok) throw new Error(j.error || `erro ${r.status}`);
  return j;
}

let toastTimer;
function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, kind === 'err' ? 4500 : 2200);
}

const fmtBytes = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB'
  : (b / 1e3).toFixed(0) + ' KB';
const fmtTime = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
  return (h ? h + ':' : '') + String(m).padStart(2, '0') + ':' + String(x).padStart(2, '0'); };
const fmtDate = t => new Date(t * 1000).toLocaleString('pt-PT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null) e.append(k.nodeType ? k : document.createTextNode(k));
  return e;
}

// ── sessão ───────────────────────────────────────────────────────────────
function showLogin(firstRun) {
  $('#app').hidden = true;
  $('#login').hidden = false;
  if (firstRun !== undefined) $('#login-first').hidden = !firstRun;
  stopStreams();
  if (S.es) { S.es.close(); S.es = null; }
  setTimeout(() => $('#password').focus(), 50);
}

$('#login-form').addEventListener('submit', async e => {
  e.preventDefault();
  const err = $('#login-error');
  err.hidden = true;
  try {
    await api('/api/login', { body: { password: $('#password').value } });
    $('#password').value = '';
    start();
  } catch (x) { err.textContent = x.message; err.hidden = false; }
});

$('#logout').addEventListener('click', async () => { await api('/api/logout', { method: 'POST' }); location.reload(); });

async function boot() {
  const s = await api('/api/session');
  S.version = s.version;
  if (!s.authed) return showLogin(s.first_run);
  start();
}

async function start() {
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#menu-version').textContent = S.version;
  try {
    const st = await api('/api/state');
    S.disk = st.disk;
    onState(st.camera); onStat(st.stat); onRec(st.rec);
    if (st.ai) aiOnState(st.ai);
    if (st.power) onPower(st.power);
  } catch (_) { return; }
  connectEvents();
  setTab(location.hash.slice(1) || 'live');
}

// ── eventos ao vivo ──────────────────────────────────────────────────────
function connectEvents() {
  if (S.es) S.es.close();
  const es = S.es = new EventSource('/api/events');
  es.addEventListener('state', e => onState(JSON.parse(e.data)));
  es.addEventListener('stat', e => onStat(JSON.parse(e.data)));
  es.addEventListener('rec', e => onRec(JSON.parse(e.data)));
  es.addEventListener('ai', e => aiOnResult(JSON.parse(e.data)));
  es.addEventListener('ai_state', e => aiOnState(JSON.parse(e.data)));
  es.addEventListener('ai_event', e => aiOnEvent(JSON.parse(e.data)));
  es.addEventListener('power', e => onPower(JSON.parse(e.data)));
  es.addEventListener('media', e => { const m = JSON.parse(e.data); if (S.tab === 'gallery') loadGallery(); if (m.name && m.kind === 'photos') showLastShot(m.name); });
  es.onerror = async () => {
    // O EventSource volta a ligar sozinho; só confirma se a sessão ainda vale
    try { const s = await api('/api/session'); if (!s.authed) showLogin(); } catch (_) { /* servidor em baixo */ }
  };
}

function onState(cam) {
  const prevStreaming = S.camera && S.camera.streaming;
  S.camera = cam;
  const pill = $('#pill-link');
  if (!cam.connected) { pill.className = 'pill off'; pill.textContent = 'Câmara desligada'; }
  else if (!cam.ready) { pill.className = 'pill wait'; pill.textContent = 'A configurar…'; }
  else { pill.className = 'pill on'; pill.textContent = (cam.info.sensor || 'Câmara') + ' · USB'; }
  $('#btn-snap').disabled = $('#btn-rec').disabled = !cam.ready && !S.rec.recording;
  $('#settings-off').hidden = !!cam.ready;
  buildControls();
  updateControls();
  updateLiveEmpty();
  renderDevice();
  if (cam.ready && !prevStreaming) refreshStreams();
}

function onStat(st) {
  S.stat = st;
  const parts = [];
  if (st.streaming) parts.push(`${Math.round(st.host_fps || 0)} fps`, `${Math.round(st.kbps || 0)} KB/s`);
  if (st.temp != null) parts.push(`${st.temp.toFixed(0)} °C`);
  $('#hdr-stats').textContent = parts.join(' · ');
  const info = S.camera && S.camera.info;
  const size = info && (info.sizes || []).find(s => s.name === S.camera.stream_size);
  $('#ov-info').textContent = st.streaming && size ? `${size.w}×${size.h} · ${Math.round(st.host_fps)} fps` : '';
  const peak = Math.min(1, (st.mic_peak || 0) / 32767);
  $('#mic-meter').style.width = (Math.sqrt(peak) * 100).toFixed(0) + '%';
  if (S.tab === 'device') renderDevice();
}

function onRec(r) {
  const was = S.rec.recording;
  S.rec = r;
  S.recBase = { t: performance.now(), elapsed: r.elapsed };
  const on = r.recording;
  $('#btn-rec').classList.toggle('active', on);
  $('#btn-rec-label').textContent = on ? 'Parar' : 'Gravar';
  $('#pill-rec').hidden = $('#ov-rec').hidden = !on;
  $('#quick-size').disabled = on;
  const badge = $('#badge-proc');
  badge.hidden = !r.processing.length;
  badge.textContent = r.processing.length;
  clearInterval(S.recTick);
  if (on) S.recTick = setInterval(updateRecTime, 500);
  updateRecTime();
  if (was && !on) toast('Gravação parada; a converter para MP4…');
  if (S.tab === 'gallery' && was !== on) loadGallery();
  if (S.tab === 'gallery' && !on && !r.processing.length) loadGallery();
}

function updateRecTime() {
  if (!S.rec.recording) return;
  const t = fmtTime(S.recBase.elapsed + (performance.now() - S.recBase.t) / 1000);
  $('#pill-rec-time').textContent = $('#ov-rec-time').textContent = t;
}

// ── abas ─────────────────────────────────────────────────────────────────
function setTab(t) {
  if (!$(`#tab-${t}`)) t = 'live';
  S.tab = t;
  $$('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === t));
  $$('.panel-tab').forEach(p => p.classList.toggle('active', p.id === `tab-${t}`));
  history.replaceState(null, '', '#' + t);
  refreshStreams();
  if (t === 'gallery') loadGallery();
  if (t === 'device') { renderDevice(); renderPower(); }
  if (t === 'ai') aiTabOpened();
}
$$('.tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));

// ── stream (só a imagem visível tem ligação aberta) ──────────────────────
function streamTarget() {
  if (document.hidden) return null;
  if (S.tab === 'live') return $('#live');
  if (S.tab === 'settings') return $('#settings-preview');
  if (S.tab === 'ai') return $('#ai-preview');
  return null;
}

function stopStreams() {
  for (const img of [$('#live'), $('#settings-preview'), $('#ai-preview')]) {
    if (img.getAttribute('src')) { img.removeAttribute('src'); img.src = ''; img.removeAttribute('src'); }
  }
}

function refreshStreams() {
  const target = streamTarget();
  for (const img of [$('#live'), $('#settings-preview'), $('#ai-preview')]) {
    if (img !== target && img.getAttribute('src')) { img.removeAttribute('src'); }
  }
  if (target && S.camera && S.camera.connected && !target.getAttribute('src')) {
    target.src = '/stream.mjpg?t=' + Date.now();
  }
  updateLiveEmpty();
}

for (const img of [$('#live'), $('#settings-preview'), $('#ai-preview')]) {
  img.addEventListener('load', () => { img.dataset.ok = '1'; updateLiveEmpty(); });
  img.addEventListener('error', () => {
    if (!img.getAttribute('src')) return;
    img.dataset.ok = '';
    img.removeAttribute('src');
    updateLiveEmpty();
    setTimeout(refreshStreams, 2000);
  });
}
document.addEventListener('visibilitychange', refreshStreams);
// O Chrome não dispara "load" num <img> MJPEG: confirma pela largura do primeiro quadro
setInterval(() => {
  const live = $('#live');
  const ok = live.getAttribute('src') && live.naturalWidth > 0 ? '1' : '';
  if (ok !== (live.dataset.ok || '')) { live.dataset.ok = ok; updateLiveEmpty(); }
}, 400);

function updateLiveEmpty() {
  const cam = S.camera;
  const live = $('#live');
  const showing = live.getAttribute('src') && live.dataset.ok && cam && cam.connected;
  $('#live-empty').hidden = !!showing;
  $('#live-empty-text').textContent = !cam || !cam.connected
    ? 'Câmara desligada. Ligue o XIAO ESP32-S3 por USB.'
    : !cam.ready ? 'A configurar a câmara…' : 'A iniciar o vídeo…';
}

// ── foto e gravação ──────────────────────────────────────────────────────
async function snap() {
  const b = $('#btn-snap');
  if (b.disabled) return;
  b.disabled = true;
  const f = $('#flash');
  f.classList.remove('go'); void f.offsetWidth; f.classList.add('go');
  try {
    const r = await api('/api/snap', { method: 'POST', body: {} });
    toast(`Foto guardada: ${r.name} (${fmtBytes(r.bytes)})`, 'ok');
    showLastShot(r.name);
  } catch (x) { toast('Foto: ' + x.message, 'err'); }
  b.disabled = !(S.camera && S.camera.ready);
}

async function toggleRec() {
  const on = !S.rec.recording;
  $('#btn-rec').disabled = true;
  try { const r = await api('/api/record', { body: { on } }); onRec(r.rec); if (on) toast('A gravar…'); }
  catch (x) { toast('Gravação: ' + x.message, 'err'); }
  $('#btn-rec').disabled = false;
}

function showLastShot(name) {
  $('#last-shot').hidden = false;
  $('#last-shot-img').src = `/thumb/photos/${encodeURIComponent(name)}`;
  $('#last-shot-name').textContent = name;
  $('#last-shot').dataset.name = name;
}

$('#btn-snap').addEventListener('click', snap);
$('#btn-rec').addEventListener('click', toggleRec);
$('#last-shot').addEventListener('click', async () => {
  await loadGallery(true);
  const i = S.gallery.findIndex(g => g.kind === 'photos' && g.name === $('#last-shot').dataset.name);
  if (i >= 0) openViewer(i);
});

function toggleFullscreen() {
  const w = $('#live-wrap');
  if (document.fullscreenElement) document.exitFullscreen();
  else if (w.requestFullscreen) w.requestFullscreen().catch(() => toast('Ecrã inteiro indisponível', 'err'));
}
$('#btn-fs').addEventListener('click', toggleFullscreen);
$('#fs-exit').addEventListener('click', toggleFullscreen);
$('#live').addEventListener('dblclick', toggleFullscreen);
document.addEventListener('fullscreenchange', () => { $('#fs-exit').hidden = !document.fullscreenElement; });

document.addEventListener('keydown', e => {
  if (!$('#viewer').hidden) return viewerKey(e);
  if (e.target.matches('input, select, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
  if (S.tab !== 'live') return;
  if (e.key === ' ') { e.preventDefault(); snap(); }
  else if (e.key === 'r' || e.key === 'R') toggleRec();
  else if (e.key === 'f' || e.key === 'F') toggleFullscreen();
});

// ── controlos (Ajustes, atalhos do Ao vivo, microfone) ───────────────────
const signed = v => v > 0 ? `+${v}` : v < 0 ? `−${-v}` : '0';
const CONTROLS = {
  'grp-image': [
    { k: 'brightness', label: 'Brilho', type: 'range', fmt: signed },
    { k: 'contrast', label: 'Contraste', type: 'range', fmt: signed },
    { k: 'saturation', label: 'Saturação', type: 'range', fmt: signed },
    { k: 'sharpness', label: 'Nitidez', type: 'range', fmt: signed },
    { k: 'denoise', label: 'Redução de ruído', type: 'range' },
    { k: 'hmirror', label: 'Espelhar (horizontal)', type: 'switch' },
    { k: 'vflip', label: 'Inverter (vertical)', type: 'switch' },
  ],
  'grp-exposure': [
    { k: 'aec', label: 'Exposição automática', type: 'switch' },
    { k: 'aec2', label: 'Exposição automática (DSP)', type: 'switch', when: s => s.aec },
    { k: 'ae_level', label: 'Compensação', type: 'range', when: s => s.aec, fmt: signed },
    { k: 'aec_value', label: 'Exposição manual', type: 'range', when: s => !s.aec },
    { k: 'agc', label: 'Ganho automático', type: 'switch' },
    // OV2640: 0..6 = 2×..128×; OV3660: valor bruto do registo, 16 = 1×
    { k: 'gainceiling', label: 'Limite do ganho', type: 'select', when: s => s.agc,
      opts: ['2×', '4×', '8×', '16×', '32×', '64×', '128×'].map((l, i) => [i, l]),
      rangeIf: r => r[1] > 6, fmt: v => `${(v / 16).toFixed(1)}×` },
    { k: 'agc_gain', label: 'Ganho manual', type: 'range', when: s => !s.agc },
  ],
  'grp-color': [
    { k: 'awb', label: 'Balanço de brancos automático', type: 'switch' },
    { k: 'awb_gain', label: 'Ganho do balanço de brancos', type: 'switch' },
    { k: 'wb_mode', label: 'Modo de luz', type: 'select', when: s => s.awb_gain,
      opts: [[0, 'Automático'], [1, 'Sol'], [2, 'Nublado'], [3, 'Escritório'], [4, 'Casa']] },
    { k: 'special_effect', label: 'Efeito', type: 'select',
      opts: [[0, 'Nenhum'], [1, 'Negativo'], [2, 'Preto e branco'], [3, 'Avermelhado'], [4, 'Esverdeado'], [5, 'Azulado'], [6, 'Sépia']] },
  ],
  'grp-stream': [
    { k: 'framesize', label: 'Resolução do vídeo', type: 'size', src: 'stream' },
    { k: 'snap_size', label: 'Resolução das fotos', type: 'size', src: 'snap', pref: true },
    { k: 'fps_max', label: 'Limite de quadros por segundo', type: 'select', stream: true,
      opts: [[0, 'Sem limite'], [5, '5 fps'], [10, '10 fps'], [15, '15 fps'], [20, '20 fps'], [25, '25 fps']] },
    { k: 'quality', label: 'Compressão JPEG (menor = melhor imagem)', type: 'range' },
  ],
  'grp-advanced': [
    { k: 'lenc', label: 'Correção da lente', type: 'switch' },
    { k: 'raw_gma', label: 'Correção de gama', type: 'switch' },
    { k: 'bpc', label: 'Corrigir píxeis pretos', type: 'switch' },
    { k: 'wpc', label: 'Corrigir píxeis brancos', type: 'switch' },
    { k: 'dcw', label: 'Redução por DCW', type: 'switch' },
    { k: 'colorbar', label: 'Barras de teste', type: 'switch' },
  ],
  'grp-mic': [
    { k: 'mic', label: 'Gravar som com o vídeo', type: 'switch', pref: true },
    { k: 'mic_gain', label: 'Ganho do microfone', type: 'range', pref: true, range: [1, 16], fmt: v => `${v}×` },
  ],
};

function sizeLabel(s) { return `${s.name} · ${s.w}×${s.h}`; }

// Valor atual de um controlo, venha do sensor, do stream ou das preferências do camd
function ctlValue(c) {
  const cam = S.camera, info = cam.info || {};
  if (c.k === 'framesize') return cam.stream_size;
  if (c.k === 'snap_size') return cam.snap_size;
  if (c.k === 'fps_max') return cam.fps_max || 0;
  if (c.k === 'mic') return cam.mic_record;
  if (c.k === 'mic_gain') return cam.mic_gain;
  return (info.settings || {})[c.k];
}

function ctlAvailable(c) {
  if (c.pref || c.stream || c.type === 'size') return true;
  return S.camera && S.camera.info && S.camera.info.settings && c.k in S.camera.info.settings;
}

function buildControls() {
  const cam = S.camera;
  if (!cam || !cam.info) return;
  const sig = JSON.stringify([Object.keys(cam.info.settings || {}), (cam.info.sizes || []).map(s => s.name)]);
  if (sig === S.builtFor) return;
  S.builtFor = sig;
  for (const [grp, list] of Object.entries(CONTROLS)) {
    const box = $('#' + grp);
    box.replaceChildren();
    for (const c of list) {
      if (!ctlAvailable(c)) continue;
      box.append(makeControl(c));
    }
  }
  // atalhos do Ao vivo
  const sizes = cam.info.sizes || [];
  $('#quick-size').replaceChildren(...sizes.map(s => el('option', { value: s.name }, sizeLabel(s))));
  $('#quick-snap').replaceChildren(...sizes.map(s => el('option', { value: s.name }, sizeLabel(s))));
}

function makeControl(c) {
  const wrap = el('div', { class: 'ctl', 'data-k': c.k });
  const range = c.range || ((S.camera.info.ranges || {})[c.k]) || [0, 1];
  let input;
  if (c.rangeIf && c.rangeIf(range)) c = { ...c, type: 'range' };
  if (c.type === 'switch') {
    input = el('input', { type: 'checkbox', class: 'switch' });
    input.addEventListener('change', () => sendCtl(c, input.checked ? 1 : 0));
    wrap.append(el('label', { class: 'switch-row' }, el('span', {}, c.label), input));
  } else if (c.type === 'range') {
    const v = el('span', { class: 'v' });
    input = el('input', { type: 'range', min: range[0], max: range[1], step: 1 });
    input.addEventListener('input', () => { v.textContent = c.fmt ? c.fmt(+input.value) : input.value; S.dragging = c.k; });
    input.addEventListener('change', () => { S.dragging = null; sendCtl(c, +input.value); });
    wrap.append(el('div', { class: 'ctl-head' }, el('span', {}, c.label), v), input);
  } else {
    input = el('select');
    const opts = c.type === 'size' ? (S.camera.info.sizes || []).map(s => [s.name, sizeLabel(s)]) : c.opts;
    for (const [val, lab] of opts) input.append(el('option', { value: val }, lab));
    input.addEventListener('change', () => sendCtl(c, c.type === 'size' ? input.value : +input.value));
    wrap.append(el('label', {}, c.label, input));
  }
  wrap._input = input;
  wrap._ctl = c;
  return wrap;
}

function updateControls() {
  const cam = S.camera;
  if (!cam || !cam.info) return;
  const settings = cam.info.settings || {};
  for (const w of $$('.ctl')) {
    const c = w._ctl, input = w._input;
    if (!c || S.dragging === c.k || document.activeElement === input) continue;
    const val = ctlValue(c);
    if (c.type === 'switch') input.checked = !!val;
    else if (val != null) input.value = val;
    if (c.type === 'range') { const v = $('.v', w); if (v) v.textContent = c.fmt ? c.fmt(+input.value) : input.value; }
    const enabled = (c.pref || cam.ready) && (!c.when || c.when(settings))
      && !(c.k === 'framesize' && S.rec.recording);
    w.classList.toggle('disabled', !enabled);
    input.disabled = !enabled;
  }
  if (document.activeElement !== $('#quick-size')) $('#quick-size').value = cam.stream_size;
  if (document.activeElement !== $('#quick-snap')) $('#quick-snap').value = cam.snap_size;
  $('#quick-mic').checked = !!cam.mic_record;
}

async function sendCtl(c, val) {
  try {
    let r;
    if (c.pref) r = await api('/api/prefs', { body: { [c.k]: val } });
    else r = await api('/api/set', { body: { key: c.k, val } });
    if (r.camera) onState(r.camera);
  } catch (x) {
    toast(`${c.label}: ${x.message}`, 'err');
    updateControls();
  }
}

$('#quick-size').addEventListener('change', e => sendCtl({ k: 'framesize', label: 'Resolução' }, e.target.value));
$('#quick-snap').addEventListener('change', e => sendCtl({ k: 'snap_size', label: 'Fotos', pref: true }, e.target.value));
$('#quick-mic').addEventListener('change', e => sendCtl({ k: 'mic', label: 'Som', pref: true }, e.target.checked));

$('#btn-reset').addEventListener('click', async () => {
  if (!confirm('Repor todos os ajustes do sensor? A câmara reinicia.')) return;
  try { await api('/api/reset', { method: 'POST' }); toast('Ajustes repostos; a câmara está a reiniciar'); }
  catch (x) { toast(x.message, 'err'); }
});

// ── Galeria ──────────────────────────────────────────────────────────────
async function loadGallery(silent) {
  try {
    const r = await api('/api/media' + (S.galKind ? `?kind=${S.galKind}` : ''));
    S.gallery = r.items;
    S.disk = r.disk;
    renderGallery(r.processing || []);
  } catch (x) { if (!silent) toast('Galeria: ' + x.message, 'err'); }
}

function renderGallery(processing) {
  const g = $('#gallery');
  const cards = [];
  if (S.galKind !== 'photos') {
    for (const p of processing) {
      cards.push(el('div', { class: 'g-item proc' },
        el('div', { class: 'g-thumb' }, 'A converter…'),
        el('div', { class: 'g-meta' }, el('b', { class: 'mono' }, p), el('span', {}, 'vídeo em processamento'))));
    }
  }
  S.gallery.forEach((it, i) => {
    const thumb = el('div', { class: 'g-thumb' },
      el('img', { loading: 'lazy', alt: '', src: `/thumb/${it.kind}/${encodeURIComponent(it.name)}` }));
    if (it.kind === 'videos') {
      thumb.append(el('span', { class: 'g-kind' }, '▶'));
      if (it.duration) thumb.append(el('span', { class: 'g-badge' }, fmtTime(it.duration)));
    } else if (it.w) thumb.append(el('span', { class: 'g-badge' }, `${it.w}×${it.h}`));
    cards.push(el('button', { class: 'g-item', onclick: () => openViewer(i) }, thumb,
      el('div', { class: 'g-meta' }, el('b', {}, fmtDate(it.mtime)), el('span', {}, fmtBytes(it.bytes)))));
  });
  g.replaceChildren(...cards);
  $('#gal-empty').hidden = cards.length > 0;
  const nP = S.gallery.filter(i => i.kind === 'photos').length, nV = S.gallery.length - nP;
  $('#gal-count').textContent = [nP && `${nP} foto${nP > 1 ? 's' : ''}`, nV && `${nV} vídeo${nV > 1 ? 's' : ''}`].filter(Boolean).join(' · ');
  if (S.disk) $('#gal-disk').textContent = `${fmtBytes(S.disk.free)} livres`;
}

$$('#gal-filter button').forEach(b => b.addEventListener('click', () => {
  $$('#gal-filter button').forEach(x => x.classList.toggle('active', x === b));
  S.galKind = b.dataset.kind;
  loadGallery();
}));

function openViewer(i) {
  S.viewerIdx = i;
  const it = S.gallery[i];
  if (!it) return closeViewer();
  const url = `/media/${it.kind}/${encodeURIComponent(it.name)}`;
  const m = $('#viewer-media');
  m.replaceChildren(it.kind === 'videos'
    ? el('video', { src: url, controls: true, autoplay: true, playsinline: true })
    : el('img', { src: url, alt: it.name }));
  $('#viewer-title').textContent = it.name;
  const info = [fmtDate(it.mtime), fmtBytes(it.bytes)];
  if (it.w) info.push(`${it.w}×${it.h}`);
  if (it.duration) info.push(fmtTime(it.duration));
  if (it.kind === 'videos') info.push(it.audio ? 'com som' : 'sem som');
  $('#viewer-info').textContent = info.join(' · ');
  $('#viewer-dl').href = url + '?dl=1';
  $('#viewer-prev').disabled = i <= 0;
  $('#viewer-next').disabled = i >= S.gallery.length - 1;
  $('#viewer').hidden = false;
}

function closeViewer() {
  $('#viewer').hidden = true;
  $('#viewer-media').replaceChildren();
  S.viewerIdx = -1;
}

async function deleteCurrent() {
  const it = S.gallery[S.viewerIdx];
  if (!it || !confirm(`Apagar ${it.name}?`)) return;
  try {
    await api(`/api/media/${it.kind}/${encodeURIComponent(it.name)}`, { method: 'DELETE' });
    S.gallery.splice(S.viewerIdx, 1);
    renderGallery([]);
    if (S.gallery.length) openViewer(Math.min(S.viewerIdx, S.gallery.length - 1)); else closeViewer();
    toast('Apagado');
  } catch (x) { toast(x.message, 'err'); }
}

function viewerKey(e) {
  if (e.key === 'Escape') closeViewer();
  else if (e.key === 'ArrowLeft' && S.viewerIdx > 0) openViewer(S.viewerIdx - 1);
  else if (e.key === 'ArrowRight' && S.viewerIdx < S.gallery.length - 1) openViewer(S.viewerIdx + 1);
  else if (e.key === 'Delete') deleteCurrent();
}

$('#viewer-close').addEventListener('click', closeViewer);
$('#viewer-prev').addEventListener('click', () => openViewer(S.viewerIdx - 1));
$('#viewer-next').addEventListener('click', () => openViewer(S.viewerIdx + 1));
$('#viewer-del').addEventListener('click', deleteCurrent);

// ── Energia do uConsole ──────────────────────────────────────────────────
const POWER_MSG = {
  low: p => `Sem corrente: o uConsole está nas baterias (${p.battery_pct} %). Verifique o carregador ou o powerbank. A análise de imagem está em pausa até lá.`,
  critical: p => `Bateria quase vazia (${p.battery_pct} %, ${p.battery_v} V) e sem corrente! Ligue o carregador já. A análise parou e as gravações são terminadas.`,
  fault: p => `Problema de energia: ${p.reason}. Ligue o uConsole à corrente; a análise de imagem está em pausa.`,
};

function onPower(p) {
  const prev = S.power;
  S.power = p;
  const pill = $('#pill-power');
  pill.hidden = !p.available;
  if (p.available) {
    const pct = p.battery_pct != null ? `${p.battery_pct} %` : '';
    pill.textContent = p.on_battery ? `🔋 ${pct}` : `⚡ ${pct}`;
    pill.className = 'pill power ' + (p.level !== 'ok' ? p.level : p.on_battery ? 'ok' : 'mains');
    pill.title = p.on_battery ? 'Na bateria' : 'Na corrente' + (p.battery_status === 'Charging' ? ' (a carregar)' : '');
  }
  const bad = POWER_MSG[p.level];
  $('#power-banner').hidden = !bad;
  if (bad) {
    $('#power-banner').className = 'power-banner ' + p.level;
    $('#power-banner-text').textContent = bad(p);
  }
  if (bad && (!prev || prev.level !== p.level)) toast(bad(p), 'err');
  if (prev && prev.level !== 'ok' && p.level === 'ok') toast('Energia normal; a análise pode continuar', 'ok');
  if (S.tab === 'device') renderPower();
}

function renderPower() {
  const p = S.power;
  if (!p) return;
  const pill = $('#power-state');
  pill.className = 'pill ' + (p.level === 'ok' ? 'on' : p.level === 'low' ? 'wait' : 'off');
  pill.textContent = { ok: 'normal', low: 'bateria fraca', critical: 'bateria quase vazia', fault: 'problema' }[p.level];
  const status = { Charging: 'a carregar', Discharging: 'a descarregar', Full: 'cheia', 'Not charging': 'sem carregar' };
  kv($('#dev-power'), [
    ['Alimentação', p.source === 'bateria' ? 'baterias' : 'corrente (carregador)'],
    ['Bateria', p.battery_pct != null ? `${p.battery_pct} % · ${status[p.battery_status] || p.battery_status || ''}` : null],
    ['Tensão da bateria', p.battery_v != null ? `${p.battery_v.toFixed(2)} V` : null],
    ['Corrente da bateria', p.battery_a != null ? `${p.battery_a.toFixed(2)} A` : null],
    ['Temperatura do CPU', p.temp != null ? `${p.temp.toFixed(1)} °C` + (p.hot ? ' (quente!)' : p.warm ? ' (morno)' : '') : null],
    ['Chip de energia (AXP)', p.pmic_ok ? 'a responder' : 'sem resposta'],
    ['Subtensão', p.undervolt ? 'sim' : 'não'],
  ]);
}

// ── Dispositivo ──────────────────────────────────────────────────────────
function kv(dl, rows) {
  dl.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v ?? '—')]));
}

function renderDevice() {
  const cam = S.camera, st = S.stat || {};
  if (!cam) return;
  const info = cam.info || {};
  const pill = $('#dev-state');
  pill.className = 'pill ' + (cam.ready ? 'on' : cam.connected ? 'wait' : 'off');
  pill.textContent = cam.ready ? 'pronta' : cam.connected ? 'a configurar' : 'desligada';
  kv($('#dev-info'), [
    ['Firmware', info.fw],
    ['Sensor', info.sensor && `${info.sensor} (máx. ${info.max})`],
    ['Temperatura do ESP', st.temp != null ? `${st.temp.toFixed(1)} °C` : null],
    ['Ligada há', st.up != null ? fmtTime(st.up) : null],
    ['PSRAM livre', st.psram != null ? `${fmtBytes(st.psram)} de ${fmtBytes(info.psram_total || 0)}` : null],
    ['RAM interna livre', st.heap != null ? fmtBytes(st.heap) : null],
    ['Rádio (Wi-Fi/BT)', 'desligado'],
  ]);
  kv($('#dev-link'), [
    ['Vídeo', st.streaming ? 'a transmitir' : 'parado'],
    ['Quadros/s (ESP → uConsole)', st.streaming ? `${(st.fps || 0).toFixed(1)} → ${st.host_fps}` : null],
    ['Débito', st.streaming ? `${Math.round(st.kbps || 0)} KB/s` : null],
    ['Quadro médio', st.avg_kb ? `${st.avg_kb.toFixed(1)} KB` : null],
    ['Quem está a ver', st.viewers],
    ['Pacotes perdidos', st.usb_lost],
    ['Erros de CRC', st.usb_crc],
    ['Falhas de envio no ESP', st.tx_err],
  ]);
  if (S.disk) kv($('#dev-disk'), [['Livre', fmtBytes(S.disk.free)], ['Total', fmtBytes(S.disk.total)],
    ['Fotos', '~/Pictures/CamDeck'], ['Vídeos', '~/Videos/CamDeck']]);
}

$('#pw-form').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('/api/password', { body: { current: $('#pw-cur').value, new: $('#pw-new').value } });
    $('#pw-cur').value = $('#pw-new').value = '';
    toast('Palavra-passe alterada', 'ok');
  } catch (x) { toast(x.message, 'err'); }
});

$('#btn-reboot').addEventListener('click', async () => {
  if (!confirm('Reiniciar a câmara?')) return;
  try { await api('/api/reboot', { method: 'POST' }); toast('A reiniciar a câmara…'); }
  catch (x) { toast(x.message, 'err'); }
});

// ── menu ─────────────────────────────────────────────────────────────────
$('#menu-btn').addEventListener('click', e => {
  e.stopPropagation();
  const m = $('#menu');
  m.hidden = !m.hidden;
  $('#menu-btn').setAttribute('aria-expanded', String(!m.hidden));
});
document.addEventListener('click', () => { $('#menu').hidden = true; $('#menu-btn').setAttribute('aria-expanded', 'false'); });
$$('[data-goto]').forEach(b => b.addEventListener('click', () => setTab(b.dataset.goto)));

boot().catch(() => showLogin());
