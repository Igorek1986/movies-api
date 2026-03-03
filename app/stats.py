import asyncio
import logging
import os
from datetime import date, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Header, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.database import async_session_maker
from app.db.models import MyShowsUser, ApiUser, CategoryRequest

load_dotenv()

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

STATS_PASSWORD = os.getenv("STATS_PASSWORD")
if not STATS_PASSWORD:
    raise RuntimeError("STATS_PASSWORD is not set in .env")

EXCLUDED_CATEGORIES = {
    "favicon.ico",
    "robots.txt",
    "apple-touch-icon.png",
    "manifest.json",
}

router = APIRouter(tags=["stats"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# GEO LOOKUP (async)
# -------------------------------------------------------------------

async def _get_location(ip: str) -> dict:
    """Геолокация через ipwho.is (async). Для локальных/приватных IP — возвращает заглушку."""
    private_prefixes = (
        "10.", "192.168.",
        "172.16.", "172.17.", "172.18.", "172.19.", "172.20.",
        "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
        "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    )
    if ip in ("127.0.0.1", "localhost", "::1", "unknown") or ip.startswith(private_prefixes):
        return {"country": "Local", "city": "Local", "region": "Local", "flag_emoji": "🏠"}

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"https://ipwho.is/{ip}?lang=ru",
                headers={"User-Agent": "NUMParser/1.0"},
            )
        if resp.status_code != 200:
            return {"country": "Unknown", "city": "Unknown", "region": "Unknown", "flag_emoji": "🌍"}
        data = resp.json()
        if not data.get("success"):
            return {"country": "Unknown", "city": "Unknown", "region": "Unknown", "flag_emoji": "🌍"}
        return {
            "country": data.get("country", "Unknown"),
            "city": data.get("city", "Unknown"),
            "region": data.get("region", "Unknown"),
            "flag_emoji": data.get("flag", {}).get("emoji", "🌍"),
        }
    except Exception:
        return {"country": "Unknown", "city": "Unknown", "region": "Unknown", "flag_emoji": "🌍"}


# -------------------------------------------------------------------
# DB INIT (no-op: tables created by SQLAlchemy create_all on startup)
# -------------------------------------------------------------------

def init_stats():
    """No-op: таблицы создаются через SQLAlchemy create_all при старте."""
    pass


# -------------------------------------------------------------------
# TRACKING FUNCTIONS (fire-and-forget background tasks)
# -------------------------------------------------------------------

async def _do_track_myshows_user(login: str):
    today = date.today().isoformat()
    async with async_session_maker() as db:
        stmt = pg_insert(MyShowsUser).values(login=login, date=today, requests=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["login", "date"],
            set_={"requests": MyShowsUser.requests + 1},
        )
        await db.execute(stmt)
        await db.commit()


async def _do_track_api_user(ip: str):
    today = date.today().isoformat()
    location = await _get_location(ip)
    async with async_session_maker() as db:
        stmt = pg_insert(ApiUser).values(
            ip=ip, date=today, requests=1,
            country=location["country"], city=location["city"],
            region=location["region"], flag_emoji=location["flag_emoji"],
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["ip", "date"],
            set_={"requests": ApiUser.requests + 1},
        )
        await db.execute(stmt)
        await db.commit()


async def _do_track_category(ip: str, category: str):
    today = date.today().isoformat()
    async with async_session_maker() as db:
        stmt = pg_insert(CategoryRequest).values(
            category=category, ip=ip, date=today, requests=1,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["category", "ip", "date"],
            set_={"requests": CategoryRequest.requests + 1},
        )
        await db.execute(stmt)
        await db.commit()


def track_myshows_user(login: str):
    if not login or login == "null":
        return
    asyncio.create_task(_do_track_myshows_user(login))


def track_api_user(request: Request):
    ip = request.client.host if request.client else "unknown"
    if ip in ("127.0.0.1", "localhost", "::1"):
        return
    asyncio.create_task(_do_track_api_user(ip))


def track_category_request(request: Request, category: str):
    if not category or category == "null":
        return
    if category.lower() in EXCLUDED_CATEGORIES:
        return
    ip = request.client.host if request.client else "unknown"
    asyncio.create_task(_do_track_category(ip, category))


# -------------------------------------------------------------------
# STATS DATA RETRIEVAL
# -------------------------------------------------------------------

async def get_stats_data() -> dict:
    today = date.today().isoformat()

    async with async_session_maker() as db:

        # ── MyShows today ─────────────────────────────────────────
        res = await db.execute(
            text("SELECT COUNT(DISTINCT login) FROM stats_myshows_users WHERE date = :d"),
            {"d": today},
        )
        myshows_today_count = res.scalar() or 0

        res = await db.execute(
            text("SELECT login, requests FROM stats_myshows_users WHERE date = :d ORDER BY requests DESC"),
            {"d": today},
        )
        myshows_today = res.fetchall()

        # ── MyShows all time ──────────────────────────────────────
        res = await db.execute(
            text("SELECT COUNT(DISTINCT login) FROM stats_myshows_users")
        )
        myshows_total_count = res.scalar() or 0

        res = await db.execute(
            text("SELECT login, SUM(requests) AS total FROM stats_myshows_users GROUP BY login ORDER BY total DESC")
        )
        myshows_total = res.fetchall()

        # ── API users today ───────────────────────────────────────
        res = await db.execute(
            text("SELECT COUNT(DISTINCT ip) FROM stats_api_users WHERE date = :d"),
            {"d": today},
        )
        api_today_count = res.scalar() or 0

        res = await db.execute(
            text("""SELECT ip, requests, country, city, region, flag_emoji
                    FROM stats_api_users WHERE date = :d ORDER BY requests DESC"""),
            {"d": today},
        )
        api_users_today = res.fetchall()

        # ── API users all time ────────────────────────────────────
        res = await db.execute(
            text("SELECT COUNT(DISTINCT ip) FROM stats_api_users")
        )
        api_total_count = res.scalar() or 0

        res = await db.execute(
            text("""SELECT ip, SUM(requests) AS total, country, city, region, flag_emoji
                    FROM stats_api_users GROUP BY ip, country, city, region, flag_emoji
                    ORDER BY total DESC""")
        )
        api_users_total = res.fetchall()

        # ── Categories today ──────────────────────────────────────
        res = await db.execute(
            text("""SELECT category, COUNT(DISTINCT ip) AS uips, SUM(requests) AS treq
                    FROM stats_category_requests WHERE date = :d
                    GROUP BY category ORDER BY treq DESC"""),
            {"d": today},
        )
        cats_today_list = res.fetchall()
        categories_today_count      = {r[0]: r[1] for r in cats_today_list}
        categories_today_total_req  = {r[0]: r[2] for r in cats_today_list}

        res = await db.execute(
            text("""SELECT category, ip, requests FROM stats_category_requests
                    WHERE date = :d
                    ORDER BY (SELECT SUM(r2.requests) FROM stats_category_requests r2
                              WHERE r2.category = stats_category_requests.category AND r2.date = :d) DESC,
                             category, requests DESC"""),
            {"d": today},
        )
        categories_today_detail: dict = {}
        for cat, ip, req in res.fetchall():
            categories_today_detail.setdefault(cat, []).append({"ip": ip, "requests": req})

        res = await db.execute(
            text("SELECT SUM(requests) FROM stats_category_requests WHERE date = :d"),
            {"d": today},
        )
        categories_today_requests_total = res.scalar() or 0

        # ── Categories all time ───────────────────────────────────
        res = await db.execute(
            text("""SELECT category, COUNT(DISTINCT ip) AS uips, SUM(requests) AS treq
                    FROM stats_category_requests
                    GROUP BY category ORDER BY treq DESC""")
        )
        cats_total_list = res.fetchall()
        categories_total_count     = {r[0]: r[1] for r in cats_total_list}
        categories_total_total_req = {r[0]: r[2] for r in cats_total_list}

        res = await db.execute(
            text("""SELECT category, ip, SUM(requests) AS total FROM stats_category_requests
                    GROUP BY category, ip
                    ORDER BY (SELECT SUM(r2.requests) FROM stats_category_requests r2
                              WHERE r2.category = stats_category_requests.category) DESC,
                             category, total DESC""")
        )
        categories_total_detail: dict = {}
        for cat, ip, req in res.fetchall():
            categories_total_detail.setdefault(cat, []).append({"ip": ip, "requests": req})

        res = await db.execute(text("SELECT SUM(requests) FROM stats_category_requests"))
        categories_total_requests_total = res.scalar() or 0

        # ── Registered users ──────────────────────────────────────
        today_date = date.today()
        res = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE DATE(created_at) = :d"),
            {"d": today_date},
        )
        users_today_count = res.scalar() or 0

        res = await db.execute(text("SELECT COUNT(*) FROM users"))
        users_total_count = res.scalar() or 0

        res = await db.execute(
            text("""SELECT username, created_at FROM users
                    WHERE DATE(created_at) = :d ORDER BY created_at DESC"""),
            {"d": today_date},
        )
        users_today_detail = res.fetchall()

        res = await db.execute(
            text("SELECT username, created_at FROM users ORDER BY created_at DESC LIMIT 50")
        )
        users_total_detail = res.fetchall()

        # ── Totals ────────────────────────────────────────────────
        res = await db.execute(text("SELECT COUNT(*) FROM stats_myshows_users"))
        total_myshows = res.scalar() or 0
        res = await db.execute(text("SELECT COUNT(*) FROM stats_api_users"))
        total_api = res.scalar() or 0
        res = await db.execute(text("SELECT COUNT(*) FROM stats_category_requests"))
        total_cats = res.scalar() or 0

    return {
        "myshows": {
            "today": {"count": myshows_today_count, "detail": myshows_today},
            "total": {"count": myshows_total_count, "detail": myshows_total},
        },
        "api_users": {
            "today": {"count": api_today_count, "detail": api_users_today},
            "total": {"count": api_total_count, "detail": api_users_total},
        },
        "categories": {
            "today": {
                "count": len(categories_today_count),
                "unique_ips": categories_today_count,
                "total_requests_per_category": categories_today_total_req,
                "detail": categories_today_detail,
                "total_requests": categories_today_requests_total,
            },
            "total": {
                "count": len(categories_total_count),
                "unique_ips": categories_total_count,
                "total_requests_per_category": categories_total_total_req,
                "detail": categories_total_detail,
                "total_requests": categories_total_requests_total,
            },
        },
        "registered_users": {
            "today": {"count": users_today_count, "detail": users_today_detail},
            "total": {"count": users_total_count, "detail": users_total_detail},
        },
        "total": {
            "myshows_records": total_myshows,
            "api_users_records": total_api,
            "category_records": total_cats,
            "all_records": total_myshows + total_api + total_cats,
        },
    }


# -------------------------------------------------------------------
# AUTH
# -------------------------------------------------------------------

def verify_password(password: str) -> bool:
    return password == STATS_PASSWORD


# -------------------------------------------------------------------
# WEB INTERFACE
# -------------------------------------------------------------------

@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, password: str = None):
    if not password or not verify_password(password):
        return templates.TemplateResponse(
            "stats_login.html",
            {"request": request, "error": "Неверный пароль" if password else None},
        )

    stats_data = await get_stats_data()
    return templates.TemplateResponse(
        "stats_dashboard.html",
        {"request": request, "stats": stats_data, "password": password, "now": datetime.now()},
    )


@router.post("/stats", response_class=HTMLResponse)
async def stats_page_post(request: Request, password: str = Form(...)):
    return await stats_page(request, password)


# -------------------------------------------------------------------
# API ENDPOINT
# -------------------------------------------------------------------

@router.get("/stats/api")
async def get_stats_api(x_password: str = Header(..., alias="X-Password")):
    if not verify_password(x_password):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await get_stats_data()


# -------------------------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------------------------

@router.get("/stats/health")
async def health_check():
    try:
        async with async_session_maker() as db:
            res = await db.execute(text("SELECT COUNT(*) FROM stats_myshows_users"))
            ms = res.scalar()
            res = await db.execute(text("SELECT COUNT(*) FROM stats_api_users"))
            api = res.scalar()
            res = await db.execute(text("SELECT COUNT(*) FROM stats_category_requests"))
            cats = res.scalar()
        return {"status": "ok", "myshows_users_records": ms, "api_users_records": api, "category_requests_records": cats}
    except Exception as e:
        return {"status": "error", "message": str(e)}
