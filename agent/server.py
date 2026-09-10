import os
import secrets

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from livekit import api

load_dotenv()

LIVEKIT_URL = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]

# Домены, с которых разрешено обращаться к этому серверу за токеном.
# Добавь сюда свой кастомный домен, если он появится.
ALLOWED_ORIGINS = [
    "https://eugene-arutyunov.github.io",
    "https://novanikita.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/token")
async def get_token():
    """Выдаёт браузеру одноразовую комнату + токен. Воркер (main.py) сам
    заходит в любую новую комнату — implicit dispatch, без явного вызова API."""
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(500, "LiveKit не сконфигурирован на сервере")

    room_name = f"emma-{secrets.token_hex(6)}"
    identity = f"user-{secrets.token_hex(4)}"

    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    return {"url": LIVEKIT_URL, "token": token, "room": room_name}


@app.get("/health")
async def health():
    return {"ok": True}
