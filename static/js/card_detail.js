/**
 * card_detail.js — полная страница карточки фильма/сериала.
 */

const _CD_IMG_BASE   = (window.TMDB_IMAGE_BASE || 'https://image.tmdb.org');
const _BACKDROP_BASE = _CD_IMG_BASE + '/t/p/w780';
const _WATCHED_THR   = 90;

(async function () {
  const cardId   = window.CARD_ID;
  const params   = new URLSearchParams(location.search);
  const backUrl  = params.get('back') || '/history';

  let deviceId  = params.get('device_id') ? parseInt(params.get('device_id')) : null;
  let profileId = params.has('profile_id') ? params.get('profile_id') : null;
  if (!deviceId) {
    try {
      const prefs = JSON.parse(localStorage.getItem('history_prefs') || '{}');
      if (prefs.device_id) {
        deviceId  = parseInt(prefs.device_id);
        if (profileId === null && prefs.hasOwnProperty('profile_id')) profileId = prefs.profile_id;
      }
    } catch { /* ignore */ }
  }

  document.getElementById('cardBack').href = backUrl;

  try {
    const res = await fetch(`/api/media-card/${encodeURIComponent(cardId)}`);
    if (!res.ok) {
      document.getElementById('cardLoading').textContent = 'Карточка не найдена';
      return;
    }
    const card = await res.json();
    _renderCard(card, cardId);

    if (!deviceId) return;

    if (card.media_type === 'movie') {
      await _loadMovieProgress(card, cardId, deviceId, profileId);
    } else {
      // Для сериала: сначала эпизоды (содержат percent), потом общий прогресс из них
      const epData = await _fetchEpisodes(cardId, deviceId, profileId);
      if (epData) {
        const qp    = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
        const tcRes = await fetch(`/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`);
        const tcRows = tcRes.ok ? await tcRes.json() : [];
        _renderEpisodes(epData, card, cardId, deviceId, profileId);
        _setupTvProgressSection(epData.episodes || [], cardId, deviceId, profileId, tcRows);
      }
    }
  } catch (e) {
    document.getElementById('cardLoading').textContent = 'Ошибка загрузки';
  }
})();


// ─── Helpers ──────────────────────────────────────────────────────────────────

function _fmtTime(sec) {
  if (!sec && sec !== 0) return '';
  sec = Math.round(sec);
  const h  = Math.floor(sec / 3600);
  const m  = Math.floor((sec % 3600) / 60);
  const s  = sec % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function _fmtRuntime(min) {
  if (!min) return '';
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h ? (m ? `${h} ч ${m} мин` : `${h} ч`) : `${m} мин`;
}

/** Парсит строку времени H:MM:SS / MM:SS → секунды, или 0 если неверный формат */
function _parseTime(s) {
  const parts = (s || '').trim().replace(/^✓\s*/, '').split(':').map(p => Number(p));
  if (!parts.length || parts.some(p => isNaN(p) || p < 0)) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0];
}

/**
 * iPhone-style drum-roll time picker.
 * currentSec — начальное значение (секунды).
 * maxSec     — если > 3600, показываем колонку часов.
 * onConfirm(seconds) — вызывается по OK.
 */
