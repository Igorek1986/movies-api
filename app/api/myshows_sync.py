import json
import logging
import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import select
from app.db.database import get_db
from app.db.models import User, Device, Timecode, MediaCard
from app.utils import lampa_hash
from app.config import get_settings
from app.api.dependencies import get_current_user
from app import rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()
settings = get_settings()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _myshows_rpc(client: httpx.AsyncClient, token: str, method: str, params: dict = None) -> dict:
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    headers = {"Content-Type": "application/json", "authorization2": f"Bearer {token}"}
    resp = await client.post(settings.MYSHOWS_API, json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"MyShows API error: {resp.status_code}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"MyShows error: {data['error']}")
    return data.get("result", {})


async def _find_tmdb_data(
    client: httpx.AsyncClient,
    title: str,
    original_title: str,
    year: int,
    imdb_id: str = None,
    is_tv: bool = False,
    cache: dict = None,
) -> dict | None:
    """Returns {"id", "title", "original_title", "poster_path", "year"} or None."""
    cache_key = f"{'tv' if is_tv else 'movie'}:{imdb_id or title}:{year}"
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    headers = {"Authorization": settings.TMDB_TOKEN, "Accept": "application/json"}
    title_key = "name" if is_tv else "title"
    orig_key = "original_name" if is_tv else "original_title"
    date_key = "first_air_date" if is_tv else "release_date"

    def _extract(item: dict) -> dict:
        date = item.get(date_key) or ""
        return {
            "id": item["id"],
            "title": item.get(title_key) or "",
            "original_title": item.get(orig_key) or "",
            "poster_path": item.get("poster_path") or "",
            "year": date[:4],
        }

    # 1. By IMDB ID
    if imdb_id:
        try:
            imdb_clean = str(imdb_id).replace("tt", "")
            resp = await client.get(
                f"https://api.themoviedb.org/3/find/tt{imdb_clean}",
                params={"external_source": "imdb_id", "language": "ru-RU"},
                headers=headers, timeout=10,
            )
            if resp.status_code == 200:
                results = resp.json().get("tv_results" if is_tv else "movie_results", [])
                if results:
                    data = _extract(results[0])
                    if cache is not None:
                        cache[cache_key] = data
                    return data
        except Exception as e:
            logger.warning(f"IMDB lookup error for '{title}': {e}")

    # 2. By title
    endpoint = f"https://api.themoviedb.org/3/search/{'tv' if is_tv else 'movie'}"
    for query in list(dict.fromkeys(q for q in [original_title, title] if q)):
        for search_year in [year, None]:
            try:
                params = {"query": query, "language": "ru-RU"}
                if search_year:
                    params["first_air_date_year" if is_tv else "year"] = search_year
                resp = await client.get(endpoint, params=params, headers=headers, timeout=10)
                if resp.status_code == 200:
                    results = resp.json().get("results", [])
                    if results:
                        for item in results:
                            if item.get(title_key, "").lower() == query.lower():
                                data = _extract(item)
                                if cache is not None:
                                    cache[cache_key] = data
                                return data
                        data = _extract(results[0])
                        if cache is not None:
                            cache[cache_key] = data
                        return data
            except Exception as e:
                logger.warning(f"Title search error for '{query}': {e}")

    return None


def _lampa_hash_for_movie(movie: dict) -> str:
    title = movie.get("titleOriginal") or movie.get("title", "")
    return str(lampa_hash(title))


def _lampa_hash_for_episode(season: int, episode: int, show_title: str) -> str:
    season_prefix = f"{season}:" if season >= 10 else str(season)
    return str(lampa_hash(f"{season_prefix}{episode}{show_title}"))


# ─── Sync stream generator ─────────────────────────────────────────────────────

async def _sync_generator(device: Device, ms_login: str, ms_password: str, db: AsyncSession):
    all_timecodes: list[dict] = []
    all_media_cards: list[dict] = []
    tmdb_cache: dict = {}
    stats = {"movies_ok": 0, "movies_err": 0, "shows_ok": 0, "shows_err": 0}

    try:
        yield _sse({"type": "status", "message": "Авторизация в MyShows…"})

        async with httpx.AsyncClient(timeout=30.0) as client:

            # ── Auth ────────────────────────────────────────────────────────
            auth_resp = await client.post(
                settings.MYSHOWS_AUTH_URL,
                json={"login": ms_login, "password": ms_password},
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if auth_resp.status_code != 200:
                yield _sse({"type": "error", "message": "Ошибка авторизации MyShows (неверный логин/пароль?)"})
                return

            auth_data = auth_resp.json()
            token = auth_data.get("token") or auth_data.get("token_v3")
            if not token:
                yield _sse({"type": "error", "message": "MyShows не вернул токен"})
                return

            # ── Movies ──────────────────────────────────────────────────────
            yield _sse({"type": "status", "message": "Загружаю просмотренные фильмы…"})
            movies_raw = await _myshows_rpc(client, token, "profile.WatchedMovies")
            movies = movies_raw if isinstance(movies_raw, list) else []

            yield _sse({"type": "stage", "stage": "movies", "current": 0, "total": len(movies),
                        "message": f"Обрабатываю {len(movies)} фильмов…"})

            for idx, movie in enumerate(movies):
                title = movie.get("title", "")
                tmdb_data = await _find_tmdb_data(
                    client, title=title,
                    original_title=movie.get("titleOriginal", ""),
                    year=movie.get("year"),
                    imdb_id=movie.get("imdbId"),
                    is_tv=False, cache=tmdb_cache,
                )
                if tmdb_data:
                    tmdb_id = tmdb_data["id"]
                    card_id = f"{tmdb_id}_movie"
                    duration = (movie.get("runtime") or 120) * 60
                    all_timecodes.append({
                        "card_id": card_id,
                        "item": _lampa_hash_for_movie(movie),
                        "data": json.dumps({"duration": duration, "time": duration, "percent": 100}),
                    })
                    all_media_cards.append({
                        "card_id": card_id,
                        "tmdb_id": tmdb_id,
                        "media_type": "movie",
                        "title": tmdb_data["title"] or title,
                        "original_title": tmdb_data["original_title"] or movie.get("titleOriginal", ""),
                        "poster_path": tmdb_data["poster_path"],
                        "year": tmdb_data["year"] or str(movie.get("year", "") or ""),
                    })
                    stats["movies_ok"] += 1
                else:
                    logger.info(f"Not found in TMDB: movie '{title}' ({movie.get('year', '')})")
                    stats["movies_err"] += 1

                if (idx + 1) % 10 == 0 or idx + 1 == len(movies):
                    yield _sse({"type": "stage", "stage": "movies",
                                "current": idx + 1, "total": len(movies)})

                await asyncio.sleep(0)  # yield control

            # ── Shows ───────────────────────────────────────────────────────
            yield _sse({"type": "status", "message": "Загружаю список сериалов…"})
            shows_raw = await _myshows_rpc(client, token, "profile.Shows", {"page": 0, "pageSize": 1000})
            user_shows = shows_raw if isinstance(shows_raw, list) else []

            yield _sse({"type": "stage", "stage": "shows", "current": 0, "total": len(user_shows),
                        "message": f"Обрабатываю {len(user_shows)} сериалов…"})

            for idx, user_show in enumerate(user_shows):
                show_id = user_show.get("show", {}).get("id")
                show_title_short = user_show.get("show", {}).get("title", "")
                if not show_id:
                    continue

                try:
                    show_details = await _myshows_rpc(
                        client, token, "shows.GetById",
                        {"showId": show_id, "withEpisodes": True},
                    )
                    if not show_details:
                        stats["shows_err"] += 1
                        continue

                    episodes_result = await _myshows_rpc(
                        client, token, "profile.Episodes", {"showId": show_id}
                    )
                    watched_episodes = episodes_result if isinstance(episodes_result, list) else []
                    if not watched_episodes:
                        stats["shows_ok"] += 1
                        continue

                    show_title = show_details.get("titleOriginal") or show_details.get("title", "")
                    show_tmdb_data = await _find_tmdb_data(
                        client,
                        title=show_details.get("title", ""),
                        original_title=show_details.get("titleOriginal", ""),
                        year=show_details.get("year"),
                        imdb_id=show_details.get("imdbId"),
                        is_tv=True, cache=tmdb_cache,
                    )
                    if not show_tmdb_data:
                        logger.info(f"Not found in TMDB: show '{show_details.get('title', '')}' ({show_details.get('year', '')})")
                        stats["shows_err"] += 1
                        continue
                    tmdb_id = show_tmdb_data["id"]

                    default_runtime = show_details.get("runtime", 45)
                    episodes_map = {
                        ep["id"]: ep for ep in show_details.get("episodes", []) if ep.get("id")
                    }

                    card_id_tv = f"{tmdb_id}_tv"
                    all_media_cards.append({
                        "card_id": card_id_tv,
                        "tmdb_id": tmdb_id,
                        "media_type": "tv",
                        "title": show_tmdb_data["title"] or show_details.get("title", ""),
                        "original_title": show_tmdb_data["original_title"] or show_details.get("titleOriginal", ""),
                        "poster_path": show_tmdb_data["poster_path"],
                        "year": show_tmdb_data["year"] or str(show_details.get("year", "") or ""),
                    })

                    for watched_ep in watched_episodes:
                        ep_info = episodes_map.get(watched_ep.get("id"))
                        if not ep_info:
                            continue
                        season = ep_info.get("seasonNumber", 1)
                        episode = ep_info.get("episodeNumber", 1)
                        runtime = ep_info.get("runtime") or default_runtime
                        duration = runtime * 60
                        all_timecodes.append({
                            "card_id": card_id_tv,
                            "item": _lampa_hash_for_episode(season, episode, show_title),
                            "data": json.dumps({"duration": duration, "time": duration, "percent": 100}),
                        })

                    stats["shows_ok"] += 1

                except Exception as e:
                    logger.warning(f"Show {show_title_short}: {e}")
                    stats["shows_err"] += 1

                yield _sse({"type": "stage", "stage": "shows",
                            "current": idx + 1, "total": len(user_shows),
                            "name": show_title_short})

                await asyncio.sleep(0)  # yield control

            # ── Save to DB ──────────────────────────────────────────────────
            if all_timecodes:
                yield _sse({"type": "status", "message": f"Сохраняю {len(all_timecodes)} таймкодов в базу…"})

                # Deduplicate timecodes
                unique: dict[tuple, dict] = {}
                for tc in all_timecodes:
                    unique[(tc["card_id"], tc["item"])] = tc
                cleaned = list(unique.values())

                values = [
                    {"device_id": device.id, "lampa_profile_id": profile_id, "card_id": tc["card_id"],
                     "item": tc["item"], "data": tc["data"]}
                    for tc in cleaned
                ]
                stmt = insert(Timecode).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        Timecode.device_id, Timecode.lampa_profile_id,
                        Timecode.card_id, Timecode.item,
                    ],
                    set_={"data": stmt.excluded.data},
                )
                await db.execute(stmt)

            # ── Save MediaCards ──────────────────────────────────────────────
            if all_media_cards:
                # Deduplicate by card_id (last write wins)
                mc_unique = {mc["card_id"]: mc for mc in all_media_cards}
                mc_stmt = insert(MediaCard).values(list(mc_unique.values()))
                mc_stmt = mc_stmt.on_conflict_do_update(
                    index_elements=["card_id"],
                    set_={
                        "title": mc_stmt.excluded.title,
                        "original_title": mc_stmt.excluded.original_title,
                        "poster_path": mc_stmt.excluded.poster_path,
                        "year": mc_stmt.excluded.year,
                    },
                )
                await db.execute(mc_stmt)

            if all_timecodes or all_media_cards:
                await db.commit()

        total_ok = stats["movies_ok"] + stats["shows_ok"]
        total_err = stats["movies_err"] + stats["shows_err"]
        yield _sse({
            "type": "done",
            "added": len(all_timecodes),
            "stats": stats,
            "message": (
                f"Готово! Таймкодов: {len(all_timecodes)}. "
                f"Обработано: {total_ok}, не найдено в TMDB: {total_err}."
            ),
        })

    except httpx.RequestError as e:
        logger.error(f"MyShows request error: {e}")
        yield _sse({"type": "error", "message": "Ошибка соединения с MyShows. Попробуйте позже."})
    except RuntimeError as e:
        yield _sse({"type": "error", "message": str(e)})
    except Exception as e:
        await db.rollback()
        logger.error(f"Sync error: {e}", exc_info=True)
        yield _sse({"type": "error", "message": f"Внутренняя ошибка: {e}"})


# ─── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/myshows/sync")
async def sync_myshows(
    request: Request,
    device_id: int = Form(...),
    login: str = Form(...),
    password: str = Form(...),
    profile_id: str = Form(""),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Синхронизация MyShows → устройство. Возвращает SSE-поток прогресса."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Необходима авторизация")

    allowed, wait_sec = rate_limit.check_sync(current_user.id)
    if not allowed:
        if wait_sec >= 60:
            wait_str = f"{wait_sec // 60} мин. {wait_sec % 60} сек."
        else:
            wait_str = f"{wait_sec} сек."
        raise HTTPException(
            status_code=429,
            detail=f"Синхронизация недавно запускалась. Подождите ещё {wait_str}",
        )

    device_result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == current_user.id)
    )
    device = device_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    logger.info(f"MyShows sync: user={current_user.username}, device={device.name}")

    return StreamingResponse(
        _sync_generator(device, login, password, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
