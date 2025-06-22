import asyncio
import gzip
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any, Dict, Tuple
from logging import DEBUG, INFO

import aiofiles
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

# Загрузка переменных окружения
load_dotenv()
TMDB_TOKEN = os.getenv("TMDB_TOKEN")

# Получаем путь к директории, где находится текущий скрипт
BASE_DIR = Path(__file__).parent.parent
CACHE_FILE = BASE_DIR / "tmdb_cache.json"
tmdb_cache: Dict[Tuple[str, int], Any] = None

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

    # Инициализация кэша
    tmdb_cache = await load_cache_from_file()
    logger.debug(f"Кэш инициализирован, записей: {len(tmdb_cache)}")

    # Логируем первые 5 ключей для проверки
    sample_keys = list(tmdb_cache.keys())[:5]
    logger.debug(f"Пример ключей в кэше: {sample_keys}")

    yield  # Приложение работает

    # Очистка при завершении (опционально)
    await save_cache_to_file(tmdb_cache)


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


logger.debug("Настройки окружения загружены успешно")

executor = ThreadPoolExecutor(max_workers=10)  # Пул потоков для запросов


# Кэш для TMDB данных
def tuple_to_str(key: Tuple[str, int]) -> str:
    """Преобразует кортеж (media_type, tmdb_id) в строку"""
    return f"{key[0]}_{key[1]}"


def str_to_tuple(key_str: str) -> Tuple[str, int]:
    """Преобразует строку обратно в кортеж"""
    parts = key_str.split("_")
    return (parts[0], int(parts[1]))


async def save_cache_to_file(cache: Dict[Tuple[str, int], Any]) -> None:
    """Асинхронно сохраняет кэш в сжатый GZIP файл"""
    try:
        # Преобразуем кортежные ключи в строки для JSON
        cache_with_str_keys = {f"{k[0]}_{k[1]}": v for k, v in cache.items()}

        async with aiofiles.open(CACHE_FILE, mode="wb") as f:
            # Сжимаем данные с помощью gzip
            json_data = json.dumps(cache_with_str_keys, ensure_ascii=False).encode(
                "utf-8"
            )
            compressed_data = gzip.compress(json_data)
            await f.write(compressed_data)

        logger.debug(f"Кэш TMDB сохранен в сжатый файл: {CACHE_FILE}")
    except Exception as e:
        logger.error(f"Ошибка сохранения сжатого кэша: {str(e)}")


async def load_cache_from_file() -> Dict[Tuple[str, int], Any]:
    """Асинхронно загружает кэш из сжатого GZIP файла"""
    try:
        if not CACHE_FILE.exists():
            logger.debug("Файл кэша не найден, будет создан новый")
            return {}

        async with aiofiles.open(CACHE_FILE, mode="rb") as f:
            compressed_data = await f.read()
            json_data = gzip.decompress(compressed_data).decode("utf-8")
            cache_with_str_keys = json.loads(json_data)

            # Преобразуем строковые ключи обратно в кортежи с правильными типами
            result = {}
            for k, v in cache_with_str_keys.items():
                media_type, tmdb_id_str = k.split("_")
                try:
                    tmdb_id = int(tmdb_id_str)
                    result[(media_type, tmdb_id)] = v
                except ValueError:
                    logger.warning(f"Некорректный TMDB ID в кэше: {tmdb_id_str}")
            return result
    except Exception as e:
        logger.error(f"Ошибка загрузки кэша: {str(e)}")
        return {}


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
    home_path = Path.home()
    # path = home_path / f"releases/{category}.json"
    releases_dir = os.getenv("RELEASES_DIR", "code/NUMParser/public")
    path = home_path / releases_dir / f"{category}.json"

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
            results[key] = data
            tmdb_cache[key] = data  # Обновляем глобальный кэш

            # Периодически сохраняем (каждые 10 записей)
            if len(results) % 10 == 0:
                await save_cache_to_file(tmdb_cache)

    # Финализируем сохранение
    await save_cache_to_file(tmdb_cache)
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


