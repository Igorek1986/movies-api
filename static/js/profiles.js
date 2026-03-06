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

  // ── Device activation ──────────────────────────────────────
  const linkForm = document.getElementById('linkDeviceForm');
  if (linkForm) {
    linkForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const code      = document.getElementById('deviceCode').value.trim().toUpperCase();
      const profileId = parseInt(document.getElementById('deviceProfile').value);
      const statusEl  = document.getElementById('linkStatus');
      const btn       = document.getElementById('linkBtn');

      btn.disabled = true;
      statusEl.textContent = 'Привязываю…';
      statusEl.className = 'status-text';

      try {
        const res = await fetch('/device/link', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code, device_id: profileId }),
        });
        const data = await res.json();
        if (res.ok) {
          statusEl.textContent = 'Устройство привязано!';
          statusEl.className = 'status-text status-ok';
          linkForm.reset();
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
                status.textContent = evt.message;
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
          statusEl.textContent = `Импортировано: ${data.saved}`;
          statusEl.className = 'status-text status-ok';
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
          statusEl.textContent = `Импортировано: ${data.saved}`;
          statusEl.className = 'status-text status-ok';
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