function _showTimePicker(currentSec, maxSec, onConfirm, onCancel) {
  const ITEM_H  = 44;
  const VISIBLE = 5;
  const CENTER  = Math.floor(VISIBLE / 2); // = 2

  const sec0  = Math.round(Math.max(0, currentSec || 0));
  const initH = Math.floor(sec0 / 3600);
  const initM = Math.floor((sec0 % 3600) / 60);
  const initS = sec0 % 60;
  const showHours = (maxSec || 0) > 3600;

  /* ── overlay & dialog ── */
  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';

  const dialog = document.createElement('div');
  dialog.className = 'tp-dialog';

  const titleEl = document.createElement('div');
  titleEl.className   = 'tp-title';
  titleEl.textContent = 'Установить время';
  dialog.appendChild(titleEl);

  const colsWrap = document.createElement('div');
  colsWrap.className = 'tp-cols-wrap';

  const band = document.createElement('div');
  band.className = 'tp-band-line';
  colsWrap.appendChild(band);

  /* ── shared drag state ── */
  let activeCtrl = null;
  function onDocMove(e) { if (activeCtrl) activeCtrl._drag(e.clientY); }
  function onDocUp()    { if (activeCtrl) { activeCtrl._end(); activeCtrl = null; } }
  document.addEventListener('mousemove', onDocMove);
  document.addEventListener('mouseup',   onDocUp);

  /* ── column factory ── */
  function makeCol(count, initial, labelText) {
    const wrap = document.createElement('div');
    wrap.className = 'tp-col-wrap';

    /* drum */
    const colEl = document.createElement('div');
    colEl.className = 'tp-col';

    const listEl = document.createElement('div');
    listEl.className = 'tp-list';

    const fadeT = document.createElement('div'); fadeT.className = 'tp-fade-top';
    const fadeB = document.createElement('div'); fadeB.className = 'tp-fade-bot';

    colEl.appendChild(listEl);
    colEl.appendChild(fadeT);
    colEl.appendChild(fadeB);
    wrap.appendChild(colEl);

    for (let i = 0; i < count; i++) {
      const it = document.createElement('div');
      it.className   = 'tp-item';
      it.textContent = String(i).padStart(2, '0');
      it.dataset.idx = i;
      listEl.appendChild(it);
    }
    const items = Array.from(listEl.querySelectorAll('.tp-item'));

    /* input field */
    const inputEl = document.createElement('input');
    inputEl.type      = 'number';
    inputEl.min       = 0;
    inputEl.max       = count - 1;
    inputEl.className = 'tp-input';
    wrap.appendChild(inputEl);

    /* label */
    const lbl = document.createElement('div');
    lbl.className   = 'tp-label';
    lbl.textContent = labelText;
    wrap.appendChild(lbl);

    let maxIdx = count - 1;
    let curIdx = Math.max(0, Math.min(maxIdx, Math.round(initial)));
    let ty     = (CENTER - curIdx) * ITEM_H;

    function refreshActive(approxIdx) {
      const snap = Math.max(0, Math.min(maxIdx, Math.round(approxIdx)));
      items.forEach((el, i) => el.classList.toggle('tp-item-active', i === snap));
    }

    function applyTY(y, animate) {
      listEl.style.transition = animate ? 'transform 0.2s ease' : 'none';
      listEl.style.transform  = `translateY(${y}px)`;
      refreshActive(CENTER - y / ITEM_H);
    }

    /* silent=true — не вызывать onChange (используется из setMax) */
    function snapTo(idx, animate, silent) {
      curIdx = Math.max(0, Math.min(maxIdx, Math.round(idx)));
      ty     = (CENTER - curIdx) * ITEM_H;
      applyTY(ty, animate !== false);
      if (document.activeElement !== inputEl) inputEl.value = curIdx;
      if (!silent && ctrl.onChange) ctrl.onChange();
    }

    applyTY(ty, false);
    inputEl.value = curIdx;

    /* input → drum */
    inputEl.addEventListener('change', () => {
      const v = parseInt(inputEl.value);
      if (!isNaN(v)) snapTo(v, true);
    });

    /* wheel on input → drum */
    inputEl.addEventListener('wheel', e => {
      e.preventDefault();
      snapTo(curIdx + (e.deltaY > 0 ? 1 : -1), true);
    }, { passive: false });

    let dragY0 = null, dragTY0 = null;

    const ctrl = {
      getValue: () => curIdx,
      onChange: null,
      setMax(limit) {
        maxIdx = Math.max(0, Math.min(count - 1, limit));
        inputEl.max = maxIdx;
        items.forEach((el, i) => el.classList.toggle('tp-item-disabled', i > maxIdx));
        if (curIdx > maxIdx) snapTo(maxIdx, true, true);
      },
      _start(clientY) {
        dragY0  = clientY;
        dragTY0 = ty;
        listEl.style.transition = 'none';
      },
      _drag(clientY) {
        if (dragY0 === null) return;
        ty = dragTY0 + (clientY - dragY0);
        listEl.style.transform = `translateY(${ty}px)`;
        refreshActive(CENTER - ty / ITEM_H);
      },
      _end() {
        if (dragY0 === null) return;
        dragY0 = null;
        snapTo(CENTER - ty / ITEM_H, true);
      },
    };

    /* mouse */
    listEl.addEventListener('mousedown', e => {
      e.preventDefault();
      activeCtrl = ctrl;
      ctrl._start(e.clientY);
    });

    /* touch */
    listEl.addEventListener('touchstart', e => { ctrl._start(e.touches[0].clientY); }, { passive: true });
    listEl.addEventListener('touchmove',  e => { e.preventDefault(); ctrl._drag(e.touches[0].clientY); }, { passive: false });
    listEl.addEventListener('touchend',   () => ctrl._end());

    /* wheel on drum */
    listEl.addEventListener('wheel', e => {
      e.preventDefault();
      snapTo(curIdx + (e.deltaY > 0 ? 1 : -1), true);
    }, { passive: false });

    /* click on item */
    listEl.addEventListener('click', e => {
      const it = e.target.closest('.tp-item');
      if (it) snapTo(parseInt(it.dataset.idx), true);
    });

    return { wrap, ctrl };
  }

  /* ── build columns ── */
  let hCtrl = null, mCtrl, sCtrl;

  if (showHours) {
    const maxH = Math.max(1, Math.floor((maxSec || 0) / 3600));
    const { wrap, ctrl } = makeCol(maxH + 1, initH, 'ч');
    colsWrap.appendChild(wrap);
    hCtrl = ctrl;
    const sep = document.createElement('div');
    sep.className = 'tp-sep'; sep.textContent = ':';
    colsWrap.appendChild(sep);
  }

  { const { wrap, ctrl } = makeCol(60, initM, 'мин'); colsWrap.appendChild(wrap); mCtrl = ctrl; }

  {
    const sep = document.createElement('div');
    sep.className = 'tp-sep'; sep.textContent = ':';
    colsWrap.appendChild(sep);
  }

  { const { wrap, ctrl } = makeCol(60, initS, 'сек'); colsWrap.appendChild(wrap); sCtrl = ctrl; }

  /* ── каскадные ограничения по maxSec ── */
  if (maxSec) {
    const limH = Math.floor(maxSec / 3600);
    const limM = Math.floor((maxSec % 3600) / 60);
    const limS = maxSec % 60;

    function applyMinLimit() {
      const h = hCtrl ? hCtrl.getValue() : 0;
      mCtrl.setMax(h >= limH ? limM : 59);
    }
    function applySecLimit() {
      const h = hCtrl ? hCtrl.getValue() : 0;
      const m = mCtrl.getValue();
      sCtrl.setMax((h >= limH && m >= limM) ? limS : 59);
    }

    if (hCtrl) hCtrl.onChange = () => { applyMinLimit(); applySecLimit(); };
    mCtrl.onChange = applySecLimit;

    applyMinLimit();
    applySecLimit();
  }

  dialog.appendChild(colsWrap);

  /* ── buttons ── */
  const btns = document.createElement('div');
  btns.className = 'tp-btns';

  const cancelBtn = document.createElement('button');
  cancelBtn.className   = 'secondary';
  cancelBtn.textContent = 'Отмена';

  const okBtn = document.createElement('button');
  okBtn.className   = 'tp-ok';
  okBtn.textContent = 'OK';

  btns.appendChild(okBtn);
  btns.appendChild(cancelBtn);
  dialog.appendChild(btns);

  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';

  /* блокируем скролл страницы при касании вне барабанов */
  overlay.addEventListener('touchmove', e => { e.preventDefault(); }, { passive: false });
  overlay.addEventListener('wheel',     e => { e.stopPropagation(); }, { passive: true });

  /* ── close / confirm ── */
  function close() {
    document.removeEventListener('mousemove', onDocMove);
    document.removeEventListener('mouseup',   onDocUp);
    document.body.style.overflow = '';
    overlay.remove();
  }

  function cancel() { close(); if (onCancel) onCancel(); }
  cancelBtn.addEventListener('click', cancel);
  overlay.addEventListener('click', e => { if (e.target === overlay) cancel(); });

  okBtn.addEventListener('click', () => {
    const h = hCtrl ? hCtrl.getValue() : 0;
    const m = mCtrl.getValue();
    const s = sCtrl.getValue();
    close();
    onConfirm(h * 3600 + m * 60 + s);
  });
}

