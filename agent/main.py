import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.voice.events import ConversationItemAddedEvent
from livekit.plugins import cartesia, deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from catalog import CHARACTERS, DEFAULT_CHARACTER, DEFAULT_MODEL, DEFAULT_VOICE

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

GREETING = {
    "ru": "Поздоровайся одной короткой фразой и спроси, о чём собеседник хочет поговорить.",
    "en": "Say hello in one short sentence and ask what the person would like to talk about.",
}

REFLECTION_INSTRUCTIONS = {
    "ru": """Ты — второй, наблюдающий канал голосового собеседника Эммы. В разговоре
ты не участвуешь. Твоя задача — замечать в последних репликах собеседника сигналы:
эмоциональное состояние, противоречие с тем, что он говорил раньше, невысказанный
вопрос, смену темы, усталость, сомнение, что человек ходит кругами.

Верни JSON-массив из 0–2 объектов вида {"signal": "<2–4 слова>", "note": "<одно
предложение>"}. Если ничего существенного нет — верни []. Только JSON, без пояснений.""",
    "en": """You are the second, observing channel of the voice companion Emma. You do
not take part in the conversation. Your job is to notice signals in the person's
latest turns: emotional state, a contradiction with what they said earlier, an
unasked question, a change of subject, fatigue, doubt, going in circles.

Return a JSON array of 0–2 objects like {"signal": "<2–4 words>", "note": "<one
sentence>"}. If nothing notable — return []. JSON only, no commentary.""",
}

SUMMARY_INSTRUCTIONS = {
    "ru": """Разговор закончился. Напиши для собеседника короткий итог от лица Эммы:
о чём говорили, к чему пришли, что осталось открытым. 3–5 предложений, без списков,
без вступления. Обращайся к собеседнику на «ты».""",
    "en": """The conversation is over. Write a short wrap-up for the person in Emma's
voice: what you talked about, where you landed, what is still open. 3–5 sentences,
no lists, no preamble.""",
}

SPEAKER = {
    "ru": {"user": "Собеседник", "assistant": "Эмма"},
    "en": {"user": "Person", "assistant": "Emma"},
}


@dataclass
class SessionConfig:
    model: str
    character: str
    voice: str
    prompt: str
    lang: str

    @classmethod
    def from_attributes(cls, attrs: Mapping[str, str]) -> "SessionConfig":
        return cls(
            model=attrs.get("emma.model") or DEFAULT_MODEL,
            character=attrs.get("emma.character") or DEFAULT_CHARACTER,
            voice=attrs.get("emma.voice") or DEFAULT_VOICE,
            prompt=(attrs.get("emma.prompt") or "").strip(),
            lang="en" if attrs.get("emma.lang") == "en" else "ru",
        )


def build_instructions(cfg: SessionConfig) -> str:
    character = CHARACTERS.get(cfg.character, CHARACTERS[DEFAULT_CHARACTER])
    instructions = character["instructions"]
    if cfg.prompt:
        instructions += "\n\nДополнительное пожелание собеседника к этому разговору: " + cfg.prompt
    return instructions


def openrouter_llm(model: str) -> openai.LLM:
    return openai.LLM(
        model=model,
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
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
        self.summary: str | None = None
        self._saved = False

    def turns(self) -> list[dict]:
        return [
            {"role": item.role, "text": item.text_content or "", "at": item.created_at}
            for item in self.session.history.items
            if item.type == "message" and item.role in ("user", "assistant")
        ]

    def as_text(self, lang: str) -> str:
        names = SPEAKER[lang]
        return "\n".join(f"{names[t['role']]}: {t['text']}" for t in self.turns() if t["text"])

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

    def __init__(self, room: rtc.Room, conversation: Conversation, model: llm.LLM, lang: str) -> None:
        self._room = room
        self._conversation = conversation
        self._model = model
        self._lang = lang
        self._busy = False

    async def observe(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            turns = self._conversation.turns()[-10:]
            if not any(t["role"] == "user" for t in turns):
                return
            names = SPEAKER[self._lang]
            text = "\n".join(f"{names[t['role']]}: {t['text']}" for t in turns if t["text"])
            raw = await complete(self._model, REFLECTION_INSTRUCTIONS[self._lang], text)
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


class Emma(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    participant = await ctx.wait_for_participant()
    cfg = SessionConfig.from_attributes(participant.attributes)
    logger.info("session config: %s", cfg)

    tts = cartesia.TTS(voice=cfg.voice, language=cfg.lang)

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
        # Турн-детектор создаётся только здесь: в prewarm ему не хватает
        # контекста задачи, и весь процесс падает с "no job context found".
        turn_handling=TurnHandlingOptions(turn_detection=MultilingualModel()),
    )

    conversation = Conversation(ctx.room.name, cfg, session)
    reflector = Reflector(ctx.room, conversation, openrouter_llm(REFLECTION_MODEL), cfg.lang)

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent) -> None:
        if getattr(ev.item, "role", None) == "user":
            asyncio.create_task(reflector.observe())

    async def finish() -> None:
        if conversation.summary is not None:
            return
        conversation.summary = ""
        try:
            conversation.summary = await complete(
                main_llm, SUMMARY_INSTRUCTIONS[cfg.lang], conversation.as_text(cfg.lang)
            )
        except Exception:
            logger.exception("summary failed")
        await ctx.room.local_participant.send_text(conversation.summary, topic=TOPIC_SUMMARY)
        conversation.save()

    def on_control(reader: rtc.TextStreamReader, participant_identity: str) -> None:
        async def handle() -> None:
            command = (await reader.read_all()).strip()
            if command == "end":
                await finish()

        asyncio.create_task(handle())

    ctx.room.register_text_stream_handler(TOPIC_CONTROL, on_control)

    async def save_on_shutdown() -> None:
        conversation.save()

    ctx.add_shutdown_callback(save_on_shutdown)

    await session.start(agent=Emma(build_instructions(cfg)), room=ctx.room)
    session.generate_reply(instructions=GREETING[cfg.lang])


if __name__ == "__main__":
    # Без agent_name — implicit dispatch: воркер сам заходит в любую новую
    # комнату. Работает и с Agents Playground, и с нашим token-сервером
    # без явного вызова dispatch API.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
