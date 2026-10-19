'use strict';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const number = value => value === null || value === undefined || value === '' ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value));
const dateLabel = value => value || 'Дата неизвестна';
const NS = 'http://www.w3.org/2000/svg';
const state = {
  metadata: null, geo: null, list: [], profile: null, selected: null,
  view: [-180, -90, 360, 180], focus: null, viewMode: 'ranking', limit: 50,
  page: 0, pageSize: 60, query: '', eligibility: 'all', sort: 'rank',
  request: 0, routeFrom: '', routeTo: '', showUndated: true,
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function svg(tag, attrs, text) {
  const element = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) element.setAttribute(key, value);
  if (text !== undefined) element.textContent = text;
  return element;
}
async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
async function post(path, body) { return api(path, {method: 'POST', body}); }
function showError(message) { $('#error').hidden = !message; $('#error').textContent = message || ''; }
function statusLabel(status) { return {eligible: 'СИГНАЛЫ', no_matches: 'БЕЗ СОВПАДЕНИЙ', insufficient_data: 'МАЛО ДАННЫХ'}[status] || status || '—'; }
function statusClass(status) { return ({eligible: 'eligible', no_matches: 'quiet', insufficient_data: 'muted'}[status] || 'muted'); }
function eligibleCount() { return state.metadata?.eligibility?.find(row => row.eligibility_status === 'eligible')?.profiles || 0; }

function renderMetadata() {
  const m = state.metadata || {};
  const counts = m.counts || {};
  $('#metricPersons').textContent = number(counts.persons);
  $('#metricSegments').textContent = number(counts.segments);
  $('#metricEligible').textContent = number(eligibleCount());
  $('#metricMode').textContent = m.data_mode === 'demo_subset' ? 'DEMO' : 'SOURCE';
  $('#metricModeHint').textContent = m.data_mode === 'demo_subset' ? 'реальный набор · демо-подмножество' : 'проверенный snapshot';
  $('#sourceBadge').textContent = m.data_mode === 'demo_subset' ? 'ДЕМО · РЕАЛЬНЫЕ ПРОФИЛИ' : 'SOURCE DATASET · VERIFIED';
  $('#windowLabel').textContent = `Окно наблюдений · ${m.window?.start || '—'} — ${m.window?.end || '—'}`;
  $('#runLabel').textContent = `run_id · ${m.run_id || '—'}`;
  $('#agentMode').textContent = m.data_mode === 'demo_subset' ? 'ДЕМО-КОНТУР МОНИТОРИНГА' : 'КОНТУР МОНИТОРИНГА';
  $('#resetButton').hidden = m.data_mode !== 'demo_subset';
}

function renderList() {
  const root = $('#peopleList'); root.replaceChildren();
  let rows = state.list;
  if (state.viewMode === 'ranking' && state.query.trim()) {
    const query = state.query.trim().toLocaleLowerCase('ru');
    rows = rows.filter(row => `${row.display_name || ''} ${row.person_id}`.toLocaleLowerCase('ru').includes(query));
  }
  $('#listTitle').textContent = state.viewMode === 'ranking' ? `Топ-${state.limit}` : 'Все профили';
  $('#listDescription').textContent = state.viewMode === 'ranking' ? 'Сначала число сработавших групп, затем сумма вкладов и стабильный ID.' : 'Поиск и фильтры работают по всей популяции, а не только по рейтингу.';
  const total = state.viewMode === 'ranking' ? state.list.length : (state.list.pagination?.total || 0);
  $('#listCount').textContent = number(total);
  if (state.viewMode === 'all') {
    const page = state.list.pagination || {offset: 0, limit: state.pageSize, total: 0, has_next: false};
    $('#pageLabel').textContent = `${page.total ? page.offset + 1 : 0}–${Math.min(page.offset + state.list.items.length, page.total)} из ${number(page.total)}`;
    $('#prevPage').disabled = page.offset <= 0;
    $('#nextPage').disabled = !page.has_next;
  } else {
    $('#pageLabel').textContent = `${rows.length} профилей`;
    $('#prevPage').disabled = true; $('#nextPage').disabled = true;
  }
  if (!rows.length) { root.append(node('div', 'Ничего не найдено по текущему фильтру.', 'empty-list')); return; }
  for (const row of rows) {
    const button = node('button', undefined, `person-row ${row.person_id === state.selected ? 'selected' : ''}`); button.type = 'button';
    const place = node('span', row.rank ? String(row.rank).padStart(2, '0') : '··', 'person-place');
    const identity = node('span', undefined, 'person-identity'); identity.append(node('strong', row.display_name || 'Имя не указано'), node('small', row.person_id));
    const score = node('span', undefined, 'person-score'); score.append(node('strong', number(row.rule_score)), node('small', `${row.matched_criteria_count || 0} / 3 факторов`));
    const badge = node('span', statusLabel(row.eligibility_status), `person-status ${statusClass(row.eligibility_status)}`);
    button.append(place, identity, score, badge); button.addEventListener('click', () => selectProfile(row.person_id)); root.append(button);
  }
}

