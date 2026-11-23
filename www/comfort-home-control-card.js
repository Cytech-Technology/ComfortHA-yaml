/* Dynamic Comfort menu. Uses HA state and button.press; no broker credentials. */
class ComfortHomeControlCard extends HTMLElement {
  setConfig(config) {
    this.config = { domain: "cytech_comfort_mqtt", ...config };
    this.attachShadowIfNeeded();
    this.signature = undefined;
    if (this._hass) this.render();
  }

  attachShadowIfNeeded() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  set hass(hass) { this._hass = hass; if (this.config) this.render(); }
  getCardSize() { return 10; }

  resolveEntity(c, suffix) {
    const domain = this.config.domain;
    return (this._relevant || []).find(s =>
      s.attributes.comfort_home_control === `${domain}:${c.group}:${c.key}:${suffix}`);
  }

  actionTarget(c, action) {
    if (action.default_action) {
      if (c.feedback_number == null) return undefined;
      const type = (c.feedback_type || "").toLowerCase();
      const entityId = `switch.${this.config.domain}_${type}${String(c.feedback_number).padStart(3, "0")}`;
      const state = this._hass.states[entityId];
      return state ? { entityId, type: "switch", on: action.number === 1, state } : undefined;
    }
    const entity = this.resolveEntity(c, action.number);
    return entity ? { entityId: entity.entity_id, type: "button", state: entity } : undefined;
  }

  async press(c, action) {
    const target = this.actionTarget(c, action);
    if (!target) return;
    this.busy = target.entityId;
    this.error = "";
    this.signature = undefined;
    this.render();
    try {
      if (target.type === "switch") {
        await this._hass.callService("switch", target.on ? "turn_on" : "turn_off",
          { entity_id: target.entityId });
      } else {
        await this._hass.callService("button", "press", { entity_id: target.entityId });
      }
    } catch (error) {
      this.error = `Action could not be sent: ${error.message || error}`;
    } finally {
      this.busy = undefined;
      this.signature = undefined;
      this.render();
    }
  }

  feedback(c) {
    const type = (c.feedback_type || "").toLowerCase();
    const hasFeedback = !["", "none", "x10"].includes(type);
    if (!hasFeedback) return { hasFeedback: false };
    const binary = type === "output" || type === "flag";
    const state = this.resolveEntity(c, "state");
    if (!state) return { hasFeedback: true, binary, text: "Waiting for entity", pending: true };
    if (state.state === "unknown") return { hasFeedback: true, binary, text: "Unknown", unknown: true };
    if (state.state === "unavailable") return { hasFeedback: true, binary, text: "Unavailable", unavailable: true };
    if (binary) {
      const on = state.state !== "0";
      return { hasFeedback: true, binary, on, definite: true, text: on ? "On" : "Off" };
    }
    const numeric = state.state !== "" && !Number.isNaN(Number(state.state));
    return { hasFeedback: true, binary, text: state.state, numeric };
  }

  groupIcon(label) {
    const text = (label || "").toLowerCase();
    if (text.includes("light")) return "mdi:lightbulb-group";
    if (text.includes("flag")) return "mdi:flag";
    if (text.includes("counter")) return "mdi:counter";
    if (text.includes("sensor")) return "mdi:gauge";
    if (text.includes("curtain") || text.includes("blind")) return "mdi:curtains";
    if (text.includes("scene")) return "mdi:palette";
    if (text.includes("fan")) return "mdi:fan";
    if (text.includes("heat")) return "mdi:radiator";
    return "mdi:view-grid";
  }

