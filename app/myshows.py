import httpx
import gzip
import json
import logging
import os
import math

from fastapi import APIRouter, HTTPException, Header, Request
from pathlib import Path
from typing import Dict, Any
from datetime import datetime
from dotenv import load_dotenv


load_dotenv()
MYSHOWS_AUTH_URL = os.getenv("MYSHOWS_AUTH_URL")


BASE_DIR = Path(__file__).parent.parent

# Настройка логгера для MyShows
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/myshows", tags=["myshows"])


@router.post("/auth")
async def proxy_auth(request: Request):
    try:
        data = await request.json()
        login = data.get("login")
        password = data.get("password")

        if not login or not password:
            raise HTTPException(
                status_code=400, detail="Login and password are required"
            )

        logger.info(f"Received auth request for login: {login}")

        # Выполняем запрос к MyShows API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                MYSHOWS_AUTH_URL,
                json={"login": login, "password": password},
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )

            if response.status_code != 200:
                logger.error(
                    f"MyShows auth failed: {response.status_code} - {response.text}"
                )
                raise HTTPException(
                    status_code=response.status_code,
                    detail="MyShows authentication failed",
                )

            auth_data = response.json()
            token = auth_data.get("token")
            refresh_token = auth_data.get("refreshToken")

            logger.info(f"auth_data: {auth_data}")
            logger.info(f"Cookies from response_v3: {response.cookies}")

            token_v3 = response.cookies.get("msAuthToken")

            if not token:
                logger.error("No token received from MyShows")
                raise HTTPException(
                    status_code=500, detail="No token received from MyShows"
                )

            logger.info(f"Successfully authenticated user: {login}")

            return {"token": token, "token_v3": token_v3, "refreshToken": refresh_token}

    except httpx.RequestError as e:
        logger.error(f"Request to MyShows failed: {str(e)}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/{hashed_login}/{path}/{file_hash}")
async def get_myshows_cache(
    hashed_login: str,
    path: str,
    file_hash: str,
    page: int = 1,
    per_page: int = 20,
    profile_id: str = Header("default", alias="X-Profile-ID"),
):
    """Получает кеш MyShows для пользователя с поддержкой gzip и пагинации"""
    logger.info(
        f"[MyShows GET] Запрос кеша для hashed_login={hashed_login}, path={path}, file={file_hash}, page={page}, profile_id={profile_id}"
    )

    cache_path = BASE_DIR / Path("myshows_cache") / hashed_login / path / file_hash
    logger.debug(f"[MyShows GET] Путь к кешу: {cache_path}")

    if not cache_path.exists():
        logger.warning(f"[MyShows GET] Кеш не найден: {cache_path}")
        raise HTTPException(status_code=404, detail="Cache not found")

    try:
        # Читаем файл как бинарные данные
        with open(cache_path, "rb") as f:
            compressed_data = f.read()

            # Декомпрессируем gzip данные
        try:
            json_data = gzip.decompress(compressed_data).decode("utf-8")
            logger.debug(f"[MyShows GET] Файл успешно декомпрессирован из gzip")
        except gzip.BadGzipFile:
            # Fallback для несжатых файлов (старый формат)
            json_data = compressed_data.decode("utf-8")
            logger.debug(f"[MyShows GET] Файл прочитан как обычный текст")

        data = json.loads(json_data)

        if "results" in data or isinstance(data, list):
            items = data["results"] if "results" in data else data
            total = len(items)
            start = (page - 1) * per_page
            return {
                "page": page,
                "results": items[start : start + per_page],
                "total_pages": math.ceil(total / per_page),
                "total_results": total,
            }

        # Применяем пагинацию к shows
        elif "shows" in data and isinstance(data["shows"], list):
            shows = data["shows"]
            total = len(shows)
            start = (page - 1) * per_page
            end = start + per_page

            paginated_shows = shows[start:end]

            logger.info(
                f"[MyShows GET] Возвращаем {len(paginated_shows)} из {total} сериалов (страница {page})"
            )

            # Возвращаем в формате, совместимом с numparser
            return {
                "page": page,
                "results": paginated_shows,  # Используем results вместо shows для совместимости
                "total_pages": math.ceil(total / per_page) if per_page > 0 else 1,
                "total_results": total,
                # "cached_at": data.get("cached_at"),
                # "profile_id": data.get("profile_id"),
                # "hashed_login": data.get("hashed_login"),
            }
        else:
            logger.info(f"[MyShows GET] Кеш загружен без пагинации")
            return data

    except Exception as e:
        logger.error(f"[MyShows GET] Ошибка чтения кеша: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error reading cache: {str(e)}")


@router.post("/{hashed_login}/{path}/{file_hash}")
async def save_myshows_cache(
    hashed_login: str,
    path: str,
    file_hash: str,
    data: Dict[str, Any],
    profile_id: str = Header("default", alias="X-Profile-ID"),
):
    """Сохраняет кеш MyShows с gzip сжатием"""
    cache_path = BASE_DIR / Path("myshows_cache") / hashed_login / path / file_hash
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        cache_data = {
            **data,
            "cached_at": datetime.now().isoformat(),
            "profile_id": profile_id,
            "hashed_login": hashed_login,
        }

        # Сжимаем данные с помощью gzip
        json_data = json.dumps(cache_data, ensure_ascii=False, indent=2)
        compressed_data = gzip.compress(json_data.encode("utf-8"))

        # Сохраняем без расширения
        with open(cache_path, "wb") as f:
            f.write(compressed_data)

        logger.info(f"[MyShows POST] Кеш сохранен с gzip сжатием в {cache_path}")
        return {"status": "success", "path": str(cache_path)}
    except Exception as e:
        logger.error(f"[MyShows POST] Ошибка сохранения кеша: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error saving cache: {str(e)}")


@router.delete("/{hashed_login}/{path}/{file_hash}")
async def clear_myshows_cache(
    hashed_login: str,
    path: str,
    file_hash: str,
    profile_id: str = Header("default", alias="X-Profile-ID"),
):
    """Очищает кеш MyShows для пользователя"""
    logger.info(
        f"[MyShows DELETE] Очистка кеша для hashed_login={hashed_login}, path={path}, file_hash={file_hash}, profile_id={profile_id}"
    )

    cache_path = BASE_DIR / Path("myshows_cache") / hashed_login / path / file_hash

    if cache_path.exists():
        cache_path.unlink()
        logger.info(f"[MyShows DELETE] Кеш успешно удален: {cache_path}")
        return {"status": "success", "message": "Cache cleared"}
    else:
        logger.warning(f"[MyShows DELETE] Кеш не найден для удаления: {cache_path}")
        raise HTTPException(status_code=404, detail="Cache not found")
