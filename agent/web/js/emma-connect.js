const t = {
  idle: "",
  connecting: "Connecting…",
  connected: "Emma is listening. Go ahead.",
  summarizing: "Emma is wrapping up…",
  error: "Couldn't connect. ",
  wrongCode: "Wrong access code.",
  askCode: "Access code",
  catalogError: "The server is unavailable, try reloading the page.",
  disconnect: "End conversation",
  talk: "Talk to Emma",
  you: "You",
  emma: "Emma",
  summary: "Wrap-up",
};

const STORAGE_KEYS = { code: "emma-access-code", settings: "emma-settings" };

const TOPICS = {
  transcription: "lk.transcription",
  signals: "emma.signals",
  summary: "emma.summary",
  control: "emma.control",
};

const SUMMARY_TIMEOUT_MS = 20000;

// Состояние агента (listening / thinking / speaking), которое LiveKit Agents
// публикует в атрибутах участника.
const AGENT_STATE_ATTRIBUTE = "lk.agent.state";

(function () {
  const $ = (id) => document.getElementById(id);
  const els = {
    button: $("emma-talk-button"),
    status: $("emma-status"),
    model: $("emma-model"),
    character: $("emma-character"),
    voice: $("emma-voice"),
    prompt: $("emma-prompt"),
    transcript: $("emma-transcript"),
  };
  if (Object.values(els).some((el) => !el)) return;

  let room = null;
  let ending = false;
  let summaryReceived = null;
  const audioElements = [];
  const segments = new Map();

  function setStatus(text) {
    els.status.textContent = text;
  }

  // --- свечение активности Эммы ---------------------------------------------
  //
  // Каждый кадр меряем громкость голоса Эммы и микрофона собеседника и задаём
  // по ним --emma-glow (0…1): свечение разгорается быстро, гаснет медленно,
  // почти исчезает, когда говорит собеседник, и слегка пульсирует, пока Эмма
  // думает. Громкость — из Web Audio; если браузер не отдаёт звук звонка в
  // анализатор (бывает в Safari), выручает audioLevel участника от LiveKit.

  const glow = (function () {
    const el = $("emma-glow");
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    const IDLE = 0.12;
    const buffer = new Float32Array(512);

    let audioContext = null;
    let emma = null;
    let user = null;
    let active = false;
    let agentState = "";
    let frame = 0;
    let level = 0;
    let emmaLevel = 0;
    let userLevel = 0;

    // Звук должен запускаться прямо в обработчике клика, иначе Safari оставит
    // AudioContext приостановленным.
    function prepare() {
      if (!audioContext || audioContext.state === "closed") {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        audioContext = AudioContextClass ? new AudioContextClass() : null;
      }
      if (audioContext && audioContext.state === "suspended") audioContext.resume();
    }

    function watch(mediaStreamTrack, participant) {
      const source = { participant, analyser: null };
      if (audioContext && mediaStreamTrack) {
        try {
          source.analyser = audioContext.createAnalyser();
          source.analyser.fftSize = buffer.length;
          audioContext.createMediaStreamSource(new MediaStream([mediaStreamTrack])).connect(source.analyser);
        } catch (err) {
          console.warn("glow: no audio analyser, falling back to LiveKit levels", err);
          source.analyser = null;
        }
      }
      return source;
    }

    function loudness(source) {
      if (!source) return 0;
      let fromAnalyser = 0;
      if (source.analyser) {
        source.analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        for (const sample of buffer) sum += sample * sample;
        fromAnalyser = (Math.sqrt(sum / buffer.length) - 0.01) / 0.12;
      }
      const fromLiveKit = source.participant ? source.participant.audioLevel || 0 : 0;
      return Math.min(1, Math.max(0, fromAnalyser, fromLiveKit));
    }

    function follow(current, target, attack, release) {
      return current + (target - current) * (target > current ? attack : release);
    }

    function tick(now) {
      let target = 0;
      if (active) {
        if (reducedMotion.matches) {
          target = 0.3;
        } else {
          emmaLevel = follow(emmaLevel, loudness(emma), 0.35, 0.06);
          userLevel = follow(userLevel, loudness(user), 0.4, 0.05);
          const idle = agentState === "thinking" ? IDLE + 0.08 * (1 + Math.sin(now / 350)) : IDLE;
          target = Math.max(idle, emmaLevel) * (1 - 0.8 * userLevel);
        }
      }
      level = follow(level, target, 0.15, 0.04);
      el.style.setProperty("--emma-glow", level.toFixed(3));

      if (active || level > 0.002) {
        frame = requestAnimationFrame(tick);
      } else {
        frame = 0;
        el.style.setProperty("--emma-glow", "0");
      }
    }

    function run() {
      if (!frame) frame = requestAnimationFrame(tick);
    }

    return {
      prepare,
      watchEmma(mediaStreamTrack, participant) {
        emma = watch(mediaStreamTrack, participant);
      },
      watchUser(mediaStreamTrack, participant) {
        user = watch(mediaStreamTrack, participant);
      },
      setAgentState(state) {
        agentState = state || "";
      },
      start() {
        active = true;
        run();
      },
      stop() {
        active = false;
        emma = null;
        user = null;
        agentState = "";
        run();
      },
    };
  })();

  // --- настройки сессии -----------------------------------------------------

  function readStored(key) {
    try {
      return localStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }

  function writeStored(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch (_) {}
  }

  // Пока в списке ничего не выбрано, поле не отправляется — сервер подставит
  // значение по умолчанию.
  function currentSettings() {
    return {
      model: els.model.value || undefined,
      character: els.character.value || undefined,
      voice: els.voice.value || undefined,
      prompt: els.prompt.value.trim(),
    };
  }

  function saveSettings() {
    writeStored(STORAGE_KEYS.settings, JSON.stringify(currentSettings()));
  }

  function loadSettings() {
    try {
      return JSON.parse(readStored(STORAGE_KEYS.settings)) || {};
    } catch (_) {
      return {};
    }
  }

  // Название списка (aria-label) показывается внутри него как пункт по умолчанию.
  function fillSelect(select, items, selected) {
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = select.getAttribute("aria-label");
    placeholder.disabled = true;
    placeholder.selected = true;
    select.appendChild(placeholder);
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      select.appendChild(option);
    }
    if (selected && items.some((item) => item.id === selected)) select.value = selected;
  }

  async function loadCatalog() {
    try {
      const res = await fetch("/catalog");
      if (!res.ok) throw new Error(`catalog: ${res.status}`);
      const catalog = await res.json();
      const saved = loadSettings();
      fillSelect(els.model, catalog.models, saved.model);
      fillSelect(els.character, catalog.characters, saved.character);
      fillSelect(els.voice, catalog.voices, saved.voice);
      els.prompt.value = saved.prompt || "";
    } catch (err) {
      console.error(err);
      setStatus(t.catalogError);
      els.button.disabled = true;
    }
  }

  function setControlsDisabled(disabled) {
    for (const el of [els.model, els.character, els.voice, els.prompt]) el.disabled = disabled;
  }

  // --- панели ---------------------------------------------------------------

  function scrollToBottom(container) {
    container.scrollTop = container.scrollHeight;
  }

  function addEntry(container, className, title, text) {
    const entry = document.createElement("div");
    entry.className = className;
    const head = document.createElement("div");
    head.className = "emma__entry-title";
    head.textContent = title;
    const body = document.createElement("div");
    body.className = "emma__entry-text";
    body.textContent = text;
    entry.append(head, body);
    container.appendChild(entry);
    scrollToBottom(container);
    return body;
  }

  function renderSegment(id, who, text) {
    let body = segments.get(id);
    if (!body) {
      body = addEntry(els.transcript, `emma__turn emma__turn--${who}`, who === "user" ? t.you : t.emma, "");
      segments.set(id, body);
    }
    body.textContent = text;
    scrollToBottom(els.transcript);
  }

  function resetPanels() {
    segments.clear();
    els.transcript.innerHTML = "";
  }

  // --- текстовые потоки от воркера -----------------------------------------

  async function onTranscription(reader, participantInfo) {
    const attrs = reader.info.attributes || {};
    const id = attrs["lk.segment_id"] || reader.info.id;
    const who = participantInfo.identity === room.localParticipant.identity ? "user" : "emma";
    let text = "";
    for await (const chunk of reader) {
      text += chunk;
      renderSegment(id, who, text);
    }
  }

  async function onSignal(reader) {
    const raw = await reader.readAll();
    try {
      const signal = JSON.parse(raw);
      addEntry(els.transcript, "emma__signal", signal.signal, signal.note);
    } catch (err) {
      console.error("bad signal payload", raw, err);
    }
  }

  async function onSummary(reader) {
    const text = await reader.readAll();
    if (text) addEntry(els.transcript, "emma__summary", t.summary, text);
    if (summaryReceived) summaryReceived();
  }

  // --- соединение -----------------------------------------------------------

  function getAccessCode() {
    let code = readStored(STORAGE_KEYS.code) || "";
    if (!code) {
      code = (prompt(t.askCode) || "").trim();
      if (code) writeStored(STORAGE_KEYS.code, code);
    }
    return code;
  }

  async function connect() {
    const accessCode = getAccessCode();
    if (!accessCode) return;

    els.button.disabled = true;
    setControlsDisabled(true);
    setStatus(t.connecting);

    try {
      const res = await fetch("/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_code: accessCode, ...currentSettings() }),
      });
      if (res.status === 403) {
        writeStored(STORAGE_KEYS.code, null);
        throw new Error(t.wrongCode);
      }
      if (!res.ok) throw new Error(`token server: ${res.status}`);
      const { url, token } = await res.json();

      room = new LivekitClient.Room();

      room.on(LivekitClient.RoomEvent.TrackSubscribed, (track, publication, participant) => {
        if (track.kind === LivekitClient.Track.Kind.Audio) {
          const el = track.attach();
          el.autoplay = true;
          document.body.appendChild(el);
          audioElements.push(el);
          glow.watchEmma(track.mediaStreamTrack, participant);
          glow.setAgentState(participant.attributes[AGENT_STATE_ATTRIBUTE]);
        }
      });
      room.on(LivekitClient.RoomEvent.ParticipantAttributesChanged, (changed) => {
        if (AGENT_STATE_ATTRIBUTE in changed) glow.setAgentState(changed[AGENT_STATE_ATTRIBUTE]);
      });
      room.on(LivekitClient.RoomEvent.Disconnected, onDisconnected);
      room.registerTextStreamHandler(TOPICS.transcription, onTranscription);
      room.registerTextStreamHandler(TOPICS.signals, onSignal);
      room.registerTextStreamHandler(TOPICS.summary, onSummary);

      await room.connect(url, token);
      const microphone = await room.localParticipant.setMicrophoneEnabled(true);
      glow.watchUser(microphone && microphone.track && microphone.track.mediaStreamTrack, room.localParticipant);
      glow.start();

      resetPanels();
      setStatus(t.connected);
      els.button.textContent = t.disconnect;
      els.button.disabled = false;
    } catch (err) {
      console.error(err);
      setStatus(t.error + err.message);
      els.button.disabled = false;
      setControlsDisabled(false);
      glow.stop();
      room = null;
    }
  }

  async function endConversation() {
    if (!room || ending) return;
    ending = true;
    els.button.disabled = true;
    setStatus(t.summarizing);
    try {
      const waitForSummary = new Promise((resolve) => {
        summaryReceived = resolve;
      });
      const timeout = new Promise((resolve) => setTimeout(resolve, SUMMARY_TIMEOUT_MS));
      await room.localParticipant.sendText("end", { topic: TOPICS.control });
      await Promise.race([waitForSummary, timeout]);
    } catch (err) {
      console.error(err);
    }
    summaryReceived = null;
    await room.disconnect();
  }

  function onDisconnected() {
    glow.stop();
    room = null;
    ending = false;
    for (const el of audioElements.splice(0)) el.remove();
    setStatus(t.idle);
    els.button.textContent = t.talk;
    els.button.disabled = false;
    setControlsDisabled(false);
  }

  els.button.addEventListener("click", () => {
    if (room) {
      endConversation();
    } else {
      glow.prepare();
      connect();
    }
  });

  for (const el of [els.model, els.character, els.voice, els.prompt]) {
    el.addEventListener("change", saveSettings);
  }

  resetPanels();
  loadCatalog();
})();
