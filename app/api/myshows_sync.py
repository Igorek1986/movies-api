import json
import logging
import asyncio
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select, func, update
from app.db.database import get_db
from app.db.models import User, Device, Timecode, MediaCard, LampaProfile, Episode
from app.utils import lampa_hash, build_episode_hash_string
from app.config import get_settings
from app.api.dependencies import get_current_user
from app import rate_limit
from app.api.timecodes import _trim_to_limit, _merge_favorite_history, _media_card_to_entry
from app.api.episodes import _should_sync, _parse_air_date

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

    # 1. By IMDB ID (с валидацией названия — у MyShows бывают неверные IMDB ID)
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
                    # Проверяем, что найденный сериал хоть как-то совпадает с ожидаемым
                    found_title = (results[0].get(title_key) or "").lower()
                    found_orig = (results[0].get(orig_key) or "").lower()
                    expect_titles = {t.lower() for t in [title, original_title] if t}
                    title_ok = any(
                        et in found_title or found_title in et or
                        et in found_orig or found_orig in et
                        for et in expect_titles
                    )
                    if title_ok:
                        logger.debug(f"Found by IMDB {imdb_id}: '{title}' → tmdb={data['id']} '{data['title']}'")
                        if cache is not None:
                            cache[cache_key] = data
                        return data
                    logger.warning(
                        f"IMDB {imdb_id} вернул '{results[0].get(title_key)}' "
                        f"вместо '{title}' — пропускаю, ищу по названию"
                    )
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
                if resp.status_code != 200:
                    logger.warning(f"TMDB search {resp.status_code} for '{query}' year={search_year}")
                    continue
                results = resp.json().get("results", [])
                if results:
                    exact = [r for r in results if r.get(orig_key, "").lower() == query.lower()]
                    if exact:
                        # Среди точных совпадений берём самый популярный
                        best = max(exact, key=lambda r: r.get("popularity", 0))
                        data = _extract(best)
                        logger.debug(f"Found by title exact '{query}': '{title}' → tmdb={data['id']} (popularity={best.get('popularity', 0):.1f})")
                    else:
                        best = results[0]
                        data = _extract(best)
                        logger.debug(f"Found by title first '{query}': '{title}' → tmdb={data['id']} '{data['title']}'")
                    if cache is not None:
                        cache[cache_key] = data
                    return data
                else:
                    logger.debug(f"TMDB search empty: query='{query}' year={search_year}")
            except Exception as e:
                logger.warning(f"Title search error for '{query}': {e}")

    return None


def _lampa_hash_for_movie(movie: dict) -> str:
    title = movie.get("titleOriginal") or movie.get("title", "")
    return str(lampa_hash(title))



def _parse_watch_date(date_str: str | None) -> datetime:
    if date_str:
        try:
            return datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


# ─── Sync stream generator ─────────────────────────────────────────────────────

