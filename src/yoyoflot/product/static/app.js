'use strict';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const number = value => value === null || value === undefined || value === '' ? '—' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(Number(value));
const dateLabel = value => value || 'Дата неизвестна';
const NS = 'http://www.w3.org/2000/svg';
const state = {
  metadata: null, geo: null, list: [], profile: null, sourceDetails: null, selected: null,
  view: [-180, -90, 360, 180], focus: null, viewMode: 'ranking', limit: 50,
  page: 0, pageSize: 60, query: '', eligibility: 'all', sort: 'rank',
  timelinePage: 0, timelinePageSize: 8,
  request: 0, routeFrom: '', routeTo: '', showUndated: true,
  mosquitoFlight: null, mosquitoFrame: null, playback: null,
};

function applyTheme(theme) {
  const next = theme === 'dark' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('yoyoflot-theme', next); } catch (_) {}
  const button = $('#themeToggle');
  if (button) {
    button.textContent = next === 'dark' ? 'Светлая тема' : 'Тёмная тема';
    button.setAttribute('aria-pressed', String(next === 'dark'));
    button.setAttribute('aria-label', next === 'dark' ? 'Переключить на светлую тему' : 'Переключить на тёмную тему');
  }
}
function initTheme() {
  let saved = 'dark';
  try { saved = localStorage.getItem('yoyoflot-theme') || 'dark'; } catch (_) {}
  applyTheme(saved);
}

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
function assessmentStatus(person) { return person.assessment_status || person.eligibility_status; }

function renderMetadata() {
  const m = state.metadata || {};
  const counts = m.counts || {};
  $('#metricPersons').textContent = number(counts.persons);
  $('#metricSegments').textContent = number(counts.segments);
  const assessment = m.assessment || {};
  const assessed = assessment.assessed;
  $('#metricEligible').textContent = number(assessed);
  const eligibleHint = $('#metricEligibleHint'); if (eligibleHint) { const total = Number(counts.persons) || 0; const share = total && assessed !== undefined ? (assessed / total * 100).toFixed(2).replace('.', ',') : '—'; eligibleHint.textContent = `${share}% профилей · оценён хотя бы один маркер`; }
  $('#metricSignals').textContent = number(assessment.with_signals ?? eligibleCount());
  $('#metricSignalsHint').textContent = `${number(assessment.without_signals)} проверены без срабатываний`;
  $('#metricIdentity').textContent = number(assessment.identity_accepted);
  $('#metricMode').textContent = m.data_mode === 'demo_subset' ? 'DEMO' : 'SOURCE';
  $('#metricModeHint').textContent = m.data_mode === 'demo_subset' ? 'реальный набор · демо-подмножество' : 'проверенный snapshot';
  $('#agentMode').textContent = m.data_mode === 'demo_subset' ? 'ДЕМО-КОНТУР МОНИТОРИНГА' : 'КОНТУР МОНИТОРИНГА';
  $('#resetButton').hidden = m.data_mode !== 'demo_subset';
  const summary = $('#markerSummary'); const summaryRows = $('#markerSummaryRows');
  const markerStats = m.scoring?.marker_statistics || [];
  summary.hidden = !markerStats.length; summaryRows.replaceChildren();
  if (markerStats.length) {
    const table = node('table'); const head = node('tr');
    for (const title of ['Фактор', 'Вес', 'Проверено', 'Совпадения', 'Похожие имена без связи', 'Начислены баллы']) head.append(node('th', title));
    const thead = node('thead'); thead.append(head); table.append(thead); const tbody = node('tbody');
    for (const row of markerStats) {
      const tr = node('tr'); const label = node('td', `${row.criterion_id} · ${row.title_ru}`);
      const reason = m.source_assessment?.unavailable_reasons?.[row.criterion_id];
      if (!row.evaluated) { label.append(node('small', reason || 'Расчёт ещё не подготовлен', 'marker-unavailable')); }
      const matched = node('td');
      if (row.matched && row.sample_person_id) { const example = node('button', number(row.matched), 'evidence-link'); example.type = 'button'; example.title = 'Открыть пример срабатывания'; example.addEventListener('click', () => selectProfile(row.sample_person_id)); matched.append(example); }
      else matched.textContent = number(row.matched);
      const candidates=node('td',number(row.unconfirmed_candidates || 0));
      if (row.unconfirmed_candidates && row.sample_candidate_person_id) { candidates.replaceChildren(); const link=node('button',number(row.unconfirmed_candidates),'evidence-link'); link.type='button'; link.title='Похожие имена без подтверждения связи; баллы не начисляются'; link.addEventListener('click',()=>selectProfile(row.sample_candidate_person_id)); candidates.append(link); }
      tr.append(label,node('td', number(row.weight)),node('td', number(row.evaluated)),matched,candidates,node('td',number(row.credited))); tbody.append(tr);
    }
    table.append(tbody); summaryRows.append(table);
  }
}

function renderList() {
  const root = $('#peopleList'); root.replaceChildren();
  let rows = state.viewMode === 'ranking' ? state.list : (state.list.items || []);
  if (state.viewMode === 'ranking' && state.query.trim()) {
    const query = state.query.trim().toLocaleLowerCase('ru');
    rows = rows.filter(row => `${row.display_name || ''} ${row.person_id}`.toLocaleLowerCase('ru').includes(query));
  }
  $('#listTitle').textContent = state.viewMode === 'ranking' ? `Топ-${state.limit}` : 'Все профили';
  $('#listDescription').textContent = state.viewMode === 'ranking' ? (state.metadata?.scoring ? 'По сумме баллов из текущего конфига; при равенстве — стабильный ID.' : 'Сначала число сработавших групп, затем сумма вкладов и стабильный ID.') : 'Поиск и фильтры работают по всей популяции, а не только по рейтингу.';
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
    const score = node('span', undefined, 'person-score'); score.append(node('strong', number(row.rule_score)), node('small', `${row.matched_criteria_count || 0} / ${state.metadata?.scoring?.marker_count || 3} факторов`));
    const badge = node('span', statusLabel(assessmentStatus(row)), `person-status ${statusClass(assessmentStatus(row))}`);
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
  stopFlightPlayback();
  const shouldScrollToProfile = state.selected !== null && window.matchMedia('(max-width: 780px)').matches;
  const request = ++state.request; state.selected = id; state.profile = null; state.sourceDetails = null; state.focus = null; state.mosquitoFlight = null; state.timelinePage = 0; state.view = [-180, -90, 360, 180];
  renderList(); $('#profilePanel').replaceChildren(node('div', 'Загружаем карточку и историю…', 'panel empty-state')); showError(null);
  try { const data = await api(`/api/profile?id=${encodeURIComponent(id)}`); if (request !== state.request) return; state.profile = data; renderProfile(); loadSourceDetails(id, request); if (shouldScrollToProfile) window.setTimeout(() => $('#profilePanel')?.scrollIntoView({behavior:'smooth', block:'start'}), 20); }
  catch (error) { if (request === state.request) { $('#profilePanel').replaceChildren(node('div', 'Карточка недоступна.', 'panel empty-state')); showError(error.message); } }
}

