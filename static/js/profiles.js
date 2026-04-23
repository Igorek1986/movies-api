// Copy token to clipboard on click
async function copyKey(el, fullKey) {
  try {
    await navigator.clipboard.writeText(fullKey);
    const prev = el.textContent;
    el.textContent = '✓ Скопировано!';
    el.style.color = 'var(--pico-color-green-500, #2e7d32)';
    setTimeout(() => { el.textContent = prev; el.style.color = ''; }, 1500);
  } catch (e) {}
}

// Toggle API key visibility in profiles table
function toggleKey(btn, fullKey) {
  const code = btn.previousElementSibling;
  if (code.dataset.shown === '1') {
    code.textContent = fullKey.slice(0, 4) + '-\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022-\u2022\u2022\u2022\u2022';
    code.dataset.shown = '0';
  } else {
    code.textContent = fullKey;
    code.dataset.shown = '1';
  }
}

// Загружает lampa-профили устройства и заполняет select
async function _loadLampaProfiles(deviceId, selectEl) {
  selectEl.innerHTML = '';
  if (!deviceId) {
    selectEl.innerHTML = '<option value="">Основной (без профиля)</option>';
    return;
  }
  try {
    const res  = await fetch(`/api/profile-ids?device_id=${deviceId}`);
    const data = await res.json();
    if (data.основной_available !== false) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = 'Основной (без профиля)';
      selectEl.appendChild(opt);
    }
    (data.profiles || []).forEach(p => {
      if (!p.profile_id) return;
      const opt   = document.createElement('option');
      opt.value   = p.profile_id;
      opt.textContent = p.name || p.profile_id;
      selectEl.appendChild(opt);
    });
  } catch {
    selectEl.innerHTML = '<option value="">Основной (без профиля)</option>';
  }
}

function _updateLampaCmd(profileId) {
  const btn = document.getElementById('copyLampaCmd');
  if (!btn) return;
  const key = profileId ? `file_view_${profileId}` : 'file_view';
  btn.dataset.cmd = `copy(localStorage.getItem('${key}'))`;
  btn.textContent = btn.dataset.cmd;
}

