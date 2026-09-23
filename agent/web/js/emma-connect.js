const t = {
  catalogError: "The server is unavailable, try reloading the page.",
  disconnect: "End conversation",
  talk: "Talk to Emma",
  you: "You",
  emma: "Emma",
  summary: "Wrap-up",
  averageReply: "average reply",
  mute: "Mute",
  unmute: "Unmute",
};

// Строки под кнопкой: у каждого звена — состояние и цвет точки.
// Уровни: off — звено ещё не участвует, pending — в процессе, ok, warn, error.
const HEALTH = {
  mic: {
    off: ["off", "off"],
    checking: ["pending", "checking…"],
    on: ["ok", "on"],
    muted: ["warn", "muted"],
    silent: ["warn", "no sound"],
    blocked: ["error", "blocked"],
    missing: ["error", "not found"],
    busy: ["error", "in use"],
    failed: ["error", "failed"],
    unplugged: ["error", "disconnected"],
  },
  net: {
    waiting: ["off", ""],
    connecting: ["pending", "connecting…"],
    good: ["ok", "good"],
    weak: ["warn", "weak"],
    reconnecting: ["pending", "reconnecting…"],
    lost: ["error", "lost"],
    failed: ["error", "failed"],
  },
  emma: {
    waiting: ["off", ""],
    joining: ["pending", "joining…"],
    preparing: ["pending", "getting ready…"],
    idle: ["ok", "idle"],
    listening: ["ok", "listening"],
    hearing: ["ok", "hears you"],
    thinking: ["ok", "thinking…"],
    speaking: ["ok", "speaking"],
    ending: ["pending", "wrapping up…"],
    slow: ["warn", "slow to answer"],
    retrying: ["warn", "retrying…"],
    absent: ["error", "didn't join"],
    stuck: ["error", "stuck"],
    left: ["error", "left"],
    broken: ["error", "stopped"],
  },
};

// Подсказка под строками: что с этим сделать. Сами состояния видны в строках,
// поэтому подсказка появляется только при сбое и как приглашение начать.
const HINT = {
  ready: "Go ahead, start talking.",
  micBlocked: "Microphone access is blocked. Allow it for this site in the browser's address bar, then try again.",
  micMissing: "No microphone found. Plug one in or choose an input in your system settings, then try again.",
  micBusy: "Another app is using the microphone. Close it and try again.",
  micFailed: "Couldn't start the microphone.",
  micUnplugged: "Your microphone was disconnected. End the conversation and start again.",
  micMuted: "Your microphone is muted, so Emma can't hear you.",
  micSilent: "Your microphone is sending silence. Check it isn't muted in system settings, or pick another input.",
  server: "The prototype server didn't respond. Try again in a minute.",
  network: "Couldn't reach the voice server. Check your internet connection.",
  reconnecting: "Connection lost. Reconnecting…",
  dropped: "The connection dropped. Start the conversation again.",
  weak: "Weak connection: Emma may hear you late or cut out.",
  absent: "Emma didn't join. The agent may be restarting — try again in a minute.",
  stuck: "Emma joined but never got ready. Try again; if it repeats, the agent is failing.",
  left: "Emma left the conversation unexpectedly. Start again.",
  slow: "Emma is taking longer than usual to answer.",
  playback: "The browser blocked Emma's sound.",
  stage: {
    stt: {
      retry: "Speech recognition hiccuped, retrying.",
      fail: "Speech recognition stopped, so Emma can't hear you.",
    },
    llm: {
      retry: "The model didn't answer, retrying.",
      fail: "The model stopped answering. Try another model.",
    },
    tts: {
      retry: "Emma's voice hiccuped, retrying.",
      fail: "Emma's voice stopped working. Try another voice.",
    },
  },
};

const SETTINGS_STORAGE_KEY = "emma-settings";

const TOPICS = {
  transcription: "lk.transcription",
  signals: "emma.signals",
  summary: "emma.summary",
  control: "emma.control",
  status: "emma.status",
  latency: "emma.latency",
};

