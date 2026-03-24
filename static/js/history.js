/**
 * history.js — загрузка и отображение истории просмотров.
 * Используется на index.html и profiles.html.
 *
 * Ожидает на странице:
 *   #historyDeviceSelect   — <select> со списком устройств (value = device_id)
 *   #historyProfileTabs    — контейнер для табов profile_id
 *   #historyFilterAll      — кнопка «Все»
 *   #historyFilterMovie    — кнопка «Фильмы»
 *   #historyFilterTv       — кнопка «Сериалы»
 *   #historyGrid           — контейнер для карточек
 */

const _IMG_BASE      = (window.TMDB_IMAGE_BASE || 'https://image.tmdb.org');
const POSTER_BASE    = _IMG_BASE + '/t/p/w300';
const BACKDROP_BASE  = _IMG_BASE + '/t/p/w780';
const WATCHED_THRESHOLD = 90;
const _PREFS_KEY = 'history_prefs';

let _allCards      = [];
let _cardMap       = {};
let _activeFilter  = 'all';
let _activeSort    = 'watched';
let _searchQuery   = '';
let _currentDevice = null;
let _currentProfile = '';

function _loadPrefs() {
  try { return JSON.parse(localStorage.getItem(_PREFS_KEY) || '{}'); } catch { return {}; }
}

function _savePrefs(patch) {
  const prefs = Object.assign(_loadPrefs(), patch);
  try { localStorage.setItem(_PREFS_KEY, JSON.stringify(prefs)); } catch {}
}

function _sortCards(cards) {
  return [...cards].sort((a, b) => {
    if (_activeSort === 'release') {
      const ad = a.release_date || a.year || '0000';
      const bd = b.release_date || b.year || '0000';
      return bd.localeCompare(ad);
    }
    if (_activeSort === 'pct_asc')  return (a.progress || 0) - (b.progress || 0);
    if (_activeSort === 'pct_desc') return (b.progress || 0) - (a.progress || 0);
    return (b.last_watched || '').localeCompare(a.last_watched || '');
  });
}

function _updateFilterCounts(cards) {
  const counts = {
    all:      cards.length,
    movie:    cards.filter(c => c.media_type === 'movie').length,
    tv:       cards.filter(c => c.media_type === 'tv').length,
    watching: cards.filter(c => !c.is_complete).length,
  };
  const labels = { all: 'Все', movie: 'Фильмы', tv: 'Сериалы', watching: 'В&nbspпроцессе' };
  const ids = {
    all: 'historyFilterAll', movie: 'historyFilterMovie',
    tv: 'historyFilterTv', watching: 'historyFilterWatching',
  };
  Object.entries(ids).forEach(([type, id]) => {
    const btn = document.getElementById(id);
    if (btn) btn.innerHTML = `${labels[type]}&nbsp;(${counts[type]})`;
  });
}

function _renderCards(cards) {
  const grid = document.getElementById('historyGrid');
  if (!grid) return;

  let filtered = _activeFilter === 'all'
    ? cards
    : _activeFilter === 'watching'
      ? cards.filter(c => !c.is_complete)
      : cards.filter(c => c.media_type === _activeFilter);

  if (_searchQuery) {
    const q = _searchQuery.toLowerCase();
    filtered = filtered.filter(c =>
      (c.title || '').toLowerCase().includes(q) ||
      (c.original_title || '').toLowerCase().includes(q)
    );
  }

  const sorted = _sortCards(filtered);

  if (!sorted.length) {
    grid.innerHTML = '<p class="history-empty">История просмотров пуста</p>';
    return;
  }

  grid.innerHTML = sorted.map(card => {
    _cardMap[card.card_id] = card;
    const pct = Math.min(100, Math.max(0, card.progress ?? card.max_percent ?? 0));
    const poster = card.poster_path
      ? `<img src="${POSTER_BASE}${card.poster_path}" alt="" loading="lazy">`
      : `<div class="card-no-poster">${card.title || '—'}</div>`;

    // Бейдж top-right: ✓ если завершён, для сериалов — число оставшихся
    let topBadge = '';
    if (card.is_complete) {
      topBadge = '<div class="watched-badge">✓</div>';
    } else if (card.media_type === 'tv' && card.watched_episodes != null && card.total_episodes != null) {
      const remaining = card.total_episodes - card.watched_episodes;
      if (remaining > 0) topBadge = `<div class="card-remaining">${remaining}</div>`;
    }

    // Прогресс сериала: "12 / 24 эп."
    let episodeInfo = '';
    if (card.media_type === 'tv' && card.watched_episodes != null && card.total_episodes != null) {
      const ongoing = card.is_ongoing ? ' (онгоинг)' : '';
      episodeInfo = `<p class="card-eps">${card.watched_episodes} / ${card.total_episodes} эп.${ongoing}</p>`;
    }

    return `
      <div class="media-card" role="button" tabindex="0" data-card-id="${card.card_id}" style="cursor:pointer;">
        ${poster}
        ${topBadge}
        <div class="card-info">
          ${episodeInfo}
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
          <p class="card-title">${card.title || card.card_id}</p>
          ${card.year ? `<p class="card-year">${card.year}</p>` : ''}
        </div>
      </div>`;
  }).join('');

  grid.querySelectorAll('.media-card[data-card-id]').forEach(el => {
    el.addEventListener('click', () => _navigateToCard(el.dataset.cardId));
    el.addEventListener('keydown', e => { if (e.key === 'Enter') _navigateToCard(el.dataset.cardId); });
  });
}

