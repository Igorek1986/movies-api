(function () {
  'use strict';

  const IMAGE_BASE = window.IMAGE_BASE || 'https://image.tmdb.org';

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function createPosterCard(item, catId) {
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
    section.innerHTML = `
      <div class="catalog-row-header">
        <h3 class="catalog-row-title">${esc(cat.name)}</h3>
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
      for (const item of items) inner.appendChild(createPosterCard(item, catId));
    } catch (e) {
      inner.innerHTML = '<div class="catalog-row-empty">Ошибка загрузки</div>';
    }
  }

  async function initCatalog() {
    const container = document.getElementById('catalogContainer');
    const loading   = document.getElementById('catalogLoading');

    try {
      const resp = await fetch('/api/categories');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const categories = await resp.json();

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
    } catch (e) {
      loading.textContent = 'Ошибка загрузки категорий.';
    }
  }

  document.addEventListener('DOMContentLoaded', initCatalog);
})();
