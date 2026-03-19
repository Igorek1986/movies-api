/**
 * card_detail.js — полная страница карточки фильма/сериала.
 * Читает window.CARD_ID, параметры device_id / profile_id / back из query string.
 */

const _CD_IMG_BASE   = (window.TMDB_IMAGE_BASE || 'https://image.tmdb.org');
const _BACKDROP_BASE = _CD_IMG_BASE + '/t/p/w780';
const _WATCHED_THR   = 90;

(async function () {
  const cardId   = window.CARD_ID;
  const params   = new URLSearchParams(location.search);
  const backUrl  = params.get('back') || '/history';
  const deviceId = params.get('device_id') ? parseInt(params.get('device_id')) : null;
  const profileId = params.has('profile_id') ? params.get('profile_id') : null;

  document.getElementById('cardBack').href = backUrl;

  try {
    const res = await fetch(`/api/media-card/${encodeURIComponent(cardId)}`);
    if (!res.ok) {
      document.getElementById('cardLoading').textContent = 'Карточка не найдена';
      return;
    }
    const card = await res.json();
    _renderCard(card, cardId);

    // Параллельно: прогресс + эпизоды
    const tasks = [];
    if (deviceId) {
      tasks.push(_loadProgress(cardId, deviceId, profileId));
      if (card.media_type === 'tv') tasks.push(_loadEpisodes(card, cardId, deviceId, profileId));
    }
    await Promise.all(tasks);
  } catch (e) {
    document.getElementById('cardLoading').textContent = 'Ошибка загрузки';
  }
})();