/**
 * Упрощённый модал выбора процента (0–100) для эпизодов без длительности.
 * onConfirm(pct) вызывается при подтверждении.
 */
function _showPctPicker(currentPct, onConfirm, onCancel) {
  const ITEM_H  = 44;
  const VISIBLE = 5;
  const CENTER  = Math.floor(VISIBLE / 2);
  const MAX     = 100;

  const overlay = document.createElement('div');
  overlay.className = 'tp-overlay';

  const dialog = document.createElement('div');
  dialog.className = 'tp-dialog';

  const titleEl = document.createElement('div');
  titleEl.className   = 'tp-title';
  titleEl.textContent = 'Установить прогресс';
  dialog.appendChild(titleEl);

  const colsWrap = document.createElement('div');
  colsWrap.className = 'tp-cols-wrap';
  const band = document.createElement('div');
  band.className = 'tp-band-line';
  colsWrap.appendChild(band);

  /* drum */
  const wrap   = document.createElement('div'); wrap.className = 'tp-col-wrap';
  const colEl  = document.createElement('div'); colEl.className = 'tp-col';
  const listEl = document.createElement('div'); listEl.className = 'tp-list';
  const fadeT  = document.createElement('div'); fadeT.className = 'tp-fade-top';
  const fadeB  = document.createElement('div'); fadeB.className = 'tp-fade-bot';
  colEl.appendChild(listEl); colEl.appendChild(fadeT); colEl.appendChild(fadeB);
  wrap.appendChild(colEl);

  for (let i = 0; i <= MAX; i++) {
    const it = document.createElement('div');
    it.className   = 'tp-item';
    it.textContent = String(i);
    it.dataset.idx = i;
    listEl.appendChild(it);
  }
  const items = Array.from(listEl.querySelectorAll('.tp-item'));

  const inputEl = document.createElement('input');
  inputEl.type = 'number'; inputEl.min = 0; inputEl.max = MAX;
  inputEl.className = 'tp-input';
  wrap.appendChild(inputEl);

  const lbl = document.createElement('div');
  lbl.className = 'tp-label'; lbl.textContent = '%';
  wrap.appendChild(lbl);

  colsWrap.appendChild(wrap);

  let curIdx = Math.max(0, Math.min(MAX, Math.round(currentPct || 0)));
  let ty = (CENTER - curIdx) * ITEM_H;

  function refreshActive(approxIdx) {
    const snap = Math.max(0, Math.min(MAX, Math.round(approxIdx)));
    items.forEach((el, i) => el.classList.toggle('tp-item-active', i === snap));
  }
  function applyTY(y, animate) {
    listEl.style.transition = animate ? 'transform 0.2s ease' : 'none';
    listEl.style.transform  = `translateY(${y}px)`;
    refreshActive(CENTER - y / ITEM_H);
  }
  function snapTo(idx, animate) {
    curIdx = Math.max(0, Math.min(MAX, Math.round(idx)));
    ty = (CENTER - curIdx) * ITEM_H;
    applyTY(ty, animate !== false);
    if (document.activeElement !== inputEl) inputEl.value = curIdx;
  }
  applyTY(ty, false);
  inputEl.value = curIdx;

  inputEl.addEventListener('change', () => { const v = parseInt(inputEl.value); if (!isNaN(v)) snapTo(v, true); });
  inputEl.addEventListener('wheel', e => { e.preventDefault(); snapTo(curIdx + (e.deltaY > 0 ? 1 : -1), true); }, { passive: false });
  listEl.addEventListener('wheel', e => { e.preventDefault(); snapTo(curIdx + (e.deltaY > 0 ? 1 : -1), true); }, { passive: false });
  listEl.addEventListener('click', e => { const it = e.target.closest('.tp-item'); if (it) snapTo(parseInt(it.dataset.idx), true); });

  let dragY0 = null, dragTY0 = null;
  function onDocMove(e) { if (dragY0 === null) return; ty = dragTY0 + (e.clientY - dragY0); listEl.style.transform = `translateY(${ty}px)`; refreshActive(CENTER - ty / ITEM_H); }
  function onDocUp()    { if (dragY0 === null) return; dragY0 = null; snapTo(CENTER - ty / ITEM_H, true); }
  document.addEventListener('mousemove', onDocMove);
  document.addEventListener('mouseup',   onDocUp);
  listEl.addEventListener('mousedown', e => { e.preventDefault(); dragY0 = e.clientY; dragTY0 = ty; listEl.style.transition = 'none'; });
  listEl.addEventListener('touchstart', e => { dragY0 = e.touches[0].clientY; dragTY0 = ty; listEl.style.transition = 'none'; }, { passive: true });
  listEl.addEventListener('touchmove',  e => { e.preventDefault(); ty = dragTY0 + (e.touches[0].clientY - dragY0); listEl.style.transform = `translateY(${ty}px)`; refreshActive(CENTER - ty / ITEM_H); }, { passive: false });
  listEl.addEventListener('touchend',   () => { snapTo(CENTER - ty / ITEM_H, true); dragY0 = null; });

  dialog.appendChild(colsWrap);

  const btns = document.createElement('div'); btns.className = 'tp-btns';
  const cancelBtn = document.createElement('button'); cancelBtn.className = 'secondary'; cancelBtn.textContent = 'Отмена';
  const okBtn     = document.createElement('button'); okBtn.className = 'tp-ok'; okBtn.textContent = 'OK';
  btns.appendChild(okBtn); btns.appendChild(cancelBtn);
  dialog.appendChild(btns);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  document.body.style.overflow = 'hidden';

  overlay.addEventListener('touchmove', e => e.preventDefault(), { passive: false });

  function close() {
    document.removeEventListener('mousemove', onDocMove);
    document.removeEventListener('mouseup',   onDocUp);
    document.body.style.overflow = '';
    overlay.remove();
  }
  function cancel() { close(); if (onCancel) onCancel(); }
  cancelBtn.addEventListener('click', cancel);
  overlay.addEventListener('click', e => { if (e.target === overlay) cancel(); });
  okBtn.addEventListener('click', () => { close(); onConfirm(curIdx); });
}

