// Password injected by template via window.STATS_PASSWORD
const password = window.STATS_PASSWORD || '';

let isRefreshing = false;


function switchTab(section, period, btn) {
    document.querySelectorAll(`#${section}-today, #${section}-total`).forEach(el => el.classList.remove('active'));
    if (btn) {
        btn.closest('.stats-tabs').querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    }
    document.getElementById(`${section}-${period}`).classList.add('active');
}

async function refreshData() {
    if (isRefreshing) return;
    const btn = document.getElementById('refreshBtn');
    if (btn) { btn.setAttribute('aria-busy', 'true'); btn.disabled = true; }
    isRefreshing = true;

    try {
        const res = await fetch('/stats/api', { headers: { 'X-Password': password }, cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        updateDashboard(data);
        const el = document.getElementById('lastUpdate');
        if (el) el.textContent = new Date().toLocaleTimeString('ru-RU');
    } catch (err) {
        console.error('Stats refresh error:', err);
        const el = document.getElementById('lastUpdate');
        if (el) el.textContent = `Ошибка: ${err.message}`;
    } finally {
        if (btn) { btn.removeAttribute('aria-busy'); btn.disabled = false; }
        isRefreshing = false;
    }
}

function updateDashboard(stats) {
    setText('usersTodayCount',       stats.registered_users?.today?.count);
    setText('usersTotalCount',       stats.registered_users?.total?.count);
    setText('myshowsTodayCount',     stats.myshows?.today?.count);
    setText('myshowsTotalCount',     stats.myshows?.total?.count);
    setText('apiUsersTodayCount',    stats.api_users?.today?.count);
    setText('apiUsersTotalCount',    stats.api_users?.total?.count);
    setText('categoriesTodayCount',      stats.categories?.today?.count);
    setText('categoriesTodayRequests',   stats.categories?.today?.total_requests);
    setText('categoriesTodayTotalBadge', stats.categories?.today?.total_requests);
    setText('categoriesTotalTotalBadge', stats.categories?.total?.total_requests);

    // Registered users today
    rebuildTable('usersTodayTable', stats.registered_users?.today?.detail, (rows) =>
        rows.map(([username, created_at]) => {
            return `<tr><td data-label="Логин"><strong>${esc(username)}</strong></td><td data-label="Время">${esc(created_at) || '—'}</td></tr>`;
        }),
    '<tr><td colspan="99" class="muted">Новых пользователей сегодня нет</td></tr>');

    // Registered users total
    rebuildTable('usersTotalTable', stats.registered_users?.total?.detail, (rows) =>
        rows.map(([username, created_at]) => {
            return `<tr><td data-label="Логин"><strong>${esc(username)}</strong></td><td data-label="Дата">${esc(created_at) || '—'}</td></tr>`;
        }),
    '<tr><td colspan="99" class="muted">Нет данных</td></tr>');

    // MyShows today
    rebuildTable('myshowsTodayTable', stats.myshows?.today?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([login, req]) =>
            `<tr><td data-label="Логин"><strong>${esc(login)}</strong></td><td data-label="Запросов">${req}</td><td data-label="Доля">${pct(req, total)}</td></tr>`
        );
    }, '<tr><td colspan="99" class="muted">Нет данных</td></tr>');

    // MyShows total
    rebuildTable('myshowsTotalTable', stats.myshows?.total?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([login, req]) =>
            `<tr><td data-label="Логин"><strong>${esc(login)}</strong></td><td data-label="Запросов">${req}</td><td data-label="Доля">${pct(req, total)}</td></tr>`
        );
    }, '<tr><td colspan="99" class="muted">Нет данных</td></tr>');

    // API users today
    rebuildTable('apiUsersTodayTable', stats.api_users?.today?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([ip, req, country, city, region, flag]) =>
            `<tr><td data-label="IP"><code>${esc(ip)}</code></td><td data-label="Место">${locationStr(flag, country, city, region)}</td><td data-label="Запросов">${req}</td><td data-label="Доля">${pct(req, total)}</td></tr>`
        );
    }, '<tr><td colspan="99" class="muted">Нет данных</td></tr>');

    // API users total
    rebuildTable('apiUsersTotalTable', stats.api_users?.total?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([ip, req, country, city, region, flag]) =>
            `<tr><td data-label="IP"><code>${esc(ip)}</code></td><td data-label="Место">${locationStr(flag, country, city, region)}</td><td data-label="Запросов">${req}</td><td data-label="Доля">${pct(req, total)}</td></tr>`
        );
    }, '<tr><td colspan="99" class="muted">Нет данных</td></tr>');

    // Categories
    rebuildCategoryGrid('categoriesTodayGrid',
        stats.categories?.today?.detail,
        stats.categories?.today?.unique_ips,
        stats.categories?.today?.total_requests_per_category);
    rebuildCategoryGrid('categoriesTotalGrid',
        stats.categories?.total?.detail,
        stats.categories?.total?.unique_ips,
        stats.categories?.total?.total_requests_per_category);
}

