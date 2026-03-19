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

  function createPosterCard(item) {
    const mediaType = item.media_type || (item.name ? 'tv' : 'movie');
    const title     = item.title || item.name || '';
    const year      = (item.release_date || item.first_air_date || '').slice(0, 4);
    const poster    = item.poster_path ? `${IMAGE_BASE}/t/p/w300${item.poster_path}` : '';
    const cardId    = `${item.id}_${mediaType}`;
    const back      = encodeURIComponent('/');

    const a = document.createElement('a');
    a.className = 'catalog-poster-card';
    a.href = `/card/${encodeURIComponent(cardId)}?back=${back}`;

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

  async function initCatalog() {
    const container = document.getElementById('catalogContainer');
    const loading   = document.getElementById('catalogLoading');

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

  document.addEventListener('DOMContentLoaded', initCatalog);
})();