document.addEventListener('DOMContentLoaded', () => {

  // ── Copy Lampa console command ──────────────────────────────────────────────
  const copyLampaCmd = document.getElementById('copyLampaCmd');
  if (copyLampaCmd) {
    copyLampaCmd.addEventListener('click', () => {
      const cmd = (copyLampaCmd.dataset.cmd || copyLampaCmd.textContent).trim();
      navigator.clipboard.writeText(cmd).then(() => {
        copyLampaCmd.textContent = 'Скопировано ✓';
        setTimeout(() => { copyLampaCmd.textContent = cmd; }, 2000);
      });
    });
  }

  // ── Telegram link ───────────────────────────────────────────────────────────
  const tgLinkBtn = document.getElementById('tgLinkBtn');
  const tgConsentCheck = document.getElementById('tgConsentCheck');
  if (tgConsentCheck) {
    tgConsentCheck.addEventListener('change', () => {
      tgLinkBtn.disabled = !tgConsentCheck.checked;
    });
  }
  if (tgLinkBtn) {
    tgLinkBtn.addEventListener('click', async () => {
      const statusEl = document.getElementById('tgLinkStatus');
      tgLinkBtn.disabled = true;
      statusEl.textContent = 'Открываю Telegram…';
      statusEl.className = 'status-text';

      try {
        const res  = await fetch('/telegram/generate-link-code', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
          statusEl.textContent = data.detail || 'Ошибка';
          statusEl.className = 'status-text status-err';
          tgLinkBtn.disabled = false;
          return;
        }

        const tgUrl = `https://t.me/${data.bot_name}?start=${data.code}`;
        const botUrl = `https://t.me/${data.bot_name}`;
        statusEl.innerHTML =
          `<a href="${tgUrl}" target="_blank" rel="noopener" role="button" class="secondary" style="display:inline-block;margin-bottom:.5rem">Открыть Telegram</a><br>` +
          `<span style="font-size:.85rem">Нажмите кнопку «Открыть Telegram» — код отправится автоматически.</span><br>` +
          `<span style="font-size:.85rem">Или перейдите в <a href="${botUrl}" target="_blank" rel="noopener">бот</a> и отправьте код: <strong style="letter-spacing:.1em">${data.code}</strong></span>`;

        // Поллинг пока пользователь не привяжется
        const pollInterval = setInterval(async () => {
          try {
            const r = await fetch('/telegram/status');
            const d = await r.json();
            if (d.linked) {
              clearInterval(pollInterval);
              statusEl.textContent = 'Telegram привязан!';
              statusEl.className = 'status-text status-ok';
              setTimeout(() => location.reload(), 1000);
            }
          } catch { /* игнорируем */ }
        }, 3000);

        // Прекращаем поллинг по истечении TTL
        setTimeout(() => {
          clearInterval(pollInterval);
          tgLinkBtn.disabled = tgConsentCheck ? !tgConsentCheck.checked : false;
          if (statusEl.className !== 'status-text status-ok') {
            statusEl.textContent = 'Время истекло. Попробуйте снова.';
            statusEl.className = 'status-text status-err';
          }
        }, data.expires_in * 1000);

      } catch {
        statusEl.textContent = 'Ошибка соединения';
        statusEl.className = 'status-text status-err';
        tgLinkBtn.disabled = false;
      }
    });
  }



  // ── Device activation ──────────────────────────────────────
  const linkForm = document.getElementById('linkDeviceForm');
  if (linkForm) {
    const deviceSelect   = document.getElementById('deviceProfile');
    const newDeviceRow   = document.getElementById('newDeviceNameRow');

    // Показываем поле имени если выбрано «Создать новое»
    if (deviceSelect) {
      deviceSelect.addEventListener('change', () => {
        newDeviceRow.style.display = deviceSelect.value === 'new' ? '' : 'none';
      });
    }

    linkForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const code     = document.getElementById('deviceCode').value.trim();
      const selected = deviceSelect ? deviceSelect.value : '';
      const statusEl = document.getElementById('linkStatus');
      const btn      = document.getElementById('linkBtn');

      if (!/^\d{6}$/.test(code)) {
        statusEl.textContent = 'Код должен состоять из 6 цифр';
        statusEl.className = 'status-text status-err';
        return;
      }

      btn.disabled = true;
      statusEl.textContent = 'Привязываю…';
      statusEl.className = 'status-text';

      try {
        const body = { code };
        if (selected === 'new') {
          body.device_name = (document.getElementById('newDeviceName').value.trim()) || 'Новое устройство';
        } else {
          body.device_id = parseInt(selected);
        }

        const res = await fetch('/device/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = 'Устройство привязано!';
          statusEl.className = 'status-text status-ok';
          linkForm.reset();
          if (newDeviceRow) newDeviceRow.style.display = 'none';
          setTimeout(() => location.reload(), 1500);
        } else {
          statusEl.textContent = data.detail || 'Ошибка';
          statusEl.className = 'status-text status-err';
        }
      } catch {
        statusEl.textContent = 'Ошибка соединения';
        statusEl.className = 'status-text status-err';
      } finally {
        btn.disabled = false;
      }
    });
  }

  // Реестр пар [devSelect, pidSelect] для обновления при создании профиля
  const _profileSelectPairs = [];
  function _refreshAllProfileSelects() {
    _profileSelectPairs.forEach(([devSel, pidSel]) => {
      const did = devSel.options[devSel.selectedIndex]?.dataset?.deviceId
               || devSel.value;
      _loadLampaProfiles(did, pidSel);
    });
  }

  // ── MyShows sync (SSE streaming) ───────────────────────────
  const syncForm = document.getElementById('myshowsSyncForm');
  if (syncForm) {
    const syncDeviceSel  = document.getElementById('syncProfileId');
    const syncLampaSel   = document.getElementById('syncLampaProfile');
    if (syncDeviceSel && syncLampaSel) {
      _profileSelectPairs.push([syncDeviceSel, syncLampaSel]);
      const initialDeviceId = syncDeviceSel.options[syncDeviceSel.selectedIndex]?.dataset.deviceId;
      _loadLampaProfiles(initialDeviceId, syncLampaSel);
      syncDeviceSel.addEventListener('change', () => {
        const did = syncDeviceSel.options[syncDeviceSel.selectedIndex]?.dataset.deviceId;
        _loadLampaProfiles(did, syncLampaSel);
      });
    }

    syncForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const profileId  = document.getElementById('syncProfileId').value;
      const lampaProfile = document.getElementById('syncLampaProfile')?.value || '';
      const login      = document.getElementById('myshowsLogin').value.trim();
      const password   = document.getElementById('myshowsPassword').value;
      const btn        = document.getElementById('syncBtn');
      const progress   = document.getElementById('syncProgress');
      const status     = document.getElementById('syncStatus');

      btn.disabled = true;
      progress.hidden = false;
      status.textContent = 'Начинаю синхронизацию…';

      const body = new FormData();
      body.append('device_id', profileId);
      body.append('login', login);
      body.append('password', password);
      body.append('profile_id', lampaProfile);

      try {
        const res = await fetch('/myshows/sync', { method: 'POST', body });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          const detail = err.detail || {};
          if (res.status === 429 && detail.wait_sec) {
            let left = detail.wait_sec;
            const fmt = s => s >= 60 ? `${Math.floor(s/60)} мин. ${s%60} сек.` : `${s} сек.`;
            status.textContent = `${detail.message} Подождите ещё ${fmt(left)}`;
            const timer = setInterval(() => {
              left--;
              if (left <= 0) {
                clearInterval(timer);
                status.textContent = 'Можно повторить синхронизацию.';
                btn.disabled = false;
              } else {
                status.textContent = `${detail.message} Подождите ещё ${fmt(left)}`;
              }
            }, 1000);
          } else {
            status.textContent = (typeof detail === 'string' ? detail : detail.message) || 'Ошибка синхронизации';
            btn.disabled = false;
          }
          return;
        }

        // Read SSE stream
        const reader  = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop(); // keep incomplete last line

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let evt;
            try { evt = JSON.parse(line.slice(6)); } catch { continue; }

            switch (evt.type) {
              case 'status':
                status.textContent = evt.message;
                break;
              case 'stage':
                if (evt.stage === 'movies') {
                  status.textContent = evt.message
                    || `Фильмы: ${evt.current}/${evt.total}`;
                } else if (evt.stage === 'shows') {
                  status.textContent = `Сериалы: ${evt.current}/${evt.total}`
                    + (evt.name ? ` — ${evt.name}` : '');
                }
                break;
              case 'done': {
                const notFound = evt.not_found || [];
                const trimmed  = evt.trimmed  || 0;
                let html = evt.message;
                if (trimmed) {
                  html += `<br><b style="color:#d97706">⚠️ Удалено ${trimmed} старых таймкодов — превышен лимит.</b>`;
                  status.className = 'status-text status-warn';
                }
                if (notFound.length) {
                  const list = notFound.map(s => `<li>${s}</li>`).join('');
                  html += `<br><b>Не найдено в TMDB (${notFound.length}):</b><ul style="margin:.4em 0 0 1em;text-align:left">${list}</ul>`;
                }
                if (!trimmed && !notFound.length) html += ' Обновление…';
                status.innerHTML = html;
                setTimeout(() => location.reload(), trimmed ? 10000 : notFound.length ? 6000 : 1500);
                break;
              }
              case 'error':
                status.textContent = '❌ ' + evt.message;
                break;
            }
          }
        }
      } catch (err) {
        status.textContent = 'Ошибка соединения: ' + err.message;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ── Import from Lampa ──────────────────────────────────────
  const importLampaForm = document.getElementById('importLampaForm');
  if (importLampaForm) {
    const lampaDevSel = document.getElementById('importLampaProfile');
    const lampaPidSel = document.getElementById('importLampaProfilePid');
    if (lampaDevSel && lampaPidSel) {
      _profileSelectPairs.push([lampaDevSel, lampaPidSel]);
      const _loadAndSyncCmd = async (deviceId) => {
        await _loadLampaProfiles(deviceId, lampaPidSel);
        _updateLampaCmd(lampaPidSel.value);
      };
      _loadAndSyncCmd(lampaDevSel.options[0]?.dataset?.deviceId);
      lampaDevSel.addEventListener('change', () =>
        _loadAndSyncCmd(lampaDevSel.options[lampaDevSel.selectedIndex]?.dataset?.deviceId));
      lampaPidSel.addEventListener('change', () => _updateLampaCmd(lampaPidSel.value));
    }
    _updateLampaCmd(lampaPidSel?.value || '');

    importLampaForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const apiKey   = document.getElementById('importLampaProfile').value;
      const pid      = document.getElementById('importLampaProfilePid')?.value || '';
      const raw      = document.getElementById('importLampaData').value.trim();
      const statusEl = document.getElementById('importLampaStatus');
      const btn      = document.getElementById('importLampaBtn');

      let json;
      try { json = JSON.parse(raw); } catch {
        statusEl.textContent = 'Невалидный JSON';
        statusEl.className = 'status-text status-err';
        return;
      }

      btn.disabled = true;
      statusEl.textContent = 'Импортирую…';
      statusEl.className = 'status-text';

      const pidParam = pid ? `&profile_id=${encodeURIComponent(pid)}` : '';
      try {
        const res = await fetch(`/timecode/import/lampa?token=${encodeURIComponent(apiKey)}${pidParam}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(json),
        });
        const data = await res.json();
        if (res.ok) {
          if (data.trimmed) {
            statusEl.innerHTML = `Импортировано: ${data.saved}.<br><b style="color:#d97706">⚠️ Удалено ${data.trimmed} старых таймкодов — превышен лимит.</b>`;
            statusEl.className = 'status-text status-warn';
            setTimeout(() => location.reload(), 10000);
          } else {
            statusEl.textContent = `Импортировано: ${data.saved}. Обновление…`;
            statusEl.className = 'status-text status-ok';
            setTimeout(() => location.reload(), 1200);
          }
        } else {
          statusEl.textContent = data.detail || 'Ошибка';
          statusEl.className = 'status-text status-err';
          btn.disabled = false;
        }
      } catch {
        statusEl.textContent = 'Ошибка соединения';
        statusEl.className = 'status-text status-err';
        btn.disabled = false;
      }
    });
  }

  // ── Import from Lampac ─────────────────────────────────────
  const importLampacForm = document.getElementById('importLampacForm');
  if (importLampacForm) {
    const lampacDevSel = document.getElementById('importLampacProfile');
    const lampacPidSel = document.getElementById('importLampacProfilePid');
    if (lampacDevSel && lampacPidSel) {
      _profileSelectPairs.push([lampacDevSel, lampacPidSel]);
      _loadLampaProfiles(lampacDevSel.options[0]?.dataset?.deviceId, lampacPidSel);
      lampacDevSel.addEventListener('change', () =>
        _loadLampaProfiles(lampacDevSel.options[lampacDevSel.selectedIndex]?.dataset?.deviceId, lampacPidSel));
    }

    importLampacForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const apiKey   = document.getElementById('importLampacProfile').value;
      const pid      = document.getElementById('importLampacProfilePid')?.value || '';
      const raw      = document.getElementById('importLampacData').value.trim();
      const statusEl = document.getElementById('importLampacStatus');
      const btn      = document.getElementById('importLampacBtn');

      let json;
      try { json = JSON.parse(raw); } catch {
        statusEl.textContent = 'Невалидный JSON';
        statusEl.className = 'status-text status-err';
        return;
      }

      btn.disabled = true;
      statusEl.textContent = 'Импортирую…';
      statusEl.className = 'status-text';

      const pidParam = pid ? `&profile_id=${encodeURIComponent(pid)}` : '';
      try {
        const res = await fetch(`/timecode/import/lampac?token=${encodeURIComponent(apiKey)}${pidParam}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(json),
        });
        const data = await res.json();
        if (res.ok) {
          if (data.trimmed) {
            statusEl.innerHTML = `Импортировано: ${data.saved}.<br><b style="color:#d97706">⚠️ Удалено ${data.trimmed} старых таймкодов — превышен лимит.</b>`;
            statusEl.className = 'status-text status-warn';
            setTimeout(() => location.reload(), 10000);
          } else {
            statusEl.textContent = `Импортировано: ${data.saved}. Обновление…`;
            statusEl.className = 'status-text status-ok';
            setTimeout(() => location.reload(), 1200);
          }
        } else {
          statusEl.textContent = data.detail || 'Ошибка';
          statusEl.className = 'status-text status-err';
          btn.disabled = false;
        }
      } catch {
        statusEl.textContent = 'Ошибка соединения';
        statusEl.className = 'status-text status-err';
        btn.disabled = false;
      }
    });
  }

  // ── Lampa Profiles management ──────────────────────────────
  const lpDeviceSel  = document.getElementById('lpDeviceSelect');
  const lpList       = document.getElementById('lpProfilesList');
  const lpCreateForm = document.getElementById('lpCreateForm');
  const lpQuota      = document.getElementById('lpQuota');

  async function _lpLoad() {
    const deviceId = lpDeviceSel?.value;
    if (!deviceId || !lpList) return;
    try {
      const res  = await fetch(`/api/profile-ids?device_id=${deviceId}`);
      const data = await res.json();
      const profiles = data.profiles || [];

      if (!profiles.length) {
        lpList.innerHTML = '<p class="muted small">Профилей нет. Они появятся автоматически при первом использовании или после создания вручную.</p>';
      } else {
        lpList.innerHTML = `<div style="display:flex;flex-direction:column;gap:.4rem">${
          profiles.map(p => `
            <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;padding:.5rem .75rem;
                        border:1px solid var(--pico-table-border-color);border-radius:var(--pico-border-radius)">
              <div style="flex:1;min-width:0">
                <div style="font-weight:500">${p.name || p.profile_id || 'Основной'}</div>
                <div style="font-size:.8rem;color:var(--pico-muted-color)">
                  ${p.profile_id ? `ID: <code>${p.profile_id}</code> · ` : ''}таймкодов: <strong>${p.timecodes_count}</strong>
                </div>
                ${p.profile_id ? `<div style="margin-top:.35rem;display:flex;gap:.35rem;flex-wrap:wrap">
                  <button class="btn-sm lp-child-btn ${p.child ? '' : 'outline'}" data-pid="${p.profile_id}" data-child="${p.child}" style="${p.child ? 'background:var(--pico-primary);color:#fff;border-color:var(--pico-primary)' : ''}">Детский</button>
                  ${(function(){ const hasParams = p.params && Object.keys(p.params).length > 0; return `<button class="btn-sm lp-params-btn ${hasParams ? '' : 'outline'}" data-pid="${p.profile_id}" data-params='${JSON.stringify(p.params || {})}' style="${hasParams ? 'background:var(--pico-primary);color:#fff;border-color:var(--pico-primary)' : ''}">Параметры</button>`; })()}
                </div>` : ''}
              </div>
              <div style="display:flex;gap:.35rem;flex-shrink:0">
                ${p.profile_id ? `<button class="btn-sm outline lp-rename-btn" data-pid="${p.profile_id}" data-name="${p.name || ''}" title="Переименовать">✎</button>` : ''}
                <button class="btn-sm outline secondary lp-clear-btn" data-pid="${p.profile_id}" data-name="${p.name || p.profile_id || 'Основной'}" ${p.timecodes_count === 0 ? 'disabled' : ''}>Очистить</button>
                ${p.profile_id ? `<button class="btn-sm outline danger-btn lp-delete-btn" data-pid="${p.profile_id}" data-name="${p.name || p.profile_id}">Удалить</button>` : ''}
              </div>
            </div>`
          ).join('')
        }</div>`;

        lpList.querySelectorAll('.lp-child-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            const newChild = btn.dataset.child !== 'true';
            await fetch('/api/lampa-profile', {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ device_id: parseInt(deviceId), profile_id: btn.dataset.pid, child: newChild }),
            });
            _lpLoad();
          });
        });

        const dialog     = document.getElementById('lpParamsDialog');
        const textarea   = document.getElementById('lpParamsTextarea');
        const errorEl    = document.getElementById('lpParamsError');
        const saveBtn    = document.getElementById('lpParamsSaveBtn');
        const cancelBtn  = document.getElementById('lpParamsCancelBtn');

        lpList.querySelectorAll('.lp-params-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const params = JSON.parse(btn.dataset.params || '{}');
            const inner = JSON.stringify(params).replace(/^\{|\}$/g, '').trim();
            textarea.value = inner;
            errorEl.style.display = 'none';
            errorEl.textContent = '';
            dialog._pid = btn.dataset.pid;
            dialog.showModal();
          });
        });

        cancelBtn.onclick = () => dialog.close();

        saveBtn.onclick = async () => {
          const raw = textarea.value.trim();
          let parsed;
          try {
            parsed = raw ? JSON.parse('{' + raw + '}') : {};
          } catch(e) {
            errorEl.textContent = 'Ошибка JSON: ' + e.message;
            errorEl.style.display = 'block';
            return;
          }
          saveBtn.disabled = true;
          await fetch('/api/lampa-profile', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device_id: parseInt(deviceId), profile_id: dialog._pid, params: parsed }),
          });
          saveBtn.disabled = false;
          dialog.close();
          _lpLoad();
        };

        lpList.querySelectorAll('.lp-rename-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            const name = window.prompt('Новое название:', btn.dataset.name || '');
            if (name === null) return;
            await fetch('/api/profile-name', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ device_id: parseInt(deviceId), profile_id: btn.dataset.pid, name: name.trim() }),
            });
            _lpLoad();
          });
        });

        lpList.querySelectorAll('.lp-clear-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            if (!confirm(`Очистить таймкоды профиля «${btn.dataset.name}»? Профиль останется.`)) return;
            btn.disabled = true;
            await fetch(`/api/lampa-profile/clear?device_id=${deviceId}&profile_id=${encodeURIComponent(btn.dataset.pid)}`, { method: 'POST' });
            _lpLoad();
          });
        });

        lpList.querySelectorAll('.lp-delete-btn').forEach(btn => {
          btn.addEventListener('click', async () => {
            if (!confirm(`Удалить профиль «${btn.dataset.name}» и все его таймкоды?`)) return;
            await fetch(`/api/lampa-profile?device_id=${deviceId}&profile_id=${encodeURIComponent(btn.dataset.pid)}`, { method: 'DELETE' });
            _lpLoad();
            _refreshAllProfileSelects();
          });
        });
      }

      // Обновляем счётчик таймкодов в строке таблицы устройств
      const total = profiles.reduce((s, p) => s + (p.timecodes_count || 0), 0);
      const row = document.querySelector(`tr[data-device-id="${deviceId}"]`);
      if (row) {
        const cell = row.querySelector('td[data-label="Таймкодов"]');
        if (cell) cell.textContent = total;
      }

      // Обновляем квоту
      if (lpQuota) {
        const r = await fetch('/api/lampa-profile/quota?device_id=' + deviceId).catch(() => null);
        if (r?.ok) {
          const q = await r.json();
          lpQuota.textContent = q.limit === null ? `Профилей: ${q.count} (без лимита)` : `Профилей: ${q.count} из ${q.limit}`;
        }
      }
    } catch { /* ignore */ }
  }

  if (lpDeviceSel) {
    lpDeviceSel.addEventListener('change', _lpLoad);
    _lpLoad();
  }

  if (lpCreateForm) {
    lpCreateForm.addEventListener('submit', async e => {
      e.preventDefault();
      const name      = document.getElementById('lpProfileName').value.trim();
      const profileId = document.getElementById('lpProfileIdInput').value.trim();
      const statusEl  = document.getElementById('lpCreateStatus');
      const btn       = document.getElementById('lpCreateBtn');

      btn.disabled = true;
      statusEl.textContent = '';
      try {
        const res  = await fetch('/api/lampa-profile/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_id: parseInt(lpDeviceSel.value), name, profile_id: profileId || null }),
        });
        const data = await res.json();
        if (res.ok) {
          document.getElementById('lpProfileName').value = '';
          document.getElementById('lpProfileIdInput').value = '';
          statusEl.textContent = `Профиль создан: ${data.profile_id}`;
          statusEl.className = 'status-text status-ok';
          _lpLoad();
          _refreshAllProfileSelects();
        } else {
          statusEl.textContent = data.detail || 'Ошибка';
          statusEl.className = 'status-text status-err';
        }
      } catch {
        statusEl.textContent = 'Ошибка соединения';
        statusEl.className = 'status-text status-err';
      } finally {
        btn.disabled = false;
      }
    });
  }

});
