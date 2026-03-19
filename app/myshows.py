import httpx
import logging

from fastapi import APIRouter, HTTPException, Request

from app import stats
from app.config import get_settings

MYSHOWS_AUTH_URL = get_settings().MYSHOWS_AUTH_URL

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
            stats.track_myshows_user(login)

            return {"token": token, "token_v3": token_v3, "refreshToken": refresh_token}

    except httpx.RequestError as e:
        logger.error(f"Request to MyShows failed: {str(e)}")
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")