async function loadList(selectFirst = false) {
  const request = ++state.request;
  showError(null); $('#peopleList').innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
  try {
    if (state.viewMode === 'ranking') {
      state.list = await api(`/api/top?limit=${state.limit}`);
    } else {
      const params = new URLSearchParams({limit: String(state.pageSize), offset: String(state.page * state.pageSize), q: state.query, eligibility: state.eligibility, sort: state.sort});
      state.list = await api(`/api/users?${params}`);
    }
    if (request !== state.request) return;
    renderList();
    const rows = state.viewMode === 'ranking' ? state.list : state.list.items;
    if (selectFirst && rows.length) await selectProfile(rows[0].person_id);
  } catch (error) {
    if (request !== state.request) return;
    $('#peopleList').replaceChildren(node('div', 'Список недоступен.', 'empty-list')); showError(error.message);
  }
}

async function selectProfile(id) {
  const request = ++state.request; state.selected = id; state.profile = null; state.focus = null; state.view = [-180, -90, 360, 180];
  renderList(); $('#profilePanel').replaceChildren(node('div', 'Загружаем карточку и историю…', 'panel empty-state')); showError(null);
  try { const data = await api(`/api/profile?id=${encodeURIComponent(id)}`); if (request !== state.request) return; state.profile = data; renderProfile(); }
  catch (error) { if (request === state.request) { $('#profilePanel').replaceChildren(node('div', 'Карточка недоступна.', 'panel empty-state')); showError(error.message); } }
}

