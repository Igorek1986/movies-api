// Password injected by template via window.STATS_PASSWORD
const password = window.STATS_PASSWORD || '';

let autoRefreshInterval = null;
let isRefreshing = false;

document.addEventListener('DOMContentLoaded', () => {
    const autoRefreshEl = document.getElementById('autoRefresh');
    const saved = localStorage.getItem('stats_autoRefresh');
    autoRefreshEl.checked = saved !== 'false';
    if (autoRefreshEl.checked) startAutoRefresh();

    autoRefreshEl.addEventListener('change', (e) => {
        localStorage.setItem('stats_autoRefresh', e.target.checked);
        e.target.checked ? startAutoRefresh() : stopAutoRefresh();
    });
});

function startAutoRefresh() {
    stopAutoRefresh();
    autoRefreshInterval = setInterval(refreshData, 30000);
}
function stopAutoRefresh() {
    if (autoRefreshInterval) { clearInterval(autoRefreshInterval); autoRefreshInterval = null; }
}

function switchTab(section, period, btn) {
    document.querySelectorAll(`#${section}-today, #${section}-total`).forEach(el => el.classList.remove('active'));
    // reset tab buttons in same group
    if (btn) {
        btn.closest('.stats-tabs').querySelectorAll('.tab-btn').forEach(b => {
            b.classList.remove('outline');
            b.classList.add('outline', 'secondary');
        });
        btn.classList.remove('secondary');
    }
    document.getElementById(`${section}-${period}`).classList.add('active');
}

async function refreshData() {
    if (isRefreshing) return;
    const btn = document.getElementById('refreshBtn');
    if (btn) { btn.setAttribute('aria-busy', 'true'); btn.disabled = true; }
    isRefreshing = true;

    try {
        const res = await fetch('/stats/api', { headers: { 'X-Password': password } });
        if (!res.ok) throw new Error('Ошибка загрузки');
        const data = await res.json();
        updateDashboard(data);
        const el = document.getElementById('lastUpdate');
        if (el) el.textContent = `Обновлено: ${new Date().toLocaleTimeString('ru-RU')}`;
    } catch (err) {
        console.error('Stats refresh error:', err);
    } finally {
        if (btn) { btn.removeAttribute('aria-busy'); btn.disabled = false; }
        isRefreshing = false;
    }
}

function updateDashboard(stats) {
    setText('myshowsTodayCount',   stats.myshows?.today?.count);
    setText('myshowsTotalCount',   stats.myshows?.total?.count);
    setText('apiUsersTodayCount',  stats.api_users?.today?.count);
    setText('apiUsersTotalCount',  stats.api_users?.total?.count);
    setText('categoriesTodayCount',    stats.categories?.today?.count);
    setText('categoriesTodayRequests', stats.categories?.today?.total_requests);
    setText('categoriesTodayTotalBadge', stats.categories?.today?.total_requests);
    setText('categoriesTotalTotalBadge', stats.categories?.total?.total_requests);

    // MyShows today
    rebuildTable('myshowsTodayTable', stats.myshows?.today?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([login, req]) =>
            `<tr><td><strong>${esc(login)}</strong></td><td>${req}</td><td>${pct(req, total)}</td></tr>`
        ).join('');
    }, '<tr><td colspan="3" class="muted">Нет данных</td></tr>');

    // MyShows total
    rebuildTable('myshowsTotalTable', stats.myshows?.total?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([login, req]) =>
            `<tr><td><strong>${esc(login)}</strong></td><td>${req}</td><td>${pct(req, total)}</td></tr>`
        ).join('');
    }, '<tr><td colspan="3" class="muted">Нет данных</td></tr>');

    // API users today
    rebuildTable('apiUsersTodayTable', stats.api_users?.today?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([ip, req, country, city, region, flag]) =>
            `<tr><td><code>${esc(ip)}</code></td><td>${locationStr(flag, country, city, region)}</td><td>${req}</td><td>${pct(req, total)}</td></tr>`
        ).join('');
    }, '<tr><td colspan="4" class="muted">Нет данных</td></tr>');

    // API users total
    rebuildTable('apiUsersTotalTable', stats.api_users?.total?.detail, (rows) => {
        const total = rows.reduce((s, r) => s + (r[1] || 0), 0);
        return rows.map(([ip, req, country, city, region, flag]) =>
            `<tr><td><code>${esc(ip)}</code></td><td>${locationStr(flag, country, city, region)}</td><td>${req}</td><td>${pct(req, total)}</td></tr>`
        ).join('');
    }, '<tr><td colspan="4" class="muted">Нет данных</td></tr>');

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

function rebuildTable(id, rows, builder, empty) {
    const tbody = document.querySelector(`#${id} tbody`);
    if (!tbody) return;
    if (!rows || !rows.length) { tbody.innerHTML = empty; return; }
    tbody.innerHTML = builder(rows);
}

function rebuildCategoryGrid(id, detail, uniqueIps, totalReq) {
    const grid = document.getElementById(id);
    if (!grid || !detail) return;
    const entries = Object.entries(detail).sort((a, b) =>
        (totalReq?.[b[0]] || 0) - (totalReq?.[a[0]] || 0)
    );
    grid.innerHTML = entries.map(([cat, ips]) => {
        const rows = (ips || []).map(item =>
            `<tr><td><code>${esc(item.ip)}</code></td><td>${item.requests}</td></tr>`
        ).join('');
        return `<article class="category-card">
            <header><strong>${esc(cat)}</strong>
            <small class="muted"> — ${uniqueIps?.[cat] || 0} IP, ${totalReq?.[cat] || 0} запросов</small></header>
            <table><thead><tr><th>IP адрес</th><th>Запросов</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="2" class="muted">Нет данных</td></tr>'}</tbody></table>
        </article>`;
    }).join('');
    if (!entries.length) grid.innerHTML = '<p class="muted">Нет данных</p>';
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
