# Эмма — голосовой прототип (агент)

Python-воркер на LiveKit Agents: Deepgram (STT) → Claude через OpenRouter (LLM) →
Cartesia (TTS), с моделью turn-detection вместо голой VAD-паузы (см. находки
исследования про turn-taking как отдельную инженерную задачу).

Изменения по пути:
- Изначально планировался ElevenLabs, но он блокирует доступ из России (см.
  help.elevenlabs.io — санкционный список стран). Cartesia — рабочая замена.
- LLM ходит не напрямую в Anthropic, а через OpenRouter (OpenAI-совместимый API,
  `openai.LLM(base_url="https://openrouter.ai/api/v1", model="anthropic/claude-sonnet-5")`).
- Известный баг livekit-agents (github.com/livekit/agents/issues/4135, закрыт
  мейнтейнерами как "not planned"): первая отправка текста в TTS-стрим иногда
  проигрывает гонку с установкой websocket-соединения и падает с "no audio frames
  were pushed for text". Обходим явным прогревом соединения в начале `entrypoint`
  (см. `main.py`) — до старта сессии дожидаемся реального коннекта из пула Cartesia.

## 1. Заведи аккаунты и ключи

Скопируй `.env.example` в `.env` и впиши ключи:

```
cd agent
cp .env.example .env
```

- LiveKit: cloud.livekit.io → New Project → Settings → Keys (`LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`)
- Deepgram: console.deepgram.com → API Keys
- Cartesia: play.cartesia.ai → API Keys
- OpenRouter: openrouter.ai → Keys — там же пополняется баланс (Credits)

## 2. Установи зависимости

```
cd agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Первый запуск скачает модели VAD/turn-detector — команда ниже сделает это заранее:

```
python main.py download-files
```

## 3. Запусти воркер (агент)

```
python main.py dev
```

Воркер подключится к LiveKit Cloud проекту и будет ждать участников в комнате.
Оставь это окно терминала открытым.

## 4. Запусти token-сервер (для своей страницы)

В отдельном терминале, тоже из `agent/` с активным venv:

```
uvicorn server:app --reload
```

Он выдаёт браузеру одноразовую комнату + токен по адресу `/token`. Если сервер
будет доступен по адресу, отличному от `http://localhost:8000`, добавь его в
`ALLOWED_ORIGINS` в `server.py` (CORS).

## 5. Поговори с Эммой

Два варианта:

**Своя страница** (`../index.html` и `../en/index.html` в корне репозитория) —
открой файл в браузере или подними через любой статический сервер
(например `python -m http.server` из корня репозитория), впиши адрес
token-сервера в `EMMA_TOKEN_SERVER_URL` в `../js/emma-connect.js`, нажми
«Поговорить с Эммой».

**LiveKit Agents Playground** (без своего фронтенда, для быстрой проверки) —
открой https://agents-playground.livekit.io, залогинься тем же LiveKit Cloud
проектом, подключись к любой комнате — воркер зайдёт в неё автоматически.

Если слышишь ответы Эммы и она реагирует на речь — пайплайн STT→LLM→TTS работает.

## Дальше (следующие шаги)

- Задеплоить воркер и token-сервер на Railway/Render, чтобы работали постоянно,
  не только пока запущены локально.
- Добавить память между сессиями (сейчас Эмма помнит только текущий разговор).
