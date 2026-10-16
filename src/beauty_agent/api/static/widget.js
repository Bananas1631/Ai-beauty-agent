/*
 * Виджет чата ИИ-администратора для сайта салона. Подключение одной строкой:
 *
 *   <script src="https://<сервис>/widget.js" data-salon="studio-lavanda" async></script>
 *
 * Необязательно: data-title="Запись онлайн", data-color="#8a4f9e".
 *
 * - Shadow DOM (closed): стили сайта не ломают виджет, а скрипты сайта не читают переписку.
 * - Текст сообщений вставляется только через textContent — ответ не выполнится как HTML.
 * - Сессия — случайный ID в localStorage: клиент продолжает разговор после перезагрузки.
 * - Переподключение с растущей паузой (1 → 30 с), сразу — когда вкладка снова видна или сеть вернулась.
 * - Домен сайта должен быть в allowed_origins салона: иначе сервер не примет соединение.
 */
(() => {
  "use strict";

  const script = document.currentScript;
  if (!script || window.__beautyAgentWidget) return;
  window.__beautyAgentWidget = true;

  const slug = (script.dataset.salon || "").trim();
  if (!/^[a-z0-9-]{1,64}$/.test(slug)) {
    console.warn("beauty-agent: укажите data-salon=\"<slug салона>\"");
    return;
  }
  const base = new URL(script.src);
  const title = (script.dataset.title || "Запись онлайн").slice(0, 40);
  const color = /^#[0-9a-fA-F]{6}$/.test(script.dataset.color || "") ? script.dataset.color : "#8a4f9e";
  const storageKey = `beauty-agent:session:${slug}`;
  const MAX_TEXT = 2000;

  const css = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
    .launcher { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000; border: 0; border-radius: 28px;
      padding: 14px 20px; background: var(--accent); color: #fff; font-size: 15px; cursor: pointer;
      box-shadow: 0 6px 20px rgba(0,0,0,.2); }
    .panel { position: fixed; right: 20px; bottom: 84px; z-index: 2147483000; width: 360px; max-width: calc(100vw - 32px);
      height: 520px; max-height: calc(100vh - 120px); display: flex; flex-direction: column; background: #fff;
      color: #2b2330; border-radius: 16px; box-shadow: 0 12px 40px rgba(0,0,0,.25); overflow: hidden; }
    .panel[hidden] { display: none; }
    .head { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px;
      background: var(--accent); color: #fff; font-size: 15px; font-weight: 600; }
    .close { border: 0; background: transparent; color: #fff; font-size: 20px; cursor: pointer; line-height: 1; }
    .list { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 8px; background: #faf7fb; }
    .msg { max-width: 85%; padding: 8px 12px; border-radius: 12px; font-size: 14px; line-height: 1.4;
      white-space: pre-wrap; overflow-wrap: anywhere; }
    .agent { align-self: flex-start; background: #fff; border: 1px solid #e6dcea; }
    .client { align-self: flex-end; background: #efe4f3; }
    .system { align-self: center; background: #fff4e5; font-size: 13px; }
    .typing { padding: 0 12px 8px; color: #7a6f80; font-size: 13px; background: #faf7fb; }
    .typing[hidden] { display: none; }
    form { display: flex; gap: 8px; padding: 10px; border-top: 1px solid #e6dcea; }
    input { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid #e6dcea; border-radius: 10px; font-size: 14px; }
    button.send { border: 0; border-radius: 10px; padding: 0 14px; background: var(--accent); color: #fff; cursor: pointer; }
    button.send:disabled { opacity: .5; cursor: wait; }
    .note { padding: 0 12px 10px; font-size: 11px; color: #7a6f80; }
    .note a { color: var(--accent); }
    @media (prefers-color-scheme: dark) {
      .panel { background: #26212b; color: #f1eaf4; }
      .list, .typing { background: #1c1820; }
      .agent { background: #26212b; border-color: #3a3240; }
      .client { background: #3a2f42; }
      .system { background: #3d3322; }
      input { background: #1c1820; color: #f1eaf4; border-color: #3a3240; }
    }`;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  const host = document.createElement("div");
  host.setAttribute("data-beauty-agent", slug);
  const root = host.attachShadow({ mode: "closed" });
  const style = document.createElement("style");
  style.textContent = css;
  root.appendChild(style);
  host.style.setProperty("--accent", color);

  const launcher = el("button", "launcher", title);
  launcher.type = "button";
  launcher.setAttribute("aria-expanded", "false");
  const panel = el("section", "panel");
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", title);
  const head = el("div", "head", title);
  const close = el("button", "close", "×");
  close.type = "button";
  close.setAttribute("aria-label", "Свернуть чат");
  head.appendChild(close);
  const list = el("div", "list");
  list.setAttribute("aria-live", "polite");
  const typing = el("div", "typing", "Администратор печатает…");
  typing.hidden = true;
  const form = el("form");
  const input = el("input");
  input.maxLength = MAX_TEXT;
  input.placeholder = "Напишите сообщение…";
  input.setAttribute("aria-label", "Сообщение");
  const send = el("button", "send", "→");
  send.type = "submit";
  send.setAttribute("aria-label", "Отправить");
  form.append(input, send);
  const note = el("div", "note");
  const policy = el("a", "", "Политика обработки персональных данных");
  policy.href = `${base.origin}/privacy/${slug}`;
  policy.target = "_blank";
  policy.rel = "noopener";
  note.appendChild(policy);
  panel.append(head, list, typing, form, note);
  root.append(launcher, panel);

  let socket = null;
  let retryDelay = 1000;
  let retryTimer = null;
  let waiting = false;
  let greeted = false;

  function readSession() {
    try {
      return window.localStorage.getItem(storageKey) || "";
    } catch {
      return "";
    }
  }

  function saveSession(id) {
    try {
      window.localStorage.setItem(storageKey, id);
    } catch {
      /* без localStorage чат работает до закрытия вкладки */
    }
  }

  function add(text, author) {
    list.appendChild(el("div", `msg ${author}`, text));
    list.scrollTop = list.scrollHeight;
  }

  function setWaiting(value) {
    waiting = value;
    typing.hidden = !value;
    send.disabled = value;
  }

  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    clearTimeout(retryTimer);
    const scheme = base.protocol === "https:" ? "wss" : "ws";
    const session = encodeURIComponent(readSession());
    socket = new WebSocket(`${scheme}://${base.host}/ws/s/${encodeURIComponent(slug)}?session=${session}`);
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
      if (data.type === "session" && typeof data.id === "string") saveSession(data.id);
      else if (data.type === "typing") setWaiting(true);
      else if (data.type === "message" && typeof data.text === "string") {
        setWaiting(false);
        add(data.text, "agent");
      } else if (data.type === "error" && typeof data.text === "string") {
        setWaiting(false);
        add(data.text, "system");
      }
    });
    socket.addEventListener("close", () => {
      setWaiting(false);
      if (panel.hidden) return; // свёрнутый чат не держит соединение
      retryTimer = setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    });
  }

  function open() {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    if (!greeted) {
      add("Здравствуйте! Помогу выбрать услугу и записаться.", "agent");
      greeted = true;
    }
    connect();
    input.focus();
  }

  function hide() {
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
  }

  launcher.addEventListener("click", () => (panel.hidden ? open() : hide()));
  close.addEventListener("click", hide);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim().slice(0, MAX_TEXT);
    if (!text || waiting) return;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      add("Нет связи — переподключаюсь, отправьте ещё раз через пару секунд.", "system");
      connect();
      return;
    }
    add(text, "client");
    socket.send(JSON.stringify({ type: "message", text }));
    input.value = "";
    setWaiting(true);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !panel.hidden) {
      retryDelay = 1000;
      connect();
    }
  });
  window.addEventListener("online", () => {
    if (!panel.hidden) connect();
  });

  (document.body || document.documentElement).appendChild(host);
})();
