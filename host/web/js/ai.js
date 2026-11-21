/* CamDeck: análise de imagem (IA). Desenha os resultados por cima do vídeo, liga e desliga
   as funções, gere as pessoas conhecidas e mostra os eventos. Usa os utilitários de app.js
   ($, $$, el, api, toast, S) e é carregado depois dele. */
'use strict';

const AI = {
  state: null,         // public_state do analisador
  last: null,          // último resultado
  lastAt: 0,
  people: [],
  events: [],
  enrolling: false,
};

const AI_FEATURES = [
  { k: 'objects', chip: 'Objetos', label: 'Objetos e pessoas', desc: '80 tipos: pessoas, animais, veículos, objetos do dia a dia' },
  { k: 'faces', chip: 'Caras', label: 'Caras', desc: 'deteção de caras' },
  { k: 'emotion', chip: 'Humor', label: 'Humor', desc: 'neutro, feliz, surpreso, triste, zangado, nojo, medo, desprezo' },
  { k: 'age', chip: 'Idade', label: 'Idade e género', desc: 'estimativa aproximada' },
  { k: 'names', chip: 'Nomes', label: 'Reconhecer pessoas', desc: 'dá o nome das pessoas registadas' },
  { k: 'pose', chip: 'Pose', label: 'Pose do corpo', desc: 'esqueleto e gestos: mão levantada, mãos no ar, deitado' },
  { k: 'codes', chip: 'Códigos', label: 'Códigos QR e de barras', desc: 'lê o conteúdo dos códigos' },
];

// Cores por tipo de objeto (classe COCO)
const ANIMALS = new Set([14, 15, 16, 17, 18, 19, 20, 21, 22, 23]);
const VEHICLES = new Set([1, 2, 3, 4, 5, 6, 7, 8]);
function objColor(k) {
  if (k === 0) return '#39d353';
  if (ANIMALS.has(k)) return '#f0883e';
  if (VEHICLES.has(k)) return '#58a6ff';
  return '#bc8cff';
}
const FACE_COLOR = '#f2cc60';
const SKELETON = [[5, 7], [7, 9], [6, 8], [8, 10], [5, 6], [5, 11], [6, 12], [11, 12], [11, 13], [13, 15], [12, 14], [14, 16], [0, 1], [0, 2], [1, 3], [2, 4]];

// ── estado e eventos do servidor ────────────────────────────────────────────
function aiOnState(st) {
  AI.state = st;
  renderAIChips();
  renderAIFeatures();
  renderAIOptions();
  const miss = st.missing || [];
  $('#ai-missing').hidden = !miss.length;
  $('#ai-missing').textContent = miss.length
    ? `Faltam ficheiros da análise (${miss.join(', ')}). Corra setup/install-ai.sh no uConsole.` : '';
  $('#ai-error').hidden = !st.error;
  $('#ai-error').textContent = st.error ? 'Análise: ' + st.error : '';
  const pill = $('#ai-state');
  pill.className = 'pill ' + (st.active ? (st.paused ? 'wait' : st.running ? 'on' : 'wait') : 'off');
  pill.textContent = st.active ? (st.paused ? 'em pausa' : st.running ? 'a analisar' : 'a arrancar') : 'desligada';
  const n = Object.values(st.features).filter(Boolean).length;
  $('#badge-ai').hidden = !n;
  $('#badge-ai').textContent = n;
  if (!st.active) aiOnResult({ n: null });
}

function aiOnResult(r) {
  if (r.n == null) { AI.last = null; drawAll(); aiSummary(); return; }
  AI.last = r;
  AI.lastAt = performance.now();
  $('#ai-fps').textContent = r.fps ? `${r.fps}/s` : '';
  drawAll();
  aiSummary();
  if (S.tab === 'ai') renderAIPerf();
}

function aiOnEvent(ev) {
  AI.events.unshift(ev);
  AI.events.length = Math.min(AI.events.length, 200);
  if (S.tab === 'ai') renderEvents();
  if (ev.kind === 'person') toast(`${ev.label} reconhecido(a)`, 'ok');
  else if (ev.kind === 'code') toast(`Código lido: ${ev.label.slice(0, 80)}`, 'ok');
  else if (ev.kind === 'pose' && ev.label === 'deitado') toast('Pessoa deitada ou caída', 'err');
}