/**
 * Делает элемент кликабельным — по клику открывается drum-picker.
 * durationSec используется чтобы показать колонку часов (если > 3600).
 * onTime(seconds) вызывается при подтверждении.
 */
function _attachTimeEditor(el, durationSec, onTime) {
  el.classList.add('clickable-time');
  el.title = 'Нажмите чтобы изменить время';

  el.addEventListener('click', e => {
    e.stopPropagation();
    const currentSec = _parseTime(el.textContent);
    _showTimePicker(currentSec, durationSec, onTime);
  });
}


// ─── Render card metadata ─────────────────────────────────────────────────────

function _renderCard(card, cardId) {
  document.title = card.title || cardId;
  document.getElementById('cardLoading').style.display = 'none';
  document.getElementById('cardContent').style.display = 'block';

  if (card.backdrop_path) {
    const img = document.getElementById('cardBackdrop');
    img.src = _BACKDROP_BASE + card.backdrop_path;
    img.style.display = 'block';
  } else {
    document.getElementById('cardNoBackdrop').style.display = 'block';
  }

  document.getElementById('cardTitle').textContent = card.title || cardId;

  if (card.original_title && card.original_title !== card.title) {
    const el = document.getElementById('cardOrigTitle');
    el.textContent = card.original_title;
    el.style.display = 'block';
  }

  const tags = [];
  if (card.year)         tags.push({ text: card.year, accent: true });
  if (card.vote_average) tags.push({ text: `★ ${Number(card.vote_average).toFixed(1)}` });
  if (card.media_type === 'movie' && card.runtime) {
    tags.push({ text: _fmtRuntime(card.runtime) });
  }
  if (card.media_type === 'tv' && card.number_of_seasons) {
    tags.push({ text: `${card.number_of_seasons} сез.` });
  }
  if (card.media_type === 'tv' && card.episode_run_time) {
    tags.push({ text: `~${card.episode_run_time} мин / серия` });
  }
  tags.push({ text: card.media_type === 'movie' ? 'Фильм' : 'Сериал' });
  document.getElementById('cardTags').innerHTML = tags
    .map(t => `<span class="card-detail-tag${t.accent ? ' accent' : ''}">${t.text}</span>`)
    .join('');

  if (card.overview) {
    const el = document.getElementById('cardOverview');
    el.textContent = card.overview;
    el.style.display = 'block';
  }
}


// ─── Interactive progress bar ─────────────────────────────────────────────────

/**
 * Интерактивный прогресс-бар.
 * Drag и tap — оба показывают позицию визуально, при отпускании вызывают
 * onPick(pct) с процентом позиции где отпустили. Сохранение — на стороне вызывающего.
 */
function _makeInteractiveBar(barEl, fillEl, onPick) {
  let active   = false;
  let lastPct  = null;
  let prevWidth = null; // захватывается до начала движения

  function pctFromEvent(e) {
    const rect    = barEl.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    return Math.round(Math.min(100, Math.max(0, (clientX - rect.left) / rect.width * 100)));
  }

  function start(e) {
    prevWidth = fillEl.style.width; // сохраняем ДО изменения
    active    = true;
    lastPct   = pctFromEvent(e);
    fillEl.style.width = lastPct + '%';
  }

  barEl.style.cursor = 'pointer';

  /* mouse */
  barEl.addEventListener('mousedown', e => start(e));
  document.addEventListener('mousemove', e => {
    if (!active) return;
    lastPct = pctFromEvent(e);
    fillEl.style.width = lastPct + '%';
  });
  document.addEventListener('mouseup', () => {
    if (!active) return;
    active = false;
    if (lastPct !== null) onPick(lastPct, prevWidth);
  });

  /* touch */
  barEl.addEventListener('touchstart', e => start(e), { passive: true });
  barEl.addEventListener('touchmove', e => {
    e.preventDefault();
    lastPct = pctFromEvent(e);
    fillEl.style.width = lastPct + '%';
  }, { passive: false });
  barEl.addEventListener('touchend', () => {
    if (!active) return;
    active = false;
    if (lastPct !== null) onPick(lastPct, prevWidth);
  });
}


// ─── Movie progress ───────────────────────────────────────────────────────────

