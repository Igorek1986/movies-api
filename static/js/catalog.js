(function () {
  'use strict';

  const IMAGE_BASE = window.IMAGE_BASE || 'https://image.tmdb.org';
  const LS_KEY = 'catalog_row_order';

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function saveOrder(container) {
    const ids = Array.from(container.querySelectorAll('.catalog-row'))
      .map(el => el.dataset.catId);
    localStorage.setItem(LS_KEY, JSON.stringify(ids));
  }

  function applySavedOrder(categories) {
    try {
      const saved = JSON.parse(localStorage.getItem(LS_KEY) || '[]');
      if (!saved.length) return categories;
      const map = Object.fromEntries(categories.map(c => [c.id, c]));
      const ordered = saved.filter(id => map[id]).map(id => map[id]);
      const rest = categories.filter(c => !saved.includes(c.id));
      return [...ordered, ...rest];
    } catch (e) {
      return categories;
    }
  }

  function _deviceParams() {
    try {
      const prefs = JSON.parse(localStorage.getItem('history_prefs') || '{}');
      const p = new URLSearchParams();
      if (prefs.device_id) p.set('device_id', prefs.device_id);
      if (prefs.profile_id != null) p.set('profile_id', prefs.profile_id);
      const s = p.toString();
      return s ? '&' + s : '';
    } catch { return ''; }
  }

  function createPosterCard(item) {
    const mediaType = item.media_type || (item.name ? 'tv' : 'movie');
    const title     = item.title || item.name || '';
    const year      = (item.release_date || item.first_air_date || '').slice(0, 4);
    const poster    = item.poster_path ? `${IMAGE_BASE}/t/p/w300${item.poster_path}` : '';
    const cardId    = `${item.id}_${mediaType}`;
    const back      = encodeURIComponent('/');

    const a = document.createElement('a');
    a.id = 'card-' + cardId;
    a.className = 'catalog-poster-card' + (mediaType === 'tv' ? ' is-tv' : '');
    a.href = `/card/${encodeURIComponent(cardId)}?back=${back}${_deviceParams()}`;

    if (poster) {
      a.innerHTML = `<img src="${esc(poster)}" alt="${esc(title)}" loading="lazy">`;
    } else {
      a.innerHTML = `<div class="card-no-poster">${esc(title)}</div>`;
    }
    a.innerHTML += `
      <div class="catalog-poster-info">
        <div class="catalog-poster-title">${esc(title)}</div>
        ${year ? `<div class="catalog-poster-year">${esc(year)}</div>` : ''}
      </div>`;
    return a;
  }

  function createRowSkeleton(cat) {
    const section = document.createElement('section');
    section.className = 'catalog-row';
    section.dataset.catId = cat.id;
    section.draggable = true;
    section.innerHTML = `
      <div class="catalog-row-header">
        <div class="catalog-row-header-left">
          <span class="catalog-drag-handle" title="Перетащить">⠿</span>
          <h3 class="catalog-row-title">${esc(cat.name)}</h3>
        </div>
        <a href="/catalog/${encodeURIComponent(cat.id)}" class="catalog-row-more">Все →</a>
      </div>
      <div class="catalog-row-scroll">
        <div class="catalog-row-inner">
          <div class="catalog-row-loading">Загрузка...</div>
        </div>
      </div>`;
    return section;
  }

  async function loadRow(rowEl) {
    const catId = rowEl.dataset.catId;
    const inner = rowEl.querySelector('.catalog-row-inner');
    try {
      const resp = await fetch(`/${encodeURIComponent(catId)}?per_page=20&page=1`);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();

      inner.innerHTML = '';
      const items = data.results || [];
      if (items.length === 0) {
        inner.innerHTML = '<div class="catalog-row-empty">Нет данных</div>';
        return;
      }
      for (const item of items) inner.appendChild(createPosterCard(item));
    } catch (e) {
      inner.innerHTML = '<div class="catalog-row-empty">Ошибка загрузки</div>';
    }
  }

  function initDragAndDrop(container) {
    let dragSrc = null;

    container.addEventListener('dragstart', e => {
      const row = e.target.closest('.catalog-row');
      if (!row) return;
      dragSrc = row;
      row.classList.add('catalog-row--dragging');
      e.dataTransfer.effectAllowed = 'move';
    });

    container.addEventListener('dragend', e => {
      const row = e.target.closest('.catalog-row');
      if (row) row.classList.remove('catalog-row--dragging');
      container.querySelectorAll('.catalog-row--over').forEach(el => el.classList.remove('catalog-row--over'));
      saveOrder(container);
    });

    container.addEventListener('dragover', e => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const row = e.target.closest('.catalog-row');
      if (!row || row === dragSrc) return;
      container.querySelectorAll('.catalog-row--over').forEach(el => el.classList.remove('catalog-row--over'));
      row.classList.add('catalog-row--over');

      const rows = Array.from(container.querySelectorAll('.catalog-row'));
      const srcIdx = rows.indexOf(dragSrc);
      const tgtIdx = rows.indexOf(row);
      if (srcIdx < tgtIdx) {
        row.after(dragSrc);
      } else {
        row.before(dragSrc);
      }
    });

    container.addEventListener('dragleave', e => {
      const row = e.target.closest('.catalog-row');
      if (row) row.classList.remove('catalog-row--over');
    });

    container.addEventListener('drop', e => {
      e.preventDefault();
    });
  }

  async function _initMainCatalog() {
    const container = document.getElementById('catalogContainer');
    const loading   = document.getElementById('catalogLoading');
    if (!container || !loading) return;  // не главная страница каталога

    try {
      const resp = await fetch('/api/categories');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const categories = applySavedOrder(await resp.json());

      loading.remove();

      if (categories.length === 0) {
        container.innerHTML = '<p class="muted">Категории не найдены.</p>';
        return;
      }

      const observer = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            observer.unobserve(entry.target);
            loadRow(entry.target);
          }
        }
      }, { rootMargin: '300px' });

      for (const cat of categories) {
        const row = createRowSkeleton(cat);
        container.appendChild(row);
        observer.observe(row);
      }

      initDragAndDrop(container);
    } catch (e) {
      loading.textContent = 'Ошибка загрузки категорий.';
    }
  }

  document.addEventListener('DOMContentLoaded', _initMainCatalog);
})();