function _navigateToCard(cardId) {
  const p = new URLSearchParams({ back: location.pathname + location.search });
  if (_currentDevice)       p.set('device_id', _currentDevice);
  if (_currentProfile != null) p.set('profile_id', _currentProfile);
  location.href = `/card/${encodeURIComponent(cardId)}?${p}`;
}

function _profileLabel(p) {
  return p.name || (p.profile_id === '' ? 'Основной' : p.profile_id);
}

async function _loadProfileTabs(deviceId, currentProfileId) {
  const container = document.getElementById('historyProfileTabs');
  if (!container) return;

  try {
    const res = await fetch(`/api/profile-ids?device_id=${deviceId}`);
    const data = await res.json();
    const profiles = data.profiles || [];

    if (profiles.length === 0) {
      container.innerHTML = '';
      container.classList.remove('stats-tabs');
      return;
    }

    // Если профиль не выбран — автоматически берём первый
    if (currentProfileId === null) {
      currentProfileId = profiles[0].profile_id;
      _currentProfile  = currentProfileId;
      _savePrefs({ profile_id: currentProfileId });
      loadHistory(deviceId, currentProfileId);
    }

    container.classList.add('stats-tabs');

    const tabs = [];
    profiles.forEach(p => {
      const active = p.profile_id === currentProfileId ? ' active' : '';
      const label  = _profileLabel(p);
      tabs.push(`<button class="tab-btn${active}" data-profile="${p.profile_id}" data-label="${label}" title="Двойной клик — переименовать">${label}</button>`);
    });

    container.innerHTML = tabs.join('');

    container.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        container.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const pid = btn.dataset.profile;
        _currentProfile = pid;
        _savePrefs({ profile_id: pid });
        loadHistory(deviceId, pid);
      });

      btn.addEventListener('dblclick', () => _renameProfile(deviceId, btn));
    });
  } catch {
    container.innerHTML = '';
  }
}

async function _renameProfile(deviceId, btn) {
  const current = btn.dataset.label || '';
  const name = window.prompt('Название профиля:', current);
  if (name === null) return;  // отмена

  try {
    const res = await fetch('/api/profile-name', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        device_id: deviceId,
        profile_id: btn.dataset.profile,
        name: name.trim(),
      }),
    });
    if (res.ok) {
      await _loadProfileTabs(deviceId, _currentProfile);
    }
  } catch { /* игнорируем */ }
}

async function loadHistory(deviceId, profileId = null) {
  _currentDevice  = deviceId;
  _currentProfile = profileId;

  const grid = document.getElementById('historyGrid');
  if (!grid) return;
  grid.innerHTML = '<p class="history-empty">Загрузка…</p>';

  try {
    const url = profileId === null
      ? `/api/history?device_id=${deviceId}`
      : `/api/history?device_id=${deviceId}&profile_id=${encodeURIComponent(profileId)}`;
    const res = await fetch(url);
    if (!res.ok) { grid.innerHTML = '<p class="history-empty">Ошибка загрузки</p>'; return; }
    _allCards = await res.json();
    _updateFilterCounts(_allCards);
    _renderCards(_allCards);
  } catch {
    grid.innerHTML = '<p class="history-empty">Ошибка соединения</p>';
  }
}