async function _loadMovieProgress(card, cardId, deviceId, profileId) {
  try {
    const qp  = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const res = await fetch(`/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`);
    if (!res.ok) return;
    const rows = await res.json();

    const tc   = rows.reduce((best, r) => (!best || r.percent > best.percent) ? r : best, null);
    const pct  = tc ? Math.min(100, Math.max(0, tc.percent || 0)) : 0;
    const item = tc ? tc.item : (card.movie_item || null);

    if (!tc && !card.movie_item) return;

    // Длительность: из таймкода, иначе из MediaCard.runtime
    const durationSec = (tc && tc.duration_sec) || (card.runtime ? card.runtime * 60 : null);

    const section   = document.getElementById('cardProgressSection');
    const labelEl   = document.getElementById('cardProgressLabel');
    const fillEl    = document.getElementById('cardProgressFill');
    const deleteBtn = document.getElementById('cardDeleteBtn');

    section.style.display = 'block';
    fillEl.style.width = pct + '%';

    // Строим label из отдельных span-ов чтобы время было кликабельным
    labelEl.innerHTML = '';
    const statusSpan = document.createElement('span');
    labelEl.appendChild(statusSpan);

    let posSpan = null;
    if (durationSec) {
      const sep1 = document.createElement('span'); sep1.textContent = ' · ';
      posSpan    = document.createElement('span');
      const sep2 = document.createElement('span'); sep2.textContent = ' / ';
      const durSpan = document.createElement('span'); durSpan.textContent = _fmtTime(durationSec);
      labelEl.appendChild(sep1);
      labelEl.appendChild(posSpan);
      labelEl.appendChild(sep2);
      labelEl.appendChild(durSpan);
    }

    function updateLabel(p) {
      statusSpan.textContent = p >= _WATCHED_THR ? '✓ Просмотрено' : `Просмотрено ${Math.round(p)}%`;
      if (posSpan) posSpan.textContent = _fmtTime(durationSec * p / 100);
    }
    updateLabel(pct);

    // ── Кнопка "Смотрю" ──
    const watchSection = document.getElementById('cardWatchSection');
    const watchBtn     = document.getElementById('cardWatchBtn');
    let inHistory = !!tc;

    function syncWatchBtn(val) {
      inHistory = val;
      watchBtn.classList.toggle('active', val);
      watchBtn.textContent = val ? '✓ Смотрю' : 'Смотрю';
    }

    if (item && watchSection) {
      watchSection.style.display = 'block';
      syncWatchBtn(inHistory);
      watchBtn.addEventListener('click', async () => {
        if (inHistory && !confirm('Убрать из истории просмотра?')) return;
        watchBtn.disabled = true;
        if (inHistory) {
          const r = await fetch(
            `/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`,
            { method: 'DELETE' }
          );
          if (r.ok) {
            syncWatchBtn(false);
            fillEl.style.width = '0%';
            updateLabel(0);
            syncDeleteBtn(0);
          }
        } else {
          await fetch('/api/set-timecode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: deviceId, card_id: cardId, item, percent: 0, profile_id: profileId ?? '' }),
          });
          syncWatchBtn(true);
        }
        watchBtn.disabled = false;
      });
    }

    function syncDeleteBtn(p) {
      deleteBtn.style.display = p > 0 ? 'inline' : 'none';
    }
    syncDeleteBtn(pct);

    async function setTimecode(newPct) {
      await fetch('/api/set-timecode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, card_id: cardId, item, percent: newPct, profile_id: profileId ?? '' }),
      });
      syncDeleteBtn(newPct);
      if (!inHistory) syncWatchBtn(true); // первое сохранение = добавление в историю
    }

    deleteBtn.addEventListener('click', async () => {
      if (!confirm('Удалить историю просмотра?')) return;
      deleteBtn.disabled = true;
      const r = await fetch(
        `/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`,
        { method: 'DELETE' }
      );
      if (r.ok) {
        fillEl.style.width = '0%';
        updateLabel(0);
        syncDeleteBtn(0);
        syncWatchBtn(false);
      }
      deleteBtn.disabled = false;
    });

    if (item) {
      const barEl = document.querySelector('#cardProgressSection .modal-progress-bar');
      _makeInteractiveBar(barEl, fillEl, (pickedPct, prevWidth) => {
        const pickedSec = durationSec ? Math.round(durationSec * pickedPct / 100) : 0;
        _showTimePicker(pickedSec, durationSec, async (newSec) => {
          const newPct = durationSec ? Math.min(100, newSec / durationSec * 100) : pickedPct;
          fillEl.style.width = newPct + '%';
          updateLabel(newPct);
          await setTimecode(newPct);
        }, () => { fillEl.style.width = prevWidth; });
      });

      if (posSpan && durationSec) {
        _attachTimeEditor(posSpan, durationSec, async (newSec) => {
          const newPct = Math.min(100, newSec / durationSec * 100);
          fillEl.style.width = newPct + '%';
          updateLabel(newPct);
          await setTimecode(newPct);
        });
      }
    }
  } catch { /* ignore */ }
}


// ─── TV: общий прогресс из эпизодов ──────────────────────────────────────────

