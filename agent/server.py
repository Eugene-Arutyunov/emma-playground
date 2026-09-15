import os
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

# Общий код доступа для команды. Прототип закрытый: без кода токен не выдаётся,
# чтобы случайный человек со ссылкой не тратил баланс OpenRouter и Cartesia.
ACCESS_CODE = os.environ["EMMA_ACCESS_CODE"]

# Домены, с которых разрешено обращаться к этому серверу за токеном.
# Добавь сюда свой кастомный домен, если он появится.
ALLOWED_ORIGINS = [
    "https://eugene-arutyunov.github.io",
    "https://novanikita.github.io",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]

MODEL_IDS = {id for id, _ in MODELS}
VOICE_IDS = {id for id, _ in VOICES}

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


class TokenRequest(BaseModel):
    access_code: str
    model: str = DEFAULT_MODEL
    character: str = DEFAULT_CHARACTER
    voice: str = DEFAULT_VOICE
    prompt: str = Field(default="", max_length=PROMPT_MAX_LENGTH)
    lang: str = "ru"


@app.get("/catalog")
async def catalog():
    return public_catalog()


@app.post("/token")
async def create_token(body: TokenRequest):
    """Выдаёт браузеру одноразовую комнату + токен. Настройки сессии уезжают
    в атрибуты участника: воркер (main.py) читает их при входе в комнату."""
    if not secrets.compare_digest(body.access_code, ACCESS_CODE):
        raise HTTPException(403, "Неверный код доступа")
    if body.model not in MODEL_IDS:
        raise HTTPException(400, "Неизвестная модель")
    if body.character not in CHARACTERS:
        raise HTTPException(400, "Неизвестный характер")
    if body.voice not in VOICE_IDS:
        raise HTTPException(400, "Неизвестный голос")
    if body.lang not in ("ru", "en"):
        raise HTTPException(400, "Неизвестный язык")

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
                "emma.lang": body.lang,
            }
        )
        .to_jwt()
    )

    return {"url": LIVEKIT_URL, "token": token, "room": room_name}


@app.get("/health")
async def health():
    return {"ok": True}