// ---------------------------------------------------------------------------
// Модальное окно карточки
// ---------------------------------------------------------------------------

function _fillModalContent(modal, card, full) {
  const data    = full || card;
  const pct     = Math.min(100, Math.max(0, card.max_percent || 0));
  const watched = pct >= WATCHED_THRESHOLD;

  const body = document.getElementById('modalBody');
  if (!body) return;

  const backdrop = data.backdrop_path
    ? `<img class="modal-backdrop" src="${BACKDROP_BASE}${data.backdrop_path}" alt="">`
    : '';

  const origTitle = (data.original_title && data.original_title !== data.title)
    ? `<p class="modal-orig">${data.original_title}</p>` : '';

  const tags = [];
  if (data.year) tags.push({ text: data.year, accent: true });
  if (data.vote_average) tags.push({ text: `★ ${Number(data.vote_average).toFixed(1)}` });
  if (data.media_type === 'tv' && data.number_of_seasons) {
    const n = data.number_of_seasons;
    tags.push({ text: `${n} сезон${n > 4 ? 'ов' : n > 1 ? 'а' : ''}` });
  }
  const tagsHtml = tags.map(t => `<span class="modal-tag${t.accent ? ' accent' : ''}">${t.text}</span>`).join('');

  const overview = data.overview
    ? `<p class="modal-overview">${data.overview}</p>` : '';

  const lastWatched = card.last_watched
    ? new Date(card.last_watched).toLocaleDateString('ru-RU') : '';

  body.innerHTML = `
    ${backdrop}
    <div class="modal-content">
      <p class="modal-title">${data.title || card.card_id}</p>
      ${origTitle}
      <div class="modal-tags">${tagsHtml}</div>
    </div>
    ${overview}
    <div class="modal-progress-row">
      <div class="modal-progress-top">
        <span>${watched ? '✓ Просмотрено' : `Просмотрено ${pct}%`}${lastWatched ? ` · ${lastWatched}` : ''}</span>
        ${_currentDevice ? `<button class="modal-delete-btn" id="modalDeleteBtn" title="Удалить историю просмотра">🗑</button>` : ''}
      </div>
      <div class="modal-progress-bar"><div class="modal-progress-fill" style="width:${pct}%"></div></div>
    </div>`;

  // Кнопка удаления таймкодов
  const deleteBtn = document.getElementById('modalDeleteBtn');
  if (deleteBtn) {
    deleteBtn.addEventListener('click', async () => {
      if (!confirm('Удалить историю просмотра этой карточки?')) return;
      deleteBtn.disabled = true;
      const profileParam = _currentProfile != null ? `&profile_id=${encodeURIComponent(_currentProfile)}` : '';
      try {
        const res = await fetch(
          `/api/card-timecodes?device_id=${_currentDevice}&card_id=${encodeURIComponent(card.card_id)}${profileParam}`,
          { method: 'DELETE' }
        );
        if (res.ok) {
          document.getElementById('cardModal')?.close();
          await loadHistory(_currentDevice, _currentProfile);
        } else {
          deleteBtn.disabled = false;
        }
      } catch { deleteBtn.disabled = false; }
    });
  }
}

async function openCardModal(cardId) {
  const modal = document.getElementById('cardModal');
  if (!modal) return;

  const card = _cardMap[cardId];
  if (!card) return;

  // Показываем сразу с базовыми данными
  _fillModalContent(modal, card, null);
  modal.showModal();

  // Догружаем полные данные из TMDB + список серий (параллельно)
  try {
    const fetches = [fetch(`/api/media-card/${encodeURIComponent(cardId)}`)];
    const isTV = card.media_type === 'tv' && _currentDevice;
    if (isTV) {
      const epUrl = `/api/episodes?device_id=${_currentDevice}&card_id=${encodeURIComponent(cardId)}`
        + (_currentProfile != null ? `&profile_id=${encodeURIComponent(_currentProfile)}` : '');
      fetches.push(fetch(epUrl));
    }

    const [mcRes, epRes] = await Promise.all(fetches);
    const full = mcRes.ok ? await mcRes.json() : null;
    const epData = (epRes && epRes.ok) ? await epRes.json() : null;

    _fillModalContent(modal, card, full);
    if (epData) _renderUnwatchedEpisodes(modal, card, epData);
  } catch { /* показываем то что есть */ }
}

