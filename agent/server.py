import asyncio
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from livekit import api
from pydantic import BaseModel, Field

from catalog import (
    CHARACTERS,
    DEFAULT_CHARACTER,
    DEFAULT_MODEL,
    DEFAULT_VOICE,
    MODELS,
    PROMPT_MAX_LENGTH,
    VOICES,
    public_catalog,
)

load_dotenv()

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

# Прототип закрытый, только для команды: страница, каталог и выдача токенов
# доступны только после входа, чтобы случайный человек со ссылкой не тратил
# баланс OpenRouter и Cartesia. Вход — своя страница /login (окно HTTP Basic
# Auth браузер рисует сам, и оформить его нельзя); после входа сервер ставит
# подписанную cookie сессии.
AUTH_USER = os.environ["EMMA_AUTH_USER"]
AUTH_PASSWORD = os.environ["EMMA_AUTH_PASSWORD"]

SESSION_COOKIE = "emma_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60
# Ключ подписи выводится из логина и пароля: смена пароля в Railway сразу
# разлогинивает всех, отдельный секрет заводить не нужно.
SESSION_KEY = hashlib.sha256(f"emma-session\0{AUTH_USER}\0{AUTH_PASSWORD}".encode()).digest()

# Без входа доступны: страница входа, проверка живости и оформление страницы
# входа. Сама страница прототипа, /catalog и /token — только после входа.
PUBLIC_PATHS = {"/login", "/health", "/js/theme-toggle.js"}
PUBLIC_PREFIXES = ("/css/", "/fonts/", "/images/")
API_PATHS = {"/catalog", "/token"}

WEB_DIR = Path(__file__).parent / "web"

MODEL_IDS = {id for id, _ in MODELS}
VOICE_IDS = {id for id, _ in VOICES}

app = FastAPI()


def sign(expires: int) -> str:
    return hmac.new(SESSION_KEY, str(expires).encode(), hashlib.sha256).hexdigest()


def new_session() -> str:
    expires = int(time.time()) + SESSION_MAX_AGE
    return f"{expires}.{sign(expires)}"


def is_valid_session(value: str | None) -> bool:
    if not value:
        return False
    expires, _, signature = value.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(signature, sign(int(expires)))


def credentials_match(user: str, password: str) -> bool:
    user_ok = secrets.compare_digest(user.encode(), AUTH_USER.encode())
    password_ok = secrets.compare_digest(password.encode(), AUTH_PASSWORD.encode())
    return user_ok and password_ok


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


# Middleware, а не зависимость FastAPI: так вход закрывает и статику страницы,
# которую отдаёт StaticFiles, а не только API.
@app.middleware("http")
async def require_session(request: Request, call_next):
    path = request.url.path
    if is_public(path) or is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return await call_next(request)
    if path in API_PATHS:
        return JSONResponse({"detail": "Not signed in"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
async def login_page(request: Request):
    if is_valid_session(request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB_DIR / "login.html", headers={"Cache-Control": "no-store"})


@app.post("/login")
async def login(request: Request):
    # Разбираем форму вручную, чтобы не тянуть python-multipart ради двух полей.
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    user = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    if not credentials_match(user, password):
        # Небольшая пауза делает перебор пароля медленнее.
        await asyncio.sleep(1)
        return RedirectResponse("/login?error=1", status_code=303)

    response = RedirectResponse("/", status_code=303)
    # Railway отдаёт сайт по HTTPS через прокси: схему берём из его заголовка,
    # локально (http://localhost) cookie остаётся без флага Secure.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        SESSION_COOKIE,
        new_session(),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=scheme == "https",
        samesite="lax",
    )
    return response


class TokenRequest(BaseModel):
    model: str = DEFAULT_MODEL
    character: str = DEFAULT_CHARACTER
    voice: str = DEFAULT_VOICE
    prompt: str = Field(default="", max_length=PROMPT_MAX_LENGTH)


@app.get("/catalog")
async def catalog():
    return public_catalog()


@app.post("/token")
async def create_token(body: TokenRequest):
    """Выдаёт браузеру одноразовую комнату + токен. Настройки сессии уезжают
    в атрибуты участника: воркер (main.py) читает их при входе в комнату."""
    if body.model not in MODEL_IDS:
        raise HTTPException(400, "Unknown model")
    if body.character not in CHARACTERS:
        raise HTTPException(400, "Unknown character")
    if body.voice not in VOICE_IDS:
        raise HTTPException(400, "Unknown voice")

    room_name = f"emma-{secrets.token_hex(6)}"
    identity = f"user-{secrets.token_hex(4)}"

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_attributes(
            {
                "emma.model": body.model,
                "emma.character": body.character,
                "emma.voice": body.voice,
                "emma.prompt": body.prompt.strip(),
            }
        )
        .to_jwt()
    )

    return {"url": LIVEKIT_URL, "token": token, "room": room_name}


@app.get("/health")
async def health():
    return {"ok": True}


# Страница прототипа живёт на том же домене, что и API. Монтируется последней,
# чтобы /catalog, /token и /health не перекрывались статикой.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
