let autoRefreshInterval = null;
const password = '{{ password }}';
let isRefreshing = false;

// Инициализация
document.addEventListener('DOMContentLoaded', () => {
    // Загрузка состояния чекбокса из localStorage
    const autoRefreshEnabled = localStorage.getItem('autoRefresh') !== 'false';
    document.getElementById('autoRefresh').checked = autoRefreshEnabled;

    if (autoRefreshEnabled) {
        startAutoRefresh();
    }

    // Обработчик чекбокса
    document.getElementById('autoRefresh').addEventListener('change', (e) => {
        localStorage.setItem('autoRefresh', e.target.checked);
        if (e.target.checked) {
            startAutoRefresh();
        } else {
            stopAutoRefresh();
        }
    });
});

function startAutoRefresh() {
    stopAutoRefresh(); // Останавливаем предыдущий интервал
    autoRefreshInterval = setInterval(refreshData, 30000); // 30 секунд
}

function stopAutoRefresh() {
    if (autoRefreshInterval) {
        clearInterval(autoRefreshInterval);
        autoRefreshInterval = null;
    }
}

function switchTab(section, period) {
    // Скрываем все табы секции
    document.querySelectorAll(`#${section}-today, #${section}-total`).forEach(el => {
        el.classList.remove('active');
    });

    // Убираем активный класс у всех кнопок секции
    document.querySelectorAll(`.tab-btn`).forEach(el => {
        el.classList.remove('active');
    });

    // Показываем нужный таб
    document.getElementById(`${section}-${period}`).classList.add('active');

    // Добавляем активный класс к нажатой кнопке
    event.target.classList.add('active');

    setTimeout(adjustTableForMobile, 50);
}

async function refreshData() {
    if (isRefreshing) return;

    const refreshBtn = document.querySelector('.refresh-btn');
    refreshBtn.classList.add('loading');
    isRefreshing = true;

    try {
        const response = await fetch(`/stats/api`, {
            headers: {
                'X-Password': password
            }
        });

        if (!response.ok) {
            throw new Error('Ошибка при загрузке данных');
        }

        const data = await response.json();
        updateDashboard(data);

        // Показываем успешное обновление
        const lastUpdate = document.getElementById('lastUpdate');
        lastUpdate.className = 'last-update update-success';
        lastUpdate.innerHTML = `✅ Данные обновлены: <span id="updateTime">${new Date().toLocaleTimeString('ru-RU')}</span>`;

        setTimeout(() => {
            lastUpdate.className = 'last-update';
        }, 2000);

    } catch (error) {
        console.error('Ошибка обновления:', error);

        // Показываем ошибку
        const lastUpdate = document.getElementById('lastUpdate');
        lastUpdate.className = 'last-update update-error';
        lastUpdate.innerHTML = `❌ Ошибка обновления: ${error.message}`;

        setTimeout(() => {
            lastUpdate.className = 'last-update';
        }, 3000);
    } finally {
        refreshBtn.classList.remove('loading');
        isRefreshing = false;
    }
}