async def _sync_generator(device: Device, ms_login: str, ms_password: str, db: AsyncSession, profile_id: str = "", user_role: str = "simple"):
    all_timecodes: list[dict] = []
    all_media_cards: list[dict] = []
    all_episode_rows: dict[int, list[dict]] = {}   # tmdb_id → episode rows
    all_myshows_ids:  dict[int, int]        = {}   # tmdb_id → myshows_show_id
    all_media_cards_next_air: dict[int, str] = {}  # tmdb_id → next_ep_air_date из MyShows
    tmdb_cache: dict = {}
    stats = {"movies_ok": 0, "movies_err": 0, "shows_ok": 0, "shows_err": 0}
    not_found: list[str] = []

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
                    watch_date = _parse_watch_date(
                        (movie.get("userMovie") or {}).get("watchDate")
                    )
                    all_timecodes.append({
                        "card_id": card_id,
                        "item": _lampa_hash_for_movie(movie),
                        "data": json.dumps({"duration": duration, "time": duration, "percent": 100}),
                        "updated_at": watch_date,
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
                    label = f"{title} ({movie.get('year', '')})"
                    logger.warning(f"Not found in TMDB: movie '{label}'")
                    not_found.append(f"🎬 {label}")
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

                    show_title_myshows = show_details.get("titleOriginal") or show_details.get("title", "")
                    show_tmdb_data = await _find_tmdb_data(
                        client,
                        title=show_details.get("title", ""),
                        original_title=show_details.get("titleOriginal", ""),
                        year=show_details.get("year"),
                        imdb_id=show_details.get("imdbId"),
                        is_tv=True, cache=tmdb_cache,
                    )
                    if not show_tmdb_data:
                        label = f"{show_details.get('title', '')} ({show_details.get('year', '')})"
                        logger.warning(f"Not found in TMDB: show '{label}'")
                        not_found.append(f"📺 {label}")
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

                    orig = show_tmdb_data["original_title"] or show_title_myshows
                    for watched_ep in watched_episodes:
                        ep_info = episodes_map.get(watched_ep.get("id"))
                        if not ep_info:
                            continue
                        season = ep_info.get("seasonNumber", 1)
                        episode = ep_info.get("episodeNumber", 1)
                        runtime = ep_info.get("runtime") or default_runtime
                        duration = runtime * 60
                        watch_date = _parse_watch_date(watched_ep.get("watchDate"))
                        all_timecodes.append({
                            "card_id": card_id_tv,
                            "item": lampa_hash(build_episode_hash_string(season, episode, orig)),
                            "data": json.dumps({"duration": duration, "time": duration, "percent": 100}),
                            "updated_at": watch_date,
                        })

                    # Собираем ВСЕ эпизоды шоу (не только просмотренные) для таблицы episodes
                    # Пропускаем если другой пользователь уже синхронизировал и обновление не нужно
                    needs_ep_sync = True
                    mc_existing = await db.get(MediaCard, card_id_tv)
                    if mc_existing is not None:
                        mc_existing.myshows_show_id = show_details.get("id")  # для _should_sync
                        needs_ep_sync = _should_sync(mc_existing)

                    if needs_ep_sync:
                        ep_rows = []
                        for ep in show_details.get("episodes", []):
                            snum = ep.get("seasonNumber")
                            enum = ep.get("episodeNumber")
                            if snum is None or enum is None:
                                continue
                            if not ep.get("airDate") and not ep.get("airDateUTC"):
                                continue  # эпизод без даты — анонс, пропускаем
                            runtime_min = ep.get("runtime") or 0
                            ep_rows.append({
                                "tmdb_show_id": tmdb_id,
                                "season":        snum,
                                "episode":       enum,
                                "title":         ep.get("title") or None,
                                "duration_sec":  runtime_min * 60 if runtime_min else None,
                                "is_special":    bool(ep.get("isSpecial", False)) or enum == 0 or snum == 0,
                                "myshows_ep_id": ep.get("id"),
                                "hash":          lampa_hash(build_episode_hash_string(snum, enum, orig)),
                                "air_date":      _parse_air_date(ep),
                            })
                        if ep_rows:
                            all_episode_rows[tmdb_id] = ep_rows
                            all_myshows_ids[tmdb_id]  = show_details.get("id")

                            # Если TMDB не знает о следующем эпизоде — берём дату из MyShows
                            today = datetime.now().date()
                            future_dates = [
                                r["air_date"] for r in ep_rows
                                if r["air_date"] and r["air_date"] > today and not r["is_special"]
                            ]
                            if future_dates:
                                next_air_str = min(future_dates).isoformat()
                                all_media_cards_next_air[tmdb_id] = next_air_str

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
                     "item": tc["item"], "data": tc["data"], "updated_at": tc["updated_at"]}
                    for tc in cleaned
                ]
                # asyncpg limit: 32767 params; 6 columns → max 5000 rows per batch
                chunk_size = 5000
                for i in range(0, len(values), chunk_size):
                    chunk = values[i:i + chunk_size]
                    stmt = pg_insert(Timecode).values(chunk)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[
                            Timecode.device_id, Timecode.lampa_profile_id,
                            Timecode.card_id, Timecode.item,
                        ],
                        set_={"updated_at": stmt.excluded.updated_at},
                    )
                    await db.execute(stmt)

            # ── Save MediaCards ──────────────────────────────────────────────
            if all_media_cards:
                # Deduplicate by card_id (last write wins)
                mc_unique = {mc["card_id"]: mc for mc in all_media_cards}
                mc_stmt = pg_insert(MediaCard).values(list(mc_unique.values()))
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

                # Добавляем в favorite.history: читаем из БД чтобы использовать полные данные
                # (карточки могли быть обогащены TMDB раньше — там есть backdrop, overview, vote_average)
                db_cards = (await db.execute(
                    select(MediaCard).where(MediaCard.card_id.in_(list(mc_unique.keys())))
                )).scalars().all()

                # Определяем дату последнего просмотра для каждой карточки (для сортировки)
                card_last_watched: dict[str, datetime] = {}
                for tc in all_timecodes:
                    cid = tc["card_id"]
                    dt = tc.get("updated_at")
                    if dt and (cid not in card_last_watched or dt > card_last_watched[cid]):
                        card_last_watched[cid] = dt

                db_cards_sorted = sorted(
                    db_cards,
                    key=lambda mc: card_last_watched.get(mc.card_id, datetime.min),
                    reverse=True,
                )

                history_entries = [_media_card_to_entry(mc) for mc in db_cards_sorted]
                await _merge_favorite_history(db, device.id, profile_id, history_entries, user_role)

            # ── Save Episodes ────────────────────────────────────────────────
            if all_episode_rows:
                now_utc = datetime.now(timezone.utc)
                # Дедуплицируем по (tmdb_show_id, season, episode) — MyShows может вернуть дубли
                seen: dict[tuple, dict] = {}
                for rows in all_episode_rows.values():
                    for row in rows:
                        key = (row["tmdb_show_id"], row["season"], row["episode"])
                        seen[key] = row
                all_rows_flat = list(seen.values())
                chunk_size = 1000
                for i in range(0, len(all_rows_flat), chunk_size):
                    stmt = pg_insert(Episode).values(all_rows_flat[i:i + chunk_size])
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[Episode.tmdb_show_id, Episode.season, Episode.episode],
                        set_={
                            "title":         stmt.excluded.title,
                            "duration_sec":  stmt.excluded.duration_sec,
                            "is_special":    stmt.excluded.is_special,
                            "myshows_ep_id": stmt.excluded.myshows_ep_id,
                            "hash":          stmt.excluded.hash,
                            "air_date":      stmt.excluded.air_date,
                        },
                    )
                    await db.execute(stmt)
                for tmdb_id in all_episode_rows:
                    myshows_id = all_myshows_ids.get(tmdb_id)
                    upd: dict = {"myshows_show_id": myshows_id, "episodes_synced_at": now_utc}
                    next_air = all_media_cards_next_air.get(tmdb_id)
                    if next_air:
                        upd["next_ep_air_date"] = next_air
                    await db.execute(
                        update(MediaCard)
                        .where(MediaCard.card_id == f"{tmdb_id}_tv")
                        .values(**upd)
                    )
                logger.info(f"myshows_sync: episodes upserted for {len(all_episode_rows)} shows")

            if all_timecodes or all_media_cards or all_episode_rows:
                await db.commit()

            trimmed = 0
            if all_timecodes:
                trimmed = await _trim_to_limit(db, device.id, profile_id, user_role)

        total_ok = stats["movies_ok"] + stats["shows_ok"]
        total_err = stats["movies_err"] + stats["shows_err"]
        trim_note = f" Удалено старых: {trimmed} (превышен лимит)." if trimmed else ""
        yield _sse({
            "type": "done",
            "added": len(all_timecodes),
            "trimmed": trimmed,
            "stats": stats,
            "not_found": not_found,
            "message": (
                f"Готово! Таймкодов: {len(all_timecodes)}.{trim_note} "
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

    from app import settings_cache

    if current_user.role == "simple":
        raise HTTPException(status_code=403, detail="Синхронизация MyShows доступна только для Premium")

    # Super без лимита (myshows_daily == 0), premium и другие — проверяем cooldown
    myshows_limit = settings_cache.get_role_limit(current_user.role, "myshows_daily")
    if myshows_limit is not None:
        allowed, wait_sec = rate_limit.check_sync(current_user.id)
        if not allowed:
            hours = wait_sec // 3600
            mins  = (wait_sec % 3600) // 60
            wait_str = f"{hours} ч {mins} мин" if hours else f"{mins} мин"
            raise HTTPException(
                status_code=429,
                detail={"message": f"Синхронизация уже выполнялась сегодня. Следующая через {wait_str}.", "wait_sec": wait_sec},
            )

    device_result = await db.execute(
        select(Device).where(Device.id == device_id, Device.user_id == current_user.id)
    )
    device = device_result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Устройство не найдено")

    # Проверяем лимит профилей
    limit = settings_cache.get_role_limit(current_user.role, "profile_limit")
    if limit is not None:
        pid = profile_id or ""
        if pid:
            lp_exists = (await db.execute(
                select(LampaProfile).where(
                    LampaProfile.device_id == device_id,
                    LampaProfile.lampa_profile_id == pid,
                )
            )).scalar_one_or_none()
            is_new = not lp_exists
        else:
            has_tc = (await db.execute(
                select(func.count()).select_from(Timecode).where(
                    Timecode.device_id == device_id,
                    Timecode.lampa_profile_id == "",
                )
            )).scalar() or 0
            is_new = has_tc == 0

        if is_new:
            lp_count = (await db.execute(
                select(func.count()).select_from(LampaProfile)
                .where(LampaProfile.device_id == device_id)
            )).scalar() or 0
            if lp_count >= limit:
                raise HTTPException(status_code=403, detail="Достигнут лимит профилей")

    logger.info(f"MyShows sync: user={current_user.username}, device={device.name}")

    return StreamingResponse(
        _sync_generator(device, login, password, db, profile_id, current_user.role),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