// builder(rows) must return an array of <tr> HTML strings
function rebuildTable(id, rows, builder, empty) {
    const tbody = document.querySelector(`#${id} tbody`);
    if (!tbody) return;
    if (!rows || !rows.length) { tbody.innerHTML = empty; return; }
    const rowsHtml = builder(rows);
    const visible = rowsHtml.slice(0, 5).join('');
    const extra = rowsHtml.slice(5).map(h => h.replace(/^<tr/, '<tr class="tbl-extra" style="display:none"')).join('');
    const btn = rowsHtml.length > 5
        ? `<tr class="tbl-show-more"><td colspan="99"><button class="outline secondary btn-sm" onclick="expandTable(this)">Ещё ${rowsHtml.length - 5}…</button></td></tr>`
        : '';
    tbody.innerHTML = visible + extra + btn;
}

function expandTable(btn) {
    const tbody = btn.closest('tbody');
    tbody.querySelectorAll('.tbl-extra').forEach(r => r.style.display = '');
    const row = btn.closest('.tbl-show-more');
    row.innerHTML = `<td colspan="99"><button class="outline secondary btn-sm" onclick="collapseTable(this)">Свернуть</button></td>`;
}

function collapseTable(btn) {
    const tbody = btn.closest('tbody');
    tbody.querySelectorAll('.tbl-extra').forEach(r => r.style.display = 'none');
    const extras = tbody.querySelectorAll('.tbl-extra').length;
    const row = btn.closest('.tbl-show-more');
    row.innerHTML = `<td colspan="99"><button class="outline secondary btn-sm" onclick="expandTable(this)">Ещё ${extras}…</button></td>`;
}

function rebuildCategoryGrid(id, detail, uniqueIps, totalReq) {
    const grid = document.getElementById(id);
    if (!grid || !detail) return;
    const entries = Object.entries(detail).sort((a, b) =>
        (totalReq?.[b[0]] || 0) - (totalReq?.[a[0]] || 0)
    );
    grid.innerHTML = entries.map(([cat, ips]) => {
        const rows = (ips || []).map((item, i) =>
            `<tr${i >= 5 ? ' class="cat-extra" style="display:none"' : ''}><td><code>${esc(item.ip)}</code></td><td>${item.requests}</td></tr>`
        ).join('');
        const moreBtn = ips && ips.length > 5
            ? `<tr class="cat-show-more"><td colspan="2"><button class="outline secondary btn-sm" onclick="expandCategory(this)">Ещё ${ips.length - 5}…</button></td></tr>`
            : '';
        return `<article class="category-card">
            <header><strong>${esc(cat)}</strong>
            <small class="muted"> — ${uniqueIps?.[cat] || 0} IP, ${totalReq?.[cat] || 0} запросов</small></header>
            <table><thead><tr><th>IP адрес</th><th>Запросов</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="2" class="muted">Нет данных</td></tr>'}${moreBtn}</tbody></table>
        </article>`;
    }).join('');
    if (!entries.length) grid.innerHTML = '<p class="muted">Нет данных</p>';
}

function expandCategory(btn) {
    const tbody = btn.closest('tbody');
    tbody.querySelectorAll('.cat-extra').forEach(r => r.style.display = '');
    const row = btn.closest('.cat-show-more');
    row.innerHTML = `<td colspan="2"><button class="outline secondary btn-sm" onclick="collapseCategory(this)">Свернуть</button></td>`;
}

function collapseCategory(btn) {
    const tbody = btn.closest('tbody');
    tbody.querySelectorAll('.cat-extra').forEach(r => r.style.display = 'none');
    const extras = tbody.querySelectorAll('.cat-extra').length;
    const row = btn.closest('.cat-show-more');
    row.innerHTML = `<td colspan="2"><button class="outline secondary btn-sm" onclick="expandCategory(this)">Ещё ${extras}…</button></td>`;
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el && val !== undefined) el.textContent = val;
}
function pct(val, total) { return total > 0 ? ((val / total) * 100).toFixed(1) + '%' : ''; }
function locationStr(flag, country, city, region) {
    let s = `${flag || '🌍'} ${country || 'Unknown'}, ${city || 'Unknown'}`;
    if (region && region !== city && region !== 'Unknown') s += `, ${region}`;
    return esc(s);
}
function esc(str) {
    const d = document.createElement('div');
    d.textContent = String(str || '');
    return d.innerHTML;
}