// ─── Shared: prefs + profile tabs ────────────────────────────────────────────

const _CATALOG_PREFS_KEY = 'history_prefs';

function _catalogLoadPrefs() {
  try { return JSON.parse(localStorage.getItem(_CATALOG_PREFS_KEY) || '{}'); } catch { return {}; }
}
function _catalogSavePrefs(patch) {
  const p = Object.assign(_catalogLoadPrefs(), patch);
  try { localStorage.setItem(_CATALOG_PREFS_KEY, JSON.stringify(p)); } catch {}
}
function _catalogDeviceParams() {
  const p = _catalogLoadPrefs();
  const q = new URLSearchParams();
  if (p.device_id) q.set('device_id', p.device_id);
  if (p.profile_id != null) q.set('profile_id', p.profile_id);
  const s = q.toString();
  return s ? '&' + s : '';
}

async function _catalogLoadProfileTabs(deviceId, currentProfileId, onSelect) {
  const container = document.getElementById('catalogProfileTabs');
  if (!container) return;
  try {
    const res  = await fetch(`/api/profile-ids?device_id=${deviceId}`);
    const data = await res.json();
    const profiles = data.profiles || [];
    if (profiles.length === 0) { container.innerHTML = ''; container.classList.remove('stats-tabs'); return; }

    // Если профиль не выбран — автоматически берём первый
    if (currentProfileId === null) {
      currentProfileId = profiles[0].profile_id;
      _catalogSavePrefs({ profile_id: currentProfileId });
      if (onSelect) onSelect(currentProfileId);
    }

    container.classList.add('stats-tabs');
    const tabs = [];
    profiles.forEach(p => {
      const active = p.profile_id === currentProfileId ? ' active' : '';
      const label  = p.name || (p.profile_id === '' ? 'Основной' : p.profile_id);
      tabs.push(`<button class="tab-btn${active}" data-profile="${esc2(p.profile_id)}">${esc2(label)}</button>`);
    });
    container.innerHTML = tabs.join('');

    container.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        container.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const pid = btn.dataset.profile;
        _catalogSavePrefs({ profile_id: pid });
        if (onSelect) onSelect(pid);
      });
    });
  } catch { container.innerHTML = ''; }
}

