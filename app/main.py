import asyncio
import gzip
import json
import logging
import os
import re
import httpx
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any, Dict, Tuple
from logging import DEBUG, INFO

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Query, status, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import init_db, async_session_maker
from app.db.models import MediaCard
from app.config import get_settings
from app.api import auth, myshows_sync, profiles, timecodes as timecodes_router
from app.api.dependencies import get_profile_by_api_key
from app.api.timecodes import load_profile_timecodes, get_watched_movie_ids
from app.utils import lampa_hash, build_episode_hash_string
from app.db.database import get_db

settings = get_settings()

from app import myshows
from app import stats

# Загрузка переменных окружения
load_dotenv()
TMDB_TOKEN = os.getenv("TMDB_TOKEN")
releases_dir_env = os.getenv("RELEASES_DIR", "NUMParser/public")
BANNED_PATTERNS = json.loads(os.getenv("BANNED_PATTERNS", "[]"))


# Проверяем, абсолютный ли путь
if os.path.isabs(releases_dir_env):
    RELEASES_DIR = Path(releases_dir_env)
else:
    RELEASES_DIR = Path.home() / releases_dir_env

# Получаем путь к директории, где находится текущий скрипт
BASE_DIR = Path(__file__).parent.parent
BLOCKED_JSON_PATH = BASE_DIR / "blocked.json"
tmdb_cache: Dict[Tuple[str, int], Any] = None
with open(BLOCKED_JSON_PATH, "r", encoding="utf-8") as f:
    BLOCKED_RESPONSE = json.load(f)