function _setupTvProgressSection(episodes, cardId, deviceId, profileId, tcRows) {
  if (!episodes.length) return;

  const section      = document.getElementById('cardProgressSection');
  const labelEl      = document.getElementById('cardProgressLabel');
  const fillEl       = document.getElementById('cardProgressFill');
  const deleteBtn    = document.getElementById('cardDeleteBtn');
  const watchSection = document.getElementById('cardWatchSection');
  const watchBtn     = document.getElementById('cardWatchBtn');

  section.style.display = 'block';

  const qp = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';

  // ── Кнопка "Смотрю" ──
  let inHistory = (tcRows && tcRows.length > 0);

  function syncDeleteBtn() {
    const hasAny = inHistory || Array.from(document.querySelectorAll('.ep-row'))
      .some(r => parseFloat(r.dataset.percent) > 0);
    deleteBtn.style.display = hasAny ? 'inline' : 'none';
    if (hasAny) deleteBtn.disabled = false;
  }

  // Экспортируем функцию обновления чтобы вызывать из _renderEpisodes
  window._updateTvProgress = function () {
    const allRows    = document.querySelectorAll('.ep-row');
    const regularRows = Array.from(allRows).filter(r => !r.dataset.special && !r.dataset.future);
    const total = regularRows.length;
    if (!total) return;
    let watched = 0;
    regularRows.forEach(r => { if (parseFloat(r.dataset.percent) >= _WATCHED_THR) watched++; });
    const pct = Math.round(watched / total * 100);
    fillEl.style.width = pct + '%';
    labelEl.textContent = `Просмотрено ${watched} / ${total} серий`;
    syncDeleteBtn();
  };

  window._updateTvProgress();
  syncDeleteBtn();

  function syncWatchBtn(val) {
    inHistory = val;
    watchBtn.classList.toggle('active', val);
    watchBtn.textContent = val ? '✓ Смотрю' : 'Смотрю';
  }
  // Доступны из _makeEpRow
  window._syncTvWatchBtn  = (val) => syncWatchBtn(val);
  window._syncTvDeleteBtn = () => syncDeleteBtn();

  if (watchSection) {
    watchSection.style.display = 'block';
    syncWatchBtn(inHistory);

    watchBtn.addEventListener('click', async () => {
      if (inHistory && !confirm('Убрать из истории просмотра?')) return;
      watchBtn.disabled = true;
      if (inHistory) {
        const r = await fetch(
          `/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`,
          { method: 'DELETE' }
        );
        if (r.ok) {
          syncWatchBtn(false);
          _resetAllEpRows();
        }
      } else {
        // Добавляем: POST первый эпизод с percent=0
        const sorted = [...episodes].sort((a, b) =>
          a.season !== b.season ? a.season - b.season : a.episode - b.episode
        );
        const first = sorted[0];
        if (first) {
          await fetch('/api/set-timecode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              device_id: deviceId,
              card_id: cardId,
              item: first.hash,
              percent: 0,
              profile_id: profileId ?? '',
            }),
          });
        }
        syncWatchBtn(true);
      }
      watchBtn.disabled = false;
    });
  }

  function _resetAllEpRows() {
    const updaters = window._epRowUpdaters || {};
    for (const [, fn] of Object.entries(updaters)) fn(0);
    if (typeof window._updateTvProgress === 'function') window._updateTvProgress();
  }

  deleteBtn.addEventListener('click', async () => {
    if (!confirm('Удалить всю историю просмотра сериала?')) return;
    deleteBtn.disabled = true;
    const r = await fetch(
      `/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`,
      { method: 'DELETE' }
    );
    if (r.ok) {
      syncWatchBtn(false);
      _resetAllEpRows();
    }
    deleteBtn.disabled = false;
  });
}


// ─── Episodes fetch ───────────────────────────────────────────────────────────

async function _fetchEpisodes(cardId, deviceId, profileId) {
  try {
    const qp  = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const res = await fetch(`/api/episodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`);
    if (!res.ok) return null;
    return await res.json();
  } catch { return null; }
}


// ─── Episodes accordion ───────────────────────────────────────────────────────

function _epLabel(ep) {
  const code = `S${String(ep.season).padStart(2,'0')}E${String(ep.episode).padStart(2,'0')}`;
  return (ep.special && ep.title) ? `${code} — ${ep.title}` : code;
}

function _updateSeasonCount(snum) {
  const countEl = document.querySelector(`.ep-season-count[data-snum="${snum}"]`);
  if (!countEl) return;
  const regularRows = Array.from(document.querySelectorAll(`.ep-row[data-snum="${snum}"]`)).filter(r => !r.dataset.special && !r.dataset.future);
  let watched = 0;
  regularRows.forEach(r => { if (parseFloat(r.dataset.percent) >= _WATCHED_THR) watched++; });
  countEl.textContent = `${watched} / ${regularRows.length}`;
}

