# movies-api

Бэкенд для медиаплеера [Lampa](https://lampa.mx/) / NUMParser. Управление аккаунтами, таймкодами, историей просмотров, интеграция с TMDB и MyShows. Уведомления через Telegram.

## Установка

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Igorek1986/movies-api/main/scripts/install.sh)
```

Скрипт предложит выбрать режим установки (systemd-сервис или Docker), задаст вопросы по настройке `.env` и настроит базу данных.

После установки откроется ссылка на сайт. Пример конфига nginx для HTTPS — в папке `nginx/`.

## Удаление / переключение режима

Повторно запустите тот же скрипт — он предложит удалить или переключиться между systemd и Docker.

## Требования

- Debian / Ubuntu
- PostgreSQL 14+ (устанавливается автоматически)
- Python 3.11+ (устанавливается через pyenv, для режима systemd)
- Docker (устанавливается автоматически, для режима Docker)

## Настройка `.env`

Скрипт установки заполняет `.env` интерактивно. Обязательные поля:

| Переменная | Описание |
|---|---|
| `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Реквизиты PostgreSQL |
| `TMDB_TOKEN` | Bearer-токен [TMDB API](https://www.themoviedb.org/settings/api) |
| `MYSHOWS_AUTH_URL`, `MYSHOWS_API` | Эндпоинты MyShows |
| `RELEASES_DIR` | Путь к папке с gzip-файлами категорий (относительно `$HOME`), заполняется через NUMParser |
| `ADMIN_PASSWORD` | Пароль к панели `/admin` |
| `BASE_URL` | Публичный URL сервера, например `https://example.com` |

Дополнительные поля (опционально):

| Переменная | Описание |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен Telegram-бота |
| `TELEGRAM_BOT_NAME` | Имя бота (для ссылок `t.me/...`) |
| `TELEGRAM_ADMIN_IDS` | JSON-массив Telegram ID администраторов: `[123456789]` |
| `DEBUG` | `True` — включает `/docs` (Swagger UI). По умолчанию `False` |

## Наполнение каталога — NUMParser

movies-api раздаёт фильмы и сериалы из gzip-файлов в `RELEASES_DIR`. Чтобы эти файлы автоматически обновлялись, установите **NUMParser**:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Igorek1986/NUMParser/refs/heads/main/install-numparser.sh)
```

NUMParser собирает каталог с торрент-трекеров и раскладывает файлы в нужную папку. movies-api читает их и отдаёт клиенту с обогащением через TMDB.

> Установить NUMParser можно до или после movies-api — скрипты не зависят друг от друга.

## Подключение к Lampa

1. Зарегистрируйтесь на сайте, создайте устройство.
2. Добавьте плагин в Lampa:
   `https://ваш-сервер/np.js`
3. В настройках плагина укажите API-ключ устройства.

## Telegram-бот

- `/start` — главное меню
- `/status` — информация об аккаунте и подписке

Привязка аккаунта: в настройках на сайте нажмите «Привязать Telegram» — откроется deep link, бот получит код автоматически.

После привязки бот отправляет уведомления о входе, истечении подписки и неактивности.

## Панели

| URL | Доступ |
|---|---|
| `/` | История просмотров (авторизованные пользователи) |
| `/profiles` | Настройки аккаунта, устройства, таймкоды |
| `/admin` | Управление пользователями (`ADMIN_PASSWORD`) |
| `/stats` | Статистика использования (`ADMIN_PASSWORD`) |

## Обновление с предыдущей версии

### Миграция статистики из SQLite в PostgreSQL

Если вы использовали старую версию, где статистика хранилась в `stats.sqlite`, перенесите данные в PostgreSQL:

```bash
cd ~/movies-api
poetry run python migrate_stats.py
```

Скрипт сам найдёт `stats.sqlite` рядом с собой, создаст нужные таблицы в PostgreSQL и перенесёт все данные. После успешной миграции файл `stats.sqlite` можно удалить.

## Разработка

```bash
# Установить зависимости
poetry install

# Запустить PostgreSQL
docker-compose up -d

# Запустить сервер с авто-перезагрузкой
poetry run uvicorn app.main:app --host 0.0.0.0 --port 8888 --reload
```

Для работы Swagger UI (`/docs`) установите `DEBUG=True` в `.env`.