async function loadSourceDetails(id, request) {
  try {
    const data = await api(`/api/profile/source?id=${encodeURIComponent(id)}`);
    if (request !== state.request || state.selected !== id) return;
    state.sourceDetails = data;
    renderSourceDetails();
  } catch (error) {
    if (request !== state.request || state.selected !== id) return;
    state.sourceDetails = {available: false, reason: error.message, observations: [], travels: [], identifiers: [], documents: []};
    renderSourceDetails();
  }
}

function appendSourceFields(parent, fields) {
  const grid = node('dl', undefined, 'source-fields');
  for (const [label, value] of fields) {
    const dt = node('dt', label); const dd = node('dd', value === null || value === undefined || value === '' ? '—' : value);
    grid.append(dt, dd);
  }
  parent.append(grid);
}

function renderSourceDetails() {
  const root = $('#sourceDetailsPanel'); if (!root) return;
  root.replaceChildren();
  const data = state.sourceDetails;
  const heading = node('div', undefined, 'section-heading'); const copy = node('div');
  copy.append(node('p', 'ИСТОЧНИКОВЫЕ ДАННЫЕ', 'eyebrow'), node('h3', 'Документы и идентификаторы'));
  heading.append(copy); root.append(heading);
  if (!data) { root.append(node('p', 'Запрашиваем канонический parquet…', 'source-loading')); return; }
  if (!data.available) { root.append(node('p', data.reason || 'Исходный parquet недоступен.', 'source-loading')); return; }
  const identifiers = data.identifiers || [];
  const documents = data.documents || [];
  const summary = node('p', `${number(data.travels_count || 0)} связанных перелётов · ${number(identifiers.length)} записей идентификаторов`, 'source-summary'); root.append(summary);
  if (data.complete === false) root.append(node('p', `Показаны ${number(data.observations_count)} из ${number(data.expected_observations)} исходных наблюдений по проверенным связям. Полная связь ещё не восстановлена.`, 'source-loading'));
  const groupRows = rows => {
    const groups = new Map();
    for (const row of rows) {
      const key = [row.kind || '', row.programme || '', row.value || ''].join('\u001f');
      const group = groups.get(key) || {kind: row.kind, programme: row.programme, value: row.value, count: 0, sources: new Set(), locators: []};
      group.count += 1;
      if (row.source_id) group.sources.add(row.source_id);
      if (row.locator && group.locators.length < 3) group.locators.push(row.locator);
      groups.set(key, group);
    }
    return [...groups.values()];
  };
  const documentBlock = node('div', undefined, 'source-block');
  const documentGroups = groupRows(documents);
  documentBlock.append(node('h4', `Документы и паспорта (${documentGroups.length})`));
  if (!documentGroups.length) documentBlock.append(node('p', 'В связанных исходных записях паспортные/документные идентификаторы не найдены.', 'muted-copy'));
  for (const row of documentGroups) {
    const card = node('article', undefined, 'source-card document-card');
    card.append(node('strong', row.value || 'Номер не указан'), node('span', `${row.count} повторений · ${[...row.sources].join(', ') || 'источник неизвестен'}`, 'source-meta'));
    appendSourceFields(card, [['Тип', row.kind], ['Программа/серия', row.programme], ['Примеры локаторов', row.locators.join(' · ') || '—']]);
    documentBlock.append(card);
  }
  root.append(documentBlock);
  const identifiersBlock = node('div', undefined, 'source-block'); identifiersBlock.append(node('h4', 'Все идентификаторы'));
  const identifierTable = node('div', undefined, 'source-table');
  for (const row of groupRows(identifiers)) {
    const item = node('article', undefined, 'source-table-row');
    item.append(node('span', row.kind || '—', 'source-kind'), node('strong', row.value || '—'), node('span', `${row.count} повторений · ${[...row.sources].join(', ') || 'источник неизвестен'}`, 'source-meta'));
    identifierTable.append(item);
  }
  if (!identifierTable.children.length) identifierTable.append(node('p', 'Идентификаторов нет.', 'muted-copy'));
  identifiersBlock.append(identifierTable); root.append(identifiersBlock);
}

