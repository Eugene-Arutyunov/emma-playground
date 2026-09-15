// Адрес задеплоенного token-сервера (agent/server.py на Railway).
// Пример: "https://emma-token-production.up.railway.app"
const EMMA_TOKEN_SERVER_URL = "https://token-production-a25e.up.railway.app";

const EMMA_STRINGS = {
  ru: {
    idle: "",
    connecting: "Соединяюсь…",
    connected: "Эмма слушает. Говори.",
    summarizing: "Эмма подводит итог…",
    error: "Не получилось подключиться. ",
    wrongCode: "Неверный код доступа.",
    askCode: "Код доступа",
    notConfigured: "Прототип ещё не подключён к серверу — впиши EMMA_TOKEN_SERVER_URL в js/emma-connect.js.",
    catalogError: "Сервер недоступен, попробуй обновить страницу.",
    disconnect: "Закончить разговор",
    talk: "Поговорить с Эммой",
    you: "Ты",
    emma: "Эмма",
    summary: "Итог",
    transcriptPlaceholder: "Здесь появится текст разговора.",
    signalsPlaceholder: "Наблюдения второго канала появятся во время разговора.",
  },
  en: {
    idle: "",
    connecting: "Connecting…",
    connected: "Emma is listening. Go ahead.",
    summarizing: "Emma is wrapping up…",
    error: "Couldn't connect. ",
    wrongCode: "Wrong access code.",
    askCode: "Access code",
    notConfigured: "The prototype isn't wired to a server yet — set EMMA_TOKEN_SERVER_URL in js/emma-connect.js.",
    catalogError: "The server is unavailable, try reloading the page.",
    disconnect: "End conversation",
    talk: "Talk to Emma",
    you: "You",
    emma: "Emma",
    summary: "Wrap-up",
    transcriptPlaceholder: "The conversation transcript will appear here.",
    signalsPlaceholder: "Observations from the second channel will appear during the conversation.",
  },
};

const STORAGE_KEYS = { code: "emma-access-code", settings: "emma-settings" };

const TOPICS = {
  transcription: "lk.transcription",
  signals: "emma.signals",
  summary: "emma.summary",
  control: "emma.control",
};

const SUMMARY_TIMEOUT_MS = 20000;

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
    signals: $("emma-signals"),
  };
  if (Object.values(els).some((el) => !el)) return;

  const lang = document.documentElement.lang === "en" ? "en" : "ru";
  const t = EMMA_STRINGS[lang];

  let room = null;
  let ending = false;
  let summaryReceived = null;
  const audioElements = [];
  const segments = new Map();

  function setStatus(text) {
    els.status.textContent = text;
  }

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

  function currentSettings() {
    return {
      model: els.model.value,
      character: els.character.value,
      voice: els.voice.value,
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

  function fillSelect(select, items, selected) {
    select.innerHTML = "";
    for (const item of items) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = typeof item.label === "string" ? item.label : item.label[lang];
      select.appendChild(option);
    }
    if (selected && items.some((item) => item.id === selected)) select.value = selected;
  }

  async function loadCatalog() {
    if (!EMMA_TOKEN_SERVER_URL) {
      setStatus(t.notConfigured);
      els.button.disabled = true;
      return;
    }
    try {
      const res = await fetch(`${EMMA_TOKEN_SERVER_URL}/catalog`);
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

  function showPlaceholder(container, text) {
    container.innerHTML = "";
    const p = document.createElement("p");
    p.className = "emma__placeholder";
    p.textContent = text;
    container.appendChild(p);
  }

  function clearPlaceholder(container) {
    const p = container.querySelector(".emma__placeholder");
    if (p) p.remove();
  }

  function scrollToBottom(container) {
    container.scrollTop = container.scrollHeight;
  }

  function addEntry(container, className, title, text) {
    clearPlaceholder(container);
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
    showPlaceholder(els.transcript, t.transcriptPlaceholder);
    showPlaceholder(els.signals, t.signalsPlaceholder);
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
      addEntry(els.signals, "emma__signal", signal.signal, signal.note);
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
      const res = await fetch(`${EMMA_TOKEN_SERVER_URL}/token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ access_code: accessCode, lang, ...currentSettings() }),
      });
      if (res.status === 403) {
        writeStored(STORAGE_KEYS.code, null);
        throw new Error(t.wrongCode);
      }
      if (!res.ok) throw new Error(`token server: ${res.status}`);
      const { url, token } = await res.json();

      room = new LivekitClient.Room();

      room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === LivekitClient.Track.Kind.Audio) {
          const el = track.attach();
          el.autoplay = true;
          document.body.appendChild(el);
          audioElements.push(el);
        }
      });
      room.on(LivekitClient.RoomEvent.Disconnected, onDisconnected);
      room.registerTextStreamHandler(TOPICS.transcription, onTranscription);
      room.registerTextStreamHandler(TOPICS.signals, onSignal);
      room.registerTextStreamHandler(TOPICS.summary, onSummary);

      await room.connect(url, token);
      await room.localParticipant.setMicrophoneEnabled(true);

      resetPanels();
      setStatus(t.connected);
      els.button.textContent = t.disconnect;
      els.button.disabled = false;
    } catch (err) {
      console.error(err);
      setStatus(t.error + err.message);
      els.button.disabled = false;
      setControlsDisabled(false);
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
    room = null;
    ending = false;
    for (const el of audioElements.splice(0)) el.remove();
    setStatus(t.idle);
    els.button.textContent = t.talk;
    els.button.disabled = false;
    setControlsDisabled(false);
  }

  els.button.addEventListener("click", () => {
    if (room) endConversation();
    else connect();
  });

  for (const el of [els.model, els.character, els.voice, els.prompt]) {
    el.addEventListener("change", saveSettings);
  }

  resetPanels();
  loadCatalog();
})();
