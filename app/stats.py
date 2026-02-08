import os
import sqlite3
import threading
from pathlib import Path
from datetime import date, datetime
from fastapi import APIRouter, Header, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.parent
DB_PATH = BASE_DIR / "stats.sqlite"
TEMPLATES_DIR = BASE_DIR / "templates"

STATS_PASSWORD = os.getenv("STATS_PASSWORD")
if not STATS_PASSWORD:
    raise RuntimeError("STATS_PASSWORD is not set in .env")

# Категории, которые нужно исключить из статистики
EXCLUDED_CATEGORIES = {
    "favicon.ico",
    "robots.txt",
    "apple-touch-icon.png",
    "manifest.json",
}

router = APIRouter(tags=["stats"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_db_lock = threading.Lock()

# -------------------------------------------------------------------
# DB INIT
# -------------------------------------------------------------------


def init_stats():
    TEMPLATES_DIR.mkdir(exist_ok=True)

    with sqlite3.connect(DB_PATH) as db:
        # MyShows пользователи
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS myshows_users (
                                                         login TEXT NOT NULL,
                                                         date TEXT NOT NULL,
                                                         requests INTEGER DEFAULT 1,
                                                         UNIQUE(login, date)
                );
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_myshows_date ON myshows_users(date);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_myshows_login ON myshows_users(login);"
        )

        # Обычные пользователи API (по IP)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS api_users (
                                                     ip TEXT NOT NULL,
                                                     date TEXT NOT NULL,
                                                     requests INTEGER DEFAULT 1,
                                                     UNIQUE(ip, date)
                );
            """
        )
        db.execute("CREATE INDEX IF NOT EXISTS idx_api_date ON api_users(date);")
        db.execute("CREATE INDEX IF NOT EXISTS idx_api_ip ON api_users(ip);")

        # Запросы к категориям
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS category_requests (
                                                             category TEXT NOT NULL,
                                                             ip TEXT NOT NULL,
                                                             date TEXT NOT NULL,
                                                             requests INTEGER DEFAULT 1,
                                                             UNIQUE(category, ip, date)
                );
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_category_date ON category_requests(date);"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_category_name ON category_requests(category);"
        )


# -------------------------------------------------------------------
# TRACKING FUNCTIONS
# -------------------------------------------------------------------


def track_myshows_user(login: str):
    """Трекает запрос от пользователя MyShows"""
    if not login or login == "null":
        return

    today = date.today().isoformat()

    with _db_lock, sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO myshows_users (login, date, requests)
            VALUES (?, ?, 1)
                ON CONFLICT(login, date)
            DO UPDATE SET requests = requests + 1
            """,
            (login, today),
        )