STATIC_DIR = BASE_DIR / "static"
PLUGINS_DIR = BASE_DIR / "lampa-plugins"
PLUGINS_DIR.mkdir(exist_ok=True)
# Настройка логирования
DEBUG_MODE = os.getenv("DEBUG", "False").lower() == "true"
logging.basicConfig(
    level=DEBUG if DEBUG_MODE else INFO,  # Уровень логирования
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],  # Вывод в консоль
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Собственный обработчик жизненного цикла приложения"""
    global tmdb_cache

    stats.init_stats()

    # === Startup ===
    print("🔍 Connecting to:", settings.DATABASE_URL)
    await init_db()
    print("✅ Database tables created")

    # Загрузка TMDB-кэша из PostgreSQL
    tmdb_cache = await load_cache_from_db()
    logger.info(f"TMDB кэш загружен из БД, записей: {len(tmdb_cache)}")
    logger.info(f"Рабочая директория: {BASE_DIR}")
    logger.info(f"Директория с релизами: {RELEASES_DIR}")

    yield  # Приложение работает


# app = FastAPI()
app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse("static/favicon/favicon.ico", media_type="image/x-icon")


app.include_router(auth.router)
app.include_router(profiles.router)
app.include_router(timecodes_router.router)
app.include_router(myshows.router)
app.include_router(stats.router)
app.include_router(myshows_sync.router)


@app.middleware("http")
async def block_banned_origins(request: Request, call_next):
    origin = request.headers.get("origin")

    if is_banned_origin(origin):
        logger.warning(f"Blocked request from origin: {origin}")

        return JSONResponse(
            status_code=200,
            content=BLOCKED_RESPONSE,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
                "Access-Control-Allow-Origin": origin or "*",
                "Access-Control-Allow-Credentials": "true",
            },
        )

    return await call_next(request)


@app.middleware("http")
async def serve_lampa_plugins(request: Request, call_next):
    if request.method == "GET":
        rel = request.url.path.lstrip("/")
        if rel:
            try:
                plugin_path = (PLUGINS_DIR / rel).resolve()
                plugin_path.relative_to(PLUGINS_DIR.resolve())
                if plugin_path.is_file():
                    return FileResponse(str(plugin_path))
            except (ValueError, OSError):
                pass
    return await call_next(request)


def is_banned_origin(origin: str | None) -> bool:
    if not origin or origin == "null":
        return False

    origin = origin.lower()
    return any(pattern.lower() in origin for pattern in BANNED_PATTERNS)


logger.debug("Настройки окружения загружены успешно")

executor = ThreadPoolExecutor(max_workers=10)  # Пул потоков для запросов


def _extract_tmdb_fields(media_type: str, data: dict) -> dict:
    """Извлекает только нужные поля из полного ответа TMDB API для in-memory кэша."""
    base = {
        "poster_path": data.get("poster_path", ""),
        "backdrop_path": data.get("backdrop_path", ""),
        "overview": data.get("overview", ""),
        "vote_average": data.get("vote_average", 0),
    }
    if media_type == "movie":
        base.update({
            "title": data.get("title", ""),
            "original_title": data.get("original_title", ""),
            "release_date": data.get("release_date", ""),
        })
    else:  # tv
        base.update({
            "name": data.get("name", ""),
            "original_name": data.get("original_name", ""),
            "first_air_date": data.get("first_air_date", ""),
            "last_air_date": data.get("last_air_date", ""),
            "number_of_seasons": data.get("number_of_seasons", 0),
            "seasons": data.get("seasons", []),
        })
    return base


async def load_cache_from_db() -> Dict[Tuple[str, int], Any]:
    """Загружает TMDB-кэш из таблицы media_cards."""
    try:
        async with async_session_maker() as db:
            result = await db.execute(select(MediaCard))
            rows = result.scalars().all()

        cache: Dict[Tuple[str, int], Any] = {}
        for mc in rows:
            key = (mc.media_type, mc.tmdb_id)
            if mc.media_type == "movie":
                cache[key] = {
                    "title": mc.title or "",
                    "original_title": mc.original_title or "",
                    "poster_path": mc.poster_path or "",
                    "backdrop_path": mc.backdrop_path or "",
                    "overview": mc.overview or "",
                    "vote_average": mc.vote_average or 0,
                    "release_date": mc.release_date or "",
                }
            else:  # tv
                seasons = []
                if mc.seasons_json:
                    try:
                        seasons = json.loads(mc.seasons_json)
                    except Exception:
                        pass
                cache[key] = {
                    "name": mc.title or "",
                    "original_name": mc.original_title or "",
                    "poster_path": mc.poster_path or "",
                    "backdrop_path": mc.backdrop_path or "",
                    "overview": mc.overview or "",
                    "vote_average": mc.vote_average or 0,
                    "first_air_date": mc.release_date or "",
                    "last_air_date": mc.last_air_date or "",
                    "number_of_seasons": mc.number_of_seasons or 0,
                    "seasons": seasons,
                }
        return cache
    except Exception as e:
        logger.error(f"Ошибка загрузки TMDB кэша из БД: {e}")
        return {}


async def upsert_tmdb_cache(media_type: str, tmdb_id: int, data: dict) -> None:
    """Сохраняет TMDB-данные в media_cards (upsert)."""
    card_id = f"{tmdb_id}_{media_type}"
    if media_type == "movie":
        date_val = data.get("release_date") or ""
        values = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": data.get("title") or "",
            "original_title": data.get("original_title") or "",
            "poster_path": data.get("poster_path") or "",
            "year": date_val[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date_val,
        }
    else:  # tv
        date_val = data.get("first_air_date") or ""
        seasons = data.get("seasons")
        values = {
            "card_id": card_id,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "title": data.get("name") or "",
            "original_title": data.get("original_name") or "",
            "poster_path": data.get("poster_path") or "",
            "year": date_val[:4],
            "backdrop_path": data.get("backdrop_path") or "",
            "overview": data.get("overview") or "",
            "vote_average": data.get("vote_average"),
            "release_date": date_val,
            "last_air_date": data.get("last_air_date") or "",
            "number_of_seasons": data.get("number_of_seasons"),
            "seasons_json": json.dumps(seasons, ensure_ascii=False) if seasons else None,
        }

    try:
        async with async_session_maker() as db:
            stmt = pg_insert(MediaCard).values([values])
            stmt = stmt.on_conflict_do_update(
                index_elements=["card_id"],
                set_={k: stmt.excluded[k] for k in values if k != "card_id"},
            )
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        logger.error(f"Ошибка upsert MediaCard {card_id}: {e}")


def convert_date(date_str: str) -> str:
    """Конвертирует дату из формата 'дд.мм.гггг' в 'гггг-мм-дд'"""
    try:
        return datetime.strptime(date_str, "%d.%m.%Y").strftime("%Y-%m-%d")
    except:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
        except:
            return date_str


def get_quality_text(video_quality: int) -> str:
    """Возвращает текстовое описание качества"""
    quality_map = {
        (0, 99): "SD",
        100: "WEBDL 720p",
        101: "BDRip 720p",
        (102, 199): "BDRip HEVC 720p",
        200: "WEBDL 1080p",
        201: "BDRip 1080p",
        202: "BDRip HEVC 1080p",
        203: "Remux 1080p",
        (204, 299): "1080p",
        300: "WEBDL 2160p",
        301: "WEBDL HDR 2160p",
        302: "WEBDL DV 2160p",
        303: "BDRip 2160p",
        304: "BDRip HDR 2160p",
        305: "BDRip DV 2160p",
        306: "Remux 2160p",
        307: "Remux HDR 2160p",
        308: "Remux DV 2160p",
        (309, float("inf")): "2160p",
    }

    for k, v in quality_map.items():
        if isinstance(k, tuple):
            if k[0] <= video_quality <= k[1]:
                return v
        elif video_quality == k:
            return v
    return ""


def load_data(category: str):
    """Загружает данные из файла в releases/"""
    path = RELEASES_DIR / f"{category}.json"

    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    try:
        with gzip.open(path, "rt") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки файла {path}: {str(e)}")
        raise


async def fetch_tmdb_batch(requests_list: list) -> dict:
    """Пакетный запрос к TMDB API"""
    results = {}

    def make_request(media_type, tmdb_id):
        try:
            url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
            headers = {"Authorization": TMDB_TOKEN}
            params = {"language": "ru"}
            response = requests.get(url, headers=headers, params=params, timeout=5)
            response.raise_for_status()
            return (media_type, tmdb_id), response.json()
        except Exception as e:
            logger.error(f"Ошибка запроса к TMDB {media_type}/{tmdb_id}: {str(e)}")
            return (media_type, tmdb_id), None

    # Выполняем запросы в пуле потоков
    loop = asyncio.get_running_loop()
    futures = [
        loop.run_in_executor(executor, make_request, media_type, tmdb_id)
        for media_type, tmdb_id in requests_list
    ]

    for future in asyncio.as_completed(futures):
        key, data = await future
        if data:  # Сохраняем только успешные ответы
            media_type, tmdb_id = key
            cleaned = _extract_tmdb_fields(media_type, data)
            results[key] = cleaned
            tmdb_cache[key] = cleaned
            asyncio.create_task(upsert_tmdb_cache(media_type, tmdb_id, data))

    return results


def enhance_with_tmdb(item: dict, tmdb_data: dict) -> dict:
    """Обогащает данные из файла информацией с TMDB"""
    if not tmdb_data:
        return None

    result = {
        "id": item["id"],
        "poster_path": tmdb_data.get("poster_path", ""),
        "overview": tmdb_data.get("overview", ""),
        "vote_average": tmdb_data.get("vote_average", 0),
        "backdrop_path": tmdb_data.get("backdrop_path", ""),
    }

    media_type = item.get("media_type", "movie")

    if media_type == "movie":
        result.update(
            {
                "title": tmdb_data.get("title", ""),
                "original_title": tmdb_data.get("original_title", ""),
                "release_date": convert_date(
                    item.get("release_date") or tmdb_data.get("release_date", "")
                ),
            }
        )
    else:  # tv
        result.update(
            {
                "name": tmdb_data.get("name", ""),
                "original_name": tmdb_data.get("original_name", ""),
                "first_air_date": convert_date(
                    item.get("release_date") or tmdb_data.get("first_air_date", "")
                ),
                "last_air_date": convert_date(
                    item.get("release_date") or tmdb_data.get("last_air_date", "")
                ),
                "number_of_seasons": tmdb_data.get("number_of_seasons", 0),
                "seasons": tmdb_data.get("seasons", []),
            }
        )

    if "torrent" in item:
        qualities = [
            t["quality"]
            for t in item["torrent"]
            if "quality" in t and t["quality"] is not None
        ]
        if qualities:
            result["release_quality"] = get_quality_text(max(qualities))

    return result


def get_clear_cache_password():
    """Получает пароль из переменных окружения"""
    password = os.getenv("CACHE_CLEAR_PASSWORD")
    if not password:
        logger.error("Пароль для очистки кэша не задан в переменных окружения")
        raise RuntimeError("Не настроен пароль для очистки кэша")
    return password


def _item_card_id(item: dict) -> str | None:
    """Вычисляет card_id для элемента в формате '{tmdb_id}_{media_type}'."""
    try:
        tmdb_id = int(item.get("id", 0))
        media_type = item.get("media_type")
        if not media_type:
            # Lampac-файлы не содержат media_type — определяем по TMDB-полям:
            # сериалы имеют seasons/last_episode_to_air, фильмы — нет
            if item.get("seasons") is not None or item.get("last_episode_to_air") is not None:
                media_type = "tv"
            else:
                media_type = "movie"
        if tmdb_id:
            return f"{tmdb_id}_{media_type}"
    except (ValueError, TypeError):
        pass
    return None


def _tv_show_watched(item: dict, item_timecodes: dict[str, str], threshold: int = 90) -> bool:
    """
    Проверяет, все ли нужные эпизоды сериала просмотрены.

    Сериалы/мультсериалы (есть last_episode_to_air):
      - для предыдущих сезонов — все серии по seasons[].episode_count
      - для последнего сезона — только до last_episode_to_air.episode_number
        (следующая серия могла ещё не выйти)

    Аниме (нет last_episode_to_air):
      - проверяем все серии во всех сезонах по seasons[].episode_count
    """
    original_name = item.get("original_name") or item.get("original_title", "")
    if not original_name:
        logger.debug(f"[tv_watched] нет original_name/original_title, item keys={list(item.keys())}")
        return False

    seasons = [s for s in item.get("seasons", []) if s.get("season_number", 0) > 0]
    if not seasons:
        logger.debug(f"[tv_watched] нет seasons для {original_name!r}, raw seasons={item.get('seasons')}")
        return False

    # Хеши эпизодов с достаточным прогрессом
    watched_hashes: set[str] = set()
    for h, data_str in item_timecodes.items():
        try:
            if json.loads(data_str).get("percent", 0) >= threshold:
                watched_hashes.add(h)
        except (json.JSONDecodeError, TypeError):
            pass

    last_ep = item.get("last_episode_to_air")
    if last_ep:
        # Сериал/мультсериал: проверяем до последней вышедшей серии
        last_season = last_ep.get("season_number", 0)
        last_episode = last_ep.get("episode_number", 0)
        if not last_season or not last_episode:
            logger.debug(f"[tv_watched] {original_name!r}: last_episode_to_air без season/episode: {last_ep}")
            return False
        season_ep_count = {s["season_number"]: s["episode_count"] for s in seasons}
        logger.debug(f"[tv_watched] {original_name!r}: проверяем до S{last_season}E{last_episode}, "
                     f"watched_hashes={len(watched_hashes)}, season_ep_count={season_ep_count}")
        for sn in range(1, last_season + 1):
            ep_count = last_episode if sn == last_season else season_ep_count.get(sn, 0)
            for ep in range(1, ep_count + 1):
                h = lampa_hash(build_episode_hash_string(sn, ep, original_name))
                if h not in watched_hashes:
                    logger.debug(f"[tv_watched] {original_name!r}: S{sn}E{ep} hash={h} НЕ просмотрен")
                    return False
    else:
        # Аниме: нет last_episode_to_air — проверяем все серии всех сезонов
        for s in seasons:
            sn = s["season_number"]
            for ep in range(1, s.get("episode_count", 0) + 1):
                if lampa_hash(build_episode_hash_string(sn, ep, original_name)) not in watched_hashes:
                    return False

    return True


def _item_watched(item: dict, timecodes: dict[str, dict[str, str]], watched_movies: set[str]) -> bool:
    """True если элемент уже полностью просмотрен и должен быть скрыт."""
    card_id = _item_card_id(item)
    if not card_id:
        logger.debug(f"[filter] нет card_id для item id={item.get('id')} media_type={item.get('media_type')}")
        return False
    if card_id.endswith("_tv"):
        if card_id not in timecodes:
            logger.debug(f"[filter] {card_id} не найден в таймкодах (всего tv-ключей: {sum(1 for k in timecodes if k.endswith('_tv'))})")
            return False
        result = _tv_show_watched(item, timecodes[card_id])
        logger.debug(f"[filter] {card_id} → _tv_show_watched={result}, "
                     f"original_name={item.get('original_name') or item.get('original_title')!r}, "
                     f"seasons={len(item.get('seasons', []))}, "
                     f"last_episode_to_air={item.get('last_episode_to_air')}, "
                     f"timecode_keys={len(timecodes[card_id])}")
        return result
    is_watched = card_id in watched_movies
    if is_watched:
        logger.debug(f"[filter] {card_id} → фильм просмотрен")
    else:
        logger.debug(f"[filter] {card_id} → не просмотрен (movie-ветка), media_type в item={item.get('media_type')!r}")
    return is_watched


@app.get("/{category}")
async def get_category(
    category: str,
    request: Request,
    page: int = 1,
    per_page: int = 20,
    language: str = "ru",
    apikey: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if not re.match(r'^[\w\-]+$', category):
        raise HTTPException(status_code=404, detail="Not found")

    try:
        logger.debug(
            f"Запрос: {category}, страница {page}, apikey={'yes' if apikey else 'no'}"
        )

        # Загружаем таймкоды профиля (если передан apikey)
        timecodes: dict = {}
        watched_movies: set[str] = set()
        if apikey:
            profile = await get_profile_by_api_key(apikey=apikey, db=db)
            if profile:
                timecodes = await load_profile_timecodes(db, profile.id)
                watched_movies = get_watched_movie_ids(timecodes)
                logger.debug(f"Фильтрация: {len(watched_movies)} просмотренных фильмов, "
                             f"{sum(1 for k in timecodes if k.endswith('_tv'))} сериалов в таймкодах")

        # Загрузка данных из файла
        data = load_data(category)

        stats.track_api_user(request)
        stats.track_category_request(request, category)

        # ── Lampac-формат: {"results": [...]} или [...]  ─────────────────────
        if "results" in data or isinstance(data, list):
            items = data["results"] if "results" in data else data

            if timecodes or watched_movies:
                items = [i for i in items if not _item_watched(i, timecodes, watched_movies)]

            total = len(items)
            start = (page - 1) * per_page
            return {
                "page": page,
                "results": items[start : start + per_page],
                "total_pages": ceil(total / per_page) if per_page else 1,
                "total_results": total,
            }

        # ── NUMParser-формат: {"items": [...]} с обогащением TMDB  ───────────
        if "items" not in data:
            raise ValueError("Неизвестный формат данных")

        all_items = data["items"]

        # Фильтруем ДО обогащения TMDB — экономим запросы к API
        if timecodes or watched_movies:
            all_items = [i for i in all_items if not _item_watched(i, timecodes, watched_movies)]

        total = len(all_items)
        start = (page - 1) * per_page
        page_items = all_items[start : start + per_page]

        # Подготовка запросов к TMDB
        requests_to_make = []
        cached_results = {}

        for item in page_items:
            if "media_type" in item and "id" in item:
                try:
                    media_type = str(item["media_type"])
                    tmdb_id = int(item["id"])
                    cache_key = (media_type, tmdb_id)
                    if cache_key in tmdb_cache and isinstance(
                        tmdb_cache[cache_key], dict
                    ):
                        cached_results[cache_key] = tmdb_cache[cache_key]
                    else:
                        requests_to_make.append((media_type, tmdb_id))
                except (ValueError, TypeError) as e:
                    logger.warning(f"Некорректные данные в item: {item}, ошибка: {e}")

        if requests_to_make:
            logger.debug(f"Запросы к TMDB: {len(requests_to_make)} элементов")
            tmdb_batch = await fetch_tmdb_batch(requests_to_make)
            tmdb_cache.update(tmdb_batch)
            cached_results.update(tmdb_batch)

        results = []
        for item in page_items:
            if "media_type" in item and "id" in item:
                try:
                    cache_key = (str(item["media_type"]), int(item["id"]))
                    enhanced = enhance_with_tmdb(item, cached_results.get(cache_key))
                    if enhanced:
                        results.append(enhanced)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Ошибка обработки item: {item}, ошибка: {e}")

        return {
            "page": page,
            "results": results,
            "total_pages": ceil(total / per_page) if per_page else 1,
            "total_results": total,
        }
    except Exception as e:
        logger.error(f"Ошибка: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"error": "Внутренняя ошибка сервера"}
        )


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "NUMParser API работает"}


@app.get("/cache/path")
async def get_cache_path():
    """Возвращает информацию об источнике TMDB-кэша"""
    return {
        "source": "PostgreSQL (media_cards table)",
        "cache_size": len(tmdb_cache),
    }


@app.post("/cache/clear")
async def clear_cache(x_password: str = Header(..., alias="X-Password")):
    """Очистка in-memory кэша с проверкой пароля"""
    correct_password = os.getenv("CACHE_CLEAR_PASSWORD")

    if not correct_password or x_password != correct_password:
        return PlainTextResponse(
            "Неверный пароль для очистки кэша\n", status_code=status.HTTP_403_FORBIDDEN
        )

    global tmdb_cache
    tmdb_cache = {}

    return PlainTextResponse("Кэш успешно очищен\n", status_code=200)


@app.get("/cache/info")
async def cache_info():
    """Возвращает информацию о кэше"""
    return {
        "cache_size": len(tmdb_cache),
        "source": "PostgreSQL",
        "sample_keys": [f"{k[0]}_{k[1]}" for k in list(tmdb_cache.keys())[:5]],
    }


async def resolve_redirects(url: str, client: httpx.AsyncClient):
    """Рекурсивно разрешаем редиректы, пока не получим конечный URL"""
    max_redirects = 5
    current_url = url
    for _ in range(max_redirects):
        try:
            response = await client.head(current_url, follow_redirects=False)
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                if location:
                    current_url = location
                    continue
            break
        except Exception:
            break
    return current_url


@app.get("/proxy/m3u")
async def proxy_m3u(url: str, request: Request):
    """
    Прокси для загрузки M3U плейлистов с обработкой коротких ссылок
    """
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    try:
        headers = {
            "User-Agent": request.headers.get("User-Agent", "Mozilla/5.0"),
            "Accept": "*/*",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Сначала разрешаем редиректы
            final_url = await resolve_redirects(url, client)
            logger.info(f"Original URL: {url}, Final URL: {final_url}")

            # Затем загружаем контент
            response = await client.get(
                final_url, headers=headers, follow_redirects=True
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to fetch playlist (status: {response.status_code})",
                )

            content = response.text
            if not content.lstrip().upper().startswith("#EXTM3U"):
                logger.error(f"Invalid M3U content from URL: {final_url}")
                raise HTTPException(
                    status_code=400,
                    detail="The provided URL does not point to a valid M3U playlist",
                )

            return PlainTextResponse(content=content, media_type="audio/x-mpegurl")

    except httpx.TimeoutException:
        logger.error(f"Timeout while fetching playlist from {url}")
        raise HTTPException(status_code=504, detail="Request timeout")
    except httpx.RequestError as e:
        logger.error(f"Error fetching playlist: {str(e)}")
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch playlist: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


# Запуск сервера (для тестирования)
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