function renderMarkerPanel(data) {
  const order=f=>f.status==='matched'?0:f.has_unconfirmed_candidates?1:f.status==='not_matched'?2:3;
  const factors=[...(data.factors || []),...(data.additional_factors || [])]
    .sort((a,b)=>order(a)-order(b)||(b.contribution || 0)-(a.contribution || 0)||a.criterion_id.localeCompare(b.criterion_id));
  const category=f=>f.status==='matched'?'signal':f.has_unconfirmed_candidates?'review':f.status==='unavailable'?'missing':'quiet';
  const labels={signal:'Сработал',review:'Нужна проверка',quiet:'Без совпадений',missing:'Нет данных'};
  const panel=node('section',undefined,'panel marker-panel');
  const heading=node('div',undefined,'marker-heading');
  const title=node('div'); title.append(node('p','АНАЛИЗ ПРОФИЛЯ','eyebrow'),node('h3','Маркеры'));
  const available=factors.filter(f=>f.status!=='unavailable').length;
  heading.append(title,node('span',`${available} из ${factors.length} доступны`,'marker-coverage')); panel.append(heading);
  const filters=node('div',undefined,'marker-filters'); filters.setAttribute('role','group'); filters.setAttribute('aria-label','Фильтр маркеров');
  const grid=node('div',undefined,'marker-tiles'); grid.setAttribute('aria-label','Маркеры профиля');
  const inspector=node('section',undefined,'marker-inspector'); inspector.id='markerInspector'; inspector.setAttribute('aria-label','Подробности выбранного маркера');
  let selected=factors[0]?.criterion_id,filter='all';
  const filterButtons=new Map(),tileButtons=new Map();
  const showFactor=factor=>{
    selected=factor.criterion_id;
    for(const [id,button] of tileButtons) { button.classList.toggle('selected',id===selected);button.setAttribute('aria-pressed',String(id===selected)); }
    inspector.replaceChildren(); inspector.className=`marker-inspector ${category(factor)}`;
    const top=node('div',undefined,'marker-inspector-heading');
    const identity=node('div'); identity.append(node('span',factor.criterion_id,'marker-code'),node('h4',factor.title_ru));
    top.append(identity,node('span',factor.weight===0?'Отключён':labels[category(factor)],'marker-status-badge')); inspector.append(top);
    const stats=node('div',undefined,'marker-detail-stats');
    for(const [label,value] of [['Добавлено баллов',number(factor.contribution || 0)],['Вес маркера',number(factor.weight)],['Исходных записей',number(factor.used_observations || 0)]]) {
      const metric=node('div');metric.append(node('span',label),node('strong',value));stats.append(metric);
    }
    inspector.append(stats,node('p',factor.explanation_ru || 'Нет пояснения.','marker-explanation'));
    appendMarkerComparisons(inspector,factor);
    if(factor.evidence_segment_ids?.length) {
      const link=node('button',`Посмотреть перелёты · ${number(factor.evidence_segment_ids.length)}`,'button marker-evidence-button');link.type='button';
      link.addEventListener('click',()=>{selectRoute(factor.evidence_segment_ids[0]);document.querySelector('#timeline')?.scrollIntoView({behavior:'smooth',block:'center'});});inspector.append(link);
    }
  };
  const renderTiles=()=>{
    grid.replaceChildren();tileButtons.clear();
    for(const [key,button] of filterButtons)button.setAttribute('aria-pressed',String(key===filter));
    const visible=factors.filter(f=>filter==='all'||category(f)===filter);
    if(!visible.some(f=>f.criterion_id===selected))selected=visible[0]?.criterion_id;
    for(const factor of visible) {
      const kind=category(factor),tile=node('button',undefined,`marker-tile ${kind}`);tile.type='button';tile.setAttribute('aria-controls','markerInspector');
      const top=node('span',undefined,'marker-tile-top');top.append(node('span',factor.criterion_id,'marker-code'),node('span',factor.weight===0?'Отключён':labels[kind],'marker-status-badge'));
      const bottom=node('span',undefined,'marker-tile-bottom');
      const score=factor.contribution>0?`+${number(factor.contribution)}`:factor.status==='matched'?'0':kind==='review'?'Похожие имена':kind==='missing'?'Недостаточно данных':'Совпадений нет';
      bottom.append(node('span',score,'marker-tile-result'),node('span','↗','marker-tile-open'));
      tile.append(top,node('span',factor.title_ru,'marker-tile-title'),bottom);
      tile.addEventListener('click',()=>{showFactor(factor);inspector.scrollIntoView({behavior:'smooth',block:'nearest'});});tileButtons.set(factor.criterion_id,tile);grid.append(tile);
    }
    inspector.hidden=!visible.length;
    if(!visible.length)grid.append(node('p','Маркеров с таким статусом нет.','marker-empty'));
    else showFactor(visible.find(f=>f.criterion_id===selected));
  };
  for(const [key,label] of [['all','Все'],['signal','Сработали'],['review','Проверить'],['quiet','Без совпадений'],['missing','Нет данных']]) {
    const count=key==='all'?factors.length:factors.filter(f=>category(f)===key).length;
    const button=node('button',undefined,'marker-filter');button.type='button';button.disabled=key!=='all'&&!count;
    button.append(node('span',label),node('span',number(count),'marker-filter-count'));button.addEventListener('click',()=>{filter=key;renderTiles();});filterButtons.set(key,button);filters.append(button);
  }
  panel.append(filters,grid,inspector);renderTiles();return panel;
}