// ── desenho por cima do vídeo ───────────────────────────────────────────────
// O <img> usa object-fit: contain; calcula o retângulo onde a imagem aparece de facto
function contentRect(img) {
  const W = img.clientWidth, H = img.clientHeight, w = img.naturalWidth, h = img.naturalHeight;
  if (!w || !h) return null;
  const s = Math.min(W / w, H / h);
  return { x: (W - w * s) / 2, y: (H - h * s) / 2, w: w * s, h: h * s };
}

function drawAll() {
  drawOn($('#live'), $('#ai-canvas'));
  drawOn($('#ai-preview'), $('#ai-canvas2'));
}

function drawOn(img, cv) {
  const dpr = window.devicePixelRatio || 1;
  const W = img.clientWidth, H = img.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
  }
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const r = AI.last;
  if (!r || !img.getAttribute('src') || performance.now() - AI.lastAt > 2000) return;
  const R = contentRect(img);
  if (!R) return;
  const X = v => R.x + v * R.w, Y = v => R.y + v * R.h;
  const fs = Math.max(11, Math.min(15, R.w / 60));
  c.font = `600 ${fs}px system-ui, sans-serif`;
  c.lineWidth = 2;

  const label = (txt, x, y, color) => {
    const pad = 4, tw = c.measureText(txt).width;
    x = Math.max(R.x, Math.min(x, R.x + R.w - tw - pad * 2));
    const ty = y - fs - pad * 2 < R.y ? y : y - fs - pad * 2;
    c.fillStyle = color; c.fillRect(x, ty, tw + pad * 2, fs + pad * 2);
    c.fillStyle = '#0d1117'; c.fillText(txt, x + pad, ty + fs + pad - 2);
  };
  const box = (b, color, dashed) => {
    c.strokeStyle = color; c.setLineDash(dashed ? [6, 4] : []);
    c.strokeRect(X(b[0]), Y(b[1]), b[2] * R.w, b[3] * R.h);
    c.setLineDash([]);
  };

  // Pessoas com pose: o esqueleto já as mostra, não desenha a caixa de "pessoa" por cima
  for (const o of r.objects || []) {
    if (o.k === 0 && (r.poses || []).length) continue;
    const col = objColor(o.k);
    box(o.b, col);
    label(`${o.c} ${Math.round(o.p * 100)}%`, X(o.b[0]), Y(o.b[1]), col);
  }
  for (const p of r.poses || []) {
    c.strokeStyle = '#39d353';
    c.lineWidth = 3;
    for (const [a, b] of SKELETON) {
      const A = p.kp[a], B = p.kp[b];
      if (A[2] < 0.4 || B[2] < 0.4) continue;
      c.beginPath(); c.moveTo(X(A[0]), Y(A[1])); c.lineTo(X(B[0]), Y(B[1])); c.stroke();
    }
    c.fillStyle = '#e6edf3';
    for (const k of p.kp) if (k[2] >= 0.4) { c.beginPath(); c.arc(X(k[0]), Y(k[1]), 3, 0, 7); c.fill(); }
    c.lineWidth = 2;
    const tags = ['pessoa', ...(p.g || [])].join(' · ');
    label(tags, X(p.b[0]), Y(p.b[1]), p.g && p.g.includes('deitado') ? '#f85149' : '#39d353');
  }
  for (const f of r.faces || []) {
    const known = f.name, col = known ? '#39d353' : FACE_COLOR;
    box(f.b, col, !known && AI.state && AI.state.features.names);
    const parts = [];
    if (known) parts.push(f.name);
    else if (AI.state && AI.state.features.names && 'sim' in f) parts.push('?');
    if (f.emo) parts.push(f.emo);
    if (f.age != null) parts.push(`${f.sex === 'F' ? '♀' : '♂'} ${f.age}`);
    if (parts.length) label(parts.join(' · '), X(f.b[0]), Y(f.b[1]), col);
  }
  for (const q of r.codes || []) {
    c.strokeStyle = '#58a6ff'; c.lineWidth = 3;
    c.beginPath();
    q.pts.forEach(([x, y], i) => i ? c.lineTo(X(x), Y(y)) : c.moveTo(X(x), Y(y)));
    c.closePath(); c.stroke();
    c.lineWidth = 2;
    const [x0, y0] = q.pts[0];
    label(`${q.t}: ${q.d.slice(0, 40)}`, X(x0), Y(y0), '#58a6ff');
  }
}
window.addEventListener('resize', drawAll);
setInterval(() => { if (AI.last && performance.now() - AI.lastAt > 2000) drawAll(); }, 1000);