function esc2(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _catalogInitFilter(onDeviceChange) {
  const deviceSelect = document.getElementById('catalogDeviceSelect');
  const prefs = _catalogLoadPrefs();

  if (deviceSelect && deviceSelect.tagName === 'SELECT') {
    if (prefs.device_id && deviceSelect.querySelector(`option[value="${prefs.device_id}"]`)) {
      deviceSelect.value = prefs.device_id;
    }
    deviceSelect.addEventListener('change', () => {
      const did = parseInt(deviceSelect.value);
      _catalogSavePrefs({ device_id: did, profile_id: null });
      // profile_id: null → _catalogLoadProfileTabs автоматически выберет первый профиль
      _catalogLoadProfileTabs(did, null, onDeviceChange);
    });
  }

  const deviceId = deviceSelect ? parseInt(deviceSelect.value) : null;
  if (deviceId) {
    _catalogSavePrefs({ device_id: deviceId });
    const savedProfile = prefs.hasOwnProperty('profile_id') ? prefs.profile_id : null;
    _catalogLoadProfileTabs(deviceId, savedProfile, onDeviceChange);
  }
}


// ─── Main catalog page (/) ────────────────────────────────────────────────────

function initMainCatalog() {
  _catalogInitFilter(null);

  const searchInput   = document.getElementById('globalSearch');
  const catalogCont   = document.getElementById('catalogContainer');
  const catalogLoad   = document.getElementById('catalogLoading');
  const searchResults = document.getElementById('searchResults');
  const searchLoading = document.getElementById('searchLoading');
  const searchEmpty   = document.getElementById('searchEmpty');
  const searchGrid    = document.getElementById('searchGrid');
  if (!searchInput) return;

  function esc3(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function createSearchCard(item) {
    const title  = item.title || '';
    const imgBase = window.IMAGE_BASE || 'https://image.tmdb.org';
    const poster = item.poster_path ? `${imgBase}/t/p/w300${item.poster_path}` : '';
    const cardId = `${item.id}_${item.media_type}`;
    const back   = encodeURIComponent('/');
    const a = document.createElement('a');
    a.id = 'card-' + cardId;
    a.className = 'catalog-poster-card' + (item.media_type === 'tv' ? ' is-tv' : '');
    a.href = `/card/${encodeURIComponent(cardId)}?back=${back}${_catalogDeviceParams()}`;
    if (poster) {
      a.innerHTML = `<img src="${esc3(poster)}" alt="${esc3(title)}" loading="lazy">`;
    } else {
      a.innerHTML = `<div class="card-no-poster">${esc3(title)}</div>`;
    }
    a.innerHTML += `
      <div class="catalog-poster-info">
        <div class="catalog-poster-title">${esc3(title)}</div>
        ${item.year ? `<div class="catalog-poster-year">${esc3(item.year)}</div>` : ''}
        <div class="catalog-poster-cat">${esc3(item.category_name || '')}</div>
      </div>`;
    return a;
  }

  const SS_KEY = 'catalog_global_search';

  function runSearch(q) {
    if (q.length < 3) {
      sessionStorage.removeItem(SS_KEY);
      searchResults.style.display = 'none';
      catalogCont.style.display   = '';
      if (catalogLoad) catalogLoad.style.display = '';
      return;
    }
    sessionStorage.setItem(SS_KEY, q);
    catalogCont.style.display = 'none';
    if (catalogLoad) catalogLoad.style.display = 'none';
    searchResults.style.display = 'block';
    searchLoading.style.display = 'block';
    searchEmpty.style.display   = 'none';
    searchGrid.innerHTML        = '';
    fetch(`/api/search?q=${encodeURIComponent(q)}`)
      .then(r => r.json())
      .then(data => {
        searchLoading.style.display = 'none';
        const items = data.results || [];
        if (!items.length) { searchEmpty.style.display = 'block'; return; }
        for (const item of items) searchGrid.appendChild(createSearchCard(item));
      })
      .catch(() => {
        searchLoading.style.display = 'none';
        searchEmpty.style.display   = 'block';
      });
  }

  // Восстановить поиск при возврате назад
  const saved = sessionStorage.getItem(SS_KEY);
  if (saved) {
    searchInput.value = saved;
    runSearch(saved);
  }

  let _searchTimer = null;
  searchInput.addEventListener('input', () => {
    clearTimeout(_searchTimer);
    const q = searchInput.value.trim();
    _searchTimer = setTimeout(() => runSearch(q), 400);
  });
}


// ─── Category page (/catalog/{id}) ───────────────────────────────────────────

function initCatalog(categoryId, imageBase) {
  function createCard(item) {
    const mediaType = item.media_type || (item.name ? 'tv' : 'movie');
    const title     = item.title || item.name || '';
    const year      = (item.release_date || item.first_air_date || '').slice(0, 4);
    const poster    = item.poster_path ? `${imageBase}/t/p/w300${item.poster_path}` : '';
    const cardId    = `${item.id}_${mediaType}`;
    const back      = encodeURIComponent('/catalog/' + categoryId);

    const a = document.createElement('a');
    a.id = 'card-' + cardId;
    a.className = 'catalog-poster-card' + (mediaType === 'tv' ? ' is-tv' : '');
    a.href = `/card/${esc2(cardId)}?back=${back}${_catalogDeviceParams()}`;

    if (poster) {
      a.innerHTML = `<img src="${esc2(poster)}" alt="${esc2(title)}" loading="lazy">`;
    } else {
      a.innerHTML = `<div class="card-no-poster">${esc2(title)}</div>`;
    }
    a.innerHTML += `
      <div class="catalog-poster-info">
        <div class="catalog-poster-title">${esc2(title)}</div>
        ${year ? `<div class="catalog-poster-year">${esc2(year)}</div>` : ''}
      </div>`;
    return a;
  }

  function updateCardLinks() {
    document.querySelectorAll('.catalog-poster-card').forEach(a => {
      const url  = new URL(a.href, location.origin);
      const back = url.searchParams.get('back') || encodeURIComponent('/catalog/' + categoryId);
      const q    = new URLSearchParams(_catalogDeviceParams().replace(/^&/, ''));
      q.set('back', back);
      url.search = q.toString();
      a.href = url.toString();
    });
  }

  _catalogInitFilter(updateCardLinks);

  // Поиск
  let _searchQuery = '';
  const searchInput = document.getElementById('catalogSearch');
  let _searchTimer = null;
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => {
        const v = searchInput.value.trim();
        _searchQuery = v.length >= 3 ? v : '';
        resetGrid();
      }, 300);
    });
  }

  // Пагинация
  let _page = 1, _totalPages = 1, _loading = false;

  function resetGrid() {
    _page = 1;
    _totalPages = 1;
    const grid = document.getElementById('catalogGrid');
    grid.innerHTML = '';
    document.getElementById('gridEmpty').style.display = 'none';
    document.getElementById('gridLoading').style.display = 'none';
    loadPage(1);
  }

  async function loadPage(page) {
    if (_loading) return;
    _loading = true;
    try {
      let url = `/${encodeURIComponent(categoryId)}?per_page=20&page=${page}`;
      if (_searchQuery) url += `&search=${encodeURIComponent(_searchQuery)}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      _totalPages = data.total_pages || 1;
      if (page === 1) document.getElementById('gridLoading').style.display = 'none';
      const items = data.results || [];
      if (items.length === 0 && page === 1) {
        document.getElementById('gridEmpty').style.display = 'block';
        return;
      }
      const grid = document.getElementById('catalogGrid');
      for (const item of items) grid.appendChild(createCard(item));
    } catch {
      document.getElementById('gridLoading').textContent = 'Ошибка загрузки';
    } finally {
      _loading = false;
    }
  }

  const sentinel = document.getElementById('loadSentinel');
  const observer = new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && _page < _totalPages && !_loading) loadPage(++_page);
  }, { rootMargin: '300px' });

  (async function () {
    await loadPage(1);
    if (_page < _totalPages) await loadPage(++_page);
    observer.observe(sentinel);
  })();
}