function renderProfile() {
  const data = state.profile; const person = data.person; const root = $('#profilePanel'); root.replaceChildren();
  const identity = node('div', undefined, 'profile-identity');
  identity.append(node('p', person.rank ? `ПРИОРИТЕТ №${person.rank}` : 'ПРОФИЛЬ МОДЕЛИ', 'eyebrow'), node('h2', person.display_name || 'Имя не указано'), node('p', person.person_id, 'profile-id'));
  const score = node('div', undefined, 'score-block'); score.append(node('span', 'RULE SCORE', 'score-label'), node('strong', number(person.rule_score)), node('small', `${person.matched_criteria_count || 0} сработавших групп`));
  const head = node('section', undefined, 'profile-head panel'); head.append(identity, score);
  const chips = node('div', undefined, 'profile-chips'); chips.append(node('span', `${statusLabel(person.eligibility_status)}`, `chip ${statusClass(person.eligibility_status)}`), node('span', `${number(person.total_segments || data.segments.length)} сегментов`, 'chip'), node('span', `${number(data.route_balance.total)} маршрутов`, 'chip')); head.append(chips); root.append(head);

  const factorPanel = node('section', undefined, 'panel factor-panel'); const factorTitle = node('div', undefined, 'section-heading'); factorTitle.append(node('div', undefined)); factorTitle.firstChild.append(node('p', 'ДОКАЗАТЕЛЬСТВА СИГНАЛА', 'eyebrow'), node('h3', 'Что сработало')); factorPanel.append(factorTitle);
  const factors = node('div', undefined, 'factor-grid');
  for (const factor of data.factors || []) {
    const article = node('article', undefined, `factor-card ${factor.status}`); const top = node('div', undefined, 'factor-top'); top.append(node('span', factor.status === 'matched' ? 'СРАБОТАЛО' : factor.status === 'unavailable' ? 'НЕДОСТУПНО' : 'НЕ СРАБОТАЛО', 'factor-status'), node('span', factor.criterion_id, 'factor-id')); article.append(top, node('h4', factor.title_ru), node('p', factor.explanation_ru || 'Нет пояснения.'));
    const meter = node('div', undefined, 'factor-meter'); const value = Number(factor.value); const threshold = Number(factor.threshold); const ratio = Number.isFinite(value) && threshold > 0 ? Math.min(100, Math.max(5, value / threshold * 55)) : 4; meter.append(node('i')); meter.firstChild.style.width = `${ratio}%`; article.append(meter);
    if (factor.evidence_segment_ids?.length) { const link = node('button', `↳ ${factor.evidence_segment_ids.length} подтверждений`, 'evidence-link'); link.type = 'button'; link.addEventListener('click', () => { state.focus = factor.evidence_segment_ids[0]; document.querySelector('#timeline')?.scrollIntoView({behavior: 'smooth', block: 'center'}); renderTimeline(); }); article.append(link); }
    factors.append(article);
  }
  factorPanel.append(factors); root.append(factorPanel);

  const mapPanel = node('section', undefined, 'panel map-panel'); const mapHeading = node('div', undefined, 'section-heading'); const headingCopy = node('div'); headingCopy.append(node('p', 'ГЕОГРАФИЯ И ХРОНОЛОГИЯ', 'eyebrow'), node('h3', 'След маршрута')); mapHeading.append(headingCopy, node('span', 'координаты только из справочника', 'micro-tag')); mapPanel.append(mapHeading);
  const controls = node('div', undefined, 'route-controls');
  for (const [id, label] of [['routeFrom', 'С'], ['routeTo', 'По']]) { const wrap = node('label', label); const input = node('input'); input.type = 'date'; input.id = id; input.value = id === 'routeFrom' ? state.routeFrom : state.routeTo; input.addEventListener('change', () => { state.routeFrom = $('#routeFrom').value; state.routeTo = $('#routeTo').value; renderTimeline(); }); wrap.append(input); controls.append(wrap); }
  const unknown = node('label', undefined, 'check-label'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = state.showUndated; checkbox.addEventListener('change', () => { state.showUndated = checkbox.checked; renderTimeline(); }); unknown.append(checkbox, node('span', 'Без даты')); controls.append(unknown);
  const reset = node('button', 'Сбросить период', 'button subtle'); reset.type = 'button'; reset.addEventListener('click', () => { state.routeFrom = ''; state.routeTo = ''; state.showUndated = true; renderProfile(); }); controls.append(reset); mapPanel.append(controls);
  const balance = node('div', undefined, 'route-balance'); balance.id = 'routeBalance'; mapPanel.append(balance);
  const mapWrap = node('div', undefined, 'map-wrap'); const map = svg('svg', {class:'map', viewBox:'-180 -90 360 180', role:'img', 'aria-label':'Карта маршрутов'}); map.id = 'map';
  let mapDrag = null;
  map.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    mapDrag = {x: event.clientX, y: event.clientY, view: [...state.view]};
    map.setPointerCapture(event.pointerId);
  });
  map.addEventListener('pointermove', event => {
    if (!mapDrag) return;
    const rect = map.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = event.clientX - mapDrag.x;
    const dy = event.clientY - mapDrag.y;
    state.view = [
      mapDrag.view[0] - dx * mapDrag.view[2] / rect.width,
      mapDrag.view[1] - dy * mapDrag.view[3] / rect.height,
      mapDrag.view[2], mapDrag.view[3],
    ];
    drawMap(visibleSegments());
  });
  const stopMapDrag = () => { mapDrag = null; };
  map.addEventListener('pointerup', stopMapDrag); map.addEventListener('pointercancel', stopMapDrag); map.addEventListener('lostpointercapture', stopMapDrag);
  mapWrap.append(map); const tools = node('div', undefined, 'map-tools'); [['+', .7], ['−', 1.4], ['Обзор', 0]].forEach(([label, factor]) => { const button = node('button', label); button.type='button'; button.addEventListener('click', () => zoomMap(factor)); tools.append(button); }); mapWrap.append(tools); mapPanel.append(mapWrap, node('p', 'Перетаскивайте карту мышью или пальцем после увеличения. Стрелки показывают направление между аэропортами.', 'map-note')); root.append(mapPanel);
  const timeline = node('section', undefined, 'panel timeline-panel'); timeline.id = 'timeline'; const timelineHeading = node('div', undefined, 'section-heading'); timelineHeading.append(node('div', undefined), node('span', `${data.segments.length} записей`, 'micro-tag')); timelineHeading.firstChild.append(node('p', 'ПЕРВИЧНЫЕ СВИДЕТЕЛЬСТВА', 'eyebrow'), node('h3', 'Перелёты профиля')); timeline.append(timelineHeading, node('div', undefined, 'timeline-table')); root.append(timeline);
  const secondary = node('div', undefined, 'secondary-grid'); const evidence = node('section', undefined, 'panel mini-panel'); evidence.append(node('p', 'PROVENANCE', 'eyebrow'), node('h3', 'Источники и ссылки')); evidence.append(node('p', data.source_evidence?.length ? `${data.source_evidence.length} строк evidence привязаны к исходным сегментам.` : 'Отдельные source-evidence строки отсутствуют.', 'muted-copy')); const forest = node('section', undefined, 'panel mini-panel'); forest.append(node('p', 'ISOLATION FOREST', 'eyebrow'), node('h3', data.forest?.status === 'demo_only' ? 'Не отдельный вердикт' : 'Экспериментальный score'), node('p', data.forest?.status === 'demo_only' ? 'Для демо показывается только правило. Данные не переобучаются в браузере.' : 'IF хранится отдельно и не добавляет скрытый голос основному рейтингу.', 'muted-copy')); secondary.append(evidence, forest); root.append(secondary);
  renderTimeline();
}