function _renderEpisodes(epData, card, cardId, deviceId, profileId) {
  const container = document.getElementById('cardEpisodesSection');
  const allEps    = epData.episodes || [];
  if (!allEps.length) return;

  const pp = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';

  // Отсортированный список для поиска prev/next
  const sortedEps = [...allEps].sort((a, b) =>
    a.season !== b.season ? a.season - b.season : a.episode - b.episode);

  // Реестр функций обновления строки: ep.hash → updateRow(pct)
  const rowUpdaters = {};
  window._epRowUpdaters = rowUpdaters; // доступен из _setupTvProgressSection

  // Текущий процент серии по DOM
  function getRowPct(e) {
    const r = document.querySelector(`.ep-row[data-ep-hash="${e.hash}"]`);
    return r ? parseFloat(r.dataset.percent || 0) : (e.percent || 0);
  }

  async function batchSave(eps, pct) {
    for (const e of eps) {
      await fetch('/api/set-timecode', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_id: deviceId, card_id: cardId, item: e.hash, percent: pct, profile_id: profileId ?? '' }),
      });
      rowUpdaters[e.hash]?.(pct);
    }
  }

  async function batchReset(eps) {
    for (const e of eps) {
      await fetch(
        `/api/episode-timecode?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}&item=${encodeURIComponent(e.hash)}${pp}`,
        { method: 'DELETE' }
      );
      rowUpdaters[e.hash]?.(0);
    }
  }

  // Предложить отметить предыдущие если не просмотрены
  async function offerMarkPrev(ep) {
    const idx  = sortedEps.findIndex(e => e.hash === ep.hash);
    const prev = sortedEps.slice(0, idx).filter(e => getRowPct(e) < _WATCHED_THR);
    if (prev.length && confirm(`Отметить ${prev.length} предыд. серий как просмотренные?`))
      await batchSave(prev, 100);
  }

  // Предложить сбросить последующие если есть прогресс
  async function offerResetNext(ep) {
    const idx  = sortedEps.findIndex(e => e.hash === ep.hash);
    const next = sortedEps.slice(idx + 1).filter(e => getRowPct(e) > 0);
    if (next.length && confirm(`Сбросить прогресс ${next.length} следующих серий?`))
      await batchReset(next);
  }

  // Группируем по сезонам
  const seasons = {};
  for (const ep of allEps) {
    if (!seasons[ep.season]) seasons[ep.season] = [];
    seasons[ep.season].push(ep);
  }

  // Первый сезон с непросмотренными обычными сериями (спешлы не учитываем)
  let activeSeasonExpand = null;
  for (const snum of Object.keys(seasons).map(Number).sort((a, b) => a - b)) {
    // Раскрываем только если есть вышедшие (не future) непросмотренные обычные серии
    if (seasons[snum].some(e => !e.special && !e.future && e.percent < _WATCHED_THR)) { activeSeasonExpand = snum; break; }
  }

  container.innerHTML = '';
  container.style.marginTop = '1rem';
  container.style.borderTop = '1px solid var(--pico-muted-border-color)';

  for (const snum of Object.keys(seasons).map(Number).sort((a, b) => a - b)) {
    const eps        = seasons[snum];
    const regularEps = eps.filter(e => !e.special);
    const watched    = regularEps.filter(e => e.percent >= _WATCHED_THR).length;

    const details = document.createElement('details');
    if (snum === activeSeasonExpand) details.open = true;
    details.className = 'ep-season-block';

    const summary = document.createElement('summary');
    summary.className = 'ep-season-summary';

    const titleSpan = document.createElement('span');
    titleSpan.textContent = `Сезон ${snum}`;

    const countSpan = document.createElement('span');
    countSpan.className    = 'ep-season-count';
    countSpan.dataset.snum = snum;
    countSpan.textContent  = `${watched} / ${regularEps.filter(e => !e.future).length}`;

    const markAllBtn = document.createElement('button');
    markAllBtn.className   = 'ep-mark-season';
    markAllBtn.textContent = '✓ Все';
    markAllBtn.addEventListener('click', async e => {
      e.stopPropagation();
      markAllBtn.disabled = true;
      const unwatched = eps.filter(ep => getRowPct(ep) < _WATCHED_THR);
      if (unwatched.length) {
        await batchSave(unwatched, 100);
        if (typeof window._syncTvWatchBtn  === 'function') window._syncTvWatchBtn(true);
        if (typeof window._syncTvDeleteBtn === 'function') window._syncTvDeleteBtn();
      }
      markAllBtn.disabled = false;
    });

    summary.appendChild(titleSpan);
    summary.appendChild(countSpan);
    summary.appendChild(markAllBtn);
    details.appendChild(summary);

    const epList = document.createElement('div');
    epList.className = 'ep-list';
    const sortedSeasonEps = [...eps].sort((a, b) => {
      if (a.air_date && b.air_date) return a.air_date < b.air_date ? -1 : a.air_date > b.air_date ? 1 : 0;
      if (a.air_date) return -1;
      if (b.air_date) return 1;
      return a.episode - b.episode;
    });
    for (const ep of sortedSeasonEps) {
      epList.appendChild(_makeEpRow(
        ep, card, cardId, deviceId, profileId, pp,
        rowUpdaters, offerMarkPrev, offerResetNext
      ));
    }

    details.appendChild(epList);
    container.appendChild(details);
  }
}


