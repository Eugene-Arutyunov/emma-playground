import base64
import binascii
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
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
# спрятаны за HTTP Basic Auth, чтобы случайный человек со ссылкой не тратил
# баланс OpenRouter и Cartesia. Браузер сам запоминает логин и пароль и
# подставляет их в запросы страницы к /catalog и /token.
AUTH_USER = os.environ["EMMA_AUTH_USER"]
AUTH_PASSWORD = os.environ["EMMA_AUTH_PASSWORD"]
AUTH_REALM = "Emma prototype"

# Открыто без пароля: по /health проверяют, что сервис жив.
PUBLIC_PATHS = {"/health"}

WEB_DIR = Path(__file__).parent / "web"

MODEL_IDS = {id for id, _ in MODELS}
VOICE_IDS = {id for id, _ in VOICES}

app = FastAPI()


def is_authorized(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic ") :], validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    user, _, password = decoded.partition(":")
    user_ok = secrets.compare_digest(user.encode(), AUTH_USER.encode())
    password_ok = secrets.compare_digest(password.encode(), AUTH_PASSWORD.encode())
    return user_ok and password_ok


# Middleware, а не зависимость FastAPI: так авторизация закрывает и статику
# страницы, которую отдаёт StaticFiles, а не только API.
@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or is_authorized(request.headers.get("authorization")):
        return await call_next(request)
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"'},
    )


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