function visibleSegments() {
  const rows = state.profile?.segments || [];
  return rows.filter(segment => {
    const date = segment.departure_date;
    if (!date && !state.showUndated) return false;
    if (date && state.routeFrom && date < state.routeFrom) return false;
    if (date && state.routeTo && date > state.routeTo) return false;
    return true;
  });
}
function renderTimeline() {
  if (!state.profile) return;
  const rows = visibleSegments(); const balance = $('#routeBalance'); if (balance) balance.textContent = `Показано ${rows.length} из ${state.profile.route_balance.total} · с координатами ${state.profile.route_balance.plottable} · без координат ${state.profile.route_balance.unplottable}`;
  const table = document.querySelector('.timeline-table'); if (!table) return; table.replaceChildren(); const header = node('div', undefined, 'timeline-row timeline-header'); ['Дата', 'Маршрут', 'Рейс', 'Статус'].forEach(text => header.append(node('span', text))); table.append(header);
  if (!rows.length) table.append(node('div', 'В выбранном периоде записей нет.', 'empty-list'));
  for (const segment of rows) { const row=node('button', undefined, `timeline-row ${state.focus === segment.segment_id ? 'focused' : ''}`); row.type='button'; const origin=segment.origin?.airport_id || segment.origin_airport_id || '???'; const destination=segment.destination?.airport_id || segment.destination_airport_id || '???'; row.append(node('span', dateLabel(segment.departure_date), 'date-cell'), node('span', `${origin} → ${destination}`, 'route-cell'), node('span', segment.flight_number || '—', 'flight-cell'), node('span', segment.resolution_status || '—', 'state-cell')); row.addEventListener('click', () => { state.focus=segment.segment_id; drawMap(visibleSegments()); renderTimeline(); }); table.append(row); }
  drawMap(rows);
}
function routeParts(a, b) { let [x1,y1]=a, [x2,y2]=b; if (Math.abs(x2-x1)<=180) return [[a,b]]; const direction=x1>0?180:-180; x2+=x1>0?360:-360; const ratio=(direction-x1)/(x2-x1); const y=y1+ratio*(y2-y1); return [[[x1,y1],[direction,y]],[[-direction,y],[b[0],y2]]]; }
function drawMap(rows) {
  const map=$('#map'); if (!map) return; map.replaceChildren(); const defs=svg('defs'); const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto',markerUnits:'strokeWidth'}); marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#80e3d2'})); defs.append(marker); map.append(defs);
  for (let x=-180;x<=180;x+=30) map.append(svg('line',{x1:x,x2:x,y1:-90,y2:90,class:'gridline'})); for (let y=-60;y<=60;y+=30) map.append(svg('line',{x1:-180,x2:180,y1:y,y2:y,class:'gridline'}));
  for (const feature of state.geo?.features || []) if (['Polygon','MultiPolygon'].includes(feature.geometry?.type)) map.append(svg('path',{d:countryPath(feature.geometry),class:'country'}));
  for (const segment of rows) { const a=segment.origin, b=segment.destination; if (!segment.is_plottable || !a || !b) continue; const start=[Number(a.longitude),-Number(a.latitude)], end=[Number(b.longitude),-Number(b.latitude)]; const active=state.focus===segment.segment_id; for (const [from,to] of routeParts(start,end)) { const line=svg('path',{d:`M${from} L${to}`,class:`route ${active?'active':''}`}); if (to[0]===end[0] && to[1]===end[1]) line.setAttribute('marker-end','url(#arrow)'); line.append(svg('title',{},`${a.airport_id} → ${b.airport_id} · ${dateLabel(segment.departure_date)}`)); map.append(line); } map.append(svg('circle',{cx:start[0],cy:start[1],r:active?1.5:.75,class:`airport-dot ${active?'active':''}`}),svg('circle',{cx:end[0],cy:end[1],r:active?1.5:.75,class:`airport-dot ${active?'active':''}`})); }
  map.setAttribute('viewBox',state.view.join(' '));
}
function countryPath(geometry) { const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.coordinates; return polygons.map(poly=>poly.map(ring=>ring.map((v,i)=>`${i?'L':'M'}${v[0]},${-v[1]}`).join(' ')+'Z').join(' ')).join(' '); }
function zoomMap(factor) { const [x,y,w,h]=state.view; if (!factor) state.view=[-180,-90,360,180]; else { const nw=Math.min(720,Math.max(10,w*factor)); state.view=[x+(w-nw)/2,y+(h-nw/2)/2,nw,nw/2]; } drawMap(visibleSegments()); }