// Resumo em texto: "2 pessoas · 1 cão · Tiago (feliz)"
function aiSummary() {
  const r = AI.last;
  let txt = '';
  if (r) {
    const counts = {};
    for (const o of r.objects || []) counts[o.c] = (counts[o.c] || 0) + 1;
    if (!(r.objects || []).length && (r.poses || []).length) counts.pessoa = r.poses.length;
    const parts = Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c, n]) => n > 1 ? `${n} × ${c}` : c);
    for (const f of r.faces || []) {
      const bits = [f.emo, f.age != null ? `${f.age} anos` : null].filter(Boolean).join(', ');
      if (f.name) parts.push(`${f.name}${bits ? ` (${bits})` : ''}`);
      else if (bits) parts.push(`cara (${bits})`);
    }
    for (const p of r.poses || []) for (const g of p.g || []) parts.push(g);
    for (const q of r.codes || []) parts.push(`${q.t}: ${q.d.slice(0, 60)}`);
    txt = parts.join(' · ') || 'nada detetado';
  } else if (AI.state && AI.state.active && AI.state.paused) txt = `Análise em pausa: ${AI.state.paused}.`;
  else if (AI.state && AI.state.active) txt = 'a aguardar o primeiro resultado…';
  else txt = 'Ligue uma função para analisar o vídeo.';
  $('#ai-summary').textContent = $('#ai-summary2').textContent = txt;
}

// ── funções e opções ────────────────────────────────────────────────────────
async function aiSet(changes) {
  try {
    const r = await api('/api/ai', { body: changes });
    aiOnState(r.state);
  } catch (x) { toast('Análise: ' + x.message, 'err'); if (AI.state) aiOnState(AI.state); }
}

function aiToggle(k, on) {
  const f = { [k]: on };
  // Humor, idade e nomes precisam das caras
  if (on && ['emotion', 'age', 'names'].includes(k)) f.faces = true;
  if (!on && k === 'faces') Object.assign(f, { emotion: false, age: false, names: false });
  aiSet({ features: f });
}

function renderAIChips() {
  const st = AI.state;
  if (!st) return;
  const box = $('#ai-chips');
  if (!box.children.length) {
    for (const f of AI_FEATURES) {
      box.append(el('button', { class: 'chip', 'data-k': f.k, title: f.label,
        onclick: () => aiToggle(f.k, !AI.state.features[f.k]) }, f.chip));
    }
  }
  for (const b of $$('.chip', box)) {
    b.classList.toggle('on', !!st.features[b.dataset.k]);
    b.disabled = !!(st.missing || []).length;
  }
}

function renderAIFeatures() {
  const st = AI.state, box = $('#ai-features');
  if (!box.children.length) {
    for (const f of AI_FEATURES) {
      const input = el('input', { type: 'checkbox', class: 'switch' });
      input.addEventListener('change', () => aiToggle(f.k, input.checked));
      box.append(el('label', { class: 'switch-row feat', 'data-k': f.k },
        el('span', {}, el('b', {}, f.label), el('small', { class: 'muted' }, f.desc)), input));
    }
  }
  for (const row of $$('.feat', box)) {
    const i = $('input', row);
    i.checked = !!st.features[row.dataset.k];
    i.disabled = !!(st.missing || []).length;
  }
}