def track_api_user(request: Request):
    """Трекает обычного пользователя API (по IP)"""
    ip = request.client.host if request.client else "unknown"

    # Исключаем локальные запросы для тестирования
    if ip in ["127.0.0.1", "localhost", "::1"]:
        return

    today = date.today().isoformat()

    with _db_lock, sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO api_users (ip, date, requests)
            VALUES (?, ?, 1)
                ON CONFLICT(ip, date)
            DO UPDATE SET requests = requests + 1
            """,
            (ip, today),
        )


def track_category_request(request: Request, category: str):
    """Трекает запрос к категории (numparser)"""
    if not category or category == "null":
        return

    # Исключаем системные запросы
    if category.lower() in EXCLUDED_CATEGORIES:
        return

    ip = request.client.host if request.client else "unknown"
    today = date.today().isoformat()

    with _db_lock, sqlite3.connect(DB_PATH) as db:
        db.execute(
            """
            INSERT INTO category_requests (category, ip, date, requests)
            VALUES (?, ?, ?, 1)
                ON CONFLICT(category, ip, date)
            DO UPDATE SET requests = requests + 1
            """,
            (category, ip, today),
        )


# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------


def verify_password(password: str) -> bool:
    """Проверяет пароль"""
    return password == STATS_PASSWORD


def get_stats_data():
    """Получает данные статистики из базы"""
    with sqlite3.connect(DB_PATH) as db:
        cur = db.cursor()

        # ========== MyShows статистика ЗА СЕГОДНЯ ==========
        cur.execute(
            "SELECT COUNT(DISTINCT login) FROM myshows_users WHERE date = DATE('now')"
        )
        myshows_today_count = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT login, requests
            FROM myshows_users
            WHERE date = DATE('now')
            ORDER BY requests DESC
            """
        )
        myshows_today = cur.fetchall()

        # ========== MyShows статистика ВСЕГО ==========
        cur.execute("SELECT COUNT(DISTINCT login) FROM myshows_users")
        myshows_total_count = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT login, SUM(requests) as total
            FROM myshows_users
            GROUP BY login
            ORDER BY total DESC
            """
        )
        myshows_total = cur.fetchall()

        # ========== Обычные пользователи API ЗА СЕГОДНЯ ==========
        cur.execute("SELECT COUNT(DISTINCT ip) FROM api_users WHERE date = DATE('now')")
        api_users_today_count = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT ip, requests
            FROM api_users
            WHERE date = DATE('now')
            ORDER BY requests DESC
            """
        )
        api_users_today = cur.fetchall()

        # ========== Обычные пользователи API ВСЕГО ==========
        cur.execute("SELECT COUNT(DISTINCT ip) FROM api_users")
        api_users_total_count = cur.fetchone()[0] or 0

        cur.execute(
            """
            SELECT ip, SUM(requests) as total
            FROM api_users
            GROUP BY ip
            ORDER BY total DESC
            """
        )
        api_users_total = cur.fetchall()

        # ========== Категории статистика ЗА СЕГОДНЯ ==========
        cur.execute(
            """
            SELECT category, COUNT(DISTINCT ip)
            FROM category_requests
            WHERE date = DATE('now')
            GROUP BY category
            """
        )
        categories_today_count = dict(cur.fetchall())

        cur.execute(
            """
            SELECT category, ip, requests
            FROM category_requests
            WHERE date = DATE('now')
            ORDER BY category, requests DESC
            """
        )
        categories_today_detail = {}
        for row in cur.fetchall():
            category, ip, requests = row
            if category not in categories_today_detail:
                categories_today_detail[category] = []
            categories_today_detail[category].append({"ip": ip, "requests": requests})

        # Считаем общее количество запросов за сегодня по категориям
        cur.execute(
            """
            SELECT SUM(requests)
            FROM category_requests
            WHERE date = DATE('now')
            """
        )
        categories_today_requests_total = cur.fetchone()[0] or 0

        # ========== Категории статистика ВСЕГО ==========
        cur.execute(
            """
            SELECT category, COUNT(DISTINCT ip)
            FROM category_requests
            GROUP BY category
            """
        )
        categories_total_count = dict(cur.fetchall())

        cur.execute(
            """
            SELECT category, ip, SUM(requests) as total
            FROM category_requests
            GROUP BY category, ip
            ORDER BY category, total DESC
            """
        )
        categories_total_detail = {}
        for row in cur.fetchall():
            category, ip, requests = row
            if category not in categories_total_detail:
                categories_total_detail[category] = []
            categories_total_detail[category].append({"ip": ip, "requests": requests})

        # Считаем общее количество запросов всего по категориям
        cur.execute(
            """
            SELECT SUM(requests)
            FROM category_requests
            """
        )
        categories_total_requests_total = cur.fetchone()[0] or 0

        # ========== Общая статистика ==========
        cur.execute("SELECT COUNT(*) FROM myshows_users")
        total_myshows_records = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM api_users")
        total_api_users_records = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM category_requests")
        total_category_records = cur.fetchone()[0]

    return {
        "myshows": {
            "today": {
                "count": myshows_today_count,
                "detail": myshows_today,
            },
            "total": {
                "count": myshows_total_count,
                "detail": myshows_total,
            },
        },
        "api_users": {
            "today": {
                "count": api_users_today_count,
                "detail": api_users_today,
            },
            "total": {
                "count": api_users_total_count,
                "detail": api_users_total,
            },
        },
        "categories": {
            "today": {
                "count": len(categories_today_count),
                "unique_ips": categories_today_count,
                "detail": categories_today_detail,
                "total_requests": categories_today_requests_total,
            },
            "total": {
                "count": len(categories_total_count),
                "unique_ips": categories_total_count,
                "detail": categories_total_detail,
                "total_requests": categories_total_requests_total,
            },
        },
        "total": {
            "myshows_records": total_myshows_records,
            "api_users_records": total_api_users_records,
            "category_records": total_category_records,
            "all_records": total_myshows_records
            + total_api_users_records
            + total_category_records,
        },
    }


# -------------------------------------------------------------------
# WEB INTERFACE
# -------------------------------------------------------------------


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, password: str = None):
    """
    Страница статистики

    Если пароль не передан или неверный - показывает форму ввода
    Если пароль верный - показывает статистику
    """
    # Проверяем пароль
    if not password or not verify_password(password):
        return templates.TemplateResponse(
            "stats_login.html",
            {
                "request": request,
                "error": "Неверный пароль" if password else None,
            },
        )

    # Получаем данные
    stats_data = get_stats_data()

    return templates.TemplateResponse(
        "stats_dashboard.html",
        {
            "request": request,
            "stats": stats_data,
            "password": password,
            "now": datetime.now(),
        },
    )


@router.post("/stats", response_class=HTMLResponse)
async def stats_page_post(request: Request, password: str = Form(...)):
    """Обработка формы с паролем"""
    return await stats_page(request, password)


# -------------------------------------------------------------------
# API ENDPOINT (для программного доступа)
# -------------------------------------------------------------------


@router.get("/stats/api")
def get_stats_api(x_password: str = Header(..., alias="X-Password")):
    """API эндпоинт для получения статистики в JSON формате"""
    if not verify_password(x_password):
        raise HTTPException(status_code=403, detail="Forbidden")

    return get_stats_data()


# -------------------------------------------------------------------
# HEALTH CHECK
# -------------------------------------------------------------------


@router.get("/stats/health")
def health_check():
    """Проверка работоспособности базы данных"""
    try:
        with sqlite3.connect(DB_PATH) as db:
            cur = db.cursor()
            cur.execute("SELECT COUNT(*) FROM myshows_users")
            myshows_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM api_users")
            api_users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM category_requests")
            categories_count = cur.fetchone()[0]

        return {
            "status": "ok",
            "database": str(DB_PATH),
            "myshows_users_records": myshows_count,
            "api_users_records": api_users_count,
            "category_requests_records": categories_count,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