  itemIcon(c) {
    const text = `${c.label || ""} ${c.group_label || ""}`.toLowerCase();
    const has = (...keys) => keys.some(key => text.includes(key));
    if (has("light", "lamp", "chandelier", "spot", "downlight", "sconce")) return "mdi:lightbulb";
    if (has("curtain", "blind", "shade")) return "mdi:curtains";
    if (has("heater", "heating", "radiator", "thermostat")) return "mdi:radiator";
    if (has("aircon", "air con", "a/c", "hvac")) return "mdi:air-conditioner";
    if (has("fan")) return "mdi:fan";
    if (has("gate")) return "mdi:gate";
    if (has("door")) return "mdi:door";
    if (has("pump")) return "mdi:pump";
    if (has("power", "plug", "socket")) return "mdi:power-plug";
    if (has("garden", "sprinkler", "irrigation")) return "mdi:sprinkler";
    const type = (c.feedback_type || "").toLowerCase();
    if (type === "flag") return "mdi:flag";
    if (type === "counter") return "mdi:counter";
    if (type === "sensor") return "mdi:gauge";
    if (type === "output") return "mdi:toggle-switch";
    return "mdi:checkbox-blank-circle-outline";
  }

  render() {
    if (!this._hass) return;
    const states = Object.values(this._hass.states);
    const domain = this.config.domain;
    const menu = states.find(s => s.attributes.comfort_home_control_menu === domain);
    const relevant = states.filter(s => s.attributes.comfort_home_control?.startsWith(`${domain}:`));
    const signature = JSON.stringify([menu, relevant, this.selected, this.busy, this.error]);
    if (signature === this.signature) return;
    this.signature = signature;
    this._relevant = relevant;

    const root = this.shadowRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      :host { display: block; }
      * { box-sizing: border-box; }
      ha-card { padding: 20px; }
      h2 { margin: 0; font-size: 20px; font-weight: 500; color: var(--primary-text-color); }
      .empty { color: var(--secondary-text-color); margin: 16px 0 0; }
      nav { display: flex; gap: 8px; flex-wrap: wrap; margin: 16px 0; }
      .group-tab { display: inline-flex; align-items: center; gap: 7px; font: inherit; font-size: 14px;
        cursor: pointer; border: 1px solid var(--divider-color); border-radius: 999px; padding: 8px 14px;
        color: var(--primary-text-color); background: var(--card-background-color);
        transition: background-color .18s, border-color .18s, color .18s; }
      .group-tab:hover { border-color: var(--primary-color); }
      .group-tab[aria-pressed="true"] { background: var(--primary-color);
        border-color: var(--primary-color); color: var(--text-primary-color); }
      .group-tab ha-icon { --mdc-icon-size: 18px; }
      .badge { font-size: 11px; font-weight: 600; line-height: 1; padding: 3px 7px; border-radius: 999px;
        background: rgba(var(--rgb-primary-text-color, 0, 0, 0), .08); }
      .group-tab[aria-pressed="true"] .badge { background: rgba(255, 255, 255, .22); }
      .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 230px), 1fr)); gap: 12px; }
      .tile { display: flex; flex-direction: column; gap: 12px; padding: 16px;
        border: 1px solid var(--divider-color); border-radius: 16px; background: var(--card-background-color);
        transition: background-color .2s, border-color .2s, box-shadow .2s; }
      .tile.on { background: rgba(242, 169, 59, .10); border-color: rgba(242, 169, 59, .45); }
      .tile.dim { opacity: .6; }
      .head { display: flex; align-items: center; gap: 12px; min-height: 42px; }
      .icon-wrap { flex: 0 0 auto; width: 42px; height: 42px; border-radius: 13px; display: grid; place-items: center;
        background: rgba(var(--rgb-primary-color, 33, 150, 243), .13); color: var(--primary-color); }
      .icon-wrap ha-icon { --mdc-icon-size: 24px; }
      .tile.on .icon-wrap { background: rgba(242, 169, 59, .20); color: #e8930c; }
      .label { font-weight: 600; font-size: 15px; line-height: 1.25; color: var(--primary-text-color);
        overflow-wrap: anywhere; }
      .status { display: flex; align-items: center; gap: 8px; min-height: 22px; font-size: 14px;
        color: var(--secondary-text-color); }
      .status.on { color: var(--primary-text-color); font-weight: 500; }
      .dot { flex: 0 0 auto; width: 9px; height: 9px; border-radius: 50%;
        background: var(--disabled-text-color, #9e9e9e); }
      .tile.on .dot { background: #f2a93b; box-shadow: 0 0 0 4px rgba(242, 169, 59, .18); }
      .value { display: inline-block; font-size: 30px; font-weight: 600; line-height: 1.1;
        color: var(--primary-text-color); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
      .unit { margin-left: 8px; font-size: 11px; font-weight: 600; letter-spacing: .4px;
        text-transform: uppercase; color: var(--secondary-text-color); }
      .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: auto; }
      .seg { display: flex; width: 100%; margin-top: auto; border: 1px solid var(--divider-color);
        border-radius: 10px; overflow: hidden; }
      .seg button { flex: 1 1 0; border: 0; background: transparent; color: var(--primary-text-color);
        padding: 10px 8px; font: inherit; font-size: 14px; font-weight: 500; cursor: pointer;
        transition: background-color .18s, color .18s; }
      .seg button + button { border-left: 1px solid var(--divider-color); }
      .seg button:hover:not(:disabled):not(.active) {
        background: rgba(var(--rgb-primary-color, 33, 150, 243), .08); }
      .seg button.active { background: var(--primary-color); color: var(--text-primary-color); }
      .chip { font: inherit; font-size: 13px; font-weight: 500; cursor: pointer;
        border: 1px solid var(--divider-color); border-radius: 999px; padding: 8px 14px;
        color: var(--primary-text-color); background: transparent;
        transition: background-color .18s, border-color .18s; }
      .chip:hover:not(:disabled) { border-color: var(--primary-color);
        background: rgba(var(--rgb-primary-color, 33, 150, 243), .08); }
      button:disabled { opacity: .4; cursor: default; }
      button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
      .busy { animation: comfort-pulse 1s ease-in-out infinite; }
      @keyframes comfort-pulse { 50% { opacity: .55; } }
      .banner { padding: 10px 12px; border-radius: 10px; margin: 12px 0 0; font-size: 14px; }
      .banner.error { background: rgba(var(--rgb-error-color, 244, 67, 54), .12); color: var(--error-color); }
      .banner.status { background: rgba(var(--rgb-primary-color, 33, 150, 243), .10);
        color: var(--primary-text-color); }
      @media (prefers-reduced-motion: reduce) { * { transition: none !important; animation: none !important; } }
    `;
    root.append(style);
    const card = document.createElement("ha-card");
    root.append(card);
    const text = (parent, tag, value, className) => {
      const node = document.createElement(tag);
      node.textContent = value;
      if (className) node.className = className;
      parent.append(node);
      return node;
    };
    const iconEl = (name) => {
      const node = document.createElement("ha-icon");
      node.setAttribute("icon", name);
      return node;
    };
    text(card, "h2", this.config.title || "Control");
    if (!menu) { text(card, "p", "Waiting for Control discovery and menu metadata.", "empty"); return; }
    const offline = menu.state === "unavailable" || menu.state === "unknown";
    if (offline) {
      text(card, "div", "Comfort is unavailable. Actions are disabled.", "banner status")
        .setAttribute("role", "status");
    }
    if (this.error) {
      text(card, "div", this.error, "banner error").setAttribute("role", "alert");
    }
    const items = Array.isArray(menu.attributes.items) ? menu.attributes.items : [];
    if (!items.length) { text(card, "p", "No Control definitions in the active CCLX.", "empty"); return; }
    const groups = [...new Set(items.map(c => c.group))]
      .sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));
    if (!groups.includes(this.selected)) this.selected = groups[0];
    const nav = document.createElement("nav");
    nav.setAttribute("aria-label", "Control groups");
    card.append(nav);
    for (const group of groups) {
      const groupItems = items.filter(c => c.group === group);
      const sample = groupItems[0];
      const button = document.createElement("button");
      button.className = "group-tab";
      button.setAttribute("aria-pressed", String(group === this.selected));
      button.append(iconEl(this.groupIcon(sample.group_label)));
      text(button, "span", sample.group_label || `Group ${group}`);
      text(button, "span", String(groupItems.length), "badge");
      button.onclick = () => { this.selected = group; this.render(); };
      nav.append(button);
    }
    const grid = document.createElement("div");
    grid.className = "grid";
    card.append(grid);
    for (const c of items.filter(c => c.group === this.selected)
      .sort((a, b) => (a.key > b.key ? 1 : a.key < b.key ? -1 : 0))) {
      const fb = this.feedback(c);
      const tile = document.createElement("section");
      tile.className = "tile" + (fb.on ? " on" : "") + (fb.unavailable ? " dim" : "");
      grid.append(tile);
      const head = document.createElement("div");
      head.className = "head";
      tile.append(head);
      const iconWrap = document.createElement("div");
      iconWrap.className = "icon-wrap";
      iconWrap.append(iconEl(this.itemIcon(c)));
      head.append(iconWrap);
      text(head, "div", c.label || `Item ${c.key}`, "label");
      if (!fb.hasFeedback) {
        text(tile, "div", "No status feedback", "status");
      } else if (fb.numeric) {
        const row = document.createElement("div");
        row.className = "status";
        text(row, "span", String(fb.text), "value");
        text(row, "span", c.feedback_type || "", "unit");
        tile.append(row);
      } else {
        const row = document.createElement("div");
        row.className = "status" + (fb.on ? " on" : "");
        const dot = document.createElement("span");
        dot.className = "dot";
        row.append(dot);
        text(row, "span", offline ? "Unavailable" : fb.text);
        tile.append(row);
      }
      const actions = (c.actions || []).slice();
      const configured = new Set(actions.map(a => a.number));
      if (fb.binary) {
        if (!configured.has(0)) actions.push({ number: 0, label: "Off", default_action: true });
        if (!configured.has(1)) actions.push({ number: 1, label: "On", default_action: true });
        actions.sort((a, b) => a.number - b.number);
      }
      const segmented = fb.binary && actions.length === 2 &&
        actions[0].number === 0 && actions[1].number === 1;
      if (segmented) {
        const seg = document.createElement("div");
        seg.className = "seg";
        seg.setAttribute("role", "group");
        seg.setAttribute("aria-label", `${c.label || "Item"} control`);
        for (const a of actions) {
          const target = this.actionTarget(c, a);
          const unavailable = target && target.state && target.state.state === "unavailable";
          const active = !!(fb.definite && ((a.number === 1 && fb.on) || (a.number === 0 && !fb.on)));
          const button = document.createElement("button");
          button.textContent = a.label || (a.number ? "On" : "Off");
          if (active) button.classList.add("active");
          if (this.busy && target && this.busy === target.entityId) button.classList.add("busy");
          button.setAttribute("aria-pressed", String(active));
          button.disabled = offline || !target || unavailable || (this.busy === (target && target.entityId));
          button.onclick = () => this.press(c, a);
          seg.append(button);
        }
        tile.append(seg);
      } else if (actions.length) {
        const row = document.createElement("div");
        row.className = "actions";
        for (const a of actions) {
          const target = this.actionTarget(c, a);
          const unavailable = target && target.state && target.state.state === "unavailable";
          const button = text(row, "button", a.label || `Action ${a.number}`, "chip");
          if (this.busy && target && this.busy === target.entityId) button.classList.add("busy");
          button.disabled = offline || !target || unavailable || (this.busy === (target && target.entityId));
          button.onclick = () => this.press(c, a);
        }
        tile.append(row);
      }
    }
  }
}
if (!customElements.get("comfort-home-control-card")) {
  customElements.define("comfort-home-control-card", ComfortHomeControlCard);
}