function renderAIOptions() {
  const st = AI.state, box = $('#ai-options');
  if (!box.children.length) {
    const mk = (k, label, opts, conv = Number) => {
      const s = el('select', { 'data-k': k });
      for (const [v, l] of opts) s.append(el('option', { value: v }, l));
      s.addEventListener('change', () => aiSet({ [k]: conv(s.value) }));
      return el('label', {}, label, s);
    };
    box.append(
      mk('model', 'Modelo dos objetos', [['yolo11n', 'YOLO11n (mais preciso)'], ['yolox_nano', 'YOLOX-nano (mais leve, Apache)']], String),
      mk('size', 'Resolução da análise', [[320, '320 px (mais rápido)'], [416, '416 px (vê objetos mais pequenos)']]),
      mk('max_fps', 'Análises por segundo (máximo pedido)', [[1, 'até 1'], [2, 'até 2'], [3, 'até 3'], [5, 'até 5'], [0, 'o que o regulador deixar']]),
    );
    const v = el('span', { class: 'v' });
    const r = el('input', { type: 'range', min: 20, max: 80, step: 5, 'data-k': 'conf' });
    r.addEventListener('input', () => { v.textContent = r.value + '%'; });
    r.addEventListener('change', () => aiSet({ conf: r.value / 100 }));
    box.append(el('div', { class: 'ctl' }, el('div', { class: 'ctl-head' }, el('span', {}, 'Confiança mínima'), v), r));
    const ev = el('input', { type: 'checkbox', class: 'switch', 'data-k': 'events' });
    ev.addEventListener('change', () => aiSet({ events: ev.checked }));
    box.append(el('label', { class: 'switch-row' }, el('span', {}, 'Registar eventos'), ev));
  }
  for (const i of $$('[data-k]', box)) {
    if (document.activeElement === i) continue;
    const k = i.dataset.k;
    if (k === 'conf') { i.value = Math.round(st.conf * 100); i.previousSibling.lastChild.textContent = i.value + '%'; }
    else if (k === 'events') i.checked = !!st.events;
    else i.value = st[k];
  }
  const size = $('[data-k="size"]', box);
  if (size) size.disabled = st.model === 'yolox_nano';
  renderAIBudget();
  renderAIPerf();
}

function renderAIBudget() {
  const st = AI.state, b = st && st.budget, box = $('#ai-budget');
  if (!b) { box.hidden = true; return; }
  box.hidden = false;
  const why = st.paused || b.paused;
  box.classList.toggle('paused', !!why);
  if (why) {
    box.replaceChildren(el('b', {}, 'Em pausa: '), `${why}. `,
      'Volta sozinha quando a energia e a temperatura normalizarem.');
    return;
  }
  const fps = b.max_fps ? `até ${+b.max_fps.toFixed(1)} análises/s` : 'sem limite de análises/s';
  const label = (b.mode.startsWith('bateria') ? 'Na bateria (poupança)' : 'Na corrente')
    + (b.mode.includes('quente') ? ', CPU quente' : '');
  box.replaceChildren(el('b', {}, `${label}: `),
    `${fps}, no máximo ${Math.round(b.duty * 100)} % do tempo de ${b.threads} núcleo${b.threads > 1 ? 's' : ''}. `,
    'Sem movimento na imagem, os modelos descansam.');
}

const MS_LABELS = { objects: 'Objetos', pose: 'Pose', faces: 'Caras (+ humor, idade, nomes)', codes: 'Códigos' };
function renderAIPerf() {
  const st = AI.state, r = AI.last;
  if (!st) return;
  const rows = [['Processo de análise', st.running ? 'a correr' : 'parado'],
    ['Análises por segundo', st.active && r ? String(r.fps || st.fps || '—') : '—']];
  if (r && r.ms) {
    for (const [k, v] of Object.entries(r.ms)) rows.push([MS_LABELS[k] || k, `${Math.round(v)} ms`]);
    rows.push(['Total por quadro', `${Math.round(r.total_ms)} ms`]);
    if (r.w) rows.push(['Imagem analisada', `${r.w}×${r.h}`]);
  }
  if (st.worker && st.worker.ort) rows.push(['onnxruntime / OpenCV', `${st.worker.ort} / ${st.worker.cv}`]);
  kv($('#ai-perf'), rows);
}

// ── pessoas conhecidas ──────────────────────────────────────────────────────
async function loadPeople() {
  try { const r = await api('/api/ai'); AI.people = r.people; aiOnState(r.state); renderPeople(); }
  catch (x) { toast('Pessoas: ' + x.message, 'err'); }
}

function renderPeople() {
  $('#people-count').textContent = AI.people.length ? String(AI.people.length) : '';
  $('#people').replaceChildren(...AI.people.map(p => el('div', { class: 'person' },
    el('div', { class: 'person-head' },
      el('b', {}, p.name),
      el('span', { class: 'muted' }, `${p.samples.length} amostra${p.samples.length > 1 ? 's' : ''}`),
      el('span', { class: 'spacer' }),
      el('button', { class: 'small', title: 'Juntar a cara atual a esta pessoa', onclick: () => enroll(null, p.id) }, '+ amostra'),
      el('button', { class: 'small ghost', title: 'Mudar o nome', onclick: () => renamePerson(p) }, '✎'),
      el('button', { class: 'small danger', title: 'Esquecer esta pessoa', onclick: () => forgetPerson(p) }, '✕')),
    el('div', { class: 'samples' }, ...p.samples.map(s => el('img', {
      src: `/ai/face/${encodeURIComponent(s)}`, alt: '', title: 'Clique para apagar esta amostra',
      onclick: () => forgetSample(p, s) }))))));
}

