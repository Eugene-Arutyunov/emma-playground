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

## 4. Запусти token-сервер (он же отдаёт страницу)

В отдельном терминале, тоже из `agent/` с активным venv:

```
uvicorn server:app --reload --port 8080
```

Один процесс и отдаёт страницу прототипа (`web/`), и обслуживает её API:
список моделей, характеров и голосов (`/catalog`) и одноразовую комнату +
токен (`/token`). Страница и API на одном домене, поэтому адрес сервера нигде
прописывать не нужно и CORS не настраивается. В `.env` нужен
`EMMA_ACCESS_CODE` — общий код доступа для команды, страница спросит его при
первом нажатии.

## 5. Поговори с Эммой

Два варианта:

**Своя страница** — открой http://localhost:8080 (английская версия —
http://localhost:8080/en/), выбери модель, характер и голос, нажми
«Поговорить с Эммой». Файлы страницы лежат в `web/`.

Что происходит во время разговора: транскрипт речи приходит на страницу
штатным потоком LiveKit (`lk.transcription`); второй канал (`Reflector` в
`main.py`) после каждой реплики собеседника смотрит на последние ходы и
шлёт 0–2 наблюдения в поток `emma.signals` — это панель «Что Эмма замечает».
Кнопка «Закончить разговор» отправляет `end` в `emma.control`; воркер пишет
итог разговора в `emma.summary`, сохраняет транскрипт и только потом страница
отключается.

**LiveKit Agents Playground** (без своего фронтенда, для быстрой проверки) —
открой https://agents-playground.livekit.io, залогинься тем же LiveKit Cloud
проектом, подключись к любой комнате — воркер зайдёт в неё автоматически.

Если слышишь ответы Эммы и она реагирует на речь — пайплайн STT→LLM→TTS работает.

## 6. Деплой на Railway (чтобы ссылка работала без терминала)

Оба сервиса собираются из одного `agent/Dockerfile`, различаются только
командой старта. Сервис `token` одновременно отдаёт страницу прототипа, так что
GitHub Pages не нужен: прототип открывается прямо по адресу сервиса `token`.

1. railway.app → New Project → Deploy from GitHub repo → `emma-playground`.
2. В созданном сервисе: Settings → Source → Root Directory = `agent`.
   Переименуй сервис в `worker`. Команду старта не трогай (из Dockerfile:
   `python main.py start`).
3. Variables → вставь все ключи из `.env` (без `EMMA_ACCESS_CODE` — воркеру
   он не нужен). Удобно через Raw Editor одним куском. Опционально:
   `EMMA_REFLECTION_MODEL` — модель второго канала (по умолчанию
   `anthropic/claude-haiku-4.5`), `EMMA_TRANSCRIPTS_DIR` — куда писать
   транскрипты (по умолчанию `transcripts/` рядом с кодом; без Railway Volume
   папка не переживает передеплой, но транскрипт дублируется в логи).
4. Второй сервис: в проекте нажми New → GitHub Repo → тот же репозиторий.
   Settings → Root Directory = `agent`, Custom Start Command =
   `uvicorn server:app --host 0.0.0.0 --port 8080`. Назови `token`.
   Порт пишем числом: у сборки из Dockerfile Railway не подставляет `$PORT`
   в команду старта, и uvicorn падает с `'$PORT' is not a valid integer`.
5. Variables для `token`: `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
   `LIVEKIT_API_SECRET`, `EMMA_ACCESS_CODE` (придумай код для команды).
6. Settings → Networking → Generate Domain у сервиса `token`, порт `8080`.
   Это и есть адрес прототипа (сейчас https://token-production-a25e.up.railway.app).

Проверка: `https://<домен token>/health` отвечает `{"ok": true}`; в логах
`worker` есть строка `registered worker`. После этого `https://<домен token>/`
открывает страницу, при первом нажатии спрашивает код доступа и соединяет с Эммой.

Каждый пуш в `main` пересобирает оба сервиса.

## Дальше (следующие шаги)

- Добавить память между сессиями (сейчас Эмма помнит только текущий разговор).
