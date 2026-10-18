'use strict';
const $ = s => document.querySelector(s);
const number = n => new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 2}).format(n);
const NS = 'http://www.w3.org/2000/svg';
const state = {top: [], selected: null, data: null, geo: null, request: 0, view: [-180, -90, 360, 180], focus: null};
function node(tag, text, className) {const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e;}
function svg(tag, attrs, text) {const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); if (text !== undefined) e.textContent = text; return e;}
async function api(path) {const r = await fetch(path); const j = await r.json(); if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`); return j;}
function error(message) {$('#error').hidden = !message; $('#error').textContent = message || '';}
function renderTop() {
  const query = $('#search').value.toLocaleLowerCase('ru'); const list = $('#top'); list.replaceChildren();
  const rows = state.top.filter(r => `${r.display_name || ''} ${r.person_id}`.toLocaleLowerCase('ru').includes(query));
  for (const r of rows) {
    const b = node('button', undefined, 'person-button'); b.type = 'button'; b.setAttribute('role', 'listitem'); b.setAttribute('aria-pressed', String(r.person_id === state.selected));
    b.append(node('span', String(r.rank).padStart(2, '0'), 'place'));
    const label = node('span'); label.append(node('span', r.display_name || 'Имя не указано', 'person-name'), node('span', r.person_id, 'person-id')); b.append(label);
    const count = node('span', r.matched_criteria_count, 'factor-count'); count.append(node('small', `${number(r.rule_score)} балла`)); b.append(count);
    b.addEventListener('click', () => select(r.person_id)); list.append(b);
  }
  if (!rows.length) list.append(node('p', state.top.length ? 'В топе нет совпадений.' : 'Нет профилей с достаточными данными и срабатываниями. Список не дополняется.', 'empty'));
}
async function select(id) {
  state.selected = id; state.data = null; state.focus = null; state.view = [-180, -90, 360, 180]; const request = ++state.request;
  renderTop(); $('#dossier').replaceChildren(node('div', 'Загружаем историю…', 'panel empty')); error(null);
  try {const data = await api(`/api/profile?id=${encodeURIComponent(id)}`); if (request !== state.request) return; state.data = data; renderDossier();}
  catch (e) {if (request === state.request) {$('#dossier').replaceChildren(node('div', 'Карточка недоступна.', 'panel empty')); error(e.message);}}
}
function renderDossier() {
  const d = state.data, p = d.person, root = $('#dossier'); root.replaceChildren();
  const head = node('section', undefined, 'panel'); const info = node('div', undefined, 'profile-head'); const identity = node('div');
  identity.append(node('p', `МЕСТО ${p.rank || '—'} / КАРТОЧКА ПРОФИЛЯ`, 'eyebrow'), node('h2', p.display_name || 'Имя не указано'), node('p', p.person_id, 'identity'));
  const score = node('div', undefined, 'profile-score'); score.append(node('strong', number(p.rule_score)), node('small', `${p.matched_criteria_count} из 3 независимых факторов`)); info.append(identity, score); head.append(info);
  const factors = node('div', undefined, 'factors');
  for (const f of d.factors) {const card = node('article', undefined, `factor ${f.status}`); card.append(node('span', {matched:'СРАБОТАЛ', not_matched:'НЕ СРАБОТАЛ', unavailable:'НЕДОСТУПЕН'}[f.status], 'status'), node('h3', f.title_ru), node('p', f.explanation_ru));
    if (f.evidence_segment_ids.length) {const a = node('a', `Подтверждения: ${f.evidence_segment_ids.length}`); a.href = '#timeline'; a.addEventListener('click', () => {focusSegment(f.evidence_segment_ids[0]);}); card.append(a);} factors.append(card);}
  head.append(factors);
  const f = d.forest; head.append(node('div', f ? `Isolation Forest: ${number(f.if_percentile * 100)}-й процентиль. Экспериментальная оценка; основной порядок не меняет.` : 'Isolation Forest: оценка недоступна для этого профиля.', 'forest')); root.append(head);
  const mapPanel = node('section', undefined, 'panel'); const heading = node('div', undefined, 'panel-heading'); heading.append(node('h3', 'География перелётов'), node('span', 'Вся история исходного профиля', 'tag')); mapPanel.append(heading);
  const controls = node('div', undefined, 'map-controls');
  for (const [id, label] of [['from','С'], ['to','По']]) {const l = node('label', label+' '); const input = node('input'); input.type = 'date'; input.id = id; input.addEventListener('change', updateRoutes); l.append(input); controls.append(l);}
  const unknown = node('label'); const checkbox = node('input'); checkbox.type='checkbox'; checkbox.id='undated'; checkbox.checked=true; checkbox.addEventListener('change',updateRoutes); unknown.append(checkbox, document.createTextNode(' Без даты')); controls.append(unknown);
  const reset = node('button','Вся история','secondary'); reset.type='button'; reset.addEventListener('click',() => {$('#from').value=''; $('#to').value=''; $('#undated').checked=true; state.focus=null; updateRoutes();}); controls.append(reset); mapPanel.append(controls);
  const balance=node('div'); balance.id='route-balance'; mapPanel.append(balance);
  const mapWrap=node('div',undefined,'map-wrap'); const map=svg('svg',{class:'map',viewBox:'-180 -90 360 180',role:'img','aria-label':'Карта направлений перелётов. Стрелки указывают аэропорт назначения; подписи содержат дату вылета.'}); map.id='map'; mapWrap.append(map);
  const tools=node('div',undefined,'map-tools');
  for (const [label,factor] of [['+',.7],['−',1.4],['Обзор',0]]) {const b=node('button',label); b.type='button'; b.setAttribute('aria-label',factor === 0 ? 'Сбросить масштаб' : factor<1 ? 'Увеличить карту' : 'Уменьшить карту'); b.addEventListener('click',() => {zoom(factor);}); tools.append(b);} mapWrap.append(tools,node('div','→ Направление · Дата вылета · Колесо: масштаб · Перетаскивание: обзор','legend')); mapPanel.append(mapWrap,node('div','Стрелки связывают аэропорты и не изображают фактический трек самолёта. Неполные маршруты остаются в таблице.','map-caption')); root.append(mapPanel);
  const timeline=node('section',undefined,'panel'); timeline.id='timeline'; const th=node('div',undefined,'panel-heading'); th.append(node('h3','Хронология и источники')); timeline.append(th);
  const wrap=node('div',undefined,'timeline-wrap'); const table=node('table'); const row=node('tr'); for (const title of ['Вылет','Маршрут','Рейс / свидетельство','География']) row.append(node('th',title)); const thead=node('thead'); thead.append(row); table.append(thead); const tbody=node('tbody'); tbody.id='flights'; table.append(tbody); wrap.append(table); timeline.append(wrap); root.append(timeline);
  if (d.companions.length) {const c=node('section',undefined,'panel'); const h=node('div',undefined,'panel-heading'); h.append(node('h3',`Повторные совместные события: ${d.companions.length}`)); c.append(h); for (const companion of d.companions) c.append(node('p',`${companion.person_id} · ${companion.shared_days} дней · ${companion.shared_events} событий`,'hint')); c.append(node('p','M07 не добавляет балл: статистическая значимость совместных событий ещё не откалибрована.','hint')); root.append(c);}
  let drag=null;
  map.addEventListener('wheel',e => {e.preventDefault(); zoom(e.deltaY>0?1.15:.85);},{passive:false});
  map.addEventListener('pointerdown',e => {drag={x:e.clientX,y:e.clientY,view:[...state.view]}; map.setPointerCapture(e.pointerId);});
  map.addEventListener('pointermove',e => {if (!drag) return; const r=map.getBoundingClientRect(); state.view=[drag.view[0]-(e.clientX-drag.x)*drag.view[2]/r.width,drag.view[1]-(e.clientY-drag.y)*drag.view[3]/r.height,drag.view[2],drag.view[3]]; applyView();});
  map.addEventListener('pointerup',() => {drag=null;}); map.addEventListener('pointercancel',() => {drag=null;}); updateRoutes();
}
function focusSegment(id) {
  state.focus=id; const s=state.data.segments.find(r=>r.segment_id===id);
  if (s?.is_plottable && Math.abs(s.origin.longitude-s.destination.longitude)<180) {
    const centerX=(s.origin.longitude+s.destination.longitude)/2, centerY=-(s.origin.latitude+s.destination.latitude)/2;
    const width=Math.max(18,Math.abs(s.origin.longitude-s.destination.longitude)*1.7,Math.abs(s.origin.latitude-s.destination.latitude)*3.4);
    state.view=[centerX-width/2,centerY-width/4,width,width/2];
  }
  updateRoutes();
}
function zoom(factor) {const [x,y,w,h]=state.view; if (!factor) state.view=[-180,-90,360,180]; else {const nw=Math.min(720,Math.max(10,w*factor)); state.view=[x+(w-nw)/2,y+(h-nw/2)/2,nw,nw/2];} drawMap(visibleSegments());}
function applyView() {$('#map').setAttribute('viewBox',state.view.join(' '));}
function visibleSegments() {const from=$('#from').value,to=$('#to').value; if (from && to && from>to) throw new Error('Начало периода позже окончания.'); return state.data.segments.filter(s => s.departure_date ? (!from || s.departure_date>=from) && (!to || s.departure_date<=to) : $('#undated').checked);}
function updateRoutes() {
  let rows; try {rows=visibleSegments(); error(null);} catch(e) {error(e.message); return;}
  const d=state.data; $('#route-balance').textContent=`Всего: ${d.route_balance.total} · С координатами: ${d.route_balance.plottable} · Без координат: ${d.route_balance.unplottable} · Показано: ${rows.length} · Скрыто фильтром: ${d.route_balance.total-rows.length}`;
  const tbody=$('#flights'); tbody.replaceChildren();
  for (const s of rows) {const tr=node('tr',undefined,state.focus===s.segment_id?'selected':''); tr.tabIndex=0;
    tr.append(node('td',s.departure_label_ru),node('td',`${s.origin_iata || '?'} → ${s.destination_iata || '?'}`));
    const td=node('td'); td.append(node('span',s.flight_number || 'Номер неизвестен'),node('div',`${s.participation_status==='boarding_record_present'?'Запись о посадке':'Сообщение / бронирование'} · ${s.identity_status==='accepted'?'Связь принята':'Связь неоднозначна'}`,'source-tag'));
    const details=node('details'); details.append(node('summary','Происхождение')); details.append(node('div',`Сегмент: ${s.segment_id}`),node('div',`Исходная поездка: ${s.anchor_travel_id || 'неизвестна'}`),node('div',`Источники: ${s.sources_json || 'неизвестны'}`),node('div',`Точность вылета: ${s.departure_precision || 'неизвестна'}`));
    for (const ref of d.source_evidence.filter(r => r.segment_id===s.segment_id)) details.append(node('div',`${ref.source_id}: ${ref.locator}`)); td.append(details); tr.append(td,node('td',s.is_plottable?'На карте':s.unplottable_reason_ru));
    const focus=()=>focusSegment(s.segment_id); tr.addEventListener('click',e=>{if (!e.target.closest('details')) focus();}); tr.addEventListener('keydown',e=>{if(e.target===tr && (e.key==='Enter'||e.key===' ')){e.preventDefault();focus();}}); tbody.append(tr);
  }
  if (!rows.length) {const tr=node('tr'); const td=node('td','В выбранном периоде поездок нет.'); td.colSpan=4;tr.append(td);tbody.append(tr);} drawMap(rows);
}
function countryPath(geometry) {const polygons=geometry.type==='Polygon'?[geometry.coordinates]:geometry.coordinates; return polygons.map(p=>p.map(r=>r.map((v,i)=>`${i?'L':'M'}${v[0]},${-v[1]}`).join(' ')+'Z').join(' ')).join(' ');}
// Split at ±180° so a Pacific route never becomes a line across the map.
function routeParts(a,b) {let [x1,y1]=a,[x2,y2]=b;if (Math.abs(x2-x1)<=180) return [[a,b]]; const direction=x1>0?180:-180; x2+=x1>0?360:-360; const t=(direction-x1)/(x2-x1); const y=y1+t*(y2-y1); return [[[x1,y1],[direction,y]],[[-direction,y],[b[0],y2]]];}
function drawMap(rows) {
  const map=$('#map');map.replaceChildren(); const defs=svg('defs'); const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto-start-reverse',markerUnits:'strokeWidth'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#74d5ce'}));defs.append(marker);map.append(defs);
  for(let x=-180;x<=180;x+=30) map.append(svg('line',{x1:x,x2:x,y1:-90,y2:90,class:'gridline'}));for(let y=-60;y<=60;y+=30) map.append(svg('line',{x1:-180,x2:180,y1:y,y2:y,class:'gridline'}));
  for(const f of state.geo.features) if (['Polygon','MultiPolygon'].includes(f.geometry.type)) map.append(svg('path',{d:countryPath(f.geometry),class:'country'}));
  const repeats=new Map(), labels=[];
  for(const s of rows.filter(s=>s.is_plottable)) {const a=[s.origin.longitude,-s.origin.latitude],b=[s.destination.longitude,-s.destination.latitude]; const key=`${a}|${b}`,i=repeats.get(key)||0; repeats.set(key,i+1);
    const parts=routeParts(a,b); for(let j=0;j<parts.length;j++) {const [start,end]=parts[j];const path=svg('path',{d:`M${start} L${end}`,class:`route ${state.focus===s.segment_id?'active':''}`});if (j===parts.length-1) path.setAttribute('marker-end','url(#arrow)');path.append(svg('title',{},`${s.origin_iata} → ${s.destination_iata} · ${s.departure_label_ru}`)); map.append(path);}
    map.append(svg('circle',{cx:a[0],cy:a[1],r:.65,class:'airport-dot'}),svg('circle',{cx:b[0],cy:b[1],r:.65,class:'airport-dot'}));
    const [start,end]=parts[parts.length-1],x=(start[0]+end[0])/2,y=(start[1]+end[1])/2-1-i*3.5;
    labels.push({x,y,text:s.departure_label_ru,active:state.focus===s.segment_id,title:`${s.origin_iata} → ${s.destination_iata}`});
  }
  const fontSize=state.view[2]/Math.max(map.clientWidth,300)*11, boxes=[];
  labels.sort((a,b)=>Number(b.active)-Number(a.active));
  for(const l of labels) {
    const width=fontSize*l.text.length*.65,height=fontSize*1.45;
    let x=l.x,y=l.y;
    for(let attempt=0;attempt<160;attempt++) {
      const ring=Math.ceil(attempt/8), angle=(attempt%8)*Math.PI/4;
      x=l.x+Math.cos(angle)*ring*width*.58;y=l.y+Math.sin(angle)*ring*height;
      if(!boxes.some(b=>Math.abs(b.x-x)<(b.width+width)/2+fontSize*.5 && Math.abs(b.y-y)<height))break;
    }
    boxes.push({x,y,width});
    if(Math.abs(x-l.x)+Math.abs(y-l.y)>fontSize)map.append(svg('line',{x1:l.x,y1:l.y,x2:x,y2:y,class:'label-leader'}));
    map.append(svg('rect',{x:x-width/2-fontSize*.2,y:y-fontSize,width:width+fontSize*.4,height,class:'route-date-box',rx:fontSize*.2}));
    const label=svg('text',{x,y,class:`route-date ${l.active?'active':''}`,'font-size':fontSize,'text-anchor':'middle'},l.text);
    label.append(svg('title',{},l.title));map.append(label);
  }
  applyView();
}
async function init() {
  try {const [m,top,geo]=await Promise.all([api('/api/metadata'),api('/api/top'),api('/world.geojson')]);state.top=top;state.geo=geo;
    $('#window').append(node('div','ОКНО НАБЛЮДЕНИЙ'),node('strong',`${m.window.start || 'нет дат'} — ${m.window.end || 'нет дат'}`),node('div',m.run_id));
    const eligible=m.eligibility.find(s=>s.eligibility_status==='eligible')?.profiles||0;
    const metrics=[['Профилей в исходных таблицах',m.counts.persons],['Сегментов перелётов',m.counts.segments],['С факторами и достаточными данными',eligible],['В текущем топе',top.length]];
    for (const [label,value] of metrics) {const e=node('div',undefined,'metric');e.append(node('strong',number(value)),node('span',label));$('#metrics').append(e);}
    $('#topCount').textContent=`/ ${top.length}`;const method=$('#method');method.append(node('p','Используются исходные обработанные таблицы преподавательского датасета. Дополнительная генерация людей, перелётов и разметки отсутствует.'));
    method.append(node('p','Порядок: число сработавших групп ↓, сумма вкладов ↓, стабильный ID ↑. Вклад группы = вес × min(3, значение / порог). Пороги и веса сохранены в снимке. Балл — основание для проверки паттернов, а не вероятность шпионажа.'));
    method.append(node('p','Общее окно определено по датам всего датасета; непрерывность наблюдения источников не установлена. Неоднозначные профили и недостаточные данные не получают искусственных срабатываний.'));
    const list=node('ul');for (const r of m.eligibility) list.append(node('li',`${{eligible:'В рейтинге',no_matches:'Без срабатываний',insufficient_data:'Недостаточно данных'}[r.eligibility_status]}: ${number(r.profiles)}`));method.append(list);
    const limitationLabels = {
      'Original raw files and complete T13 manifests are absent from this checkout; supplied T13 bytes verified against committed LFS SHA-256.':'В этой поставке нет raw-файлов и полной цепочки манифестов T13. Используются поставленные таблицы; их SHA-256 совпадают с закреплёнными Git LFS объектами.',
      'Source locators unavailable: evidence links resolve to original T13 segment_id and anchor_travel_id only.':'Исходные locators недоступны. Подтверждения раскрываются до оригинальных идентификаторов сегмента и исходной поездки T13.',
      'The source observation window is inferred from dataset dates; continuous source coverage is not asserted.':'Окно определено по представленным датам. Полное непрерывное покрытие каждого источника не установлено.',
      'Rule thresholds and weights require domain review; scores are not class probabilities.':'Пороги и веса требуют предметной проверки. Баллы не являются вероятностями класса.'
    };
    for (const limitation of m.limitations) method.append(node('p',limitationLabels[limitation] || limitation));method.append(node('p',`IF: ${m.forest_status}; граф: ${m.graph_status}; независимое ревью: ожидается. Достоверных меток для precision/recall нет. Устойчивость рассчитана на исходных профилях при изменении порогов ±20% и отключении групп.`));
    const check=node('details');check.append(node('summary','Результаты проверки устойчивости'));const pre=node('pre',JSON.stringify(m.evaluation,null,2));check.append(pre);method.append(check);
    $('#search').addEventListener('input',renderTop);renderTop();if(top.length) await select(top[0].person_id);else $('#dossier').replaceChildren(node('div','Топ пуст: проверьте покрытие и результаты критериев. Данные не подставляются.','panel empty'));
  } catch(e) {error(`Не удалось загрузить продукт: ${e.message}`);$('#top').replaceChildren(node('p','Снимок недоступен.','empty'));}
}
init();
