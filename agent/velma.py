"""Velma (Modulate) as Emma's second ear.

The worker streams the person's microphone audio to Velma's full streaming
analysis in parallel with Deepgram (see `Emma.stt_node` in main.py). Velma
answers with two kinds of things we use:

- an acoustic emotion label per utterance: how it was said, not what was said
  (`partial_clip` while the person is still talking, `clip` when the utterance
  is final, `clip_update` when the label is refined);
- detections of the two custom behaviors below: what was said, judged by Velma
  over the transcript, with a confidence and a one-line reasoning.

Everything else Velma can produce (topics, summary, roles, conversation type)
is switched off in the config. Protocol: docs.modulate.ai/api-reference/velma/streaming.
"""

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass
from typing import Callable

import aiohttp
import numpy as np
from livekit import rtc

logger = logging.getLogger("emma.velma")


def _ssl_context() -> ssl.SSLContext:
    """Python from python.org on macOS ships an empty trust store, so the default
    context rejects platform.modulate.ai. The certifi bundle (Mozilla's roots,
    already in the venv as a dependency of the OpenAI client) is used instead,
    the same way livekit-agents does for its own connections."""
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())

DEFAULT_URL = "wss://platform.modulate.ai/api/velma-2-streaming"
SAMPLE_RATE = 16000
# Audio leaves in ~100 ms pieces: small enough for Velma's partial results,
# few enough messages.
CHUNK_BYTES = SAMPLE_RATE * 2 // 10
# How much audio may wait for Velma before the oldest piece is dropped (5 s).
QUEUE_MAX_CHUNKS = 50
RECONNECT_DELAYS = (1, 2, 4)
# A connection that lived this long counts as healthy: reconnect attempts start over.
HEALTHY_CONNECTION_S = 60
# Close codes after which reconnecting is pointless.
FATAL_CLOSE_CODES = {
    4001: "invalid API key",
    4003: "request not permitted",
    4004: "no access to this model",
    4029: "insufficient credits",
    4030: "concurrent connection limit reached",
    4031: "monthly usage limit reached",
}

# Behaviors are identified by these UUIDs in Velma's detections; keep them stable.
ANGRY_BEHAVIOR_UUID = "b178304c-b5d8-41d0-bc4c-aebbe738f17a"
SAD_BEHAVIOR_UUID = "8a608e35-655b-4495-a67e-48f4aae093f4"

# Written the way Modulate's guide asks: criteria rather than examples, plus
# explicit "do not flag" rules. `trigger_limit: None` means every occurrence is
# reported, not only the first one per speaker.
BEHAVIORS = [
    {
        "behavior_uuid": ANGRY_BEHAVIOR_UUID,
        "name": "Says They Are Angry",
        "short_description": "The speaker states in the first person that they are angry right now.",
        "detailed_description": (
            "This behavior is present if the speech meets ALL of the following criteria: "
            "the speaker attributes a state of anger to themselves in the first person, with a "
            "predicate adjective or verb phrase for anger (angry, mad, furious, pissed off, "
            "livid, enraged, or the equivalent in the speaker's language); "
            "the state is presented as current, in the present tense or with a marker such as "
            "'right now' or 'today'; "
            "the statement is about the speaker's own feeling, not a report of someone else's. "
            "Do NOT flag if: the statement is negated ('I'm not angry'); the anger is attributed "
            "to a third party or quoted from someone else; the statement is hypothetical or "
            "conditional ('I would be angry if'); the speaker describes a past episode in the "
            "past tense; the word is used as an intensifier about something else rather than "
            "the speaker's state ('this traffic is mad')."
        ),
        "trigger_limit": None,
    },
    {
        "behavior_uuid": SAD_BEHAVIOR_UUID,
        "name": "Says They Are Sad",
        "short_description": "The speaker states in the first person that they are sad right now.",
        "detailed_description": (
            "This behavior is present if the speech meets ALL of the following criteria: "
            "the speaker attributes a state of sadness to themselves in the first person, with a "
            "predicate adjective or verb phrase for sadness (sad, upset, down, depressed, "
            "heartbroken, miserable, unhappy, feel like crying, or the equivalent in the "
            "speaker's language); "
            "the state is presented as current, in the present tense or with a marker such as "
            "'right now', 'today' or 'lately'; "
            "the statement is about the speaker's own feeling, not a report of someone else's. "
            "Do NOT flag if: the statement is negated ('I'm not sad'); the sadness is attributed "
            "to a third party or quoted from someone else; the statement is hypothetical or "
            "conditional ('I would be sad if'); the speaker describes a past episode in the past "
            "tense; the word describes a thing rather than the speaker's state ('that film is "
            "sad')."
        ),
        "trigger_limit": None,
    },
]
BEHAVIOR_MOODS = {ANGRY_BEHAVIOR_UUID: "angry", SAD_BEHAVIOR_UUID: "sad"}

