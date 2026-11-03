import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobExecutorType,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    inference,
    llm,
)
from livekit.agents.voice.events import ConversationItemAddedEvent, ErrorEvent, UserStateChangedEvent
from livekit.plugins import cartesia, deepgram, openai, silero

from catalog import CHARACTERS, DEFAULT_CHARACTER, DEFAULT_MODEL, DEFAULT_VOICE
from velma import (
    BEHAVIOR_MOODS,
    DEFAULT_URL as VELMA_DEFAULT_URL,
    VelmaBehavior,
    VelmaEmotion,
    VelmaEvent,
    VelmaStatus,
    VelmaStream,
)

load_dotenv()
logger = logging.getLogger("emma")

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REFLECTION_MODEL = os.getenv("EMMA_REFLECTION_MODEL", "anthropic/claude-haiku-4.5")
TRANSCRIPTS_DIR = Path(os.getenv("EMMA_TRANSCRIPTS_DIR", "transcripts"))

# Текстовые потоки между воркером и страницей. Транскрипт речи LiveKit
# публикует сам в топике "lk.transcription".
TOPIC_SIGNALS = "emma.signals"
TOPIC_SUMMARY = "emma.summary"
TOPIC_CONTROL = "emma.control"
# Сбои звеньев конвейера (распознавание, модель, голос) — чтобы страница
# объясняла, почему Эмма молчит, а не просто молчала вместе с ней.
TOPIC_STATUS = "emma.status"

PIPELINE_STAGES = {"stt_error": "stt", "llm_error": "llm", "tts_error": "tts"}
# Время ответа: от момента, когда собеседник замолчал, до первого звука Эммы,
# с разбивкой по звеньям — чтобы сравнивать модели и голоса.
TOPIC_LATENCY = "emma.latency"
# Настроение собеседника, услышанное Velma: злость и грусть. Страница красит
# интерфейс, Эмма спрашивает, что случилось. Туда же уходит состояние самой
# Velma (слушает, переподключается, выключена).
TOPIC_EMOTION = "emma.emotion"

VELMA_URL = os.getenv("VELMA_STREAMING_URL", VELMA_DEFAULT_URL)
# Акустические метки Velma, которые считаем сигналами. Строго: только эти две,
# остальные метки пишутся в транскрипт для оценки, но ничего не запускают.
EMOTION_MOODS = {"angry": "angry", "sad": "sad"}
# Заметка для модели ждёт следующей реплики собеседника не дольше этого (секунды)...
MOOD_NOTE_MAX_AGE = 45.0
# ...а одно настроение в этом окне считается одним эпизодом: одна заметка, один вопрос.
MOOD_EPISODE_S = 60.0
MOOD_NOTE = {
    "voice": "System note: the person's voice sounds {mood} right now, whatever their words say.",
    "words": "System note: the person has just said they are {mood}. {reason}",
    "test": "System note: the person sounds {mood} right now.",
}
# Голос важнее слов: детекция по словам, пришедшая в это окно после голосовой
# метки с другим настроением, остаётся отметкой на реплике, но интерфейс не
# красит и вопроса у Эммы не вызывает.
VOICE_WINS_S = 20.0


def latency_report(assistant_metrics: Mapping, user_metrics: Mapping) -> dict | None:
    total = assistant_metrics.get("e2e_latency")
    if total is None:
        return None
    parts = {
        "total": total,
        "turn": user_metrics.get("end_of_turn_delay"),
        "model": assistant_metrics.get("llm_node_ttft"),
        "voice": assistant_metrics.get("tts_node_ttfb"),
    }
    return {key: round(value, 2) for key, value in parts.items() if value is not None}

GREETING = "Say hello in one short sentence and ask what the person would like to talk about."

REFLECTION_INSTRUCTIONS = """You are the second, observing channel of the voice companion Emma. You do
not take part in the conversation. Your job is to notice signals in the person's
latest turns: emotional state, a contradiction with what they said earlier, an
unasked question, a change of subject, fatigue, doubt, going in circles.

Return a JSON array of 0–2 objects like {"signal": "<2–4 words>", "note": "<one
sentence>"}, written in English. If nothing notable — return []. JSON only, no commentary."""

