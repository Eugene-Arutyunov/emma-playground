import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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

# Общий код доступа для команды. Прототип закрытый: без кода токен не выдаётся,
# чтобы случайный человек со ссылкой не тратил баланс OpenRouter и Cartesia.
ACCESS_CODE = os.environ["EMMA_ACCESS_CODE"]

WEB_DIR = Path(__file__).parent / "web"

MODEL_IDS = {id for id, _ in MODELS}
VOICE_IDS = {id for id, _ in VOICES}

app = FastAPI()


class TokenRequest(BaseModel):
    access_code: str
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
    if not secrets.compare_digest(body.access_code, ACCESS_CODE):
        raise HTTPException(403, "Wrong access code")
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