async function enroll(name, pid) {
  if (AI.enrolling) return;
  AI.enrolling = true;
  $('#enroll-btn').disabled = true;
  try {
    const r = await api('/api/ai/enroll', { body: { name, pid } });
    AI.people = r.people;
    renderPeople();
    toast(`Cara registada: ${r.person.name} (${r.person.samples} amostra${r.person.samples > 1 ? 's' : ''})`
      + (r.faces > 1 ? ` · usei a maior de ${r.faces} caras` : ''), 'ok');
    $('#enroll-name').value = '';
  } catch (x) { toast('Registar: ' + x.message, 'err'); }
  AI.enrolling = false;
  $('#enroll-btn').disabled = false;
}

$('#enroll-form').addEventListener('submit', e => {
  e.preventDefault();
  const name = $('#enroll-name').value.trim();
  if (!name) { toast('Escreva o nome primeiro', 'err'); $('#enroll-name').focus(); return; }
  enroll(name, null);
});

async function renamePerson(p) {
  const name = prompt('Novo nome:', p.name);
  if (!name || name === p.name) return;
  try { const r = await api(`/api/ai/people/${p.id}`, { method: 'PATCH', body: { name } }); AI.people = r.people; renderPeople(); }
  catch (x) { toast(x.message, 'err'); }
}

async function forgetPerson(p) {
  if (!confirm(`Esquecer ${p.name} e apagar as ${p.samples.length} amostras?`)) return;
  try { const r = await api(`/api/ai/people/${p.id}`, { method: 'DELETE' }); AI.people = r.people; renderPeople(); }
  catch (x) { toast(x.message, 'err'); }
}

async function forgetSample(p, s) {
  if (!confirm(p.samples.length > 1 ? 'Apagar esta amostra?' : `É a última amostra: ${p.name} será esquecido(a). Continuar?`)) return;
  try { const r = await api(`/api/ai/people/${p.id}/${encodeURIComponent(s)}`, { method: 'DELETE' }); AI.people = r.people; renderPeople(); }
  catch (x) { toast(x.message, 'err'); }
}

// ── eventos ─────────────────────────────────────────────────────────────────
const EV_TEXT = {
  enter: e => `${e.label}${e.detail ? ` (${e.detail})` : ''} apareceu`,
  leave: e => `${e.label} saiu`,
  person: e => `${e.label} reconhecido(a)${e.detail ? ` · ${e.detail}` : ''}`,
  pose: e => `Gesto: ${e.label}`,
  code: e => `${e.detail || 'Código'}: ${e.label}`,
};
const EV_CLASS = { enter: 'in', leave: 'out', person: 'person', pose: 'pose', code: 'code' };

async function loadEvents() {
  try { const r = await api('/api/ai/events'); AI.events = r.items; renderEvents(); }
  catch (x) { toast('Eventos: ' + x.message, 'err'); }
}

function renderEvents() {
  const list = AI.events.slice(0, 150);
  $('#events').replaceChildren(...list.map(e => el('div', { class: 'event ' + (EV_CLASS[e.kind] || '') },
    e.thumb ? el('img', { src: `/ai/event/${e.thumb}`, alt: '', loading: 'lazy', onclick: () => window.open(`/ai/event/${e.thumb}`, '_blank') })
      : el('div', { class: 'event-noimg' }, e.kind === 'leave' ? '↩' : '•'),
    el('div', { class: 'event-body' },
      el('span', { class: 'event-text' }, (EV_TEXT[e.kind] || (x => x.label))(e)),
      el('span', { class: 'muted mono' }, new Date(e.t * 1000).toLocaleString('pt-PT',
        { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }))))));
  $('#events-empty').hidden = list.length > 0;
}

$('#events-clear').addEventListener('click', async () => {
  if (!confirm('Apagar todos os eventos?')) return;
  try { await api('/api/ai/events', { method: 'DELETE' }); AI.events = []; renderEvents(); }
  catch (x) { toast(x.message, 'err'); }
});

// Chamado por setTab (app.js) ao abrir a aba
function aiTabOpened() { loadPeople(); loadEvents(); }