SUMMARY_INSTRUCTIONS = """The conversation is over. Write a short wrap-up for the person in Emma's
voice, in English: what you talked about, where you landed, what is still open.
3–5 sentences, no lists, no preamble."""

SPEAKER = {"user": "Person", "assistant": "Emma"}


@dataclass
class SessionConfig:
    model: str
    character: str
    voice: str
    prompt: str

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, str]) -> "SessionConfig":
        return cls(
            model=attrs.get("emma.model") or DEFAULT_MODEL,
            character=attrs.get("emma.character") or DEFAULT_CHARACTER,
            voice=attrs.get("emma.voice") or DEFAULT_VOICE,
            prompt=(attrs.get("emma.prompt") or "").strip(),
        )


def build_instructions(cfg: SessionConfig) -> str:
    character = CHARACTERS.get(cfg.character, CHARACTERS[DEFAULT_CHARACTER])
    instructions = character["instructions"]
    if cfg.prompt:
        instructions += "\n\nThe person's additional request for this conversation: " + cfg.prompt
    return instructions


# Потолок длины ответа. Реплики в голосе короткие, а без потолка OpenRouter
# резервирует под ответ максимум модели (десятки тысяч токенов) и отказывает
# с 402, когда лимит ключа или баланс меньше этого резерва.
REPLY_MAX_TOKENS = 800
REFLECTION_MAX_TOKENS = 300


def openrouter_llm(model: str, max_tokens: int = REPLY_MAX_TOKENS) -> openai.LLM:
    return openai.LLM(
        model=model,
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        max_completion_tokens=max_tokens,
    )


async def complete(model: llm.LLM, instructions: str, user_text: str) -> str:
    chat_ctx = llm.ChatContext()
    chat_ctx.add_message(role="system", content=instructions)
    chat_ctx.add_message(role="user", content=user_text)
    parts: list[str] = []
    async with model.chat(chat_ctx=chat_ctx) as stream:
        async for chunk in stream:
            if chunk.delta and chunk.delta.content:
                parts.append(chunk.delta.content)
    return "".join(parts).strip()