async function changeMode(mode) { state.viewMode=mode; state.page=0; $$('.view-tab').forEach(button => { const active=button.dataset.view===mode; button.classList.toggle('active',active); button.setAttribute('aria-selected',String(active)); }); $('#rankPicker').classList.toggle('disabled',mode==='all'); await loadList(true); }
async function activateDemo() { try { showError(null); await post('/api/demo'); await bootstrap(); } catch (error) { showError(error.message); } }
async function resetProduct() { try { showError(null); await post('/api/reset'); await bootstrap(); } catch (error) { showError(error.message); } }
async function uploadProduct(file) { if (!file) return; const form=new FormData(); form.append('product',file,file.name); try { showError(null); $('#uploadInput').disabled=true; const result=await post('/api/upload',form); state.metadata=result.metadata; await bootstrap(false); } catch (error) { showError(error.message); } finally { $('#uploadInput').disabled=false; $('#uploadInput').value=''; } }

async function bootstrap(selectFirst=true) {
  try { const [metadata, geo] = await Promise.all([api('/api/metadata'),api('/world.geojson')]); state.metadata=metadata; state.geo=geo; renderMetadata(); await loadList(selectFirst); }
  catch (error) { showError(`Не удалось загрузить snapshot: ${error.message}`); $('#peopleList').replaceChildren(node('div','Снимок недоступен. Используйте demo или загрузите ZIP продукта.','empty-list')); }
}

document.addEventListener('DOMContentLoaded', () => {
  $('#demoButton').addEventListener('click', activateDemo); $('#resetButton').addEventListener('click', resetProduct); $('#uploadInput').addEventListener('change', event => uploadProduct(event.target.files[0]));
  $$('.view-tab').forEach(button => button.addEventListener('click', () => changeMode(button.dataset.view)));
  $$('.rank-choice').forEach(button => button.addEventListener('click', async () => { state.limit=Number(button.dataset.limit); state.viewMode='ranking'; state.page=0; $$('.rank-choice').forEach(item => item.classList.toggle('active',item===button)); $$('.view-tab').forEach(item => { const active=item.dataset.view==='ranking'; item.classList.toggle('active',active); item.setAttribute('aria-selected',String(active)); }); await loadList(true); }));
  $('#statusFilter').addEventListener('change', async event => { state.eligibility=event.target.value; state.page=0; if(state.viewMode==='all') await loadList(false); }); $('#sortSelect').addEventListener('change', async event => { state.sort=event.target.value; state.page=0; if(state.viewMode==='all') await loadList(false); });
  let timer; $('#searchInput').addEventListener('input', event => { state.query=event.target.value; if(state.viewMode==='ranking'){renderList();return;} clearTimeout(timer); timer=setTimeout(() => {state.page=0;loadList(false);},220); });
  $('#prevPage').addEventListener('click', async () => { if(state.page>0){state.page-=1;await loadList(false);} }); $('#nextPage').addEventListener('click', async () => { if(state.list.pagination?.has_next){state.page+=1;await loadList(false);} });
  bootstrap(true);
});
