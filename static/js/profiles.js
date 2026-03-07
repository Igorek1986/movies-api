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

document.addEventListener('DOMContentLoaded', () => {

  // ── Copy Lampa console command ──────────────────────────────────────────────
  const copyLampaCmd = document.getElementById('copyLampaCmd');
  if (copyLampaCmd) {
    const cmd = copyLampaCmd.textContent.trim();
    copyLampaCmd.addEventListener('click', () => {
      navigator.clipboard.writeText(cmd).then(() => {
        copyLampaCmd.textContent = 'Скопировано ✓';
        setTimeout(() => { copyLampaCmd.textContent = cmd; }, 2000);
      });
    });
  }

  // ── Telegram link ───────────────────────────────────────────────────────────
  const tgLinkBtn = document.getElementById('tgLinkBtn');
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

        // Открываем Telegram deep link — бот получит /start CODE автоматически
        window.open(`https://t.me/${data.bot_name}?start=${data.code}`, '_blank');

        statusEl.textContent = 'Подтвердите в Telegram…';

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
          tgLinkBtn.disabled = false;
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

  // ── MyShows sync (SSE streaming) ───────────────────────────
  const syncForm = document.getElementById('myshowsSyncForm');
  if (syncForm) {
    syncForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const profileId = document.getElementById('syncProfileId').value;
      const login     = document.getElementById('myshowsLogin').value.trim();
      const password  = document.getElementById('myshowsPassword').value;
      const btn       = document.getElementById('syncBtn');
      const progress  = document.getElementById('syncProgress');
      const status    = document.getElementById('syncStatus');

      btn.disabled = true;
      progress.hidden = false;
      status.textContent = 'Начинаю синхронизацию…';

      const body = new FormData();
      body.append('device_id', profileId);
      body.append('login', login);
      body.append('password', password);

      try {
        const res = await fetch('/myshows/sync', { method: 'POST', body });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          status.textContent = err.detail || 'Ошибка синхронизации';
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
              case 'done':
                status.textContent = evt.message + ' Обновление…';
                setTimeout(() => location.reload(), 1500);
                break;
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
    importLampaForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const apiKey   = document.getElementById('importLampaProfile').value;
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

      try {
        const res = await fetch(`/timecode/import/lampa?token=${encodeURIComponent(apiKey)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(json),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = `Импортировано: ${data.saved}. Обновление…`;
          statusEl.className = 'status-text status-ok';
          setTimeout(() => location.reload(), 1200);
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
    importLampacForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const apiKey   = document.getElementById('importLampacProfile').value;
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

      try {
        const res = await fetch(`/timecode/import/lampac?token=${encodeURIComponent(apiKey)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(json),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = `Импортировано: ${data.saved}. Обновление…`;
          statusEl.className = 'status-text status-ok';
          setTimeout(() => location.reload(), 1200);
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

});