const SUMMARY_TIMEOUT_MS = 20000;
// Сколько ждём, пока Эмма зайдёт в комнату и будет готова слушать: воркер
// прогревает голос и модели, обычно это несколько секунд.
const AGENT_READY_TIMEOUT_MS = 30000;
const SLOW_THINKING_MS = 10000;
// «Цифровая тишина»: так звучит микрофон, выключенный в системе или мёртвый.
// Шумоподавление браузера до настоящих нулей не опускается, поэтому ложных
// срабатываний в тихой комнате нет.
const SILENCE_PEAK = 0.00001;
const SILENCE_MS = 4000;
const HEARING_HOLD_MS = 1500;
const RETRY_HOLD_MS = 8000;

// Состояние агента (initializing / idle / listening / thinking / speaking),
// которое LiveKit Agents публикует в атрибутах участника.
const AGENT_STATE_ATTRIBUTE = "lk.agent.state";
const AGENT_LIVE_STATES = ["idle", "listening", "thinking", "speaking"];

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
    health: $("emma-health"),
    mic: $("emma-health-mic"),
    micMeter: $("emma-mic-meter"),
    micToggle: $("emma-mic-toggle"),
    net: $("emma-health-net"),
    agent: $("emma-health-emma"),
    sound: $("emma-sound-button"),
  };
  if (Object.values(els).some((el) => !el)) return;

  // Текущий (или последний, если он оборвался с ошибкой) разговор.
  let conv = null;
  const audioElements = [];
  const segments = new Map();
  // Реплики Эммы по порядку — к ним приклеивается время ответа.
  const emmaEntries = [];

  // --- громкость звука -------------------------------------------------------
  //
  // Один AudioContext на страницу и по анализатору на каждый трек. Громкость
  // читается не чаще раза за кадр — её делят свечение и индикатор микрофона.
  // Если браузер не отдаёт звук в анализатор (бывает в Safari со звуком
  // звонка), выручает audioLevel участника от LiveKit.

  const audio = (function () {
    const buffer = new Float32Array(512);
    let context = null;

    // Звук должен запускаться прямо в обработчике клика, иначе Safari оставит
    // AudioContext приостановленным.
    function prepare() {
      if (!context || context.state === "closed") {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        context = AudioContextClass ? new AudioContextClass() : null;
      }
      if (context && context.state === "suspended") context.resume();
    }

    function meter(mediaStreamTrack, participant) {
      let analyser = null;
      if (context && mediaStreamTrack) {
        try {
          analyser = context.createAnalyser();
          analyser.fftSize = buffer.length;
          context.createMediaStreamSource(new MediaStream([mediaStreamTrack])).connect(analyser);
        } catch (err) {
          console.warn("no audio analyser, falling back to LiveKit levels", err);
          analyser = null;
        }
      }

      let stamp = -1;
      let rms = 0;
      let peak = 0;

      function read(now) {
        if (!analyser || now === stamp) return;
        stamp = now;
        analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        let max = 0;
        for (const sample of buffer) {
          sum += sample * sample;
          const size = Math.abs(sample);
          if (size > max) max = size;
        }
        rms = Math.sqrt(sum / buffer.length);
        peak = max;
      }

      function liveKitLevel() {
        return m.participant ? m.participant.audioLevel || 0 : 0;
      }

      const m = {
        participant,
        measuresSilence: Boolean(analyser),
        // Для индикатора микрофона: чувствительно, чтобы шевелился даже от шума.
        level(now) {
          read(now);
          return analyser ? Math.min(1, Math.sqrt(rms) * 2.2) : liveKitLevel();
        },
        peak(now) {
          read(now);
          return peak;
        },
        // Для свечения: с порогом шума, только уверенный голос.
        loudness(now) {
          read(now);
          const fromAnalyser = analyser ? (rms - 0.01) / 0.12 : 0;
          return Math.min(1, Math.max(0, fromAnalyser, liveKitLevel()));
        },
      };
      return m;
    }

    return { prepare, meter };
  })();

  // --- свечение активности Эммы ---------------------------------------------
  //
  // Каждый кадр задаём --emma-glow (0…1) по громкости голоса Эммы и микрофона
  // собеседника: свечение разгорается быстро, гаснет медленно, почти исчезает,
  // когда говорит собеседник, и слегка пульсирует, пока Эмма думает.

  const glow = (function () {
    const el = $("emma-glow");
    const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
    const IDLE = 0.12;

    let emma = null;
    let user = null;
    let active = false;
    let agentState = "";
    let frame = 0;
    let level = 0;
    let emmaLevel = 0;
    let userLevel = 0;

    function follow(current, target, attack, release) {
      return current + (target - current) * (target > current ? attack : release);
    }

    function tick(now) {
      let target = 0;
      if (active) {
        if (reducedMotion.matches) {
          target = 0.3;
        } else {
          emmaLevel = follow(emmaLevel, emma ? emma.loudness(now) : 0, 0.35, 0.06);
          userLevel = follow(userLevel, user ? user.loudness(now) : 0, 0.4, 0.05);
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
      watchEmma(meter) {
        emma = meter;
      },
      watchUser(meter) {
        user = meter;
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

  // localStorage может быть недоступен (приватный режим, запрет сайтам) —
  // тогда настройки просто не запоминаются.
  function saveSettings() {
    try {
      localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(currentSettings()));
    } catch (_) {}
  }

  function loadSettings() {
    try {
      return JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY)) || {};
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

  // Сессия входа живёт 30 дней и сбрасывается при смене пароля — тогда сервер
  // отвечает 401, и страница отправляет на вход.
  function redirectIfSignedOut(res) {
    if (res.status === 401) {
      location.href = "/login";
      return true;
    }
    return false;
  }

  async function loadCatalog() {
    try {
      const res = await fetch("/catalog");
      if (redirectIfSignedOut(res)) return;
      if (!res.ok) throw new Error(`catalog: ${res.status}`);
      const catalog = await res.json();
      const saved = loadSettings();
      fillSelect(els.model, catalog.models, saved.model);
      fillSelect(els.character, catalog.characters, saved.character);
      fillSelect(els.voice, catalog.voices, saved.voice);
      els.prompt.value = saved.prompt || "";
    } catch (err) {
      console.error(err);
      setHint({ level: "error", text: t.catalogError });
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

  // Распознавание режет речь собеседника на куски по паузам. Подряд идущие куски
  // складываются в одну реплику, иначе у каждого был бы свой заголовок «You».
  // Реплики Эммы не склеиваются: у каждой своё время ответа.
  function renderSegment(id, who, text) {
    let span = segments.get(id);
    if (!span) {
      let entry = els.transcript.lastElementChild;
      if (who !== "user" || !entry || entry.dataset.who !== "user") {
        const title = who === "user" ? t.you : t.emma;
        entry = addEntry(els.transcript, `emma__turn emma__turn--${who}`, title, "").parentElement;
        entry.dataset.who = who;
        if (who === "emma") emmaEntries.push(entry);
      }
      const body = entry.querySelector(".emma__entry-text");
      if (body.childNodes.length) body.append(" ");
      span = document.createElement("span");
      body.append(span);
      segments.set(id, span);
    }
    span.textContent = text;
    scrollToBottom(els.transcript);
  }

  function seconds(value) {
    return `${value.toFixed(1)} s`;
  }

  // «Emma · 1.4 s», при наведении — из чего сложилось: конец реплики + модель + голос.
  function showLatency(report) {
    // Замер приходит, когда Эмма договорила, — это её последняя реплика.
    // У приветствия замера нет: на него собеседник ещё ничего не говорил.
    const entry = emmaEntries[emmaEntries.length - 1];
    if (!entry || entry.dataset.latency) return;
    entry.dataset.latency = report.total;
    const head = entry.querySelector(".emma__entry-title");
    const total = document.createElement("span");
    total.className = "emma__latency";
    total.textContent = seconds(report.total);
    const parts = [
      ["turn", report.turn],
      ["model", report.model],
      ["voice", report.voice],
    ].filter(([, value]) => typeof value === "number");
    head.append(total);
    if (parts.length) {
      const breakdown = document.createElement("span");
      breakdown.className = "emma__latency-parts";
      breakdown.textContent = " = " + parts.map(([name, value]) => `${name} ${seconds(value)}`).join(" + ");
      head.append(breakdown);
    }
  }

  function resetPanels() {
    segments.clear();
    emmaEntries.length = 0;
    els.transcript.innerHTML = "";
  }

  // --- состояние цепочки «микрофон → соединение → Эмма» -----------------------

  function newConversation() {
    return {
      active: true,
      ending: false,
      room: null,
      connected: false,
      connectedAt: 0,
      micTrack: null,
      micMeter: null,
      micPublished: false,
      micError: null,
      micUnplugged: false,
      muted: false,
      silentSince: 0,
      micLevel: 0,
      netError: null,
      reconnecting: false,
      quality: "",
      playbackBlocked: false,
      agent: null,
      agentState: "",
      agentStateSince: 0,
      agentLive: false,
      hearingUntil: 0,
      heardUser: false,
      retry: null,
      fatal: null,
      latencies: [],
      summaryReceived: null,
    };
  }

  // После обрыва подсвечено только звено, которое сломалось; остальные гаснут.
  function micState(c, now) {
    if (c.micError) return c.micError;
    if (c.micUnplugged) return "unplugged";
    if (!c.active) return "off";
    if (!c.micTrack) return "checking";
    if (c.muted) return "muted";
    if (c.silentSince && now - c.silentSince > SILENCE_MS) return "silent";
    return "on";
  }

  function netState(c) {
    if (c.netError) return c.netError;
    if (!c.active) return "waiting";
    if (c.reconnecting) return "reconnecting";
    if (!c.micTrack) return "waiting";
    if (!c.connected) return "connecting";
    if (c.quality === "poor" || c.quality === "lost") return "weak";
    return "good";
  }

  function emmaState(c, now) {
    if (c.fatal && c.fatal.emma) return c.fatal.emma;
    if (!c.active || !c.connected) return "waiting";
    if (c.ending) return "ending";
    if (!c.agent) return "joining";
    if (!c.agentLive) return "preparing";
    if (c.retry && now < c.retry.until) return "retrying";
    if (c.agentState === "listening" && now < c.hearingUntil) return "hearing";
    if (c.agentState === "thinking" && now - c.agentStateSince > SLOW_THINKING_MS) return "slow";
    return HEALTH.emma[c.agentState] ? c.agentState : "listening";
  }

  function hintFor(c, mic, net, emma, now) {
    const warn = (text) => ({ level: "warn", text });
    const info = (text) => ({ level: "", text });
    if (c.fatal) return { level: "error", text: c.fatal.hint, detail: c.fatal.detail };
    if (c.ending) return info("");
    if (net === "reconnecting") return warn(HINT.reconnecting);
    if (mic === "unplugged") return { level: "error", text: HINT.micUnplugged };
    if (c.retry && now < c.retry.until) return warn(HINT.stage[c.retry.stage].retry);
    if (mic === "muted") return warn(HINT.micMuted);
    if (mic === "silent") return warn(HINT.micSilent);
    if (c.playbackBlocked) return warn(HINT.playback);
    if (net === "weak") return warn(HINT.weak);
    if (emma === "slow") return warn(HINT.slow);
    if (c.agentLive && !c.heardUser && c.agentState === "listening") return info(HINT.ready);
    return info("");
  }

  const rowText = new Map(
    [els.mic, els.net, els.agent].map((row) => [row, row.querySelector(".emma__health-text")])
  );

  function setRow(row, [level, text]) {
    if (row.dataset.level !== level) row.dataset.level = level;
    const span = rowText.get(row);
    if (span.textContent !== text) span.textContent = text;
  }

  let hintKey = "";
  function setHint({ level, text, detail }) {
    const key = `${level}|${text}|${detail || ""}`;
    if (key === hintKey) return;
    hintKey = key;
    els.status.dataset.level = level || "";
    els.status.textContent = text;
    if (detail) {
      const small = document.createElement("small");
      small.className = "emma-status__detail";
      small.textContent = detail;
      els.status.append(small);
    }
  }

  function render(now) {
    const c = conv;
    if (!c) {
      els.health.hidden = true;
      els.sound.hidden = true;
      setHint({ level: "", text: "" });
      return;
    }
    const mic = micState(c, now);
    const net = netState(c);
    const emma = emmaState(c, now);
    els.health.hidden = false;
    setRow(els.mic, HEALTH.mic[mic]);
    setRow(els.net, HEALTH.net[net]);
    setRow(els.agent, HEALTH.emma[emma]);
    els.micToggle.hidden = !(c.active && c.micPublished && !c.micUnplugged);
    const toggleText = c.muted ? t.unmute : t.mute;
    if (els.micToggle.textContent !== toggleText) els.micToggle.textContent = toggleText;
    const showMeter = c.active && mic !== "muted";
    els.micMeter.style.transform = `scaleX(${showMeter ? c.micLevel.toFixed(3) : 0})`;
    els.sound.hidden = !(c.active && c.playbackBlocked);
    setHint(hintFor(c, mic, net, emma, now));
  }

  // Пока идёт разговор, раз в кадр: громкость микрофона, тишина, сроки ожидания.
  let frame = 0;
  function tick(now) {
    frame = 0;
    const c = conv;
    if (!c || !c.active) return;

    if (c.micMeter) {
      const level = c.muted ? 0 : c.micMeter.level(now);
      c.micLevel += (level - c.micLevel) * (level > c.micLevel ? 0.5 : 0.12);
      if (c.micMeter.measuresSilence && !c.muted && !c.micUnplugged) {
        if (c.micMeter.peak(now) < SILENCE_PEAK) {
          if (!c.silentSince) c.silentSince = now;
        } else {
          c.silentSince = 0;
        }
      }
    }

    if (c.connected && !c.agentLive && now - c.connectedAt > AGENT_READY_TIMEOUT_MS) {
      fail(c, c.agent ? { emma: "stuck", hint: HINT.stuck } : { emma: "absent", hint: HINT.absent });
      return;
    }

    render(now);
    frame = requestAnimationFrame(tick);
  }

  function startLoop() {
    if (!frame) frame = requestAnimationFrame(tick);
  }

  // --- соединение -----------------------------------------------------------

  // Разговор оборвался по нашей стороне или по вине звена цепочки: причину
  // оставляем на экране, пока человек не начнёт новый разговор.
  function fail(c, fatal) {
    if (c.fatal || !c.active) return;
    c.fatal = fatal;
    if (c.room) {
      c.room.disconnect();
    } else {
      finish(c);
    }
  }

  function finish(c) {
    if (!c.active) return;
    c.active = false;
    glow.stop();
    for (const el of audioElements.splice(0)) el.remove();
    // Опубликованный трек останавливает сама комната при отключении.
    if (c.micTrack && !c.micPublished) c.micTrack.stop();
    els.button.textContent = t.talk;
    els.button.disabled = false;
    setControlsDisabled(false);
    if (!c.fatal && conv === c) conv = null;
    render(performance.now());
  }

  function setAgentState(c, state) {
    state = state || "";
    if (state !== c.agentState) {
      c.agentState = state;
      c.agentStateSince = performance.now();
    }
    if (AGENT_LIVE_STATES.includes(state)) c.agentLive = true;
    glow.setAgentState(state);
  }

  function onAgent(c, participant) {
    c.agent = participant;
    setAgentState(c, participant.attributes[AGENT_STATE_ATTRIBUTE]);
  }

  function watchMicTrack(c) {
    c.micMeter = audio.meter(c.micTrack.mediaStreamTrack, c.room ? c.room.localParticipant : null);
    glow.watchUser(c.micMeter);
  }

  function registerRoomHandlers(c, room) {
    const E = LivekitClient.RoomEvent;

    room.on(E.TrackSubscribed, (track, publication, participant) => {
      if (track.kind !== LivekitClient.Track.Kind.Audio) return;
      const el = track.attach();
      el.autoplay = true;
      document.body.appendChild(el);
      audioElements.push(el);
      glow.watchEmma(audio.meter(track.mediaStreamTrack, participant));
      if (participant.isAgent && !c.agent) onAgent(c, participant);
    });
    room.on(E.ParticipantConnected, (participant) => {
      if (participant.isAgent) onAgent(c, participant);
    });
    room.on(E.ParticipantDisconnected, (participant) => {
      if (participant === c.agent && !c.ending) fail(c, { emma: "left", hint: HINT.left });
    });
    room.on(E.ParticipantAttributesChanged, (changed, participant) => {
      if (participant && participant.isAgent && AGENT_STATE_ATTRIBUTE in changed) {
        if (!c.agent) c.agent = participant;
        setAgentState(c, changed[AGENT_STATE_ATTRIBUTE]);
      }
    });
    room.on(E.ConnectionQualityChanged, (quality, participant) => {
      if (participant === room.localParticipant) c.quality = quality;
    });
    room.on(E.Reconnecting, () => (c.reconnecting = true));
    room.on(E.SignalReconnecting, () => (c.reconnecting = true));
    room.on(E.Reconnected, () => (c.reconnecting = false));
    room.on(E.AudioPlaybackStatusChanged, () => (c.playbackBlocked = !room.canPlaybackAudio));
    room.on(E.Disconnected, (reason) => {
      if (!c.ending && !c.fatal && reason !== LivekitClient.DisconnectReason.CLIENT_INITIATED) {
        c.fatal = { hint: HINT.dropped };
        c.netError = "lost";
      }
      finish(c);
    });

    room.registerTextStreamHandler(TOPICS.transcription, async (reader, participantInfo) => {
      const attrs = reader.info.attributes || {};
      const id = attrs["lk.segment_id"] || reader.info.id;
      const who = participantInfo.identity === room.localParticipant.identity ? "user" : "emma";
      let text = "";
      for await (const chunk of reader) {
        text += chunk;
        if (who === "user") {
          c.heardUser = true;
          c.hearingUntil = performance.now() + HEARING_HOLD_MS;
        }
        renderSegment(id, who, text);
      }
    });

    room.registerTextStreamHandler(TOPICS.signals, async (reader) => {
      const raw = await reader.readAll();
      try {
        const signal = JSON.parse(raw);
        addEntry(els.transcript, "emma__signal", signal.signal, signal.note);
      } catch (err) {
        console.error("bad signal payload", raw, err);
      }
    });

    room.registerTextStreamHandler(TOPICS.summary, async (reader) => {
      const text = await reader.readAll();
      if (text) {
        let title = t.summary;
        if (c.latencies.length) {
          const average = c.latencies.reduce((sum, value) => sum + value, 0) / c.latencies.length;
          title += ` · ${t.averageReply} ${seconds(average)}`;
        }
        addEntry(els.transcript, "emma__summary", title, text);
      }
      if (c.summaryReceived) c.summaryReceived();
    });

    room.registerTextStreamHandler(TOPICS.latency, async (reader) => {
      const raw = await reader.readAll();
      try {
        const report = JSON.parse(raw);
        if (typeof report.total !== "number") return;
        c.latencies.push(report.total);
        showLatency(report);
      } catch (err) {
        console.error("bad latency payload", raw, err);
      }
    });

    room.registerTextStreamHandler(TOPICS.status, async (reader) => {
      const raw = await reader.readAll();
      let status;
      try {
        status = JSON.parse(raw);
      } catch (err) {
        console.error("bad status payload", raw, err);
        return;
      }
      const hints = HINT.stage[status.stage];
      if (!hints) return;
      console.warn("Emma pipeline error", status);
      if (status.recoverable) {
        c.retry = { stage: status.stage, until: performance.now() + RETRY_HOLD_MS };
      } else {
        fail(c, { emma: "broken", hint: hints.fail, detail: status.detail });
      }
    });
  }

  const MIC_FAILURES = {
    PermissionDenied: ["blocked", HINT.micBlocked],
    NotFound: ["missing", HINT.micMissing],
    DeviceInUse: ["busy", HINT.micBusy],
  };

  async function connect() {
    const c = newConversation();
    conv = c;
    els.button.disabled = true;
    setControlsDisabled(true);
    startLoop();

    // Сначала микрофон: если доступа нет, комнату и агента не заводим зря.
    try {
      c.micTrack = await LivekitClient.createLocalAudioTrack({
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });
    } catch (err) {
      console.error(err);
      const [state, hint] = MIC_FAILURES[LivekitClient.MediaDeviceFailure.getFailure(err)] || [
        "failed",
        HINT.micFailed,
      ];
      c.micError = state;
      fail(c, { hint, detail: state === "failed" ? err.message : "" });
      return;
    }
    watchMicTrack(c);
    glow.start();
    c.micTrack.on(LivekitClient.TrackEvent.Ended, () => (c.micUnplugged = true));
    // LiveKit сам перезапускает трек при смене устройства по умолчанию —
    // анализатор надо перевесить на новый MediaStreamTrack.
    c.micTrack.on(LivekitClient.TrackEvent.Restarted, () => {
      c.micUnplugged = false;
      c.silentSince = 0;
      watchMicTrack(c);
    });

    let url, token;
    try {
      const res = await fetch("/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentSettings()),
      });
      if (redirectIfSignedOut(res)) return;
      if (!res.ok) throw new Error(`token server: ${res.status}`);
      ({ url, token } = await res.json());
    } catch (err) {
      console.error(err);
      c.netError = "failed";
      fail(c, { hint: HINT.server, detail: err.message });
      return;
    }

    const room = new LivekitClient.Room();
    c.room = room;
    registerRoomHandlers(c, room);

    try {
      await room.connect(url, token);
    } catch (err) {
      console.error(err);
      c.netError = "failed";
      c.room = null;
      fail(c, { hint: HINT.network, detail: err.message });
      return;
    }
    if (!c.active) return;
    c.connected = true;
    c.connectedAt = performance.now();
    c.micMeter.participant = room.localParticipant;
    c.playbackBlocked = !room.canPlaybackAudio;

    try {
      await room.localParticipant.publishTrack(c.micTrack);
      c.micPublished = true;
    } catch (err) {
      console.error(err);
      c.micError = "failed";
      fail(c, { hint: HINT.micFailed, detail: err.message });
      return;
    }

    for (const participant of room.remoteParticipants.values()) {
      if (participant.isAgent) onAgent(c, participant);
    }

    resetPanels();
    els.button.textContent = t.disconnect;
    els.button.disabled = false;
  }

  async function endConversation() {
    const c = conv;
    if (!c || !c.room || c.ending) return;
    c.ending = true;
    els.button.disabled = true;
    try {
      const waitForSummary = new Promise((resolve) => {
        c.summaryReceived = resolve;
      });
      const timeout = new Promise((resolve) => setTimeout(resolve, SUMMARY_TIMEOUT_MS));
      await c.room.localParticipant.sendText("end", { topic: TOPICS.control });
      await Promise.race([waitForSummary, timeout]);
    } catch (err) {
      console.error(err);
    }
    c.summaryReceived = null;
    await c.room.disconnect();
  }

  els.button.addEventListener("click", () => {
    if (conv && conv.active) {
      endConversation();
    } else {
      audio.prepare();
      connect();
    }
  });

  els.micToggle.addEventListener("click", async () => {
    const c = conv;
    if (!c || !c.active || !c.micPublished) return;
    try {
      if (c.muted) {
        await c.micTrack.unmute();
      } else {
        await c.micTrack.mute();
      }
      c.muted = !c.muted;
      c.silentSince = 0;
    } catch (err) {
      console.error(err);
    }
  });

  els.sound.addEventListener("click", async () => {
    const c = conv;
    if (!c || !c.room) return;
    try {
      await c.room.startAudio();
    } catch (err) {
      console.error(err);
    }
    c.playbackBlocked = !c.room.canPlaybackAudio;
  });

  for (const el of [els.model, els.character, els.voice, els.prompt]) {
    el.addEventListener("change", saveSettings);
  }

  resetPanels();
  render(performance.now());
  loadCatalog();
})();