CONFIG = {
    "behaviors": BEHAVIORS,
    # Only the person's microphone is streamed: one speaker, nothing to diarize.
    # Emotion is always on in this endpoint; the flag is kept for clarity.
    "stt": {"speaker_diarization": False, "emotion_signal": True},
    # No candidates: Velma skips inferring the conversation type and roles.
    "conversation_types": [],
    "participant_roles": [],
    "produce_topics": False,
    "produce_topic_sentiments": False,
    "produce_summary": False,
}


@dataclass
class VelmaEmotion:
    clip_uuid: str
    label: str | None
    text: str
    stage: str  # "partial" while the person still talks, "final", or "update"
    at: float


@dataclass
class VelmaBehavior:
    behavior_uuid: str
    name: str
    detected: bool
    confidence: float | None
    reasoning: str
    at: float


@dataclass
class VelmaStatus:
    state: str  # "listening" | "retrying" | "off"
    detail: str


VelmaEvent = VelmaEmotion | VelmaBehavior | VelmaStatus


class VelmaFatalError(Exception):
    pass


class VelmaStream:
    """One websocket to Velma for the whole conversation, with reconnects.

    `push()` is called for every audio frame from the STT node and must stay
    cheap: it resamples to 16 kHz mono and queues ~100 ms pieces. A background
    task sends them and turns Velma's events into callbacks."""

    def __init__(self, api_key: str, on_event: Callable[[VelmaEvent], None], url: str = DEFAULT_URL) -> None:
        self._api_key = api_key
        self._url = (
            f"{url}?api_key={api_key}&audio_format=s16le&sample_rate={SAMPLE_RATE}&num_channels=1"
        )
        self._ssl = _ssl_context() if url.startswith("wss://") else None
        self._on_event = on_event
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self._resampler: rtc.AudioResampler | None = None
        self._input_rate = 0
        self._pending = bytearray()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._dropped = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="velma")

    # --- audio in ------------------------------------------------------------

    def push(self, frame: rtc.AudioFrame) -> None:
        if self._closing:
            return
        if frame.num_channels != 1:
            samples = np.frombuffer(frame.data, dtype=np.int16).reshape(-1, frame.num_channels)
            mono = samples.mean(axis=1).astype(np.int16)
            frame = rtc.AudioFrame(
                data=mono.tobytes(),
                sample_rate=frame.sample_rate,
                num_channels=1,
                samples_per_channel=len(mono),
            )
        if frame.sample_rate == SAMPLE_RATE:
            self._pending += bytes(frame.data)
        else:
            if self._resampler is None or self._input_rate != frame.sample_rate:
                self._resampler = rtc.AudioResampler(frame.sample_rate, SAMPLE_RATE)
                self._input_rate = frame.sample_rate
            for out in self._resampler.push(frame):
                self._pending += bytes(out.data)
        while len(self._pending) >= CHUNK_BYTES:
            self._enqueue(bytes(self._pending[:CHUNK_BYTES]))
            del self._pending[:CHUNK_BYTES]

    def _enqueue(self, item: bytes | None) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Velma is not keeping up: lose the oldest audio, not the newest.
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._queue.put_nowait(item)
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                logger.warning("velma is lagging, dropped %d audio chunks so far", self._dropped)

    # --- connection ----------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            started = time.monotonic()
            try:
                await self._run_once()
                if self._closing:
                    return
                reason = "stream ended"
            except VelmaFatalError as err:
                reason = self._redact(str(err))
                logger.error("velma: %s", reason)
                self._emit(VelmaStatus("off", reason))
                return
            except Exception as err:  # network, protocol, server errors
                # aiohttp puts the request URL, api_key included, into its error text.
                reason = self._redact(f"{type(err).__name__}: {err}")[:160]
                logger.warning("velma connection failed: %s", reason)
            if time.monotonic() - started > HEALTHY_CONNECTION_S:
                attempt = 0
            if attempt >= len(RECONNECT_DELAYS):
                self._emit(VelmaStatus("off", f"gave up after {attempt} reconnects: {reason}"))
                return
            delay = RECONNECT_DELAYS[attempt]
            attempt += 1
            self._emit(VelmaStatus("retrying", reason))
            await asyncio.sleep(delay)

    def _redact(self, text: str) -> str:
        """Strip the API key from anything that is logged or sent to the page."""
        return text.replace(self._api_key, "<api_key>") if self._api_key else text

    async def _run_once(self) -> None:
        async with aiohttp.ClientSession() as http:
            try:
                ws = await http.ws_connect(
                    self._url, ssl=self._ssl, heartbeat=20, max_msg_size=4 * 1024 * 1024
                )
            except aiohttp.WSServerHandshakeError as err:
                if err.status in (401, 403, 429):
                    raise VelmaFatalError(f"handshake rejected with HTTP {err.status}") from err
                raise
            async with ws:
                # The config must be the first frame; audio before it closes the socket.
                await ws.send_str(json.dumps(CONFIG))
                self._emit(VelmaStatus("listening", ""))
                sender = asyncio.create_task(self._send_audio(ws), name="velma-send")
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self._handle(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                finally:
                    sender.cancel()
                code = ws.close_code
                if code in FATAL_CLOSE_CODES:
                    raise VelmaFatalError(f"{FATAL_CLOSE_CODES[code]} (close code {code})")
                if not self._closing and code != 1000:
                    raise ConnectionError(f"closed with code {code}")

    async def _send_audio(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                await ws.send_str("")  # end of audio: Velma answers with `done` and closes
                return
            await ws.send_bytes(chunk)

    def _handle(self, raw: str) -> None:
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("velma sent non-JSON text: %.120s", raw)
            return
        kind = event.get("type")
        now = time.time()
        if kind == "partial_clip":
            clip = event["partial_clip"]
            if clip.get("emotion") is not None:
                self._emit(VelmaEmotion(clip["clip_uuid"], clip["emotion"], clip.get("text") or "", "partial", now))
        elif kind == "clip":
            clip = event["clip"]
            self._emit(VelmaEmotion(clip["clip_uuid"], clip.get("emotion"), clip.get("text") or "", "final", now))
        elif kind == "clip_update":
            clip = event["clip_update"]
            if "emotion" in clip:
                self._emit(VelmaEmotion(clip["clip_uuid"], clip["emotion"], "", "update", now))
        elif kind == "behavior_detection":
            d = event["detection"]
            self._emit(
                VelmaBehavior(
                    behavior_uuid=d.get("behavior_uuid", ""),
                    name=d.get("behavior_name", ""),
                    detected=bool(d.get("detected")),
                    confidence=d.get("confidence"),
                    reasoning=d.get("reasoning") or "",
                    at=now,
                )
            )
        elif kind == "done":
            logger.info("velma done: %s ms of audio, model %s", event.get("duration_ms"), event.get("model_version"))
        elif kind == "error":
            logger.warning("velma error event: %s", event.get("error"))

    def _emit(self, event: VelmaEvent) -> None:
        try:
            self._on_event(event)
        except Exception:
            logger.exception("velma event handler failed")

    # --- shutdown ------------------------------------------------------------

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._resampler is not None:
            for out in self._resampler.flush():
                self._pending += bytes(out.data)
        if self._pending:
            self._enqueue(bytes(self._pending))
            self._pending.clear()
        self._enqueue(None)
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except asyncio.TimeoutError:
            self._task.cancel()
        except Exception:
            pass
