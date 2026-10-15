/**
 * The Lovelace card, rendered without a browser.
 *
 *   node tests/test_card_render.mjs
 *
 * Nothing here is pip- or npm-installable: the card is plain DOM, so a ~60-line
 * stand-in for the handful of DOM APIs it touches is enough to run it for real.
 * That matters because the two failures this file exists to catch are both
 * *invisible*:
 *
 *  1. **The card drew nothing at all** (v0.1.2 → v0.2.1).  ``_ensureDom`` attached
 *     a shadow root and then appended the markup to ``this``.  Those are the light
 *     DOM, and a shadow host with no ``<slot>`` renders none of it -- so the
 *     dashboard showed an empty card, with no JS error, no missing entity, and a
 *     height of zero.  Every check except "is anything inside the shadow root?"
 *     passed.  ``assert inShadow`` below is that check.
 *
 *  2. **A tap that writes to the ledger must ask first, and say what it will
 *     do.**  ``_confirmMessage`` is asserted directly, which is why it is a
 *     method rather than three inline template strings.
 *
 * The shim deliberately models the one distinction the real bug turned on:
 * ``element.appendChild`` and ``shadowRoot.appendChild`` are different sinks.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SRC_PATH = join(ROOT, "custom_components", "taipower_bill", "www", "taipower-bill-card.js");

let checks = 0;
const ok = (cond, what) => {
  checks += 1;
  assert.ok(cond, what);
};

/* ------------------------------------------------------------------ shim */

class FakeNode {
  constructor(tag = "div", ns = null) {
    this.tagName = String(tag).toUpperCase();
    this.namespace = ns; // "shadow" marks a shadow root, so the test can look
    this.children = [];
    this.parent = null;
    this.attrs = {};
    this.className = "";
    this.style = {};
    this._html = "";
    this._text = "";
    this.listeners = [];
  }
  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
  set innerHTML(v) {
    this._html = String(v);
  }
  get innerHTML() {
    return this._html;
  }
  set textContent(v) {
    this._text = String(v);
  }
  get textContent() {
    return this._text;
  }
  setAttribute(k, v) {
    this.attrs[k] = String(v);
  }
  getAttribute(k) {
    return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
  }
  addEventListener(type, fn) {
    this.listeners.push([type, fn]);
  }
  /** Everything this subtree would show: markup plus loose text. */
  text() {
    return this._html + this._text + this.children.map((c) => c.text()).join("");
  }
}

class FakeElement extends FakeNode {
  constructor(tag) {
    super(tag);
    this.shadowRoot = null;
  }
  attachShadow() {
    this.shadowRoot = new FakeNode("#shadow", "shadow");
    this.shadowRoot.host = this;
    return this.shadowRoot;
  }
}

const registry = new Map();
const documentShim = {
  createElement(tag) {
    const Cls = registry.get(String(tag).toLowerCase());
    return Cls ? new Cls(tag) : new FakeElement(tag);
  },
};
const windowShim = { customCards: [], confirm: () => true };
const customElementsShim = {
  define(name, cls) {
    registry.set(name.toLowerCase(), cls);
  },
  get(name) {
    return registry.get(String(name).toLowerCase());
  },
};

/* --------------------------------------------------------- run the card */

const sandbox = {
  HTMLElement: FakeElement,
  document: documentShim,
  window: windowShim,
  customElements: customElementsShim,
  console,
};
vm.createContext(sandbox);
vm.runInContext(readFileSync(SRC_PATH, "utf8"), sandbox, { filename: SRC_PATH });

const Card = customElementsShim.get("taipower-bill-card");
ok(typeof Card === "function", "customElements.define('taipower-bill-card') ran");

/* A household mid-period, with a 結算 waiting to be confirmed. */
const ENTITY = "sensor.tai_dian_ahu_current_period_usage";
const attrsFor = (over = {}) => ({
  label: "A戶",
  usage_source: "statistics",
  kwh: 754.633,
  cost: 1787.73,
  period_start: "2026-09-08",
  next_read_date: "2026-11-06",
  last_read_date: "2026-09-07",
  suggested_read_date: "2026-10-05",
  days_elapsed: 37,
  days_remaining: 23,
  cycle_days: 60,
  readings: [
    { date: "2026-09-07", kwh: null, note: "migrated" },
    { date: "2026-10-05", kwh: 754.633, note: "close_period" },
  ],
  history: [],
  tiers: [{ up_to_kwh: 240, price: 1.78, kwh: 240, cost: 427.2 }],
  segments: [],
  ...over,
});
const PENDING = {
  period_start: "2026-09-08",
  read_date: "2026-10-05",
  kwh: 754.633,
  expires_at: "2026-10-14T12:59:46.974Z",
};

const mount = () => {
  const el = documentShim.createElement("taipower-bill-card");
  el.setConfig({ title: "台電電費", households: [{ entity: ENTITY, label: "A戶" }] });
  el.hass = { states: {}, callService: async () => {} };
  return el;
};

const withState = (attributes) => {
  const el = mount();
  el._hass = { states: { [ENTITY]: { state: "754.633", attributes } }, callService: async () => {} };
  el._sigs = {}; // force a real render, as a state change would
  el.hass = el._hass;
  return el;
};