function _makeEpRow(ep, card, cardId, deviceId, profileId, pp, rowUpdaters, offerMarkPrev, offerResetNext) {
  const durSec = (ep.duration_sec > 0 ? ep.duration_sec : null)
    || (card.episode_run_time > 0 ? card.episode_run_time * 60 : null);
  let curPct = Math.min(100, Math.max(0, ep.percent || 0));

  const row = document.createElement('div');
  row.className       = 'ep-row';
  row.dataset.snum    = ep.season;
  row.dataset.percent = curPct;
  row.dataset.epHash  = ep.hash;
  if (ep.special) row.dataset.special = '1';
  if (ep.future)  row.dataset.future  = '1';

  // Метка серии (для спешлов — отдельный заголовок на всю ширину)
  let labelEl = null;
  let specialTitleEl = null;
  if (ep.special) {
    specialTitleEl = document.createElement('span');
    specialTitleEl.className   = 'ep-row-special-title';
    specialTitleEl.textContent = ep.title || _epLabel(ep);
    specialTitleEl.title       = ep.title || _epLabel(ep);
  } else {
    labelEl = document.createElement('span');
    labelEl.className   = 'ep-row-label';
    labelEl.textContent = _epLabel(ep);
  }

  // Прогресс-бар
  const barWrap = document.createElement('div');
  barWrap.className = 'ep-row-bar';
  const barFill = document.createElement('div');
  barFill.className   = `ep-row-fill${curPct >= _WATCHED_THR ? ' watched' : ''}`;
  barFill.style.width = curPct + '%';
  barWrap.appendChild(barFill);

  // Метка времени / процента
  const pctEl = document.createElement('span');
  pctEl.className = 'ep-row-pct';

  // posSpan создаётся только если у эпизода есть собственное время;
  // для эпизодов без времени — фолбэк на % с аналогичной структурой
  const epDurSec = ep.duration_sec > 0 ? ep.duration_sec : null;
  let posSpan = null;
  let curPctSpan = null;
  if (epDurSec) {
    posSpan = document.createElement('span');
    const sep     = document.createElement('span'); sep.textContent = ' / ';
    const durSpan = document.createElement('span'); durSpan.textContent = _fmtTime(epDurSec);
    pctEl.appendChild(posSpan);
    pctEl.appendChild(sep);
    pctEl.appendChild(durSpan);
  } else {
    curPctSpan = document.createElement('span');
    const sep     = document.createElement('span'); sep.textContent = ' / ';
    const maxSpan = document.createElement('span'); maxSpan.textContent = '100%';
    pctEl.appendChild(curPctSpan);
    pctEl.appendChild(sep);
    pctEl.appendChild(maxSpan);
  }

  function updateRow(p) {
    curPct = p;
    row.dataset.percent = p;
    barFill.style.width = p + '%';
    barFill.className   = `ep-row-fill${p >= _WATCHED_THR ? ' watched' : ''}`;
    if (posSpan) {
      posSpan.textContent = p >= _WATCHED_THR ? `✓ ${_fmtTime(epDurSec * p / 100)}`
        : (p > 0 ? _fmtTime(epDurSec * p / 100) : '—');
    } else {
      curPctSpan.textContent = p >= _WATCHED_THR ? `✓ ${Math.round(p)}%`
        : (p > 0 ? `${Math.round(p)}%` : '—');
    }
    _updateSeasonCount(ep.season);
    if (typeof window._updateTvProgress === 'function') window._updateTvProgress();
  }
  updateRow(curPct);

  // Регистрируем функцию обновления для batch-операций
  rowUpdaters[ep.hash] = updateRow;

  async function saveEpTimecode(newPct) {
    await fetch('/api/set-timecode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: deviceId, card_id: cardId, item: ep.hash, percent: newPct, profile_id: profileId ?? '' }),
    });
    if (typeof window._syncTvWatchBtn  === 'function') window._syncTvWatchBtn(true);
    if (typeof window._syncTvDeleteBtn === 'function') window._syncTvDeleteBtn();
    if (newPct >= _WATCHED_THR) await offerMarkPrev(ep);
  }

  // Интерактивный бар
  _makeInteractiveBar(barWrap, barFill, (pickedPct, prevWidth) => {
    if (epDurSec) {
      const pickedSec = Math.round(epDurSec * pickedPct / 100);
      _showTimePicker(pickedSec, epDurSec, async (newSec) => {
        const newPct = Math.min(100, newSec / epDurSec * 100);
        updateRow(newPct);
        await saveEpTimecode(newPct);
      }, () => { barFill.style.width = prevWidth; });
    } else {
      _showPctPicker(pickedPct, async (newPct) => {
        updateRow(newPct);
        await saveEpTimecode(newPct);
      }, () => { barFill.style.width = prevWidth; });
    }
  });

  // Кликабельное значение позиции
  if (posSpan && epDurSec) {
    _attachTimeEditor(posSpan, epDurSec, async (newSec) => {
      const newPct = Math.min(100, newSec / epDurSec * 100);
      updateRow(newPct);
      await saveEpTimecode(newPct);
    });
  } else if (curPctSpan) {
    curPctSpan.classList.add('clickable-time');
    curPctSpan.title = 'Нажмите чтобы изменить прогресс';
    curPctSpan.addEventListener('click', e => {
      e.stopPropagation();
      _showPctPicker(curPct, async (newPct) => {
        updateRow(newPct);
        await saveEpTimecode(newPct);
      });
    });
  }

  // Кнопка удалить прогресс
  const delBtn = document.createElement('button');
  delBtn.className   = 'ep-row-del';
  delBtn.title       = 'Сбросить прогресс';
  delBtn.textContent = '🗑';
  delBtn.addEventListener('click', async () => {
    delBtn.disabled = true;
    const r = await fetch(
      `/api/episode-timecode?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}&item=${encodeURIComponent(ep.hash)}${pp}`,
      { method: 'DELETE' }
    );
    if (r.ok) {
      updateRow(0);
      await offerResetNext(ep);
    }
    delBtn.disabled = false;
  });

  // Кнопка Спецэпизод (★ — отмечен, ☆ — не отмечен)
  const specBtn = document.createElement('button');
  specBtn.className   = `ep-row-spec${ep.special ? ' unmark' : ''}`;
  specBtn.textContent = ep.special ? '★' : '☆';
  specBtn.title       = ep.special ? 'Снять отметку спецэпизода' : 'Отметить как спецэпизод';

  specBtn.addEventListener('click', async () => {
    specBtn.disabled = true;
    specBtn.textContent = '…';
    const url    = ep.special ? '/api/unmark-special' : '/api/mark-watched';
    const pidStr = profileId != null ? `, "profile_id": "${profileId}"` : '';
    const r = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: `{"device_id":${deviceId},"card_id":${JSON.stringify(cardId)},"item":${JSON.stringify(ep.hash)}${pidStr}}`,
    });
    if (r.ok) {
      ep.special = !ep.special;
      updateRow(ep.special ? 100 : 0);
      specBtn.className   = `ep-row-spec${ep.special ? ' unmark' : ''}`;
      specBtn.textContent = ep.special ? '★' : '☆';
      specBtn.title       = ep.special ? 'Снять отметку спецэпизода' : 'Отметить как спецэпизод';
      if (ep.special) await offerMarkPrev(ep);
      else            await offerResetNext(ep);
    } else {
      specBtn.textContent = ep.special ? '★' : '☆';
    }
    specBtn.disabled = false;
  });

  if (specialTitleEl) row.appendChild(specialTitleEl);
  if (labelEl)        row.appendChild(labelEl);
  if (specialTitleEl) row.appendChild(document.createElement('span')); // spacer под 5.5rem колонку
  row.appendChild(barWrap);
  row.appendChild(pctEl);
  row.appendChild(delBtn);
  row.appendChild(specBtn);
  return row;
}
