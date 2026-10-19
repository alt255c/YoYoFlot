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
  const eligible = eligibleCount(); $('#metricEligible').textContent = number(eligible);
  const eligibleHint = $('#metricEligibleHint'); if (eligibleHint) { const total = Number(counts.persons) || 0; const share = total ? (eligible / total * 100).toFixed(2).replace('.', ',') : '—'; eligibleHint.textContent = `${share}% популяции · есть данные для рейтинга`; }
  $('#metricMode').textContent = m.data_mode === 'demo_subset' ? 'DEMO' : 'SOURCE';
  $('#metricModeHint').textContent = m.data_mode === 'demo_subset' ? 'реальный набор · демо-подмножество' : 'проверенный snapshot';
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
  const shouldScrollToProfile = state.selected !== null && window.matchMedia('(max-width: 780px)').matches;
  const request = ++state.request; state.selected = id; state.profile = null; state.focus = null; state.view = [-180, -90, 360, 180];
  renderList(); $('#profilePanel').replaceChildren(node('div', 'Загружаем карточку и историю…', 'panel empty-state')); showError(null);
  try { const data = await api(`/api/profile?id=${encodeURIComponent(id)}`); if (request !== state.request) return; state.profile = data; renderProfile(); if (shouldScrollToProfile) window.setTimeout(() => $('#profilePanel')?.scrollIntoView({behavior:'smooth', block:'start'}), 20); }
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

  const mapPanel = node('section', undefined, 'panel map-panel'); const mapHeading = node('div', undefined, 'section-heading'); const headingCopy = node('div'); headingCopy.append(node('p', 'ГЕОГРАФИЯ И ХРОНОЛОГИЯ', 'eyebrow'), node('h3', 'Направленный граф перелётов')); mapHeading.append(headingCopy, node('span', 'дата · стрелка = направление', 'micro-tag')); mapPanel.append(mapHeading);
  const controls = node('div', undefined, 'route-controls');
  for (const [id, label] of [['routeFrom', 'С'], ['routeTo', 'По']]) { const wrap = node('label', label); const input = node('input'); input.type = 'date'; input.id = id; input.value = id === 'routeFrom' ? state.routeFrom : state.routeTo; input.addEventListener('change', () => { state.routeFrom = $('#routeFrom').value; state.routeTo = $('#routeTo').value; renderTimeline(); }); wrap.append(input); controls.append(wrap); }
  const unknown = node('label', undefined, 'check-label'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = state.showUndated; checkbox.addEventListener('change', () => { state.showUndated = checkbox.checked; renderTimeline(); }); unknown.append(checkbox, node('span', 'Без даты')); controls.append(unknown);
  const reset = node('button', 'Сбросить период', 'button subtle'); reset.type = 'button'; reset.addEventListener('click', () => { state.routeFrom = ''; state.routeTo = ''; state.showUndated = true; renderProfile(); }); controls.append(reset); mapPanel.append(controls);
  const balance = node('div', undefined, 'route-balance'); balance.id = 'routeBalance'; mapPanel.append(balance);
  const focusSummary = node('div', 'Выберите строку истории, чтобы сфокусировать перелёт.', 'route-focus'); focusSummary.id = 'routeFocus'; mapPanel.append(focusSummary);
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
  mapWrap.append(map); const tools = node('div', undefined, 'map-tools'); [['+', .7], ['−', 1.4], ['Обзор', 0]].forEach(([label, factor]) => { const button = node('button', label); button.type='button'; button.addEventListener('click', () => zoomMap(factor)); tools.append(button); }); mapWrap.append(tools); mapPanel.append(mapWrap, node('p', 'Дуги показывают направление, даты вылетов подписаны на карте. Выберите строку истории, чтобы подсветить рейс; точки — только в аэропортах отправления.', 'map-note')); root.append(mapPanel);
  const timeline = node('section', undefined, 'panel timeline-panel'); timeline.id = 'timeline'; const timelineHeading = node('div', undefined, 'section-heading'); timelineHeading.append(node('div', undefined), node('span', `${data.segments.length} записей`, 'micro-tag')); timelineHeading.firstChild.append(node('p', 'ПЕРВИЧНЫЕ СВИДЕТЕЛЬСТВА', 'eyebrow'), node('h3', 'Перелёты профиля')); timeline.append(timelineHeading, node('div', undefined, 'timeline-table')); const timelineHost = $('#sidebarTimeline'); if (timelineHost) { timelineHost.replaceChildren(timeline); } else { root.append(timeline); }
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
  if (rows.length && !rows.some(row => row.segment_id === state.focus)) state.focus = rows[0].segment_id;
  if (state.view[0] === -180 && state.view[1] === -90 && state.view[2] === 360 && state.view[3] === 180) state.view = fitMapView(rows);
  const focused = rows.find(row => row.segment_id === state.focus); const focusSummary = $('#routeFocus'); if (focusSummary) { const origin = focused?.origin?.airport_id || focused?.origin_airport_id || '???'; const destination = focused?.destination?.airport_id || focused?.destination_airport_id || '???'; focusSummary.classList.toggle('focused', Boolean(focused)); focusSummary.textContent = focused ? `Выбранный перелёт · ${dateLabel(focused.departure_date)} · ${airportLabel(origin)} → ${airportLabel(destination)} · ${focused.flight_number || 'рейс не указан'}` : 'Выберите строку истории, чтобы сфокусировать перелёт.'; }
  const table = document.querySelector('.timeline-table'); if (!table) return; table.replaceChildren(); const header = node('div', undefined, 'timeline-row timeline-header'); ['Дата', 'Маршрут', 'Рейс', 'Статус'].forEach(text => header.append(node('span', text))); table.append(header);
  if (!rows.length) table.append(node('div', 'В выбранном периоде записей нет.', 'empty-list'));
  for (const segment of rows) { const row=node('button', undefined, `timeline-row ${state.focus === segment.segment_id ? 'focused' : ''}`); row.type='button'; const origin=segment.origin?.airport_id || segment.origin_airport_id || '???'; const destination=segment.destination?.airport_id || segment.destination_airport_id || '???'; row.append(node('span', dateLabel(segment.departure_date), 'date-cell'), node('span', `${airportLabel(origin)} → ${airportLabel(destination)}`, 'route-cell'), node('span', segment.flight_number || '—', 'flight-cell'), node('span', segment.resolution_status || '—', 'state-cell')); row.addEventListener('click', () => { state.focus=segment.segment_id; drawMap(visibleSegments()); renderTimeline(); }); table.append(row); }
  drawMap(rows);
}
function routeParts(a, b) { let [x1,y1]=a, [x2,y2]=b; if (Math.abs(x2-x1)<=180) return [[a,b]]; const direction=x1>0?180:-180; x2+=x1>0?360:-360; const ratio=(direction-x1)/(x2-x1); const y=y1+ratio*(y2-y1); return [[[x1,y1],[direction,y]],[[-direction,y],[b[0],y2]]]; }
function shortDate(value) { if (!value) return 'дата неизвестна'; const parts=String(value).split('-'); return parts.length===3 ? `${parts[2]}.${parts[1]}.${parts[0]}` : String(value); }
function airportLabel(value) { return String(value || '???').replace(/^openflights:/, ''); }
function routeKey(segment) { const from=segment.origin?.airport_id || segment.origin_airport_id || '???'; const to=segment.destination?.airport_id || segment.destination_airport_id || '???'; return `${from}→${to}`; }
function curvedRoute(from, to, bend) { const [x1,y1]=from, [x2,y2]=to; const dx=x2-x1, dy=y2-y1, length=Math.hypot(dx,dy) || 1; const nx=-dy/length, ny=dx/length; const mx=(x1+x2)/2+nx*bend, my=(y1+y2)/2+ny*bend; const t=.82, u=1-t; return {path:`M${x1},${y1} Q${mx},${my} ${x2},${y2}`, label:[u*u*x1+2*u*t*mx+t*t*x2, u*u*y1+2*u*t*my+t*t*y2]}; }
function mapElementScale() {
  const width = Number(state.view[2]) || 360;
  const level = Math.pow(Math.min(1, Math.max(.02, width / 360)), 1.2);
  return {
    arrow: Math.min(4.4, Math.max(.05, .05 + level * 4.35)),
    dot: Math.min(1.1, Math.max(.04, .04 + level * 1)),
    date: Math.min(3.4, Math.max(.08, .08 + level * 3.32)),
  };
}
function fitMapView(rows) {
  const points = rows.filter(segment => segment.is_plottable && segment.origin && segment.destination).flatMap(segment => [segment.origin, segment.destination]).map(point => [Number(point.longitude), -Number(point.latitude)]).filter(([x,y]) => Number.isFinite(x) && Number.isFinite(y));
  if (!points.length) return [-180, -90, 360, 180];
  const xs = points.map(point => point[0]), ys = points.map(point => point[1]);
  const spanX = Math.max(10, Math.max(...xs) - Math.min(...xs)), spanY = Math.max(10, Math.max(...ys) - Math.min(...ys));
  const width = Math.min(360, spanX + Math.max(8, spanX * .18)); const height = Math.min(180, spanY + Math.max(8, spanY * .22));
  const centerX = (Math.min(...xs) + Math.max(...xs)) / 2; const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
  return [centerX - width / 2, centerY - height / 2, width, height];
}
function drawMap(rows) {
  const map=$('#map'); if (!map) return; map.replaceChildren();
  const scale = mapElementScale(); const defs=svg('defs');
  const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:8.5,refY:5,markerWidth:scale.arrow,markerHeight:scale.arrow,orient:'auto',markerUnits:'userSpaceOnUse'}); marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#0b57b7'}));
  const activeMarker=svg('marker',{id:'arrow-active',viewBox:'0 0 10 10',refX:8.5,refY:5,markerWidth:scale.arrow*1.18,markerHeight:scale.arrow*1.18,orient:'auto',markerUnits:'userSpaceOnUse'}); activeMarker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#d47d24'})); defs.append(marker, activeMarker); map.append(defs);
  for (let x=-180;x<=180;x+=30) map.append(svg('line',{x1:x,x2:x,y1:-90,y2:90,class:'gridline'})); for (let y=-60;y<=60;y+=30) map.append(svg('line',{x1:-180,x2:180,y1:y,y2:y,class:'gridline'}));
  for (const feature of state.geo?.features || []) if (['Polygon','MultiPolygon'].includes(feature.geometry?.type)) map.append(svg('path',{d:countryPath(feature.geometry),class:'country'}));
  const plottable=rows.filter(segment => segment.is_plottable && segment.origin && segment.destination); const groups=new Map(); const airports=new Map();
  for (const segment of plottable) { const a=segment.origin, b=segment.destination; const start=[Number(a.longitude),-Number(a.latitude)], end=[Number(b.longitude),-Number(b.latitude)]; const key=routeKey(segment); if (!groups.has(key)) groups.set(key,[]); groups.get(key).push({segment,start,end}); airports.set(a.airport_id,{point:start,active:false}); }
  const showDates=plottable.length<=40;
  for (const group of groups.values()) group.forEach((item,index) => {
    const {segment,start,end}=item; const active=state.focus===segment.segment_id; if (active && airports.has(segment.origin.airport_id)) airports.get(segment.origin.airport_id).active=true;
    const bend=(index-(group.length-1)/2)*4.5; const curve=curvedRoute(start,end,bend); const line=svg('path',{d:curve.path,class:`route ${active?'active':''}`,'marker-end':`url(#${active?'arrow-active':'arrow'})`}); line.append(svg('title',{},`${segment.origin.airport_id} → ${segment.destination.airport_id} · ${dateLabel(segment.departure_date)}`)); map.append(line);
    if (showDates || active) { const tag=svg('g',{class:`map-date-label ${active?'active':''}`}); tag.append(svg('text',{x:curve.label[0],y:curve.label[1]+1.5,'text-anchor':'middle',class:'map-date',style:`font-size:${scale.date*(active?1.1:1)}px`},shortDate(segment.departure_date))); tag.append(svg('title',{},`${segment.origin.airport_id} → ${segment.destination.airport_id} · ${dateLabel(segment.departure_date)}`)); map.append(tag); }
  });
  for (const [airport, info] of airports) { const circle=svg('circle',{cx:info.point[0],cy:info.point[1],r:info.active?scale.dot*1.55:scale.dot,class:`airport-dot ${info.active?'active':''}`}); circle.append(svg('title',{},airport)); map.append(circle); }
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