@app.get("/{category}")
async def get_category(
    category: str, page: int = 1, per_page: int = 20, language: str = "ru"
):
    try:
        logger.debug(f"Запрос: {category}, страница {page}")

        # Загрузка данных
        data = load_data(category)

        # Обработка lampac-файлов
        if "results" in data or isinstance(data, list):
            items = data["results"] if "results" in data else data
            total = len(items)
            start = (page - 1) * per_page
            return {
                "page": page,
                "results": items[start : start + per_page],
                "total_pages": ceil(total / per_page),
                "total_results": total,
            }

        # Обработка не-lampac файлов
        if "items" not in data:
            raise ValueError("Неизвестный формат данных")

        items = data["items"]
        total = len(items)
        start = (page - 1) * per_page
        page_items = items[start : start + per_page]

        # Подготовка запросов к TMDB
        requests_to_make = []
        cached_results = {}

        for item in page_items:
            if "media_type" in item and "id" in item:
                try:
                    media_type = str(item["media_type"])
                    tmdb_id = int(item["id"])  # Гарантируем что id будет int
                    cache_key = (media_type, tmdb_id)

                    # Проверяем кэш более тщательно
                    if cache_key in tmdb_cache and isinstance(
                        tmdb_cache[cache_key], dict
                    ):
                        cached_results[cache_key] = tmdb_cache[cache_key]
                    else:
                        requests_to_make.append((media_type, tmdb_id))
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"Некорректные данные в item: {item}, ошибка: {str(e)}"
                    )

        # Пакетный запрос для отсутствующих в кэше данных
        if requests_to_make:
            logger.debug(
                f"Делаем {len(requests_to_make)} запросов к TMDB для элементов: {requests_to_make}"
            )
            tmdb_batch = await fetch_tmdb_batch(requests_to_make)
            tmdb_cache.update(tmdb_batch)
            cached_results.update(tmdb_batch)
            await save_cache_to_file(tmdb_cache)  # Сохраняем обновленный кэш

        # Формируем ответ
        results = []
        for item in page_items:
            if "media_type" in item and "id" in item:
                try:
                    cache_key = (str(item["media_type"]), int(item["id"]))
                    enhanced = enhance_with_tmdb(item, cached_results.get(cache_key))
                    if enhanced:
                        results.append(enhanced)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Ошибка обработки item: {item}, ошибка: {str(e)}")

        return {
            "page": page,
            "results": results,
            "total_pages": ceil(total / per_page),
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
    """Возвращает абсолютный путь к файлу кэша"""
    return {
        "cache_path": str(CACHE_FILE.absolute()),
        "exists": CACHE_FILE.exists(),
        "size": CACHE_FILE.stat().st_size if CACHE_FILE.exists() else 0,
    }


# Добавляем эндпоинты для управления кэшем
@app.post("/cache/clear")
async def clear_cache(
    password: str = Header(..., description="Пароль для очистки кэша"),
    correct_password: str = Depends(get_clear_cache_password),
):
    """
    Очищает кэш TMDB. Требует пароль.

    Пароль должен быть передан в заголовке:
    X-Password: ваш_пароль
    """
    if password != correct_password:
        logger.warning("Попытка очистки кэша с неверным паролем")
        raise HTTPException(status_code=403, detail="Неверный пароль для очистки кэша")

    global tmdb_cache
    tmdb_cache = {}
    await save_cache_to_file(tmdb_cache)
    logger.info("Кэш успешно очищен")

    return {
        "status": "success",
        "message": "Кэш TMDB очищен",
        "cleared_entries": len(tmdb_cache),
    }


@app.get("/cache/info")
async def cache_info():
    """Возвращает информацию о кэше"""
    cache_size = len(tmdb_cache)
    cache_size_mb = (
        CACHE_FILE.stat().st_size / (1024 * 1024) if CACHE_FILE.exists() else 0
    )

    return {
        "cache_size": cache_size,
        "cache_size_mb": round(cache_size_mb, 2),
        "sample_keys": list(tmdb_cache.keys())[:5],
    }


# @app.post("/cache/refresh")
# async def refresh_cache(category: str = "all"):
#     """
#     Запускает принудительное обновление кеша TMDB
#     Параметры:
#     - category: категория для обновления ('all', 'movies', 'tv')
#     """
#     try:
#         logger.debug(
#             f"Запущено принудительное обновление кеша для категории: {category}"
#         )
#
#         home_path = Path.home()
#         releases_path = home_path / "releases"
#
#         # Определяем какие файлы будем обрабатывать
#         files_to_process = []
#         if category == "all":
#             # Получаем все json файлы в директории releases
#             files_to_process = list(releases_path.glob("*.json"))
#         else:
#             # Ищем конкретный файл
#             file_path = releases_path / f"{category}.json"
#             if file_path.exists():
#                 files_to_process = [file_path]
#
#         if not files_to_process:
#             return {"status": "error", "message": "Не найдены файлы для обработки"}
#
#         requests_list = []
#         processed_files = 0
#
#         for file_path in files_to_process:
#             # Пропускаем lampac файлы
#             if file_path.name.startswith("lampac_"):
#                 continue
#
#             try:
#                 # Загружаем данные из файла
#                 with gzip.open(file_path, "rt") as f:
#                     data = json.load(f)
#
#                 items = data.get("items", []) if isinstance(data, dict) else data
#
#                 for item in items:
#                     if isinstance(item, dict) and "media_type" in item and "id" in item:
#                         requests_list.append((item["media_type"], item["id"]))
#
#                 processed_files += 1
#                 logger.debug(
#                     f"Обработан файл: {file_path.name}, найдено {len(items)} элементов"
#                 )
#
#             except Exception as e:
#                 logger.error(f"Ошибка при обработке файла {file_path.name}: {str(e)}")
#                 continue
#
#         if not requests_list:
#             return {"status": "success", "message": "Нет данных для обновления"}
#
#         logger.debug(f"Всего найдено {len(requests_list)} элементов для обновления")
#
#         # Выполняем пакетные запросы
#         await fetch_tmdb_batch(requests_list)
#
#         return {
#             "status": "success",
#             "message": f"Кеш обновлен. Обработано {processed_files} файлов, добавлено {len(requests_list)} записей",
#         }
#
#     except Exception as e:
#         logger.error(f"Ошибка при обновлении кеша: {str(e)}", exc_info=True)
#         return JSONResponse(
#             status_code=500,
#             content={
#                 "status": "error",
#                 "message": f"Ошибка при обновлении кеша: {str(e)}",
#             },
#         )
