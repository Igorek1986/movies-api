from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi_cache import FastAPICache
from fastapi_cache.decorator import cache
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from contextlib import asynccontextmanager
import json
import gzip
from math import ceil
from pathlib import Path
import asyncio

# Конфигурация Redis
REDIS_URL = "redis://localhost:6379"


class CacheHandler(FileSystemEventHandler):
    def on_modified(self, event):
        if event.src_path.endswith(".json"):

            async def flush():
                redis = aioredis.from_url(REDIS_URL)
                await redis.flushall()
                print("Redis cache flushed due to file changes!")

            asyncio.create_task(flush())


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Инициализация Redis при старте
    redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    FastAPICache.init(RedisBackend(redis), prefix="lampa-api")

    # Мониторинг изменений файлов
    observer = Observer()
    observer.schedule(
        CacheHandler(), path=str(Path.home() / "releases"), recursive=False
    )
    observer.start()

    yield

    # Очистка при завершении
    await FastAPICache.clear()
    observer.stop()
    observer.join()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

# Middleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@cache(expire=3600)
async def load_data(category: str):
    path = Path.home() / f"releases/{category}.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


@app.get("/{category}")
async def get_movie(
    category: str,
    page: int = 1,
    per_page: int = 20,
):
    all_data = await load_data(category)
    total_results = len(all_data)
    total_pages = ceil(total_results / per_page)

    start = (page - 1) * per_page
    return {
        "page": page,
        "results": all_data[start : start + per_page],
        "total_pages": total_pages,
        "total_results": total_results,
    }
