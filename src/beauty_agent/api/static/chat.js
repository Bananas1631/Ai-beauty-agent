// Веб-чат на странице салона. Текст сообщений вставляется только через textContent:
// ответ агента и ввод клиента не могут выполниться как HTML или скрипт.
(() => {
  "use strict";

  const root = document.getElementById("chat");
  const messages = document.getElementById("messages");
  const typing = document.getElementById("typing");
  const form = document.getElementById("form");
  const input = document.getElementById("input");
  const button = form.querySelector("button");
  const slug = root.dataset.slug;
  const storageKey = `beauty-agent:session:${slug}`;

  let socket = null;
  let retryDelay = 1000;
  let waiting = false;

  function readSession() {
    try {
      return window.localStorage.getItem(storageKey) || "";
    } catch {
      return ""; // приватный режим браузера: сессия живёт до закрытия вкладки
    }
  }

  function saveSession(id) {
    try {
      window.localStorage.setItem(storageKey, id);
    } catch {
      /* без localStorage чат всё равно работает */
    }
  }

  function addMessage(text, author) {
    const item = document.createElement("div");
    item.className = `message message--${author}`;
    item.textContent = text;
    messages.appendChild(item);
    messages.scrollTop = messages.scrollHeight;
  }

  function setWaiting(value) {
    waiting = value;
    typing.hidden = !value;
    button.disabled = value;
  }

  function connect() {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    const session = encodeURIComponent(readSession());
    socket = new WebSocket(`${scheme}://${window.location.host}/ws/s/${encodeURIComponent(slug)}?session=${session}`);

    socket.addEventListener("open", () => {
      retryDelay = 1000;
    });

    socket.addEventListener("message", (event) => {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.type === "session" && typeof data.id === "string") {
        saveSession(data.id);
      } else if (data.type === "typing") {
        setWaiting(true);
      } else if (data.type === "message" && typeof data.text === "string") {
        setWaiting(false);
        addMessage(data.text, "agent");
      } else if (data.type === "error" && typeof data.text === "string") {
        setWaiting(false);
        addMessage(data.text, "system");
      }
    });

    socket.addEventListener("close", () => {
      setWaiting(false);
      // переподключение с растущей паузой: сервер перезапускается при деплое
      window.setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || waiting || !socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    addMessage(text, "client");
    socket.send(JSON.stringify({ type: "message", text }));
    input.value = "";
    setWaiting(true);
  });

  connect();
})();