/* 1. it draws, and what it draws lives in the shadow root ----------------- */

const el = withState(attrsFor({ pending_close: PENDING, pending_close_date: "2026-10-05" }));
const shadow = el.shadowRoot;
ok(shadow && shadow.namespace === "shadow", "a shadow root was attached");
ok(
  shadow.children.length === 1,
  `the markup went into the shadow root, not the light DOM ` +
    `(light DOM children: ${el.children.length}, shadow children: ${shadow.children.length})`,
);
ok(el.children.length === 0, "nothing is parked in the light DOM where it cannot be seen");

const text = shadow.text();
ok(text.includes("A戶"), "the household label is rendered");
ok(text.includes("2026-09-08"), "the billing window is rendered");
ok(text.includes("754.6"), "the period kWh is rendered");
ok(text.includes("<style>"), "the styles travelled with the markup");
ok(text.includes('data-action="confirm"'), "the 確認寫入 button is rendered");
ok(text.includes('data-action="cancel"'), "the 取消 button is rendered");

/* 2. the 撤銷 row is always there once there is a period to take back ------ */

const noPending = withState(attrsFor()).shadowRoot.text();
ok(noPending.includes('data-action="undo"'), "撤銷最後一期 is offered when nothing is pending");
ok(noPending.includes("撤銷最後一期"), "and it is labelled, not just an icon");
ok(noPending.includes("2026-09-07"), "and it names the 抄見日 it would remove");

/* 3. every ledger-writing tap asks first, by name ------------------------- */

const asks = (action, attributes = attrsFor({ pending_close: PENDING })) => {
  const node = withState(attributes);
  return node._confirmMessage(action, ENTITY, null);
};

const confirmMsg = asks("confirm");
ok(typeof confirmMsg === "string" && confirmMsg.length > 20, "確認寫入 asks a question");
ok(confirmMsg.includes("2026-10-05"), "...naming the 抄見日 it will record");
ok(confirmMsg.includes("754.6"), "...and the kWh");
ok(confirmMsg.includes("撤銷最後一期"), "...and says how to take it back");

const undoMsg = asks("undo");
ok(undoMsg.includes("2026-09-07"), "撤銷 names the date it will remove");
ok(undoMsg.includes("隔天"), "...and what the period will restart from");
ok(undoMsg.includes("重新寫入 2026-09-07"), "...and how to put it back");

const recomputeMsg = asks("recompute");
ok(recomputeMsg.includes("覆寫"), "重算 warns that it overwrites");

// 「結算」 only *proposes* -- nothing is written until 確認寫入, so it must not
// spend the user's attention on a dialog here.
ok(asks("close") === null, "結算 does not ask: it writes nothing");
ok(asks("cancel") === null, "取消 does not ask: it discards a proposal");
ok(asks("toggle-hist") === null, "opening a section does not ask");

// A button that carries its own date wins over the attribute (the history table
// has one 重算 per row).
const rowDate = withState(attrsFor())._confirmMessage("recompute", ENTITY, {
  getAttribute: () => "2026-08-10",
});
ok(rowDate.includes("2026-08-10"), "the row's own date is what 重算 names");

// ...and an entity that has gone missing must not crash the card.
const orphan = mount();
orphan._hass = { states: {}, callService: async () => {} };
orphan.hass = orphan._hass;
ok(orphan.shadowRoot.text().includes("找不到"), "a missing entity renders an explanation");

// 2. the card's own label wins over the sensor's internal 戶名 -------------
// Live instance, v0.2.2: a household configured as "17 號" rendered "no17",
// because the sensor's own `label` attribute was consulted first.
const named = documentShim.createElement("taipower-bill-card");
named.setConfig({ households: [{ entity: ENTITY, label: "17 號" }] });
named._hass = {
  states: { [ENTITY]: { state: "24.588", attributes: attrsFor({ label: "no17" }) } },
  callService: async () => {},
};
named._sigs = {};
named.hass = named._hass;
const namedText = named.shadowRoot.text();
ok(namedText.includes("17 號"), "the configured label is what the user sees");
ok(!namedText.includes("no17"), "the integration's internal 戶名 does not shadow it");

// 3. an early-period projection must not masquerade as a bill --------------
// Three days of October data projected onto 63 days printed ~$1,052 against
// real bills of $5,000-7,000, which read as a computation bug.
const early = withState(
  attrsFor({ days_elapsed: 3, days_remaining: 60, cycle_days: 63, projected_kwh: 516.3, projected_cost: 1052 }),
);
const earlyText = early.shadowRoot.text();
ok(earlyText.includes("推估資料不足"), "a 3-day projection says it is under-evidenced");
ok(earlyText.includes("僅 3 天"), "and says how few days it has");
ok(!earlyText.includes("推估總電費"), "instead of printing a bill-shaped number");

const mid = withState(attrsFor({ days_elapsed: 37, projected_kwh: 1295.2, projected_cost: 3692 }));
ok(mid.shadowRoot.text().includes("推估總電費"), "once there is a week of data, it projects");

console.log(`test_card_render: ${checks} checks passed`);