def parse_signals(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < 0:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [
        {"signal": str(s["signal"]).strip(), "note": str(s["note"]).strip()}
        for s in data
        if isinstance(s, dict) and s.get("signal") and s.get("note")
    ]


class Conversation:
    def __init__(self, room_name: str, cfg: SessionConfig, session: AgentSession) -> None:
        self.room_name = room_name
        self.cfg = cfg
        self.session = session
        self.started_at = time.time()
        self.signals: list[dict] = []
        self.moods: list[dict] = []
        # Всё, что услышала Velma, не только злость и грусть: для оценки её меток.
        self.velma_clips: dict[str, dict] = {}
        self.behaviors: list[dict] = []
        self.summary: str | None = None
        self._saved = False

    def turns(self) -> list[dict]:
        turns = []
        for item in self.session.history.items:
            if item.type != "message" or item.role not in ("user", "assistant"):
                continue
            turn = {"role": item.role, "text": item.text_content or "", "at": item.created_at}
            latency = (item.metrics or {}).get("e2e_latency")
            if latency is not None:
                turn["latency"] = round(latency, 2)
            turns.append(turn)
        return turns

    def as_text(self) -> str:
        return "\n".join(f"{SPEAKER[t['role']]}: {t['text']}" for t in self.turns() if t["text"])

    def save(self) -> None:
        if self._saved:
            return
        self._saved = True
        data = {
            "room": self.room_name,
            "started_at": self.started_at,
            "ended_at": time.time(),
            "config": asdict(self.cfg),
            "turns": self.turns(),
            "signals": self.signals,
            "moods": self.moods,
            "velma": {"clips": list(self.velma_clips.values()), "behaviors": self.behaviors},
            "summary": self.summary,
        }
        try:
            TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            path = TRANSCRIPTS_DIR / f"{self.room_name}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            logger.info("transcript saved to %s", path)
        except OSError:
            logger.exception("could not write transcript to %s", TRANSCRIPTS_DIR)
        # Дублируем в лог одной строкой: на Railway без volume диск не переживает
        # передеплой, а логи остаются.
        logger.info("transcript %s", json.dumps(data, ensure_ascii=False))


class Reflector:
    """Второй канал: после каждой реплики собеседника смотрит на последние
    ходы разговора и отправляет на страницу 0–2 наблюдения."""

    def __init__(self, room: rtc.Room, conversation: Conversation, model: llm.LLM) -> None:
        self._room = room
        self._conversation = conversation
        self._model = model
        self._busy = False

    async def observe(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            turns = self._conversation.turns()[-10:]
            if not any(t["role"] == "user" for t in turns):
                return
            text = "\n".join(f"{SPEAKER[t['role']]}: {t['text']}" for t in turns if t["text"])
            raw = await complete(self._model, REFLECTION_INSTRUCTIONS, text)
            for signal in parse_signals(raw)[:2]:
                signal["at"] = time.time()
                self._conversation.signals.append(signal)
                await self._room.local_participant.send_text(
                    json.dumps(signal, ensure_ascii=False), topic=TOPIC_SIGNALS
                )
        except Exception:
            logger.exception("reflection failed")
        finally:
            self._busy = False


class Mood:
    """Превращает метки и детекции Velma в два сигнала Эммы, злость и грусть:
    сообщает странице, пишет в транскрипт и оставляет заметку для следующего
    ответа Эммы, чтобы она спросила, что случилось."""

    def __init__(self, room: rtc.Room, conversation: Conversation, speech_end: Callable[[], float]) -> None:
        self._room = room
        self._conversation = conversation
        self._speech_end = speech_end
        # Какое настроение уже отправлено по клипу: partial, final и update одной
        # реплики дают одну метку, а не три сигнала.
        self._signaled: dict[str, str] = {}
        self._voice: tuple[str, float] | None = None
        self._note: tuple[str, str, float] | None = None
        self._last_asked: tuple[str, float] | None = None

    def on_velma(self, ev: VelmaEvent) -> None:
        if isinstance(ev, VelmaEmotion):
            clip = self._conversation.velma_clips.setdefault(ev.clip_uuid, {"clip_uuid": ev.clip_uuid})
            if ev.text:
                clip["text"] = ev.text
            clip.update({"emotion": ev.label, "stage": ev.stage, "at": ev.at})
            label = ev.label or ""
            mood = EMOTION_MOODS.get(label.lower())
            if mood:
                if self._signaled.get(ev.clip_uuid) != mood:
                    self._signaled[ev.clip_uuid] = mood
                    self.signal(
                        mood, "voice", label=label, stage=ev.stage, clip=ev.clip_uuid, text=clip.get("text", "")
                    )
            elif label and ev.stage != "partial":
                # Остальные метки не сигналы, но страница показывает их на реплике
                # бледной плашкой: видно, что Velma слышит на каждой фразе.
                self._send(
                    {"label": label, "stage": ev.stage, "clip": ev.clip_uuid, "text": clip.get("text", ""), "at": ev.at}
                )
        elif isinstance(ev, VelmaBehavior):
            self._conversation.behaviors.append(asdict(ev))
            mood = BEHAVIOR_MOODS.get(ev.behavior_uuid)
            if mood and ev.detected:
                self.signal(mood, "words", note=ev.reasoning, confidence=ev.confidence)
        elif isinstance(ev, VelmaStatus):
            logger.info("velma %s %s", ev.state, ev.detail)
            self._send({"status": ev.state, "note": ev.detail})

    def signal(self, mood: str, source: str, note: str = "", **extra) -> None:
        now = time.time()
        payload = {"mood": mood, "source": source, "note": note, "at": now, **extra}
        end = self._speech_end()
        if end and 0 <= now - end < 30:
            payload["delay"] = round(now - end, 2)
        if source == "voice":
            self._voice = (mood, now)
        elif source == "words" and self._voice and self._voice[0] != mood and now - self._voice[1] < VOICE_WINS_S:
            payload["overruled"] = self._voice[0]
        self._conversation.moods.append(payload)
        logger.info("mood signal: %s", payload)
        self._send(payload)
        if "overruled" in payload:
            return
        if self._note is None:
            asked = self._last_asked
            if asked and asked[0] == mood and now - asked[1] < MOOD_EPISODE_S:
                return  # Эмма уже спрашивала про этот эпизод
        self._note = (mood, MOOD_NOTE[source].format(mood=mood, reason=note).strip(), now)

    def take_note(self) -> str | None:
        """Заметка для модели перед её ответом: выдаётся один раз и только свежая."""
        note, self._note = self._note, None
        if note is None or time.time() - note[2] > MOOD_NOTE_MAX_AGE:
            return None
        self._last_asked = (note[0], time.time())
        return note[1]

    def _send(self, payload: dict) -> None:
        asyncio.create_task(
            self._room.local_participant.send_text(json.dumps(payload, ensure_ascii=False), topic=TOPIC_EMOTION)
        )


class Emma(Agent):
    def __init__(self, instructions: str, mood: Mood, velma: VelmaStream | None) -> None:
        super().__init__(instructions=instructions)
        self._mood = mood
        self._velma = velma

    # Звук собеседника раздваивается: как и раньше идёт в Deepgram, а копия в Velma.
    def stt_node(self, audio, model_settings):
        if self._velma is None:
            return Agent.default.stt_node(self, audio, model_settings)

        async def tee():
            async for frame in audio:
                self._velma.push(frame)
                yield frame

        return Agent.default.stt_node(self, tee(), model_settings)

    # Перед ответом модель получает заметку о настроении, если она есть. Контекст
    # этого хода одноразовый: в историю заметка не попадает, а ответ Эммы попадает.
    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        note = self._mood.take_note()
        if note:
            logger.info("mood note for the model: %s", note)
            turn_ctx.add_message(role="system", content=note)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    participant = await ctx.wait_for_participant()
    cfg = SessionConfig.from_attributes(participant.attributes)
    logger.info("session config: %s", cfg)

    tts = cartesia.TTS(voice=cfg.voice, language="en")

    # Известный баг livekit-agents: первая отправка текста в TTS-стрим иногда
    # проигрывает гонку с установкой websocket-соединения и падает с
    # "no audio frames were pushed for text" (github.com/livekit/agents/issues/4135,
    # закрыт мейнтейнерами как "not planned"). `tts.prewarm()` не блокирует и не
    # спасает от гонки на первом реальном сообщении — поэтому явно дожидаемся
    # реального соединения из пула до старта сессии, реиспользуя внутренний
    # `_pool` плагина (публичного async-API для этого у плагина нет).
    conn = await tts._pool.get(timeout=10)
    tts._pool.put(conn)

    main_llm = openrouter_llm(cfg.model)
    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=main_llm,
        tts=tts,
        vad=ctx.proc.userdata["vad"],
        # Конец реплики определяет облачная модель LiveKit Inference (ключи те же,
        # что у LiveKit). Процесс с локальной моделью на Railway падал при старте
        # воркера ("worker failed"); local_fallback=False не даёт подгрузить её
        # даже при сбое облака (тогда реплика завершается по паузе).
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(version="v1", local_fallback=False)
        ),
    )

    conversation = Conversation(ctx.room.name, cfg, session)
    reflector = Reflector(ctx.room, conversation, openrouter_llm(REFLECTION_MODEL, REFLECTION_MAX_TOKENS))

    # Когда собеседник замолчал: от этого момента считается, насколько Velma опаздывает.
    last_speech_end = 0.0
    mood = Mood(ctx.room, conversation, lambda: last_speech_end)

    velma: VelmaStream | None = None
    velma_key = os.getenv("MODULATE_API_KEY")
    if velma_key:
        velma = VelmaStream(velma_key, mood.on_velma, url=VELMA_URL)
    else:
        logger.warning("MODULATE_API_KEY is not set: Velma is off, mood signals come only from the page")

    @session.on("user_state_changed")
    def _on_user_state(ev: UserStateChangedEvent) -> None:
        nonlocal last_speech_end
        if ev.old_state == "speaking" and ev.new_state == "listening":
            last_speech_end = time.time()

    # Задержка конца реплики лежит в метриках реплики собеседника, остальное —
    # в метриках ответа Эммы, который приходит, когда она договорила.
    last_user_metrics: dict = {}

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent) -> None:
        role = getattr(ev.item, "role", None)
        metrics = getattr(ev.item, "metrics", None) or {}
        if role == "user":
            last_user_metrics.clear()
            last_user_metrics.update(metrics)
            asyncio.create_task(reflector.observe())
        elif role == "assistant":
            report = latency_report(metrics, last_user_metrics)
            if report:
                asyncio.create_task(
                    ctx.room.local_participant.send_text(json.dumps(report), topic=TOPIC_LATENCY)
                )

    @session.on("error")
    def _on_error(ev: ErrorEvent) -> None:
        stage = PIPELINE_STAGES.get(getattr(ev.error, "type", ""))
        if stage is None:
            return
        status = {
            "stage": stage,
            "recoverable": bool(ev.error.recoverable),
            "detail": str(ev.error.error)[:240],
        }
        logger.warning("pipeline error: %s", status)
        asyncio.create_task(
            ctx.room.local_participant.send_text(json.dumps(status), topic=TOPIC_STATUS)
        )

    async def finish() -> None:
        if conversation.summary is not None:
            return
        conversation.summary = ""
        # Velma закрывается параллельно с итогом: дослушивает хвост и отдаёт последние метки.
        closing = asyncio.create_task(velma.aclose()) if velma is not None else None
        try:
            conversation.summary = await complete(main_llm, SUMMARY_INSTRUCTIONS, conversation.as_text())
        except Exception:
            logger.exception("summary failed")
        if closing is not None:
            await closing
        await ctx.room.local_participant.send_text(conversation.summary, topic=TOPIC_SUMMARY)
        conversation.save()

    def on_control(reader: rtc.TextStreamReader, participant_identity: str) -> None:
        async def handle() -> None:
            command = (await reader.read_all()).strip()
            if command == "end":
                await finish()
            elif command.startswith("mood:"):
                # Тестовые кнопки на странице (?dev): тот же путь, что у сигнала от Velma.
                requested = command.removeprefix("mood:")
                if requested in EMOTION_MOODS:
                    mood.signal(requested, "test", note="Triggered from the page")

        asyncio.create_task(handle())

    ctx.room.register_text_stream_handler(TOPIC_CONTROL, on_control)

    async def save_on_shutdown() -> None:
        if velma is not None:
            await velma.aclose()
        conversation.save()

    ctx.add_shutdown_callback(save_on_shutdown)

    if velma is not None:
        velma.start()
    await session.start(agent=Emma(build_instructions(cfg), mood, velma), room=ctx.room)
    if velma is None:
        mood.on_velma(VelmaStatus("off", "no MODULATE_API_KEY"))
    session.generate_reply(instructions=GREETING)


if __name__ == "__main__":
    # Без agent_name — implicit dispatch: воркер сам заходит в любую новую
    # комнату. Работает и с Agents Playground, и с нашим token-сервером
    # без явного вызова dispatch API.
    #
    # Разговоры идут в потоках одного процесса, а не в отдельных процессах:
    # по умолчанию воркер держит до 4 прогретых процессов по ~260 МБ каждый
    # (~1,2 ГБ ещё до первого разговора), и Railway убивал контейнер по памяти
    # (exit code -9). В потоках весь воркер занимает ~400 МБ. Для командного
    # прототипа с редкими параллельными разговорами этого достаточно.
    # spawn вместо linux-овского forkserver: forkserver заранее грузит в память
    # локальные модели livekit-local-inference, которые нам не нужны.
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            job_executor_type=JobExecutorType.THREAD,
            num_idle_processes=1,
            multiprocessing_context="spawn",
        )
    )
