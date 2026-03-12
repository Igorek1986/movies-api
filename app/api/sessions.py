from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templates import get_templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.db.database import get_db
from app.db.models import User, Session
from app.api.dependencies import get_current_user
from app.utils import parse_user_agent

router = APIRouter()
templates = get_templates()

COOKIE_NAME = "session_key"


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    current_key = request.cookies.get(COOKIE_NAME)
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Session)
        .where(Session.user_id == current_user.id, Session.expires_at > now)
        .order_by(Session.created_at.desc())
    )
    sessions = result.scalars().all()

    sessions_data = [
        {
            "id": s.id,
            "browser": parse_user_agent(s.user_agent),
            "ip": s.ip or "—",
            "created_at": s.created_at,
            "expires_at": s.expires_at,
            "is_current": s.key == current_key,
        }
        for s in sessions
    ]

    return templates.TemplateResponse("sessions.html", {
        "request": request,
        "user": current_user,
        "sessions": sessions_data,
    })


@router.post("/sessions/{session_id}/revoke")
async def revoke_session(
    session_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.user_id == current_user.id)
    )
    session = result.scalar_one_or_none()
    if session:
        is_current = session.key == request.cookies.get(COOKIE_NAME)
        await db.delete(session)
        await db.commit()
        if is_current:
            response = RedirectResponse("/login", status_code=302)
            response.delete_cookie(COOKIE_NAME)
            return response

    return RedirectResponse("/sessions", status_code=302)


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=302)

    await db.execute(delete(Session).where(Session.user_id == current_user.id))
    await db.commit()

    response = RedirectResponse("/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
