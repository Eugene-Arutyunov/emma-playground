// Впиши сюда адрес задеплоенного token-сервера (agent/server.py на Railway/Render).
// Пример: "https://emma-agent-production.up.railway.app"
const EMMA_TOKEN_SERVER_URL = "";

const EMMA_STRINGS = {
  ru: {
    idle: "",
    connecting: "Соединяюсь…",
    connected: "Эмма слушает. Говори.",
    error: "Не получилось подключиться. ",
    notConfigured: "Прототип ещё не подключён к серверу — впиши EMMA_TOKEN_SERVER_URL в js/emma-connect.js.",
    disconnect: "Закончить разговор",
    talk: "Поговорить с Эммой",
  },
  en: {
    idle: "",
    connecting: "Connecting…",
    connected: "Emma is listening. Go ahead.",
    error: "Couldn't connect. ",
    notConfigured: "The prototype isn't wired to a server yet — set EMMA_TOKEN_SERVER_URL in js/emma-connect.js.",
    disconnect: "End conversation",
    talk: "Talk to Emma",
  },
};

(function () {
  const button = document.getElementById("emma-talk-button");
  const status = document.getElementById("emma-status");
  if (!button || !status) return;

  const lang = document.documentElement.lang === "en" ? "en" : "ru";
  const strings = EMMA_STRINGS[lang];

  let room = null;

  function setStatus(text) {
    status.textContent = text;
  }

  async function connect() {
    if (!EMMA_TOKEN_SERVER_URL) {
      setStatus(strings.notConfigured);
      return;
    }

    button.disabled = true;
    setStatus(strings.connecting);

    try {
      const res = await fetch(`${EMMA_TOKEN_SERVER_URL}/token`);
      if (!res.ok) throw new Error(`token server: ${res.status}`);
      const { url, token } = await res.json();

      room = new LivekitClient.Room();

      room.on(LivekitClient.RoomEvent.TrackSubscribed, (track) => {
        if (track.kind === LivekitClient.Track.Kind.Audio) {
          const el = track.attach();
          el.autoplay = true;
          document.body.appendChild(el);
        }
      });

      room.on(LivekitClient.RoomEvent.Disconnected, () => {
        setStatus(strings.idle);
        button.textContent = strings.talk;
        button.disabled = false;
        room = null;
      });

      await room.connect(url, token);
      await room.localParticipant.setMicrophoneEnabled(true);

      setStatus(strings.connected);
      button.textContent = strings.disconnect;
      button.disabled = false;
    } catch (err) {
      console.error(err);
      setStatus(strings.error + err.message);
      button.disabled = false;
    }
  }

  async function disconnect() {
    if (room) await room.disconnect();
  }

  button.addEventListener("click", () => {
    if (room) {
      disconnect();
    } else {
      connect();
    }
  });
})();