function _renderUnwatchedEpisodes(modal, card, epData) {
  const body = document.getElementById('modalBody');
  if (!body) return;

  body.querySelector('.modal-episodes-section')?.remove();

  const allEps = epData.episodes || [];
  const unwatched = allEps.filter(e => !e.watched);
  const special   = allEps.filter(e => e.special);

  if (!unwatched.length && !special.length) return;

  const deviceId  = _currentDevice;
  const profileId = _currentProfile || '';

  function epLabel(e) {
    return `S${String(e.season).padStart(2,'0')}E${String(e.episode).padStart(2,'0')}`;
  }

  const leftHtml = unwatched.map(e =>
    `<div class="modal-ep-item" data-hash="${e.hash}">
      <span class="modal-ep-label">${epLabel(e)}</span>
      <button class="modal-ep-btn mark-btn">Спецэпизод</button>
    </div>`
  ).join('') || '<span class="modal-ep-empty">—</span>';

  const rightHtml = special.map(e =>
    `<div class="modal-ep-item" data-hash="${e.hash}">
      <span class="modal-ep-label">${epLabel(e)}</span>
      <button class="modal-ep-btn unmark unmark-btn">Отменить</button>
    </div>`
  ).join('') || '<span class="modal-ep-empty">—</span>';

  const section = document.createElement('div');
  section.className = 'modal-episodes-section';
  section.innerHTML = `
    <div class="modal-ep-hdr-row">
      <span>Непросмотрено (${unwatched.length})</span>
      <span>Спецсерии (${special.length})</span>
    </div>
    <div class="modal-ep-cols">
      <div class="modal-ep-col" id="modalEpLeft">${leftHtml}</div>
      <div class="modal-ep-col" id="modalEpRight">${rightHtml}</div>
    </div>`;
  body.appendChild(section);

  function _updateHeaders() {
    const hdrs = section.querySelectorAll('.modal-ep-hdr-row span');
    const lCount = section.querySelectorAll('#modalEpLeft .modal-ep-item').length;
    const rCount = section.querySelectorAll('#modalEpRight .modal-ep-item').length;
    if (hdrs[0]) hdrs[0].textContent = `Непросмотрено (${lCount})`;
    if (hdrs[1]) hdrs[1].textContent = `Спецсерии (${rCount})`;
    if (!lCount && !rCount) section.remove();
  }

  function _addMarkBtn(item, hash, label) {
    const btn = item.querySelector('.mark-btn');
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = '…';
      try {
        const res = await fetch('/api/mark-watched', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: deviceId, card_id: card.card_id, item: hash, profile_id: profileId }),
        });
        if (res.ok) {
          item.remove();
          const right = section.querySelector('#modalEpRight');
          right.querySelector('.modal-ep-empty')?.remove();
          const newItem = document.createElement('div');
          newItem.className = 'modal-ep-item';
          newItem.dataset.hash = hash;
          newItem.innerHTML = `<span class="modal-ep-label">${label}</span><button class="modal-ep-btn unmark unmark-btn">Отменить</button>`;
          right.appendChild(newItem);
          _addUnmarkBtn(newItem, hash, label);
          _updateHeaders();
          loadHistory(deviceId, _currentProfile);
        } else { btn.disabled = false; btn.textContent = 'Спецэпизод'; }
      } catch { btn.disabled = false; btn.textContent = 'Спецэпизод'; }
    });
  }

  function _addUnmarkBtn(item, hash, label) {
    const btn = item.querySelector('.unmark-btn');
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = '…';
      try {
        const res = await fetch('/api/unmark-special', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: deviceId, card_id: card.card_id, item: hash, profile_id: profileId }),
        });
        if (res.ok) {
          item.remove();
          const left = section.querySelector('#modalEpLeft');
          left.querySelector('.modal-ep-empty')?.remove();
          const newItem = document.createElement('div');
          newItem.className = 'modal-ep-item';
          newItem.dataset.hash = hash;
          newItem.innerHTML = `<span class="modal-ep-label">${label}</span><button class="modal-ep-btn mark-btn">Спецэпизод</button>`;
          left.appendChild(newItem);
          _addMarkBtn(newItem, hash, label);
          _updateHeaders();
          loadHistory(deviceId, _currentProfile);
        } else { btn.disabled = false; btn.textContent = 'Отменить'; }
      } catch { btn.disabled = false; btn.textContent = 'Отменить'; }
    });
  }

  section.querySelectorAll('.mark-btn').forEach(btn => {
    const item = btn.closest('.modal-ep-item');
    _addMarkBtn(item, item.dataset.hash, item.querySelector('.modal-ep-label').textContent);
  });

  section.querySelectorAll('.unmark-btn').forEach(btn => {
    const item = btn.closest('.modal-ep-item');
    _addUnmarkBtn(item, item.dataset.hash, item.querySelector('.modal-ep-label').textContent);
  });
}

