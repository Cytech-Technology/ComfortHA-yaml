(() => {
  "use strict";

  const body = document.body;
  const BASE = (body.dataset.base || "").replace(/\/+$/, "");
  const CONFIGURED = body.dataset.configured === "1";
  const HA_CONFIGURED = body.dataset.haConfigured === "1";

  const $ = (id) => document.getElementById(id);
  const form = $("chat-form");
  const input = $("input");
  const sendBtn = $("send");
  const stopBtn = $("stop");
  const messages = $("messages");
  const welcome = $("welcome");
  const conversation = $("conversation");
  const activity = $("activity");
  const activityText = $("activity-text");
  const usageEl = $("usage");
  const resetBtn = $("reset");
  const notice = $("first-run");

  let controller = null;
  let current = null; // { el, content, raw, failed }

  const TOOL_LABELS = {
    list_entities: "Looking up entities",
    get_entity: "Reading an entity",
    get_history: "Checking recent history",
    get_log_errors: "Scanning the Home Assistant log",
    get_system_info: "Checking your system",
  };

  // ---- small helpers ------------------------------------------------

  function show(el) {
    el.hidden = false;
  }

  function hide(el) {
    el.hidden = true;
  }

  function setActivity(text) {
    activityText.textContent = text;
    show(activity);
  }

  function clearActivity() {
    hide(activity);
    activityText.textContent = "";
  }

  function scrollDown() {
    requestAnimationFrame(() => {
      conversation.scrollTop = conversation.scrollHeight;
    });
  }

  function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (ch) => {
      switch (ch) {
        case "&": return "&amp;";
        case "<": return "&lt;";
        case ">": return "&gt;";
        case '"': return "&quot;";
        default: return "&#39;";
      }
    });
  }

  function inline(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    return out;
  }

  function renderMarkdown(text) {
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    const out = [];
    let list = null;
    let para = [];

    const flushPara = () => {
      if (para.length) {
        out.push("<p>" + para.join("<br>") + "</p>");
        para = [];
      }
    };
    const closeList = () => {
      if (list) {
        out.push(`</${list}>`);
        list = null;
      }
    };

    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) {
        flushPara();
        closeList();
        continue;
      }

      const heading = line.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushPara();
        closeList();
        const level = Math.min(4, heading[1].length + 1);
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        continue;
      }

      const unordered = line.match(/^[-*]\s+(.*)$/);
      const ordered = line.match(/^\d+[.)]\s+(.*)$/);
      if (unordered || ordered) {
        flushPara();
        const kind = unordered ? "ul" : "ol";
        if (list !== kind) {
          closeList();
          out.push(`<${kind}>`);
          list = kind;
        }
        out.push(`<li>${inline((unordered || ordered)[1])}</li>`);
        continue;
      }

      closeList();
      para.push(inline(line));
    }

    flushPara();
    closeList();
    return out.join("");
  }

  // ---- message rendering --------------------------------------------

  function addMessage(role, text) {
    const el = document.createElement("div");
    el.className = "message " + role;
    el.textContent = text;
    messages.appendChild(el);
    scrollDown();
    return el;
  }

  function startAssistant() {
    const el = document.createElement("div");
    el.className = "message assistant";
    const content = document.createElement("div");
    content.className = "content";
    el.appendChild(content);
    messages.appendChild(el);
    current = { el, content, raw: "", failed: false };
    scrollDown();
  }

  function appendText(text) {
    if (!current) startAssistant();
    current.raw += text;
    current.content.innerHTML = renderMarkdown(current.raw);
    clearActivity();
    scrollDown();
  }

  function appendNote(text) {
    if (!current) return;
    current.stopped = true;
    const note = document.createElement("p");
    note.className = "stopped-note";
    note.textContent = text;
    current.content.appendChild(note);
  }

  function failCurrent(text) {
    if (current) current.failed = true;
    const el = document.createElement("div");
    el.className = "message error";
    el.textContent = text;
    messages.appendChild(el);
    scrollDown();
  }

  function finishCurrent() {
    if (!current) return;
    if (!current.raw && !current.failed && !current.stopped) {
      current.el.remove();
    }
    current = null;
  }

  // ---- usage --------------------------------------------------------

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  async function refreshUsage() {
    if (!CONFIGURED) {
      hide(usageEl);
      return;
    }
    try {
      const response = await fetch(`${BASE}/api/usage`, {
        headers: { Accept: "application/json" },
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      const used = number(data.used && data.used.requests);
      const limit = number(data.limits && data.limits.requests);
      if (limit <= 0) throw new Error("unavailable");
      usageEl.textContent =
        `AI allowance: ${used.toLocaleString()} of ` +
        `${limit.toLocaleString()} service requests used this month`;
      show(usageEl);
    } catch (_) {
      hide(usageEl);
    }
  }

  // ---- chat ---------------------------------------------------------

  function busyState(isBusy) {
    const locked = isBusy || !CONFIGURED || !HA_CONFIGURED;
    sendBtn.disabled = locked;
    input.disabled = locked;
    stopBtn.hidden = !isBusy;
    if (!isBusy) {
      input.focus();
      autoGrow();
    }
  }

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 168) + "px";
  }

  async function send(text) {
    const question = (text || "").trim();
    if (!question || controller) return;

    hide(welcome);
    addMessage("user", question);
    input.value = "";
    autoGrow();
    startAssistant();
    setActivity("Thinking…");
    busyState(true);
    controller = new AbortController();
    const signal = controller.signal;

    try {
      const response = await fetch(`${BASE}/api/chat`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-Cytech-Client": "1",
        },
        body: JSON.stringify({ message: question }),
        signal,
      });

      if (!response.ok) {
        let message = "The assistant could not answer. Please try again.";
        try {
          const data = await response.json();
          if (data && data.error) message = data.error;
        } catch (_) { /* keep default */ }
        throw new Error(message);
      }

      await readEvents(response);
    } catch (error) {
      if (error && error.name === "AbortError") {
        appendNote("Stopped.");
      } else {
        failCurrent(
          (error && error.message) ||
            "The assistant could not answer. Please try again."
        );
      }
    } finally {
      controller = null;
      clearActivity();
      finishCurrent();
      busyState(false);
      refreshUsage();
    }
  }

  async function readEvents(response) {
    if (!response.body) {
      throw new Error(
        "This browser cannot show live answers. Please use a current browser."
      );
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const chunk = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        handleEvent(chunk);
      }
    }

    buffer += decoder.decode();
    if (buffer.trim()) handleEvent(buffer);
  }

  function handleEvent(chunk) {
    let eventName = "message";
    const dataLines = [];

    for (const line of chunk.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (!dataLines.length) return;

    let payload;
    try {
      payload = JSON.parse(dataLines.join("\n"));
    } catch (_) {
      return;
    }

    switch (eventName) {
      case "status":
        setActivity(payload.message || "Working…");
        break;
      case "tool":
        if (payload.status === "start") {
          const label =
            TOOL_LABELS[payload.name] || "Reading Home Assistant";
          setActivity(label + "…");
        } else {
          clearActivity();
        }
        break;
      case "text":
        appendText(payload.text || "");
        break;
      case "error":
        failCurrent(payload.message || "Something went wrong.");
        break;
      default:
        break;
    }
  }

  // ---- events -------------------------------------------------------

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (controller) return;
    send(input.value);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  input.addEventListener("input", autoGrow);

  stopBtn.addEventListener("click", () => {
    if (controller) controller.abort();
  });

  resetBtn.addEventListener("click", async () => {
    if (controller) controller.abort();
    try {
      await fetch(`${BASE}/api/reset`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "X-Cytech-Client": "1" },
      });
    } catch (_) { /* the UI resets regardless */ }
    messages.innerHTML = "";
    current = null;
    show(welcome);
    hide(notice);
    refreshUsage();
  });

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const chip = target.closest(".suggestion");
    if (chip) {
      const question = (chip.textContent || "").trim();
      input.value = question;
      autoGrow();
      send(question);
    }
  });

  $("dismiss-notice").addEventListener("click", () => {
    try {
      localStorage.setItem("cytech-ai-notice", "1");
    } catch (_) { /* private mode */ }
    hide(notice);
  });

  // ---- first run ----------------------------------------------------

  let noticeDismissed = false;
  try {
    noticeDismissed = localStorage.getItem("cytech-ai-notice") === "1";
  } catch (_) { /* private mode */ }
  if (!noticeDismissed) show(notice);

  if (!CONFIGURED || !HA_CONFIGURED) {
    const banner = $("setup-banner");
    banner.textContent = !CONFIGURED
      ? "This assistant is not set up yet. Ask your installer for an " +
        "installation token, then enter it in Settings → Add-ons → " +
        "Cytech AI Assistant → Configuration."
      : "Home Assistant access is not available to this add-on. Restart " +
        "the add-on; if this persists, reinstall it.";
    show(banner);
  }

  busyState(false);
  refreshUsage();
})();
