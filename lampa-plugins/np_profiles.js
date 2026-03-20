/**
 * NUMParser Profiles — управление профилями с синхронизацией через сервер.
 *
 * - Загружает профили с сервера (GET /timecode/profiles?token=KEY)
 * - Заменяет стандартную кнопку профиля Lampa (.open--profile)
 * - При переключении профиля: загружает file_view с сервера, обновляет np_profile_id
 * - Создание/удаление профилей синхронизируется с сервером
 * - Работает совместно с np.js (np.js читает np_profile_id с наивысшим приоритетом)
 *
 * Установка: добавить ПОСЛЕ np.js
 *   {BASE_URL}/np_profiles.js
 */
(function () {
    'use strict';

    if (window._np_profiles_started) return;
    window._np_profiles_started = true;

    // Отключаем Lampac file_view sync — таймкоды управляем мы.
    window.sync_disable = true;


    // Синхронная санация старых данных от других плагинов профилей.
    // np.js теперь читает np_profile_id (наш ключ) — lampac_profile_id не трогаем.
    // Очищаем np_active_profile если хранит числовой profile_id (стale данные от старых сессий).
    (function sanitize() {
        try {
            var rawActive = window.localStorage.getItem('np_active_profile');
            if (rawActive) {
                var parsed = JSON.parse(rawActive);
                if (parsed && parsed.profile_id && /^\d+$/.test(String(parsed.profile_id))) {
                    window.localStorage.removeItem('np_active_profile');
                }
            }
        } catch (e) {}
    })();

    // BASE_URL: из параметра ?base_url= в src, или из np.js Storage, или из src самого скрипта
    var BASE_URL = (function () {
        var src = (document.currentScript && document.currentScript.src) || '';
        var params = new URLSearchParams(src.split('?')[1] || '');
        return params.get('base_url')
            || Lampa.Storage.get('base_url_numparser', '')
            || src.replace(/\/np_profiles\.js.*$/, '');
    })();

    var FV_PREFIX  = 'np_fv_';   // локальный бэкап file_view:  np_fv_{profile_id}
    var FAV_PREFIX = 'np_fav_';  // локальный бэкап favorite:   np_fav_{profile_id}
    var ACTIVE_KEY = 'np_active_profile';  // {id, name} активного профиля

    // =========================================================================
    // API
    // =========================================================================

    function getToken() {
        return Lampa.Storage.get('numparser_api_key', '');
    }

    function apiUrl(path) {
        return BASE_URL + '/timecode/' + path + '?token=' + encodeURIComponent(getToken());
    }

    /** GET /timecode/profiles → {profiles: [{profile_id, name, timecodes_count}], limit} */
    function fetchProfiles(onDone, onFail) {
        fetch(apiUrl('profiles'))
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
            .then(onDone)
            .catch(onFail || function () {});
    }

    /** POST /timecode/profiles  body: {name, profile_id?} → {ok, profile_id, name} */
    function apiCreateProfile(name, profileId, onDone, onFail) {
        var body = { name: name };
        if (profileId) body.profile_id = profileId;
        fetch(apiUrl('profiles'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })
            .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { return Promise.reject(e.detail || 'Ошибка'); }); })
            .then(onDone)
            .catch(onFail || function () {});
    }

    /** DELETE /timecode/profiles/{profile_id} */
    function apiDeleteProfile(profileId, onDone, onFail) {
        fetch(apiUrl('profiles/' + encodeURIComponent(profileId)), { method: 'DELETE' })
            .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { return Promise.reject(e.detail || 'Ошибка'); }); })
            .then(onDone)
            .catch(onFail || function () {});
    }

    /** GET /timecode/favorite?token=KEY&profile_id=ID → {favorite: {...}|null} */
    function fetchFavorite(profileId, onDone, onFail) {
        var url = apiUrl('favorite');
        if (profileId) url += '&profile_id=' + encodeURIComponent(profileId);
        fetch(url)
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
            .then(function (data) { onDone(data.favorite); })
            .catch(onFail || function () {});
    }

    var _saveFavTimer = null;
    /** Применяет favorite с сервера. Storage.set не триггерит сохранение — мы слушаем только state:changed reason:update. */
    function applyFavorite(fav) {
        if (fav === null || fav === undefined) return;
        Lampa.Storage.set('favorite', fav);
        try { Lampa.Favorite.read(true); } catch (e) {}
    }

    /** Сохраняет текущий favorite на сервер (дебаунс 2с). Работает и без профиля (profile_id=''). */
    function scheduleSaveFavorite() {
        if (!getToken()) return;
        clearTimeout(_saveFavTimer);
        _saveFavTimer = setTimeout(function () {
            var profile = getActiveProfile();
            var profileId = profile ? profile.profile_id : '';
            var url = apiUrl('favorite');
            if (profileId) url += '&profile_id=' + encodeURIComponent(profileId);
            var fav = Lampa.Storage.get('favorite', {});
            fetch(url, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ favorite: fav }),
            }).catch(function () {});
        }, 2000);
    }

    /**
     * GET /timecode/export?token=KEY&profile_id=ID
     * Формат: {card_id: {hash: json_string}}
     * → конвертируем в file_view: {hash: {percent, time, duration, profile}}
     */
    function fetchFileView(profileId, onDone, onFail) {
        var url = apiUrl('export');
        if (profileId) url += '&profile_id=' + encodeURIComponent(profileId);
        fetch(url)
            .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
            .then(function (data) {
                var fv = {};
                Object.keys(data || {}).forEach(function (cardId) {
                    Object.keys(data[cardId] || {}).forEach(function (hash) {
                        try {
                            var tc = typeof data[cardId][hash] === 'string'
                                ? JSON.parse(data[cardId][hash])
                                : data[cardId][hash];
                            fv[hash] = { percent: tc.percent || 0, time: tc.time || 0, duration: tc.duration || 0, profile: 0 };
                        } catch (e) {}
                    });
                });
                onDone(fv);
            })
            .catch(onFail || function () {});
    }

    // =========================================================================
    // Переключение профиля
    // =========================================================================

    function getActiveProfile() {
        return Lampa.Storage.get(ACTIVE_KEY, null);
    }

    function setActiveProfile(profile) {
        Lampa.Storage.set(ACTIVE_KEY, profile);
        // Пишем в np_profile_id / np_profile_name — np.js читает их с наивысшим приоритетом.
        // Никогда не трогаем lampac_profile_id — он принадлежит Lampac profiles (levende).
        var apiId   = profile ? String(profile.profile_id) : '';
        var apiName = profile ? String(profile.name || '') : '';
        Lampa.Storage.set('np_profile_id',   apiId);
        Lampa.Storage.set('np_profile_name', apiName);
        // icon хранится внутри объекта profile в ACTIVE_KEY — updateButton читает оттуда
    }

    /**
     * Возвращает актуальный ключ localStorage для таймкодов.
     * Lampa пишет в 'file_view' (без аккаунта) или 'file_view_{id}' (с аккаунтом).
     */
    function timelineKey() {
        try {
            if (Lampa.Timeline && Lampa.Timeline.filename) return Lampa.Timeline.filename();
        } catch (e) {}
        return 'file_view';
    }

    /** Перезагружает текущую страницу Lampa — обновляет прогресс-бары и данные. */
    function refreshPage() {
        try {
            var activity = Lampa.Activity.active();
            if (activity) {
                activity.page = 1;
                Lampa.Activity.replace(activity);
            }
        } catch (e) {}
    }

    function applyFileView(profile, fileView) {
        // Пишем в тот же ключ, в который Lampa пишет во время просмотра
        var key = timelineKey();
        Lampa.Storage.set(key, fileView || {});
        if (Lampa.Timeline && Lampa.Timeline.read) Lampa.Timeline.read();

        // profile_select — официальное событие Lampa (обновляет закладки, state и перезагружает страницу)
        Lampa.Listener.send('profile_select', { profile: profile });
        // profile changed — для np.js (обновит настройки профиля)
        Lampa.Listener.send('profile', { type: 'changed', profile: profile });

        updateButton(getActiveProfile());
    }

    function switchToProfile(profile) {
        var current = getActiveProfile();

        // Бэкап текущего file_view и favorite (история, закладки)
        if (current) {
            var curKey = timelineKey();
            Lampa.Storage.set(FV_PREFIX  + current.profile_id, Lampa.Storage.get(curKey, {}));
            Lampa.Storage.set(FAV_PREFIX + current.profile_id, Lampa.Storage.get('favorite', {}));
        }

        setActiveProfile(profile);

        // Восстанавливаем favorite нового профиля и обновляем UI
        var savedFav = Lampa.Storage.get(FAV_PREFIX + profile.profile_id, null);
        if (savedFav !== null) applyFavorite(savedFav);

        if (getToken()) {
            fetchFileView(
                profile.profile_id,
                function (fv) {
                    fetchFavorite(
                        profile.profile_id,
                        function (fav) {
                            applyFavorite(fav);
                            applyFileView(profile, fv);
                            refreshPage();
                        },
                        function () {
                            applyFileView(profile, fv);
                            refreshPage();
                        }
                    );
                },
                function () {
                    applyFileView(profile, Lampa.Storage.get(FV_PREFIX + profile.profile_id, {}));
                    refreshPage();
                }
            );
        } else {
            applyFileView(profile, Lampa.Storage.get(FV_PREFIX + profile.profile_id, {}));
            refreshPage();
        }
    }

    // =========================================================================
    // UI: кнопка в хедере
    // =========================================================================

    var DEFAULT_ICON_SVG = '<svg width="48" height="49" viewBox="0 0 48 49" fill="none" xmlns="http://www.w3.org/2000/svg">'
        + '<circle cx="24.1445" cy="24.2546" r="23.8115" fill="currentColor" fill-opacity="0.2"/>'
        + '<path d="M24.1464 9.39355C19.9003 9.39355 16.4294 12.8645 16.4294 17.1106C16.4294 21.3567 19.9003 24.8277 24.1464 24.8277C28.3925 24.8277 31.8635 21.3567 31.8635 17.1106C31.8635 12.8645 28.3925 9.39355 24.1464 9.39355ZM37.3901 30.9946C37.1879 30.4891 36.9184 30.0173 36.6151 29.5792C35.0649 27.2877 32.6723 25.7712 29.9764 25.4005C29.6395 25.3669 29.2688 25.4342 28.9991 25.6364C27.5838 26.6811 25.8989 27.2203 24.1465 27.2203C22.3941 27.2203 20.7092 26.6811 19.2938 25.6364C19.0242 25.4342 18.6535 25.3331 18.3165 25.4005C15.6206 25.7712 13.1943 27.2877 11.6779 29.5792C11.3746 30.0173 11.105 30.5228 10.9028 30.9946C10.8018 31.1968 10.8354 31.4327 10.9365 31.6349C11.2061 32.1067 11.5431 32.5785 11.8464 32.9828C12.3181 33.6232 12.8236 34.196 13.3965 34.7352C13.8683 35.2069 14.4075 35.645 14.9467 36.0831C17.6089 38.0714 20.8103 39.116 24.1128 39.116C27.4153 39.116 30.6167 38.0713 33.2789 36.0831C33.8181 35.6788 34.3573 35.2069 34.8291 34.7352C35.3683 34.196 35.9074 33.6231 36.3793 32.9828C36.7162 32.5447 37.0196 32.1067 37.2891 31.6349C37.4575 31.4327 37.4912 31.1967 37.3901 30.9946Z" fill="currentColor"/>'
        + '</svg>';

    // Список доступных иконок: {id, label}
    var AVAILABLE_ICONS = [
        { id: 'id1',  label: 'Мужчина'    },
        { id: 'id2',  label: 'Женщина'    },
        { id: 'id3',  label: 'Мальчик'    },
        { id: 'id4',  label: 'Девочка'    },
        { id: 'id5',  label: 'Мышь'       },
        { id: 'id6',  label: 'Безликий'   },
        { id: 'id7',  label: 'Малыш'      },
        { id: 'id8',  label: 'Кот'        },
        { id: 'id9',  label: 'Робот'      },
        { id: 'id10', label: 'Принцесса'  },
        { id: 'id11', label: 'Дедушка'    },
        { id: 'id12', label: 'Ниндзя'     },
        { id: 'id13', label: 'Хоккеист'   },
        { id: 'id14', label: 'Тхэквондист'},
        { id: 'id15', label: 'Балерина'   },
        { id: 'id16', label: 'Супергерой' },
        { id: 'id17', label: 'Пират'      },
        { id: 'id18', label: 'Астронавт'  },
    ];

    function iconUrl(iconId) {
        if (!iconId) return null;
        var pngIds = { id1:1, id2:1, id3:1, id4:1, id5:1, id6:1, id7:1, id13:1 };
        var ext = pngIds[iconId] ? '.png' : '.svg';
        return BASE_URL + '/static/profileIcons/' + iconId + ext;
    }

    function iconHtml(iconId) {
        var url = iconUrl(iconId);
        if (!url) return DEFAULT_ICON_SVG;
        return '<img src="' + url + '" style="width:2em;height:2em;border-radius:50%;object-fit:cover"/>';
    }

    var _btn = null;

    function updateButton(profile) {
        if (!_btn) return;
        var name = profile ? profile.name : 'Профили';
        _btn.attr('title', name);
        _btn.empty();
        if (profile && profile.icon) {
            var url = iconUrl(profile.icon);
            _btn.append('<img src="' + url + '" style="width:2.2em;height:2.2em;border-radius:50%;object-fit:cover;display:block"/>');
        } else {
            _btn.append(DEFAULT_ICON_SVG);
        }
    }

    function renderButton(profile) {
        // Если кнопка уже создана, но выпала из DOM (Lampa перерисовала header) — вставляем заново
        if (_btn) {
            if (!document.contains(_btn[0])) {
                var $orig2 = $('.open--profile').not('.np-profile-btn');
                if ($orig2.length) { $orig2.replaceWith(_btn); }
                else { var $h = $('.head__actions').first(); if ($h.length) $h.prepend(_btn); }
            }
            updateButton(profile);
            return;
        }

        _btn = $('<div class="head__action selector open--profile np-profile-btn" title=""></div>');
        updateButton(profile);

        _btn.on('hover:enter hover:click hover:touch click', function () { openSelector(); });

        // На мобиле head-контроллер Lampa может быть не активен до первого взаимодействия,
        // поэтому добавляем нативный touchend который работает независимо от контроллеров
        _btn[0].addEventListener('touchend', function (e) {
            e.preventDefault();
            e.stopPropagation();
            openSelector();
        }, { passive: false });

        // Заменяем стандартную кнопку профиля Lampa
        var $orig = $('.open--profile');
        if ($orig.length) {
            $orig.replaceWith(_btn);
        } else {
            var $head = $('.head__actions').first();
            if ($head.length) $head.prepend(_btn);
        }
    }

    // =========================================================================
    // UI: меню выбора профиля
    // =========================================================================

    var _allProfiles = [];  // кэш последнего списка с сервера
    var _loading = false;   // защита от двойного нажатия

    function openSelector() {
        if (_loading) return;
        _loading = true;
        fetchProfiles(function (data) {
            _loading = false;
            _allProfiles = data.profiles || [];
            var limit = data.limit;
            var active = getActiveProfile();

            var items = _allProfiles.map(function (p) {
                var isActive = active && active.profile_id === p.profile_id;
                return {
                    title:    p.name,
                    subtitle: isActive ? '• активный' : (p.timecodes_count + ' таймкодов'),
                    selected: isActive,
                    template: 'selectbox_icon',
                    icon:     iconHtml(p.icon),
                    profile:  p,
                };
            });

            if (limit === null || _allProfiles.length < limit) {
                items.push({
                    title: 'Создать профиль',
                    template: 'selectbox_icon',
                    icon: '<svg viewBox="0 0 24 24" style="width:2em;height:2em"><use xlink:href="#sprite-plus"></use></svg>',
                    create: true,
                });
            }

            // Активируем head-контроллер прямо перед показом меню
            try { Lampa.Controller.toggle('head'); } catch(e) {}

            Lampa.Select.show({
                title: 'Профили',
                items: items,
                onSelect: function (item) {
                    if (item.create) {
                        Lampa.Select.close();
                        openCreateDialog();
                    } else {
                        openProfileMenu(item.profile);
                    }
                },
                onBack: function () { _loading = false; Lampa.Controller.toggle('content'); },
            });
        }, function () {
            _loading = false;
            Lampa.Noty.show('Не удалось загрузить профили. Проверьте API-ключ.');
        });
    }

    function openProfileMenu(profile) {
        var active = getActiveProfile();
        var isActive = active && active.profile_id === profile.profile_id;

        var items = [];
        if (!isActive) {
            items.push({ title: 'Переключиться', action: 'switch' });
        }
        items.push({ title: 'Сменить иконку',  action: 'icon'   });
        items.push({ title: 'Переименовать',    action: 'rename' });
        items.push({ title: 'Удалить профиль',  action: 'delete' });

        Lampa.Select.show({
            title: profile.name,
            items: items,
            onSelect: function (item) {
                if (item.action === 'switch') {
                    Lampa.Select.close();
                    switchToProfile(profile);
                } else if (item.action === 'icon') {
                    openIconPicker(profile);
                } else if (item.action === 'rename') {
                    Lampa.Select.close();
                    openRenameDialog(profile);
                } else if (item.action === 'delete') {
                    confirmDelete(profile);
                }
            },
            onBack: function () {
                openSelector();
            },
        });
    }

    function openIconPicker(profile) {
        var items = AVAILABLE_ICONS.map(function (ic) {
            return {
                title:    ic.label,
                template: 'selectbox_icon',
                icon:     iconHtml(ic.id),
                iconId:   ic.id,
                selected: profile.icon === ic.id,
            };
        });
        items.unshift({
            title:    'Без иконки',
            template: 'selectbox_icon',
            icon:     DEFAULT_ICON_SVG,
            iconId:   null,
            selected: !profile.icon,
        });

        Lampa.Select.show({
            title: 'Иконка профиля',
            items: items,
            onSelect: function (item) {
                var newIcon = item.iconId || null;
                fetch(apiUrl('profiles/' + encodeURIComponent(profile.profile_id)), {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ icon: newIcon || '' }),
                })
                    .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
                    .then(function () {
                        profile.icon = newIcon;
                        var active = getActiveProfile();
                        if (active && active.profile_id === profile.profile_id) {
                            active.icon = newIcon;
                            setActiveProfile(active);
                            updateButton(active);
                        }
                        Lampa.Noty.show('Иконка обновлена');
                        openProfileMenu(profile);
                    })
                    .catch(function () {
                        Lampa.Noty.show('Ошибка сохранения иконки');
                        openProfileMenu(profile);
                    });
            },
            onBack: function () {
                openProfileMenu(profile);
            },
        });
    }

    function openCreateDialog() {
        Lampa.Input.edit({
            title:  'Новый профиль',
            value:  '',
            free:   true,
            nosave: true,
        }, function (name) {
            if (!name || !name.trim()) return;
            var trimmedName = name.trim();
            // После имени — предлагаем выбрать иконку
            var iconItems = AVAILABLE_ICONS.map(function (ic) {
                return {
                    title:    ic.label,
                    template: 'selectbox_icon',
                    icon:     iconHtml(ic.id),
                    iconId:   ic.id,
                };
            });
            iconItems.unshift({
                title:    'Без иконки',
                template: 'selectbox_icon',
                icon:     DEFAULT_ICON_SVG,
                iconId:   null,
            });
            Lampa.Select.show({
                title: 'Иконка для «' + trimmedName + '»',
                items: iconItems,
                onSelect: function (item) {
                    var iconId = item.iconId || null;
                    apiCreateProfile(trimmedName, null, function (result) {
                        var profile = { profile_id: result.profile_id, name: result.name, icon: iconId };
                        if (iconId) {
                            fetch(apiUrl('profiles/' + encodeURIComponent(result.profile_id)), {
                                method: 'PATCH',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ icon: iconId }),
                            }).catch(function () {});
                        }
                        Lampa.Noty.show('Профиль «' + profile.name + '» создан');
                        switchToProfile(profile);
                    }, function (err) {
                        Lampa.Noty.show('Ошибка: ' + (err || 'не удалось создать профиль'));
                    });
                },
                onBack: function () {
                    // При отмене — создаём без иконки
                    apiCreateProfile(trimmedName, null, function (result) {
                        var profile = { profile_id: result.profile_id, name: result.name, icon: null };
                        Lampa.Noty.show('Профиль «' + profile.name + '» создан');
                        switchToProfile(profile);
                    }, function (err) {
                        Lampa.Noty.show('Ошибка: ' + (err || 'не удалось создать профиль'));
                    });
                },
            });
        });
    }

    function openRenameDialog(profile) {
        Lampa.Input.edit({
            title:  'Переименовать',
            value:  profile.name,
            free:   true,
            nosave: true,
        }, function (name) {
            if (!name || !name.trim()) return;
            var trimmed = name.trim();
            fetch(apiUrl('profiles/' + encodeURIComponent(profile.profile_id)), {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: trimmed }),
            })
                .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
                .then(function () {
                    var active = getActiveProfile();
                    if (active && active.profile_id === profile.profile_id) {
                        active.name = trimmed;
                        setActiveProfile(active);
                        updateButton(active);
                    }
                    Lampa.Noty.show('Переименовано');
                })
                .catch(function () { Lampa.Noty.show('Ошибка переименования'); });
        });
    }

    function confirmDelete(profile) {
        Lampa.Select.show({
            title: 'Удалить «' + profile.name + '»?',
            items: [
                { title: 'Да, удалить (таймкоды тоже)', action: 'yes' },
                { title: 'Отмена',                      action: 'no'  },
            ],
            onSelect: function (item) {
                if (item.action === 'no') {
                    openProfileMenu(profile);
                    return;
                }
                // action === 'yes' — удаляем
                Lampa.Select.close();
                apiDeleteProfile(profile.profile_id, function () {
                    try { localStorage.removeItem(FV_PREFIX + profile.profile_id); } catch (e) {}
                    Lampa.Noty.show('Профиль «' + profile.name + '» удалён');
                    var active = getActiveProfile();
                    var wasActive = active && active.profile_id === profile.profile_id;
                    if (wasActive) {
                        // Удалили активный — переключаемся на первый оставшийся
                        fetchProfiles(function (data) {
                            var remaining = data.profiles || [];
                            if (remaining.length > 0) {
                                switchToProfile(remaining[0]);
                            } else {
                                setActiveProfile(null);
                                Lampa.Storage.set(timelineKey(), {});
                                if (Lampa.Timeline && Lampa.Timeline.read) Lampa.Timeline.read();
                                updateButton(null);
                                refreshPage();
                            }
                        }, function () {});
                    } else {
                        // Удалили неактивный — просто обновляем список
                        openSelector();
                    }
                }, function (err) {
                    Lampa.Noty.show('Ошибка: ' + (err || 'не удалось удалить профиль'));
                });
            },
            onBack: function () {
                openProfileMenu(profile);
            },
        });
    }

    // =========================================================================
    // Инициализация
    // =========================================================================

    function init() {
        // Без токена плагин не нужен совсем
        if (!getToken()) return;

        // Блокируем bookmark.js от применения чужих закладок через WebSocket.
        // bookmark.js ставит card.received=true для карточек полученных по WebSocket от других клиентов.
        // Без NP профиля (getActiveProfile()=null) — пропускаем, bookmark.js работает штатно.
        if (!Lampa.Favorite._np_patched) {
            Lampa.Favorite._np_patched = true;
            var _favAdd    = Lampa.Favorite.add.bind(Lampa.Favorite);
            var _favRemove = Lampa.Favorite.remove.bind(Lampa.Favorite);
            Lampa.Favorite.add = function (where, card, limit) {
                if (card && card.received && getActiveProfile()) return;
                return _favAdd(where, card, limit);
            };
            Lampa.Favorite.remove = function (where, card) {
                if (card && card.received && getActiveProfile()) return;
                return _favRemove(where, card);
            };
        }

        // Загружаем профили с сервера
        fetchProfiles(function (data) {
            var profiles = data.profiles || [];
            var saved = getActiveProfile();

            // Восстанавливаем активный профиль: ищем сохранённый в списке с сервера
            var active = null;
            if (saved) {
                active = profiles.find(function (p) { return p.profile_id === saved.profile_id; }) || null;
            }
            // Если не нашли — берём первый
            if (!active && profiles.length > 0) active = profiles[0];

            if (active) {
                // Синхронизируем icon с сервера (может обновиться с другого устройства)
                active.icon = active.icon !== undefined ? active.icon : (saved ? saved.icon : null);
                setActiveProfile(active);
                renderButton(active);
                // Загружаем таймкоды и закладки для активного профиля.
                // Всегда перезаписываем file_view (даже пустым) — иначе остаются данные другого профиля из localStorage.
                // После загрузки обновляем страницу чтобы "Продолжить просмотр" перерисовался с правильными данными.
                fetchFileView(active.profile_id, function (fv) {
                    Lampa.Storage.set(timelineKey(), fv);
                    if (Lampa.Timeline && Lampa.Timeline.read) Lampa.Timeline.read();
                    fetchFavorite(active.profile_id, function (fav) {
                        applyFavorite(fav);
                        refreshPage();
                    }, function () {
                        refreshPage();
                    });
                }, function () {});

            } else {
                // Нет профилей на сервере — показываем кнопку для создания
                setActiveProfile(null);
                renderButton(null);
            }

            // Сохраняем закладки на сервер при пользовательских изменениях.
            // reason:'update' — add/remove пользователем. reason:'read' — чтение/sync — игнорируем.
            Lampa.Listener.follow('state:changed', function (e) {
                if (e.target === 'favorite' && e.reason === 'update') scheduleSaveFavorite();
            });

        }, function (status) {
            // 401/403 — токен недействителен, плагин не загружаем
            if (status === 401 || status === 403) return;
            // Сервер недоступен — используем кэш, но lampac_profile_id НЕ восстанавливаем
            // из сохранённых данных: profile_id мог быть числовым (старый plugins.js) и сломать np.js.
            // np.js корректно работает с пустым lampac_profile_id (просто без фильтрации по профилю).
            var saved = getActiveProfile();
            renderButton(saved);
        });
    }

    // При открытии full-карточки Lampac перезаписывает file_view своими данными.
    // Слушаем открытие и тихо восстанавливаем наши таймкоды с сервера (без перезагрузки страницы).
    function listenFullCardOpen() {
        Lampa.Listener.follow('activity', function (e) {
            if (e.type === 'start' && e.component === 'full') {
                setTimeout(function () {
                    var profile = getActiveProfile();
                    if (!getToken()) return;
                    fetchFileView(
                        profile ? profile.profile_id : '',
                        function (fv) {
                            Lampa.Storage.set(timelineKey(), fv || {});
                            if (Lampa.Timeline && Lampa.Timeline.read) Lampa.Timeline.read();
                        },
                        function () {}
                    );
                }, 400);
            }
        });
    }

    // listenFullCardOpen();

    // Ждём готовности Lampa
    if (window.appready) {
        init();
    } else {
        Lampa.Listener.follow('app', function (e) {
            if (e.type === 'ready') init();
        });
    }

})();
