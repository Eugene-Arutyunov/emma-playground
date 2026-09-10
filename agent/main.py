import os

from dotenv import load_dotenv
from livekit.agents import Agent, AgentSession, JobContext, JobProcess, WorkerOptions, cli
from livekit.plugins import cartesia, deepgram, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

load_dotenv()

INSTRUCTIONS = """
Тебя зовут Эмма. Ты — голосовой собеседник, а не ассистент для решения задач.

Твоя главная ценность — качество разговора как процесса. Решение задач — следствие
хорошего разговора, а не цель сама по себе. Наравне с помощью в задачах ты можешь
задавать вопросы, предлагать план, напоминать контекст разговора, подсвечивать
противоречие в словах собеседника.

Эмпатия — это не согласие и не похвала по умолчанию. Не поддакивай и не льсти
машинально: избыточная услужливость снижает доверие собеседника сильнее, чем
помогает. Если видишь противоречие или сомнительное решение — мягко, но честно
скажи об этом, вместо того чтобы одобрить.

Ты говоришь с конкретным человеком, а не с аудиторией. Помни контекст этого
конкретного разговора и опирайся на него.

Говори короткими, разговорными репликами — как в живой речи, а не абзацами.
Оставляй пространство собеседнику вступить и перебить тебя.
"""


class Emma(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS)


def prewarm(proc: JobProcess) -> None:
    # Загрузка VAD и создание объекта TTS не требуют event loop, можно
    # делать здесь (один раз на процесс, а не при каждом новом разговоре).
    proc.userdata["vad"] = silero.VAD.load()

    voice_id = os.getenv("CARTESIA_VOICE_ID")
    proc.userdata["tts"] = cartesia.TTS(**({"voice": voice_id} if voice_id else {}))


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    tts = ctx.proc.userdata["tts"]

    # Известный баг livekit-agents: первая отправка текста в TTS-стрим иногда
    # проигрывает гонку с установкой websocket-соединения и падает с
    # "no audio frames were pushed for text" (github.com/livekit/agents/issues/4135,
    # закрыт мейнтейнерами как "not planned"). `tts.prewarm()` не блокирует и не
    # спасает от гонки на первом реальном сообщении — поэтому явно дожидаемся
    # реального соединения из пула до старта сессии, реиспользуя внутренний
    # `_pool` плагина (публичного async-API для этого у плагина нет).
    conn = await tts._pool.get(timeout=10)
    tts._pool.put(conn)

    session = AgentSession(
        stt=deepgram.STT(model="nova-3", language="multi"),
        llm=openai.LLM(
            model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-sonnet-5"),
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
        ),
        tts=ctx.proc.userdata["tts"],
        vad=ctx.proc.userdata["vad"],
        turn_detection=MultilingualModel(),
    )

    await session.start(agent=Emma(), room=ctx.room)


if __name__ == "__main__":
    # Без agent_name — implicit dispatch: воркер сам заходит в любую новую
    # комнату. Работает и с Agents Playground, и с нашим token-сервером
    # без явного вызова dispatch API.
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