function renderProfile() {
  const data = state.profile; const person = data.person; const root = $('#profilePanel'); root.replaceChildren();
  const identity = node('div', undefined, 'profile-identity');
  identity.append(node('p', person.rank ? `ПРИОРИТЕТ №${person.rank}` : 'ПРОФИЛЬ МОДЕЛИ', 'eyebrow'), node('h2', person.display_name || 'Имя не указано'), node('p', person.person_id, 'profile-id'));
  const score = node('div', undefined, 'score-block'); score.append(node('span', 'RULE SCORE', 'score-label'), node('strong', number(person.rule_score)), node('small', `${person.matched_criteria_count || 0} сработавших факторов`));
  const head = node('section', undefined, 'profile-head panel'); head.append(identity, score);
  const chips = node('div', undefined, 'profile-chips'); chips.append(node('span', `${statusLabel(assessmentStatus(person))}`, `chip ${statusClass(assessmentStatus(person))}`), node('span', person.identity_status === 'accepted' ? 'Идентичность установлена' : 'Идентичность неоднозначна', 'chip'), node('span', `${number(person.total_segments || data.segments.length)} сегментов`, 'chip'), node('span', `${number(data.route_balance.total)} маршрутов`, 'chip')); head.append(chips); root.append(head);
  if (state.metadata?.scoring) chips.append(node('span', `Проверено ${person.available_criteria_count || 0} из ${state.metadata.scoring.marker_count}`, 'chip'));

  const sourcePanel = node('section', undefined, 'panel source-details-panel'); sourcePanel.id = 'sourceDetailsPanel'; root.append(sourcePanel); renderSourceDetails();

  root.append(renderMarkerPanel(data));

  const mapPanel = node('section', undefined, 'panel map-panel'); const mapHeading = node('div', undefined, 'section-heading'); const headingCopy = node('div'); headingCopy.append(node('p', 'ГЕОГРАФИЯ И ХРОНОЛОГИЯ', 'eyebrow'), node('h3', 'Направленный граф перелётов')); mapHeading.append(headingCopy, node('span', 'стрелка = направление', 'micro-tag')); mapPanel.append(mapHeading);
  if(person.map_mascot?.basis==='given_name_hint')mapPanel.append(node('p','Персонаж выбран по имени. Пол в исходных записях не указан.','map-note'));
  const controls = node('div', undefined, 'route-controls');
  for (const [id, label] of [['routeFrom', 'С'], ['routeTo', 'По']]) { const wrap = node('label', label); const input = node('input'); input.type = 'date'; input.id = id; input.value = id === 'routeFrom' ? state.routeFrom : state.routeTo; input.addEventListener('change', () => { stopFlightPlayback(); state.routeFrom = $('#routeFrom').value; state.routeTo = $('#routeTo').value; state.timelinePage = 0; renderTimeline(); }); wrap.append(input); controls.append(wrap); }
  const unknown = node('label', undefined, 'check-label'); const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = state.showUndated; checkbox.addEventListener('change', () => { stopFlightPlayback(); state.showUndated = checkbox.checked; state.timelinePage = 0; renderTimeline(); }); unknown.append(checkbox, node('span', 'Показывать без даты')); controls.append(unknown);
  const reset = node('button', 'Сбросить период', 'button subtle'); reset.type = 'button'; reset.addEventListener('click', () => { stopFlightPlayback(); state.routeFrom = ''; state.routeTo = ''; state.showUndated = true; renderProfile(); }); controls.append(reset); mapPanel.append(controls);
  const balance = node('div', undefined, 'route-balance'); balance.id = 'routeBalance'; mapPanel.append(balance);
  const focusSummary = node('div', 'Выберите строку истории, чтобы сфокусировать перелёт.', 'route-focus'); focusSummary.id = 'routeFocus'; mapPanel.append(focusSummary);
  const mapWrap = node('div', undefined, 'map-wrap'); const map = svg('svg', {class:'map', viewBox:'-180 -90 360 180', role:'img', 'aria-label':'Карта маршрутов'}); map.id = 'map';
  let mapDrag = null, suppressMapClick = false;
  map.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    suppressMapClick = false;
    mapDrag = {x: event.clientX, y: event.clientY, view: [...state.view], moved:false};
  });
  map.addEventListener('pointermove', event => {
    if (!mapDrag) return;
    const rect = map.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dx = event.clientX - mapDrag.x;
    const dy = event.clientY - mapDrag.y;
    if (!mapDrag.moved && Math.hypot(dx,dy)<4) return;
    mapDrag.moved=true; suppressMapClick=true;
    if(!map.hasPointerCapture(event.pointerId)) map.setPointerCapture(event.pointerId);
    state.view = [
      mapDrag.view[0] - dx * mapDrag.view[2] / rect.width,
      mapDrag.view[1] - dy * mapDrag.view[3] / rect.height,
      mapDrag.view[2], mapDrag.view[3],
    ];
    drawMap(visibleSegments());
  });
  const stopMapDrag = () => { mapDrag = null; };
  map.addEventListener('pointerup', stopMapDrag); map.addEventListener('pointercancel', stopMapDrag); map.addEventListener('lostpointercapture', stopMapDrag);
  map.addEventListener('click', event => {
    if(suppressMapClick) {suppressMapClick=false;return;}
    const segmentId=event.target.closest?.('[data-segment-id]')?.getAttribute('data-segment-id');
    if(segmentId) selectRoute(segmentId);
  });
  map.addEventListener('keydown',event=>{
    if(!['Enter',' '].includes(event.key))return;
    const segmentId=event.target.closest?.('[data-segment-id]')?.getAttribute('data-segment-id');
    if(segmentId){event.preventDefault();selectRoute(segmentId);}
  });
  mapWrap.append(map); const tools = node('div', undefined, 'map-tools'); [['+', .7], ['−', 1.4], ['Обзор', 0]].forEach(([label, factor]) => { const button = node('button', label); button.type='button'; button.addEventListener('click', () => zoomMap(factor)); tools.append(button); }); mapWrap.append(tools); mapPanel.append(mapWrap, node('p', 'Нажмите на стрелку, линию или строку истории, чтобы выбрать перелёт.', 'map-note')); root.append(mapPanel);
  const timeline = node('section', undefined, 'panel timeline-panel'); timeline.id = 'timeline'; const timelineHeading = node('div', undefined, 'timeline-heading'); const timelineTitle = node('div', undefined, 'timeline-title'); timelineTitle.append(node('p', 'ПЕРВИЧНЫЕ СВИДЕТЕЛЬСТВА', 'eyebrow'), node('h3', 'Перелёты профиля')); const timelineActions = node('div', undefined, 'timeline-actions'); timelineActions.append(node('span', `${data.segments.length} записей`, 'micro-tag')); const showAll = node('button', 'Показать все маршруты', 'button subtle route-reset'); showAll.type = 'button'; showAll.addEventListener('click', () => { stopFlightPlayback(); state.focus = null; renderTimeline(); });
  const play=node('button','Запустить перелёты','button button-primary flight-play');play.type='button';play.id='flightPlayButton';
  play.addEventListener('click',()=>state.playback?.status==='playing'?stopFlightPlayback():startFlightPlayback());
  timelineActions.append(play,showAll); timelineHeading.append(timelineTitle, timelineActions); timeline.append(timelineHeading, node('div', undefined, 'timeline-table'), node('div', undefined, 'timeline-pagination')); const playbackStatus=node('p',undefined,'flight-playback-status');playbackStatus.id='flightPlaybackStatus';playbackStatus.setAttribute('role','status');timelineHeading.after(playbackStatus); const timelineHost = $('#sidebarTimeline'); if (timelineHost) { timelineHost.replaceChildren(timeline); } else { root.append(timeline); }
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
function selectRoute(segmentId,{autoplay=false}={}) {
  if(!autoplay)stopFlightPlayback();
  const rows=visibleSegments(),index=rows.findIndex(row=>row.segment_id===segmentId);
  if(index<0)return;
  state.focus=segmentId;state.mosquitoFlight=null;
  state.timelinePage=Math.floor(index/state.timelinePageSize);
  if(autoplay)state.view=fitMapView([rows[index]]);
  renderTimeline();
}
function updatePlaybackControls() {
  const button=$('#flightPlayButton'),status=$('#flightPlaybackStatus');
  const rows=visibleSegments(),playable=rows.filter(row=>row.is_plottable&&row.origin&&row.destination);
  const playback=state.playback;
  if(button){button.textContent=playback?.status==='playing'?'Остановить перелёты':'Запустить перелёты';button.disabled=!playable.length;button.setAttribute('aria-pressed',String(playback?.status==='playing'));}
  if(!status)return;
  const skipped=rows.length-playable.length;
  const note=skipped ? ` Без координат: ${number(skipped)} — пропущены.` : '';
  if(playback?.status==='playing')status.textContent=`Перелёт ${playback.index+1} из ${playback.queue.length}. Следующий — после прибытия.${note}`;
  else if(playback?.status==='completed')status.textContent=`Показаны все ${number(playback.queue.length)} перелётов.${note}`;
  else status.textContent=playable.length ? `По порядку дат · ${number(playable.length)} перелётов на карте.${skipped?` Без координат: ${number(skipped)}.`:''}` : 'Нет перелётов с координатами для запуска.';
}
function stopFlightPlayback() {
  if(state.playback?.status==='playing'&&state.mosquitoFlight)state.mosquitoFlight.pausedAt=performance.now();
  state.playback=null;
  updatePlaybackControls();
}
function startFlightPlayback() {
  const rows=visibleSegments().filter(row=>row.is_plottable&&row.origin&&row.destination)
    .sort((a,b)=>(a.departure_date||'9999').localeCompare(b.departure_date||'9999'));
  if(!rows.length)return;
  state.playback={profileId:state.selected,queue:rows.map(row=>row.segment_id),index:0,status:'playing'};
  selectRoute(rows[0].segment_id,{autoplay:true});
}
function advanceFlightPlayback(playback) {
  if(!playback||state.playback!==playback||playback.status!=='playing'||playback.profileId!==state.selected)return;
  if(playback.index+1>=playback.queue.length){playback.status='completed';updatePlaybackControls();return;}
  playback.index++;
  selectRoute(playback.queue[playback.index],{autoplay:true});
}
function renderTimeline() {
  if (!state.profile) return;
  const rows = visibleSegments();
  const undated = rows.filter(row => !row.departure_date).length;
  const balance = $('#routeBalance');
  if (balance) balance.textContent = `Показано ${rows.length} из ${state.profile.route_balance.total} · с координатами ${state.profile.route_balance.plottable} · без координат ${state.profile.route_balance.unplottable} · без даты ${undated}`;
  if (state.focus && !rows.some(row => row.segment_id === state.focus)) state.focus = null;
  if (state.view[0] === -180 && state.view[1] === -90 && state.view[2] === 360 && state.view[3] === 180) state.view = fitMapView(rows);
  const focused = rows.find(row => row.segment_id === state.focus);
  const focusSummary = $('#routeFocus');
  if (focusSummary) {
    const origin = focused?.origin?.airport_id || focused?.origin_airport_id || '???';
    const destination = focused?.destination?.airport_id || focused?.destination_airport_id || '???';
    focusSummary.classList.toggle('focused', Boolean(focused));
    focusSummary.textContent = focused ? `Выбранный перелёт · ${dateLabel(focused.departure_date)} · ${airportLabel(origin)} → ${airportLabel(destination)} · ${focused.flight_number || 'рейс не указан'}` : 'Выберите строку истории, чтобы сфокусировать перелёт.';
  }
  const table = document.querySelector('.timeline-table');
  if (!table) return;
  table.replaceChildren();
  const header = node('div', undefined, 'timeline-row timeline-header');
  ['Дата', 'Маршрут', 'Рейс', 'Статус'].forEach(text => header.append(node('span', text)));
  table.append(header);
  if (!rows.length) table.append(node('div', 'В выбранном периоде записей нет.', 'empty-list'));
  const pages = Math.max(1, Math.ceil(rows.length / state.timelinePageSize));
  state.timelinePage = Math.min(Math.max(0, state.timelinePage), pages - 1);
  const offset = state.timelinePage * state.timelinePageSize;
  const pageRows = rows.slice(offset, offset + state.timelinePageSize);
  for (const segment of pageRows) {
    const row = node('button', undefined, `timeline-row ${state.focus === segment.segment_id ? 'focused' : ''}`);
    row.type = 'button';
    const origin = segment.origin?.airport_id || segment.origin_airport_id || '???';
    const destination = segment.destination?.airport_id || segment.destination_airport_id || '???';
    row.append(node('span', dateLabel(segment.departure_date), 'date-cell'), node('span', `${airportLabel(origin)} → ${airportLabel(destination)}`, 'route-cell'), node('span', segment.flight_number || '—', 'flight-cell'), node('span', segment.resolution_status || '—', 'state-cell'));
    row.addEventListener('click', () => selectRoute(segment.segment_id));
    table.append(row);
  }
  const pager = document.querySelector('.timeline-pagination');
  if (pager) {
    pager.replaceChildren();
    const previous = node('button', '← Назад', 'button subtle');
    const next = node('button', 'Вперёд →', 'button subtle');
    const label = node('span', `${rows.length ? offset + 1 : 0}–${Math.min(offset + pageRows.length, rows.length)} из ${rows.length}`, 'pagination-label');
    previous.type = next.type = 'button'; previous.disabled = state.timelinePage <= 0; next.disabled = state.timelinePage >= pages - 1;
    previous.addEventListener('click', () => { state.timelinePage -= 1; renderTimeline(); });
    next.addEventListener('click', () => { state.timelinePage += 1; renderTimeline(); });
    pager.append(previous, label, next);
  }
  updatePlaybackControls();
  drawMap(rows);
}
function routeParts(a, b) { let [x1,y1]=a, [x2,y2]=b; if (Math.abs(x2-x1)<=180) return [[a,b]]; const direction=x1>0?180:-180; x2+=x1>0?360:-360; const ratio=(direction-x1)/(x2-x1); const y=y1+ratio*(y2-y1); return [[[x1,y1],[direction,y]],[[-direction,y],[b[0],y2]]]; }
function shortDate(value) { if (!value) return 'дата неизвестна'; const parts=String(value).split('-'); return parts.length===3 ? `${parts[2]}.${parts[1]}.${parts[0]}` : String(value); }
function airportLabel(value) { return String(value || '???').replace(/^openflights:/, ''); }
function routeKey(segment) { const from=segment.origin?.airport_id || segment.origin_airport_id || '???'; const to=segment.destination?.airport_id || segment.destination_airport_id || '???'; return `${from}→${to}`; }
function curvedRoute(from, to, bend) {
  const [x1, y1] = from, [x2, y2] = to;
  const dx = x2 - x1, dy = y2 - y1, length = Math.hypot(dx, dy) || 1;
  const nx = -dy / length, ny = dx / length;
  const mx = (x1 + x2) / 2 + nx * bend, my = (y1 + y2) / 2 + ny * bend;
  const t = .86, u = 1 - t;
  return {path: `M${x1},${y1} Q${mx},${my} ${x2},${y2}`, label: [u*u*x1 + 2*u*t*mx + t*t*x2, u*u*y1 + 2*u*t*my + t*t*y2], control: [mx, my]};
}
function mapElementScale() {
  const width = Number(state.view[2]) || 360;
  const level = Math.pow(Math.min(1, Math.max(.02, width / 360)), 1.2);
  return {
    arrow: Math.min(4.4, Math.max(.38, .38 + level * 4.02)),
    dot: Math.min(.92, Math.max(.07, .07 + level * .79)),
    date: Math.min(.86, Math.max(.08, .08 + level * .78)),
  };
}
function fitMapView(rows) {
  const points = rows.filter(segment => segment.is_plottable && segment.origin && segment.destination).flatMap(segment => [segment.origin, segment.destination]).map(point => [Number(point.longitude), -Number(point.latitude)]).filter(([x, y]) => Number.isFinite(x) && Number.isFinite(y));
  if (!points.length) return [-180, -90, 360, 180];
  const xs = points.map(point => point[0]), ys = points.map(point => point[1]);
  const spanX = Math.max(10, Math.max(...xs) - Math.min(...xs)), spanY = Math.max(10, Math.max(...ys) - Math.min(...ys));
  const width = Math.min(360, spanX + Math.max(8, spanX * .18)); const height = Math.min(180, spanY + Math.max(8, spanY * .22));
  const centerX = (Math.min(...xs) + Math.max(...xs)) / 2; const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
  return [centerX - width / 2, centerY - height / 2, width, height];
}
function mapWrapOffsets() {
  const [x, , width] = state.view;
  const first = Math.floor((x - 180) / 360) - 1;
  const last = Math.ceil((x + width + 180) / 360) + 1;
  return Array.from({length: Math.max(1, last - first + 1)}, (_, index) => (first + index) * 360);
}
function animateMosquitoes(items) {
  if (!items.length || !state.mosquitoFlight) return;
  const flight=state.mosquitoFlight;
  const playback=state.playback;
  const {shiftedStart:from,shiftedEnd:to,control}=items[0];
  const samples=[{t:0,distance:0}];
  let length=0,previous=from;
  for(let step=1;step<=128;step++) {
    const t=step/128,u=1-t;
    const point=[u*u*from[0]+2*u*t*control[0]+t*t*to[0],u*u*from[1]+2*u*t*control[1]+t*t*to[1]];
    length+=Math.hypot(point[0]-previous[0],point[1]-previous[1]);
    samples.push({t,distance:length}); previous=point;
  }
  const speed=4; // Fixed map units per second, independent of route length/zoom.
  const tick=now=>{
    if(state.mosquitoFlight!==flight || !items[0].mosquito.isConnected) return;
    const elapsed=Math.max(0,(flight.pausedAt ?? now)-flight.started);
    const arrived=flight.playOnce&&elapsed>=Math.max(300,length/speed*1000);
    const distance=flight.playOnce?Math.min(length,elapsed/1000*speed):(elapsed/1000*speed)%Math.max(length,.001);
    let index=1;
    while(index<samples.length-1 && samples[index].distance<distance) index++;
    const a=samples[index-1],b=samples[index];
    const t=arrived?1:a.t+(b.t-a.t)*(distance-a.distance)/Math.max(b.distance-a.distance,.000001),u=1-t;
    for(const {mosquito,shiftedStart:start,shiftedEnd:end,control:c,size} of items) {
      const x=u*u*start[0]+2*u*t*c[0]+t*t*end[0],y=u*u*start[1]+2*u*t*c[1]+t*t*end[1];
      const dx=2*u*(c[0]-start[0])+2*t*(end[0]-c[0]),dy=2*u*(c[1]-start[1])+2*t*(end[1]-c[1]);
      const rawAngle=Math.atan2(dy,dx)*180/Math.PI,mirrored=dx<0;
      const angle=mirrored ? (rawAngle>0?rawAngle-180:rawAngle+180) : rawAngle;
      mosquito.setAttribute('x',String(x-size/2)); mosquito.setAttribute('y',String(y-size/2));
      mosquito.setAttribute('transform',`translate(${x} ${y}) rotate(${angle}) scale(${mirrored?-1:1} 1) translate(${-x} ${-y})`);
    }
    if(flight.pausedAt!==undefined)return;
    if(arrived){
      // Paint the destination for one frame before selecting the next flight.
      if(flight.arrivedAt!==undefined){advanceFlightPlayback(playback);return;}
      flight.arrivedAt=now;
    }
    state.mosquitoFrame=requestAnimationFrame(tick);
  };
  tick(performance.now());
}
function drawMap(rows) {
  if(state.mosquitoFrame!==null) { cancelAnimationFrame(state.mosquitoFrame); state.mosquitoFrame=null; }
  const map = $('#map'); if (!map) return;
  const flightKey=state.focus ? `${state.selected}:${state.focus}` : null;
  if(!flightKey) state.mosquitoFlight=null;
  else if(state.mosquitoFlight?.key!==flightKey) state.mosquitoFlight={key:flightKey,started:performance.now(),playOnce:state.playback?.status==='playing'};
  map.setAttribute('viewBox',state.view.join(' '));
  map.replaceChildren();
  const scale = mapElementScale(); const defs = svg('defs');
  const mosquitoFilter = svg('filter', {id:'mosquito-chroma', 'color-interpolation-filters':'sRGB'});
  // The source GIF has a pure yellow background. Use blue as the key channel:
  // yellow has B=0, while the white and light-blue mosquito pixels have B>0.
  const mosquitoChroma = svg('feColorMatrix', {type:'matrix', values:'1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 1 0 0'});
  const mosquitoOpaque = svg('feComponentTransfer');
  mosquitoOpaque.append(svg('feFuncA', {type:'discrete', tableValues:'0 1'}));
  mosquitoFilter.append(mosquitoChroma, mosquitoOpaque);
  defs.append(mosquitoFilter); map.append(defs);
  const matrix=map.getScreenCTM();
  const pixelsPerUnit=Math.max(.001,matrix?Math.hypot(matrix.a,matrix.b):map.getBoundingClientRect().width/state.view[2]);
  const offsets = mapWrapOffsets();
  for (const offset of offsets) for (let x=-180; x<=180; x+=30) map.append(svg('line', {x1:x+offset, x2:x+offset, y1:-90, y2:90, class:'gridline'}));
  for (let y=-60; y<=60; y+=30) map.append(svg('line', {x1:-180+offsets[0], x2:180+offsets[offsets.length-1], y1:y, y2:y, class:'gridline'}));
  for (const feature of state.geo?.features || []) if (['Polygon','MultiPolygon'].includes(feature.geometry?.type)) for (const offset of offsets) map.append(svg('path', {d:countryPath(feature.geometry, offset), class:'country'}));
  const plottable = rows.filter(segment => segment.is_plottable && segment.origin && segment.destination);
  const groups = new Map(); const airports = new Map(); const mosquitoes = [], arrows=[];
  for (const segment of plottable) {
    const a = segment.origin, b = segment.destination;
    const start = [Number(a.longitude), -Number(a.latitude)], end = [Number(b.longitude), -Number(b.latitude)];
    const key = routeKey(segment); if (!groups.has(key)) groups.set(key, []); groups.get(key).push({segment, start, end});
  }
  for (const group of groups.values()) group.forEach((item, index) => {
    const {segment, start, end} = item; const bend = (index - (group.length - 1) / 2) * 4.5;
    for (const offset of offsets) {
      const shiftedStart = [start[0] + offset, start[1]], shiftedEnd = [end[0] + offset, end[1]];
      const curve = curvedRoute(shiftedStart, shiftedEnd, bend); const active = state.focus === segment.segment_id; const dimmed = Boolean(state.focus && !active);
      const airportKey = `${segment.origin.airport_id}:${offset}`;
      const current = airports.get(airportKey) || {point: shiftedStart, airport: segment.origin.airport_id, active:false, dimmed};
      current.active = current.active || active; current.dimmed = dimmed; airports.set(airportKey, current);
      const line = svg('path', {d:curve.path, class:`route ${active?'active':''} ${dimmed?'dimmed':''}`, 'data-segment-id':segment.segment_id});
      line.append(svg('title', {}, `${segment.origin.airport_id} → ${segment.destination.airport_id} · ${dateLabel(segment.departure_date)}`)); map.append(line);
      map.append(svg('path',{d:curve.path,class:'route-hit','data-segment-id':segment.segment_id}));
      // A real SVG element receives clicks; a marker-end instance does not.
      const arrowSize=scale.arrow*(active?1.3:1);
      const angle=Math.atan2(shiftedEnd[1]-curve.control[1],shiftedEnd[0]-curve.control[0])*180/Math.PI;
      const arrow=svg('g',{class:`route-arrow ${active?'active':''} ${dimmed?'dimmed':''}`,
        'data-segment-id':segment.segment_id,role:'button',tabindex:0,'aria-pressed':String(active),
        'aria-label':`Выбрать перелёт ${dateLabel(segment.departure_date)} · ${segment.flight_number || ''} · ${airportLabel(segment.origin.airport_id)} → ${airportLabel(segment.destination.airport_id)}`,
        transform:`translate(${shiftedEnd[0]} ${shiftedEnd[1]}) rotate(${angle})`});
      arrow.append(svg('title',{},`${dateLabel(segment.departure_date)} · ${segment.flight_number || ''}`),
        svg('circle',{r:Math.max(arrowSize*.65,11/pixelsPerUnit),class:'route-arrow-hit'}),
        svg('path',{d:`M${-.85*arrowSize},${-.5*arrowSize} L${.15*arrowSize},0 L${-.85*arrowSize},${.5*arrowSize} Z`,class:'route-arrow-shape'}));
      arrows.push(arrow);
      if (active) {
        // Keep the sprite readable in screen pixels at every zoom level.
        const size=40/Math.max(pixelsPerUnit,.001);
        const female=state.profile?.person?.map_mascot?.kind==='fly'||state.profile?.person?.sex==='Female';
        const mosquito = svg('image', {href:female?'/fly.png':'/mosquito.gif', x:shiftedStart[0] - size / 2, y:shiftedStart[1] - size / 2, width:size, height:size, class:'route-mosquito', 'aria-label':female?'Муха летит по маршруту':'Комарик летит по маршруту', preserveAspectRatio:'xMidYMid meet', ...(female?{}:{filter:'url(#mosquito-chroma)'})});
        mosquito.append(svg('title', {}, 'Маршрут выбран')); mosquitoes.push({mosquito, shiftedStart, shiftedEnd, control:curve.control, size, segmentId:segment.segment_id});
      }
    }
  });
  for (const [, info] of airports) {
    const circle = svg('circle', {cx:info.point[0], cy:info.point[1], r:info.active ? scale.dot*1.55 : scale.dot, class:`airport-dot ${info.active?'active':''} ${info.dimmed?'dimmed':''}`});
    circle.append(svg('title', {}, info.airport)); map.append(circle);
  }
  for (const arrow of arrows)map.append(arrow);
  for (const item of mosquitoes) map.append(item.mosquito);
  animateMosquitoes(mosquitoes);
  map.setAttribute('viewBox', state.view.join(' '));
}
function appendMarkerComparisons(article,factor) {
  if (factor.comparison_names?.length) {
    const details=node('details',undefined,'marker-comparisons');
    details.append(node('summary',`Имена в исходных записях (${number(factor.diagnostics?.observations)})`));
    const list=node('ul');
    const labels={boardingpass_xlsx:'Посадочный талон',boarding_csv:'CSV посадок',pointz_xml:'Pointz XML',forum_json:'Форум',skyteam_yaml:'Обмен SkyTeam',sirena_fixed:'Бронирование Sirena'};
    for (const name of factor.comparison_names) { const item=node('li'); item.append(node('strong',name.name_raw || 'Имя не заполнено'),node('small',labels[name.source_id] || name.source_id)); list.append(item); }
    details.append(list); article.append(details);
  }
  const page=factor.related_profiles;
  if (!page?.pagination?.total) return;
  const personId=state.selected;
  const details=node('details',undefined,'marker-comparisons related-comparisons');
  details.append(node('summary',`Показать найденные пары (${number(page.pagination.total)})`),node('p','Сходство имени не устанавливает личность. Для начисления баллов нужна подтверждающая связь.','marker-comparison-hint'));
  const list=node('div',undefined,'related-list');
  const more=node('button','Показать ещё','button button-ghost'); more.type='button';
  const append=items=>{for(const item of items){
    const row=node('div',undefined,`related-profile ${item.link_status}`);
    row.append(node('strong',`${item.name_raw} ↔ ${item.other_name_raw}`),node('small',item.reason_ru));
    if(item.identifier_kind)row.append(node('small',`${item.identifier_kind==='document'?'Общий документ':'Общая карта'}: ${item.identifier_value}`));
    row.append(node('code',item.other_person_id));
    if(item.other_person_id!==personId && state.metadata?.data_mode!=='demo_subset') { const open=node('button','Открыть профиль','evidence-link');open.type='button';open.addEventListener('click',()=>selectProfile(item.other_person_id));row.append(open); }
    else if(state.metadata?.data_mode==='demo_subset')row.append(node('small','Открыть другой профиль можно в основном наборе.'));
    list.append(row);
  }};
  append(page.items || []); details.append(list,more); more.hidden=!page.pagination.has_next;
  let nextOffset=page.pagination.offset+(page.items || []).length;
  more.addEventListener('click',async()=>{
    more.disabled=true;
    try { const next=await api(`/api/marker-related?id=${encodeURIComponent(personId)}&marker=${encodeURIComponent(factor.criterion_id)}&offset=${nextOffset}&limit=10`);if(state.selected!==personId)return;append(next.items);nextOffset=next.pagination.offset+next.items.length;more.hidden=!next.pagination.has_next; }
    catch(error){showError(error.message);}finally{more.disabled=false;}
  });
  article.append(details);
}