function updateDashboard(stats) {
    // Обновляем сводку
    document.getElementById('myshowsTodayCount').textContent = stats.myshows.today.count;
    document.getElementById('myshowsTotalCount').textContent = stats.myshows.total.count;
    document.getElementById('apiUsersTodayCount').textContent = stats.api_users.today.count;
    document.getElementById('apiUsersTotalCount').textContent = stats.api_users.total.count;
    document.getElementById('categoriesTodayCount').textContent = stats.categories.today.count;
    document.getElementById('categoriesTodayRequests').textContent = stats.categories.today.total_requests;
    document.getElementById('categoriesTodayTotalBadge').textContent = stats.categories.today.total_requests;
    document.getElementById('categoriesTotalTotalBadge').textContent = stats.categories.total.total_requests;

    // Обновляем таблицу пользователей myshows сегодня
    const myshowsTodayTbody = document.querySelector('#myshowsTodayTable tbody');
    myshowsTodayTbody.innerHTML = '';
    const totalTodayRequests = stats.myshows.today.detail.reduce((sum, item) => sum + item[1], 0);

    stats.myshows.today.detail.forEach(([login, requests]) => {
        const row = document.createElement('tr');
        const percent = totalTodayRequests > 0 ? ((requests / totalTodayRequests) * 100).toFixed(1) : 0;
        row.innerHTML = `
            <td><strong>${escapeHtml(login)}</strong></td>
            <td><span class="badge badge-purple">${requests}</span></td>
            <td>${percent}%</td>
        `;
        myshowsTodayTbody.appendChild(row);
    });

    // Обновляем таблицу пользователей myshows всего
    const myshowsTotalTbody = document.querySelector('#myshowsTotalTable tbody');
    if (myshowsTotalTbody && stats.myshows?.total?.detail) {
        myshowsTotalTbody.innerHTML = '';
        const detail = stats.myshows.total.detail;
        // Считаем общий итог для расчёта доли
        const totalTotal = detail.reduce((sum, item) => sum + (item[1] || 0), 0);

        detail.forEach(([login, requests]) => {
            if (!login || !requests) return;
            const row = document.createElement('tr');
            // Рассчитываем долю
            const share = totalTotal > 0 ? ((requests / totalTotal) * 100).toFixed(1) + '%' : '';
            row.innerHTML = `
                <td><strong>${escapeHtml(login)}</strong></td>
                <td><span class="badge badge-green">${requests}</span></td>
                <td>${share}</td>  <!-- ДОБАВЛЕНА ДОЛЯ -->
            `;
            myshowsTotalTbody.appendChild(row);
        });
    }

    // Обновляем таблицу обычных пользователей сегодня
    const apiUsersTodayTbody = document.querySelector('#apiUsersTodayTable tbody');
    if (apiUsersTodayTbody && stats.api_users?.today?.detail) {
        apiUsersTodayTbody.innerHTML = '';
        const detail = stats.api_users.today.detail;
        const totalApiTodayRequests = detail.reduce((sum, item) => sum + (item[1] || 0), 0);

        detail.forEach(([ip, requests, country, city, region, flagEmoji]) => {
            if (!ip || !requests) return;
            const row = document.createElement('tr');
            const percent = totalApiTodayRequests > 0 ? ((requests / totalApiTodayRequests) * 100).toFixed(1) : 0;
            const location = `${flagEmoji || '🌍'} ${country || 'Unknown'}, ${city || 'Unknown'}${region && region !== city && region !== 'Unknown' ? ', ' + region : ''}`;
            row.innerHTML = `
                <td><code><strong>${escapeHtml(ip)}</strong></code></td>
                <td><span title="${escapeHtml(country || '')}, ${escapeHtml(region || '')}">${escapeHtml(location)}</span></td>
                <td><span class="badge badge-blue">${requests}</span></td>
                <td>${percent}%</td>
            `;
            apiUsersTodayTbody.appendChild(row);
        });
    }

    // Обновляем таблицу обычных пользователей всего
    const apiUsersTotalTbody = document.querySelector('#apiUsersTotalTable tbody');
    if (apiUsersTotalTbody && stats.api_users?.total?.detail) {
        apiUsersTotalTbody.innerHTML = '';
        const detail = stats.api_users.total.detail;
        const totalTotal = detail.reduce((sum, item) => sum + (item[1] || 0), 0);

        detail.forEach(([ip, requests, country, city, region, flagEmoji]) => {
            if (!ip || !requests) return;
            const row = document.createElement('tr');
            const location = `${flagEmoji || '🌍'} ${country || 'Unknown'}, ${city || 'Unknown'}${region && region !== city && region !== 'Unknown' ? ', ' + region : ''}`;
            const percent = totalTotal > 0 ? ((requests / totalTotal) * 100).toFixed(1) + '%' : '';
            row.innerHTML = `
                <td><code><strong>${escapeHtml(ip)}</strong></code></td>
                <td><span title="${escapeHtml(country || '')}, ${escapeHtml(region || '')}">${escapeHtml(location)}</span></td>
                <td><span class="badge badge-orange">${requests}</span></td>
                <td>${percent}%</td>
            `;
            apiUsersTotalTbody.appendChild(row);
        });
    }

    // Обновляем категории сегодня
    const categoriesTodayGrid = document.getElementById('categoriesTodayGrid');
    categoriesTodayGrid.innerHTML = '';

    // Сортируем категории по общему числу запросов
    const sortedCategoriesToday = Object.entries(stats.categories.today.detail).sort((a, b) => {
        const totalA = stats.categories.today.total_requests_per_category[a[0]] || 0;
        const totalB = stats.categories.today.total_requests_per_category[b[0]] || 0;
        return totalB - totalA;
    });

    for (const [category, ips] of sortedCategoriesToday) {
        const card = document.createElement('div');
        card.className = 'category-card';
        const uniqueIps = stats.categories.today.unique_ips[category] || 0;
        const totalRequests = stats.categories.today.total_requests_per_category[category] || 0;

        let tableRows = '';
        ips.forEach(item => {
            if (!item?.ip || !item?.requests) return;
            tableRows += `
                <tr>
                    <td><code>${escapeHtml(item.ip)}</code></td>
                    <td><span class="badge badge-green">${item.requests}</span></td>
                </tr>
            `;
        });

        card.innerHTML = `
            <h4>
                <span class="category-name">${escapeHtml(category)}</span>
                <span class="ip-count">${uniqueIps} IP</span>
            </h4>
            <table style="margin-top: 10px; font-size: 14px;">
                <thead>
                    <tr>
                        <th style="width: 60%;">IP адрес</th>
                        <th style="width: 40%;">Запросов (${totalRequests})</th>
                    </tr>
                </thead>
                <tbody>
                    ${tableRows || '<tr><td colspan="2" style="text-align: center; color: #999;">Нет данных</td></tr>'}
                </tbody>
            </table>
        `;
        categoriesTodayGrid.appendChild(card);
    }

    // Обновляем категории всего
    const categoriesTotalGrid = document.getElementById('categoriesTotalGrid');
    categoriesTotalGrid.innerHTML = '';

    const sortedCategoriesTotal = Object.entries(stats.categories.total.detail).sort((a, b) => {
        const totalA = stats.categories.total.total_requests_per_category[a[0]] || 0;
        const totalB = stats.categories.total.total_requests_per_category[b[0]] || 0;
        return totalB - totalA;
    });

    for (const [category, ips] of sortedCategoriesTotal) {
        const card = document.createElement('div');
        card.className = 'category-card';
        const uniqueIps = stats.categories.total.unique_ips[category] || 0;
        const totalRequests = stats.categories.total.total_requests_per_category[category] || 0;

        let tableRows = '';
        ips.forEach(item => {
            if (!item?.ip || !item?.requests) return;
            tableRows += `
                <tr>
                    <td><code>${escapeHtml(item.ip)}</code></td>
                    <td><span class="badge badge-green">${item.requests}</span></td>
                </tr>
            `;
        });

        card.innerHTML = `
            <h4>
                <span class="category-name">${escapeHtml(category)}</span>
                <span class="ip-count">${uniqueIps} IP</span>
            </h4>
            <table style="margin-top: 10px; font-size: 14px;">
                <thead>
                    <tr>
                        <th style="width: 60%;">IP адрес</th>
                        <th style="width: 40%;">Запросов (${totalRequests})</th>
                    </tr>
                </thead>
                <tbody>
                    ${tableRows || '<tr><td colspan="2" style="text-align: center; color: #999;">Нет данных</td></tr>'}
                </tbody>
            </table>
        `;
        categoriesTotalGrid.appendChild(card);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function adjustTableForMobile() {
    const isMobile = window.innerWidth < 600;

    // Скрываем ВСЕ таблицы и карточки
    document.querySelectorAll('.table-container, .mobile-cards').forEach(el => {
        el.style.display = 'none';
    });

    if (!isMobile) {
        // На десктопе показываем ВСЕ таблицы
        document.querySelectorAll('.table-container').forEach(el => {
            el.style.display = 'block';
        });
        return;
    }

    // На мобильном: определяем активную вкладку и показываем соответствующие карточки
    const sections = ['myshows', 'apiusers', 'categories'];

    sections.forEach(section => {
        const todayTab = document.getElementById(`${section}-today`);
        const totalTab = document.getElementById(`${section}-total`);

        if (todayTab?.classList.contains('active')) {
            // Показываем карточки "Сегодня" если они есть
            const mobileCards = document.getElementById(`${section}MobileCardsToday`);
            if (mobileCards) {
                mobileCards.style.display = 'block';
            } else {
                // Если карточек нет (например, для категорий) - показываем таблицу
                const tableContainer = todayTab.querySelector('.table-container');
                if (tableContainer) tableContainer.style.display = 'block';
            }
        } else if (totalTab?.classList.contains('active')) {
            // Показываем карточки "Всё время" если они есть
            const mobileCards = document.getElementById(`${section}MobileCardsTotal`);
            if (mobileCards) {
                mobileCards.style.display = 'block';
            } else {
                // Если карточек нет - показываем таблицу
                const tableContainer = totalTab.querySelector('.table-container');
                if (tableContainer) tableContainer.style.display = 'block';
            }
        }
    });
}

// Инициализация
document.addEventListener('DOMContentLoaded', adjustTableForMobile);
window.addEventListener('resize', adjustTableForMobile);