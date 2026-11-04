"""Velma (Modulate) as Emma's second ear.

The worker streams the person's microphone audio to Velma's full streaming
analysis in parallel with Deepgram (see `Emma.stt_node` in main.py). Velma
answers with two kinds of things we use:

- an acoustic emotion label per utterance: how it was said, not what was said
  (`partial_clip` while the person is still talking, `clip` when the utterance
  is final, `clip_update` when the label is refined);
- detections of the six custom behaviors below ("Says They Are Angry" and so
  on): what was said, judged by Velma over the transcript, with a confidence
  and a one-line reasoning.

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

# Velma's 26 streaming labels folded into Modulate's nine emotion families
# (the palette of the batch endpoint: angry, disgusted, afraid, surprised, happy,
# sad, neutral, other, unknown). The chip on the page keeps the original label;
# the family picks the color, the signal and Emma's reaction. A label that is
# not listed here (a new one, for instance) lands in "unknown".
FAMILY_NAMES = ("angry", "disgusted", "afraid", "surprised", "happy", "sad", "neutral", "other", "unknown")
GRAY_FAMILIES = frozenset({"neutral", "other", "unknown"})
FAMILIES = {
    "angry": ("Angry", "Frustrated", "Contemptuous"),
    "disgusted": ("Disgusted",),
    "afraid": ("Afraid", "Anxious", "Stressed", "Concerned"),
    "surprised": ("Surprised",),
    "happy": ("Happy", "Amused", "Excited", "Proud", "Affectionate", "Hopeful", "Relieved", "Confident"),
    "sad": ("Sad", "Disappointed", "Ashamed"),
    "neutral": ("Neutral", "Calm"),
    "other": ("Interested", "Bored", "Tired", "Confused"),
}
_FAMILY_OF_LABEL = {label.lower(): family for family, labels in FAMILIES.items() for label in labels}


def family_of(label: str | None) -> str:
    return _FAMILY_OF_LABEL.get((label or "").strip().lower(), "unknown")


# One "Says They Are …" behavior per colored family. Velma identifies behaviors
# by these UUIDs in its detections; keep them stable.
BEHAVIOR_UUIDS = {
    "angry": "b178304c-b5d8-41d0-bc4c-aebbe738f17a",
    "disgusted": "ef16626a-fa1e-5e98-a453-acb4745bfa0b",
    "afraid": "707a6c55-7d3d-5a4b-bf30-cb3ca002555c",
    "surprised": "e88e122b-1c0e-595d-98c1-6cf8bb2169e6",
    "happy": "3d912c51-174e-5766-9dad-32a2b6d93ccc",
    "sad": "8a608e35-655b-4495-a67e-48f4aae093f4",
}

# Written the way Modulate's guide asks: criteria rather than examples, plus
# explicit "do not flag" rules. `trigger_limit: None` means every occurrence is
# reported, not only the first one per speaker.
_SAYS = {
    # family: (the state as a noun, words that name it, the word used about a thing instead)
    "angry": ("anger", "angry, mad, furious, pissed off, livid, enraged", "this traffic is mad"),
    "disgusted": ("disgust", "disgusted, grossed out, repulsed, revolted, sick of it", "that's disgusting"),
    "afraid": ("fear", "afraid, scared, frightened, terrified, worried, anxious, nervous, panicking", "a scary film"),
    "surprised": ("surprise", "surprised, shocked, stunned, can't believe it, did not expect that", "a surprising result"),
    "happy": ("happiness", "happy, glad, delighted, thrilled, excited, cheerful, in a great mood, over the moon", "a happy ending"),
    "sad": ("sadness", "sad, upset, down, depressed, heartbroken, miserable, unhappy, feel like crying", "that film is sad"),
}


def _behavior(family: str) -> dict:
    noun, words, about_a_thing = _SAYS[family]
    return {
        "behavior_uuid": BEHAVIOR_UUIDS[family],
        "name": f"Says They Are {family.capitalize()}",
        "short_description": f"The speaker states in the first person that they are {family} right now.",
        "detailed_description": (
            "This behavior is present if the speech meets ALL of the following criteria: "
            f"the speaker attributes a state of {noun} to themselves in the first person, with a "
            f"predicate adjective or verb phrase for {noun} ({words}, or the equivalent in the "
            "speaker's language); "
            "the state is presented as current, in the present tense or with a marker such as "
            "'right now', 'today' or 'lately'; "
            "the statement is about the speaker's own feeling, not a report of someone else's. "
            f"Do NOT flag if: the statement is negated ('I'm not {family}'); the {noun} is attributed "
            "to a third party or quoted from someone else; the statement is hypothetical or "
            f"conditional ('I would be {family} if'); the speaker describes a past episode in the "
            "past tense; the word describes a thing rather than the speaker's state "
            f"('{about_a_thing}')."
        ),
        "trigger_limit": None,
    }


BEHAVIORS = [_behavior(family) for family in _SAYS]
BEHAVIOR_MOODS = {BEHAVIOR_UUIDS[family]: family for family in _SAYS}

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
    clip_uuid: str  # the clip Velma points to as the definitive evidence
    evidence: list[str]
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
                    clip_uuid=d.get("definitive_clip_uuid") or next(iter(d.get("evidence_clip_uuids") or []), ""),
                    evidence=list(d.get("evidence_clip_uuids") or []),
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