function countryPath(geometry, offset = 0) {
  const polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates;
  return polygons.map(poly => poly.map(ring => ring.map((v, i) => `${i?'L':'M'}${v[0] + offset},${-v[1]}`).join(' ') + 'Z').join(' ')).join(' ');
}
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
  initTheme();
  $('#themeToggle').addEventListener('click', () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
  $('#demoButton').addEventListener('click', activateDemo); $('#resetButton').addEventListener('click', resetProduct); $('#uploadInput').addEventListener('change', event => uploadProduct(event.target.files[0]));
  $$('.view-tab').forEach(button => button.addEventListener('click', () => changeMode(button.dataset.view)));
  $$('.rank-choice').forEach(button => button.addEventListener('click', async () => { state.limit=Number(button.dataset.limit); state.viewMode='ranking'; state.page=0; $$('.rank-choice').forEach(item => item.classList.toggle('active',item===button)); $$('.view-tab').forEach(item => { const active=item.dataset.view==='ranking'; item.classList.toggle('active',active); item.setAttribute('aria-selected',String(active)); }); await loadList(true); }));
  $('#statusFilter').addEventListener('change', async event => { state.eligibility = event.target.value; state.page = 0; if (state.viewMode === 'ranking') { state.viewMode = 'all'; $$('.view-tab').forEach(button => { const active = button.dataset.view === 'all'; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); }); $('#rankPicker').classList.add('disabled'); } await loadList(false); }); $('#sortSelect').addEventListener('change', async event => { state.sort = event.target.value; state.page = 0; if (state.viewMode === 'ranking') { state.viewMode = 'all'; $$('.view-tab').forEach(button => { const active = button.dataset.view === 'all'; button.classList.toggle('active', active); button.setAttribute('aria-selected', String(active)); }); $('#rankPicker').classList.add('disabled'); } await loadList(false); });
  let timer; $('#searchInput').addEventListener('input', event => { state.query=event.target.value; if(state.viewMode==='ranking'){renderList();return;} clearTimeout(timer); timer=setTimeout(() => {state.page=0;loadList(false);},220); });
  $('#prevPage').addEventListener('click', async () => { if(state.page>0){state.page-=1;await loadList(false);} }); $('#nextPage').addEventListener('click', async () => { if(state.list.pagination?.has_next){state.page+=1;await loadList(false);} });
  bootstrap(true);
});