function _initModal() {
  const modal     = document.getElementById('cardModal');
  const closeBtn  = document.getElementById('cardModalClose');
  if (!modal) return;

  closeBtn && closeBtn.addEventListener('click', () => modal.close());
  modal.addEventListener('click', e => { if (e.target === modal) modal.close(); });
}

function initHistory(defaultDeviceId) {
  const deviceSelect = document.getElementById('historyDeviceSelect');
  const filterBtns = {
    all:      document.getElementById('historyFilterAll'),
    movie:    document.getElementById('historyFilterMovie'),
    tv:       document.getElementById('historyFilterTv'),
    watching: document.getElementById('historyFilterWatching'),
  };

  // Восстановить сохранённые настройки
  const prefs = _loadPrefs();
  if (prefs.filter) _activeFilter = prefs.filter;
  if (prefs.sort)   _activeSort   = prefs.sort;

  // Активировать нужную кнопку фильтра
  Object.entries(filterBtns).forEach(([type, btn]) => {
    if (!btn) return;
    if (type === _activeFilter) btn.classList.add('active');
    else btn.classList.remove('active');
  });

  // Сортировка
  const sortSelect = document.getElementById('historySortSelect');
  if (sortSelect) {
    sortSelect.value = _activeSort;
    sortSelect.addEventListener('change', () => {
      _activeSort = sortSelect.value;
      _savePrefs({ sort: _activeSort });
      _renderCards(_allCards);
    });
  }

  // Фильтры
  Object.entries(filterBtns).forEach(([type, btn]) => {
    if (!btn) return;
    btn.addEventListener('click', () => {
      Object.values(filterBtns).forEach(b => b && b.classList.remove('active'));
      btn.classList.add('active');
      _activeFilter = type;
      _savePrefs({ filter: _activeFilter });
      _renderCards(_allCards);
    });
  });

  // Смена устройства
  if (deviceSelect && deviceSelect.tagName === 'SELECT') {
    // Восстановить сохранённое устройство
    if (prefs.device_id && deviceSelect.querySelector(`option[value="${prefs.device_id}"]`)) {
      deviceSelect.value = prefs.device_id;
    }
    deviceSelect.addEventListener('change', () => {
      const did = parseInt(deviceSelect.value);
      _currentProfile = null;
      _savePrefs({ device_id: did, profile_id: null });
      // profile_id: null → _loadProfileTabs автоматически выберет первый профиль
      _loadProfileTabs(did, null);
    });
  }

  // Поиск
  const searchInput = document.getElementById('historySearch');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      _searchQuery = searchInput.value.trim();
      _renderCards(_allCards);
    });
  }

  const savedDevice = prefs.device_id
    && deviceSelect?.querySelector(`option[value="${prefs.device_id}"]`)
    ? prefs.device_id : null;

  // savedDevice имеет приоритет над defaultDeviceId (который всегда devices[0])
  const startId = savedDevice
    || defaultDeviceId
    || (deviceSelect ? parseInt(deviceSelect.value) : null);

  if (startId) {
    // Синхронизируем select с реальным стартовым устройством
    if (deviceSelect && deviceSelect.tagName === 'SELECT') {
      deviceSelect.value = startId;
    }
    _savePrefs({ device_id: startId });

    // profile_id: null = «все профили»; undefined = «не было сохранено» → null
    const savedProfile = prefs.hasOwnProperty('profile_id') ? prefs.profile_id : null;
    _loadProfileTabs(startId, savedProfile);
    loadHistory(startId, savedProfile);
  }
}