function _renderCard(card, cardId) {
  document.title = card.title || cardId;
  document.getElementById('cardLoading').style.display = 'none';
  document.getElementById('cardContent').style.display  = 'block';

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
  if (card.year)            tags.push({ text: card.year, accent: true });
  if (card.vote_average)    tags.push({ text: `★ ${Number(card.vote_average).toFixed(1)}` });
  if (card.media_type === 'tv' && card.number_of_seasons) {
    const n = card.number_of_seasons;
    tags.push({ text: `${n} сез.` });
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


async function _loadProgress(cardId, deviceId, profileId) {
  try {
    const qp = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const res = await fetch(`/api/history?device_id=${deviceId}${qp}`);
    if (!res.ok) return;
    const cards = await res.json();
    const data  = cards.find(c => c.card_id === cardId);
    if (!data) return;

    const pct     = Math.min(100, Math.max(0, data.max_percent || 0));
    const watched = pct >= _WATCHED_THR;
    const section = document.getElementById('cardProgressSection');
    section.style.display = 'block';

    const lastWatched = data.last_watched
      ? new Date(data.last_watched).toLocaleDateString('ru-RU') : '';

    document.getElementById('cardProgressLabel').textContent =
      (watched ? '✓ Просмотрено' : `Просмотрено ${pct}%`) +
      (lastWatched ? ` · ${lastWatched}` : '');
    document.getElementById('cardProgressFill').style.width = pct + '%';

    const deleteBtn = document.getElementById('cardDeleteBtn');
    deleteBtn.style.display = 'inline';
    deleteBtn.addEventListener('click', async () => {
      if (!confirm('Удалить историю просмотра?')) return;
      deleteBtn.disabled = true;
      const dp = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
      const r  = await fetch(
        `/api/card-timecodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${dp}`,
        { method: 'DELETE' }
      );
      if (r.ok) { section.style.display = 'none'; }
      else      { deleteBtn.disabled = false; }
    });
  } catch { /* ignore */ }
}


async function _loadEpisodes(card, cardId, deviceId, profileId) {
  try {
    const qp = profileId != null ? `&profile_id=${encodeURIComponent(profileId)}` : '';
    const res = await fetch(`/api/episodes?device_id=${deviceId}&card_id=${encodeURIComponent(cardId)}${qp}`);
    if (!res.ok) return;
    const epData = await res.json();
    _renderEpisodes(card, epData, cardId, deviceId, profileId);
  } catch { /* ignore */ }
}


function _renderEpisodes(card, epData, cardId, deviceId, profileId) {
  const container = document.getElementById('cardEpisodesSection');
  const allEps    = epData.episodes || [];
  const unwatched = allEps.filter(e => !e.watched);
  const special   = allEps.filter(e => e.special);
  if (!unwatched.length && !special.length) return;

  function epLabel(e) {
    return `S${String(e.season).padStart(2,'0')}E${String(e.episode).padStart(2,'0')}`;
  }

  function itemHtml(e, isSpecial) {
    const btnClass = isSpecial ? 'modal-ep-btn unmark unmark-btn' : 'modal-ep-btn mark-btn';
    const btnText  = isSpecial ? 'Отменить' : 'Спецэпизод';
    return `<div class="modal-ep-item" data-hash="${e.hash}">
      <span class="modal-ep-label">${epLabel(e)}</span>
      <button class="${btnClass}">${btnText}</button>
    </div>`;
  }

  const leftHtml  = unwatched.map(e => itemHtml(e, false)).join('') || '<span class="modal-ep-empty">—</span>';
  const rightHtml = special.map(e => itemHtml(e, true)).join('')    || '<span class="modal-ep-empty">—</span>';

  container.innerHTML = `
    <div class="modal-episodes-section" style="margin-top:1rem;border-top:1px solid var(--pico-muted-border-color)">
      <div class="modal-ep-hdr-row">
        <span id="epHdrLeft">Непросмотрено (${unwatched.length})</span>
        <span id="epHdrRight">Спецсерии (${special.length})</span>
      </div>
      <div class="modal-ep-cols">
        <div class="modal-ep-col" id="epColLeft">${leftHtml}</div>
        <div class="modal-ep-col" id="epColRight">${rightHtml}</div>
      </div>
    </div>`;

  function updateHeaders() {
    const l = container.querySelectorAll('#epColLeft .modal-ep-item').length;
    const r = container.querySelectorAll('#epColRight .modal-ep-item').length;
    document.getElementById('epHdrLeft').textContent  = `Непросмотрено (${l})`;
    document.getElementById('epHdrRight').textContent = `Спецсерии (${r})`;
    if (!l && !r) container.innerHTML = '';
  }

  async function markSpecial(item, hash, label) {
    const btn = item.querySelector('.mark-btn');
    btn.disabled = true; btn.textContent = '…';
    const pp = profileId != null ? `, "profile_id": "${profileId}"` : '';
    const r  = await fetch('/api/mark-watched', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: `{"device_id":${deviceId},"card_id":${JSON.stringify(cardId)},"item":${JSON.stringify(hash)}${pp}}`,
    });
    if (r.ok) {
      item.remove();
      const right = document.getElementById('epColRight');
      right.querySelector('.modal-ep-empty')?.remove();
      const ni = document.createElement('div');
      ni.className = 'modal-ep-item'; ni.dataset.hash = hash;
      ni.innerHTML = `<span class="modal-ep-label">${label}</span><button class="modal-ep-btn unmark unmark-btn">Отменить</button>`;
      right.appendChild(ni);
      ni.querySelector('.unmark-btn').addEventListener('click', () => unmarkSpecial(ni, hash, label));
      updateHeaders();
    } else { btn.disabled = false; btn.textContent = 'Спецэпизод'; }
  }

  async function unmarkSpecial(item, hash, label) {
    const btn = item.querySelector('.unmark-btn');
    btn.disabled = true; btn.textContent = '…';
    const pp = profileId != null ? `, "profile_id": "${profileId}"` : '';
    const r  = await fetch('/api/unmark-special', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: `{"device_id":${deviceId},"card_id":${JSON.stringify(cardId)},"item":${JSON.stringify(hash)}${pp}}`,
    });
    if (r.ok) {
      item.remove();
      const left = document.getElementById('epColLeft');
      left.querySelector('.modal-ep-empty')?.remove();
      const ni = document.createElement('div');
      ni.className = 'modal-ep-item'; ni.dataset.hash = hash;
      ni.innerHTML = `<span class="modal-ep-label">${label}</span><button class="modal-ep-btn mark-btn">Спецэпизод</button>`;
      left.appendChild(ni);
      ni.querySelector('.mark-btn').addEventListener('click', () => markSpecial(ni, hash, label));
      updateHeaders();
    } else { btn.disabled = false; btn.textContent = 'Отменить'; }
  }

  container.querySelectorAll('.mark-btn').forEach(btn => {
    const item = btn.closest('.modal-ep-item');
    btn.addEventListener('click', () => markSpecial(item, item.dataset.hash, item.querySelector('.modal-ep-label').textContent));
  });
  container.querySelectorAll('.unmark-btn').forEach(btn => {
    const item = btn.closest('.modal-ep-item');
    btn.addEventListener('click', () => unmarkSpecial(item, item.dataset.hash, item.querySelector('.modal-ep-label').textContent));
  });
}
