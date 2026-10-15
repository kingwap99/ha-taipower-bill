/**
 * taipower-bill-card  (v0.2.3)
 *
 * Plain-DOM Lovelace card (no lit, no build step, no external deps).
 * Reads the attributes published by the taipower_bill integration on any of its
 * sensors: every sensor of an entry carries the whole state block, so this card
 * only needs the one entity you configure.
 *
 * v0.2.0 model: the source of truth is the list of ACTUAL meter-read dates
 * (歷次實際抄見日). A period runs from (previous read date + 1 day) to the next
 * read date, and periods roll over by date, so pressing the button late (or
 * early) no longer shifts anything. Closing a period is explicit and
 * reversible: propose a close (待確認) → confirm it, undo the last one, or
 * rewrite/recompute any past one.
 *
 * v0.2.2 is about not being able to get it wrong. The 撤銷按鈕 used to sit at the
 * very bottom of 歷次帳期, behind the table, so a mis-tap looked permanent; it
 * now has its own 撤銷 row in a fixed spot, and both card actions that change
 * the ledger -- 確認寫入 and 撤銷 -- open a dialog that names the 抄見日 they are
 * about to touch. (The card is the only place a confirmation can happen: Home
 * Assistant has no confirmation step for a service call or a button entity.)
 *
 * v0.2.3 fixes why the card never appeared at all on a real instance. It used to
 * be loaded from raw.githubusercontent.com, which answers with
 * `content-type: text/plain` AND `x-content-type-options: nosniff`, so the browser
 * refuses to execute it as an ES module and Lovelace reports
 * `Custom element doesn't exist`. The card now ships inside this integration under
 * `www/` and Home Assistant serves it same-origin as `text/javascript`, so a HACS
 * upgrade is the only step. Two smaller fixes: a household's configured `label`
 * beats the sensor's internal 戶名 (it used to render `no17` over `17 號`), and a
 * projection made from under a week of data now says 推估資料不足 instead of
 * printing a bill-shaped number that reads as a bug.
 *
 * Example configuration:
 *
 * type: custom:taipower-bill-card
 * title: 台電電費
 * households:
 *   - entity: sensor.taipower_b_period_kwh
 *     label: B戶
 *   - entity: sensor.taipower_a_period_kwh
 *     label: A戶
 *
 * A single-household shorthand is also accepted:
 *
 * type: custom:taipower-bill-card
 * entity: sensor.taipower_b_period_kwh
 *
 * `label` is optional: the integration's own `label` attribute is used when the
 * config does not override it. Any attribute may be absent; the card degrades
 * gracefully.
 */

/* ------------------------------------------------------------------ helpers */

const num = (v) => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

const str = (v, fallback = "") =>
  v === null || v === undefined || v === "" ? fallback : String(v);

const arr = (v) => (Array.isArray(v) ? v : []);

const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

const fmt = (v, d = 0) => {
  const n = num(v);
  return n === null
    ? "—"
    : n.toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });
};

const money = (v) => {
  const n = num(v);
  return n === null ? "—" : `$${fmt(n, 0)}`;
};

const price = (v) => fmt(v, 3); // 每度單價

const signed = (v) => {
  const n = num(v);
  if (n === null) return "—";
  return `${n >= 0 ? "+" : ""}${fmt(n, 0)}`;
};

const SOURCE_LABELS = {
  statistics: "長期統計",
  "live-delta": "即時累計",
  "live-delta-reset": "即時累計（基準已重置）",
  manual: "手動",
  none: "來源無資料",
  inactive: "未啟用",
  "period-not-started": "本期未開始",
};

const sourceLabel = (s) => SOURCE_LABELS[str(s)] || str(s, "未知");

const seasonLabel = (s) => {
  const v = str(s);
  if (v === "summer") return "夏月";
  if (v === "offseason" || v === "off-peak-season" || v === "nonsummer") return "非夏月";
  return v || "—";
};

const untilText = (iso) => {
  const t = Date.parse(str(iso));
  if (!iso || Number.isNaN(t)) return null;
  const ms = t - Date.now();
  if (ms <= 0) return "已過期";
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return "不到 1 分鐘";
  if (mins < 60) return `${mins} 分鐘`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `${h} 小時 ${mins % 60} 分鐘`;
  return `${Math.floor(h / 24)} 天 ${h % 24} 小時`;
};

const issueText = (i) =>
  typeof i === "string" ? i : i && typeof i === "object" ? str(i.message || i.issue, JSON.stringify(i)) : String(i);

/* --------------------------------------------------------------------- card */

class TaipowerBillCard extends HTMLElement {
  setConfig(config) {
    const cfg = config || {};
    const households = this._normalise(cfg);
    if (!households.length) {
      throw new Error("taipower-bill-card: 需要 households（或 entity）設定");
    }
    this._config = cfg;
    this._households = households;
    this._sigs = {};
    this._nodes = this._nodes || {};
  }

  _normalise(cfg) {
    if (Array.isArray(cfg.households) && cfg.households.length) {
      return cfg.households
        .map((h) => ({ entity: str(h && (h.entity || h.entity_id)), label: str(h && h.label) }))
        .filter((h) => h.entity);
    }
    if (str(cfg.entity)) return [{ entity: str(cfg.entity), label: "" }];
    return [];
  }

  set hass(hass) {
    this._hass = hass;
    this._ensureDom();
    this._render();
  }

  _ensureDom() {
    if (this._root) return;
    // Everything must go *into the shadow root*.  Appending to ``this`` instead
    // puts the markup in the element's light DOM, and a shadow host with no
    // ``<slot>`` renders none of it -- the card then draws literally nothing
    // while every other symptom (entity found, no JS error, right height of 0)
    // looks fine.  jsdom's ``shadowRoot.innerHTML === ""`` is what caught it.
    const shadow = this.attachShadow({ mode: "open" });
    this._root = document.createElement("div");
    const card = document.createElement("ha-card");
    card.className = "tpc-card";
    this._root.innerHTML = `<style>${this._styles()}</style>`;
    this._body = document.createElement("div");
    this._body.className = "tpc-body";
    this._titleEl = document.createElement("div");
    this._titleEl.className = "card-title";
    this._body.appendChild(this._titleEl);
    card.appendChild(this._body);
    this._root.appendChild(card);
    shadow.appendChild(this._root);

    this._nodes = this._nodes || {};
    this._sigs = this._sigs || {};
    this._ui = this._ui || {};

    this._root.addEventListener("click", (e) => this._onClick(e));
    const onInput = (e) => this._onInput(e);
    this._root.addEventListener("input", onInput);
    this._root.addEventListener("change", onInput);
  }

  _uiFor(entity) {
    if (!this._ui[entity]) {
      this._ui[entity] = {
        histOpen: false,
        tiersOpen: false,
        readDate: "",
        actualKwh: "",
        actualBill: "",
        force: false,
        error: "",
      };
    }
    return this._ui[entity];
  }

  /* --------------------------------------------------------------- styling */

  _styles() {
    return `
      :host{display:block}
      .tpc-card{display:block;padding:0;overflow:hidden}
      .tpc-body{font-family:var(--paper-font-body1,-apple-system,"Noto Sans TC",sans-serif);padding:0}
      .head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 16px 2px}
      .head h1{font-size:15px;font-weight:600;margin:0;color:var(--primary-text-color)}
      .badge{font-size:11px;line-height:1.6;padding:1px 8px;border-radius:10px;
        background:var(--secondary-background-color,rgba(0,0,0,.06));color:var(--secondary-text-color);white-space:nowrap}
      .card-title{font-size:16px;font-weight:600;color:var(--primary-text-color);padding:12px 16px 0}
      .card-title:empty{display:none}
      .hh{padding:12px 16px;border-top:1px solid var(--divider-color,rgba(0,0,0,.08))}
      .window{font-size:12.5px;color:var(--secondary-text-color);padding:0 16px 2px}
      .window b{color:var(--primary-text-color);font-weight:600}
      .row{display:flex;justify-content:space-between;align-items:flex-end;gap:8px;margin-top:6px}
      .big{font-size:28px;font-weight:600;color:var(--primary-text-color);line-height:1.1}
      .big small{font-size:13px;font-weight:400;color:var(--secondary-text-color)}
      .money{font-size:22px;font-weight:600;color:var(--accent-color,var(--primary-color))}
      .cap{font-size:11px;color:var(--secondary-text-color);margin-top:2px}
      .bar{height:8px;border-radius:4px;background:var(--disabled-text-color,rgba(0,0,0,.12));margin:10px 0 6px;overflow:hidden}
      .bar>i{display:block;height:100%;border-radius:4px;background:var(--primary-color,#03a9f4)}
      .meta{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12.5px;color:var(--secondary-text-color)}
      .meta b{color:var(--primary-text-color);font-weight:600}
      .section{margin-top:12px;border-top:1px solid var(--divider-color,rgba(0,0,0,.08));padding-top:10px}
      .section-head{display:flex;align-items:center;justify-content:space-between;gap:8px;
        font-size:13px;font-weight:600;color:var(--primary-text-color)}
      .hint{font-size:11.5px;line-height:1.5;color:var(--secondary-text-color);margin-top:4px}
      .scroll{overflow-x:auto}
      table{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
      th{text-align:right;font-weight:500;color:var(--secondary-text-color);padding:4px 6px;
        border-bottom:1px solid var(--divider-color,rgba(0,0,0,.1));white-space:nowrap}
      td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--divider-color,rgba(0,0,0,.05));white-space:nowrap}
      th:first-child,td:first-child{text-align:left}
      .pos{color:var(--error-color,#d64000)}.neg{color:var(--success-color,#2e7d32)}
      .warn{font-size:12px;color:var(--warning-color,#b26a00);margin-top:6px;line-height:1.5}
      .src{font-size:11px;color:var(--disabled-text-color);margin-top:6px;line-height:1.5}
      .alert{font-size:12.5px;line-height:1.5;margin-top:8px;padding:8px 10px;border-radius:6px;
        background:var(--error-color,#b00020);color:#fff}
      .alert b{font-weight:600}
      .alert ul{margin:6px 0 0;padding-left:18px}
      .pending{margin-top:10px;padding:10px 12px;border-radius:8px;
        border:1px solid var(--warning-color,#ff9800);
        background:color-mix(in srgb,var(--warning-color,#ff9800) 14%,transparent)}
      .pending .ptitle{font-size:13px;font-weight:600;color:var(--warning-color,#b26a00)}
      .pending .prow{font-size:12.5px;color:var(--primary-text-color);margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 16px}
      .pending .prow b{font-weight:600}
      .form{margin-top:10px;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:flex-end}
      .form label{display:flex;flex-direction:column;gap:2px;font-size:11.5px;color:var(--secondary-text-color)}
      .form input[type=date],.form input[type=number]{font:inherit;font-size:13px;padding:4px 6px;
        border:1px solid var(--divider-color,rgba(0,0,0,.2));border-radius:6px;
        background:var(--card-background-color,transparent);color:var(--primary-text-color);min-width:110px}
      .form .check{flex-direction:row;align-items:center;gap:6px;padding-bottom:6px}
      button{font:inherit;font-size:12.5px;padding:5px 12px;border-radius:16px;cursor:pointer;
        border:1px solid var(--primary-color,#03a9f4);
        background:var(--primary-color,#03a9f4);color:var(--text-primary-color,#fff)}
      button.ghost{background:transparent;color:var(--primary-color,#03a9f4)}
      button.danger{border-color:var(--error-color,#b00020);background:transparent;color:var(--error-color,#b00020)}
      button.tiny{font-size:11.5px;padding:2px 8px;border-radius:10px}
      .btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
      /* 撤銷列: kept in one predictable place.  It used to live at the bottom of
         the 歷次帳期 table, which is exactly why a mis-tap felt unrecoverable. */
      .safety{margin-top:10px;padding:10px 12px;border-radius:8px;
        border:1px dashed var(--divider-color,rgba(0,0,0,.22));
        background:var(--secondary-background-color,rgba(0,0,0,.03))}
      .safety .srow{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--primary-text-color)}
      .safety .srow b{font-weight:600}
      .rd{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:12.5px;color:var(--primary-text-color);margin-top:6px}
      .rd b{font-weight:600}
      .empty{font-size:11.5px;color:var(--secondary-text-color);margin-top:6px}
      .err{font-size:12px;color:var(--error-color,#b00020);margin-top:8px;line-height:1.5}
    `;
  }

  /* ---------------------------------------------------------------- action */

  async _call(entity, service, data) {
    if (!this._hass) throw new Error("尚未連線到 Home Assistant");
    await this._hass.callService("taipower_bill", service, data || {}, { entity_id: entity });
  }

  _onInput(e) {
    const el = e.target && e.target.closest ? e.target.closest("[data-field]") : null;
    if (!el) return;
    const entity = el.getAttribute("data-entity");
    const field = el.getAttribute("data-field");
    if (!entity || !field) return;
    const ui = this._uiFor(entity);
    ui[field] = el.type === "checkbox" ? !!el.checked : el.value;
  }

  _onClick(e) {
    const btn = e.target && e.target.closest ? e.target.closest("[data-action]") : null;
    if (!btn) return;
    this._handle(btn.getAttribute("data-action"), btn.getAttribute("data-entity"), btn);
  }

  /* ---------------------------------------------------------- confirmation */

  /**
   * The exact question a ledger-writing tap has to answer before it runs.
   *
   * Kept apart from the click plumbing on purpose: this *is* the safety rule --
   * 「按下去之前，先講清楚會發生什麼」 -- and as a pure function of the entity's
   * own attributes it can be asserted without a browser, in
   * ``tests/test_card_render.mjs``.
   *
   * ``close_period`` deliberately returns ``null``: it only *proposes*, and
   * nothing reaches the ledger until 確認寫入.  Asking there would train the user
   * to dismiss the dialog that actually matters.
   */
  _confirmMessage(action, entity, btn) {
    const st = this._hass && this._hass.states[entity];
    const attrs = (st && st.attributes) || {};
    const dataDate = str(btn && btn.getAttribute ? btn.getAttribute("data-date") : "");

    if (action === "confirm") {
      const pc = attrs.pending_close && typeof attrs.pending_close === "object"
        ? attrs.pending_close
        : {};
      return `確定要把這筆結算寫入？\n\n` +
        `· 帳期 ${str(pc.period_start, "—")} → ${str(pc.read_date, "—")}\n` +
        `· 本期度數 ${fmt(pc.kwh, 1)} 度\n\n` +
        `寫入後本期就會從 ${str(pc.read_date, "—")} 的隔天重新起算。\n` +
        `（寫錯了也救得回來：按「撤銷最後一期」即可。）`;
    }

    if (action === "undo") {
      // Name the date.  「確定要撤銷最後一期？」 on its own is not a
      // confirmation, it is a dare -- you cannot tell what you are about to
      // lose.  ``last_read_date`` is the date the coordinator would remove.
      const date = dataDate || str(attrs.last_read_date) || "最後一期";
      const back = str(attrs.readings && attrs.readings.length > 1
        ? attrs.readings[attrs.readings.length - 2].date
        : "");
      return `確定要撤銷抄見日 ${date}？\n\n` +
        `· 這筆會從抄見日清單移除，該期不會進歷史\n` +
        `· 本期會從 ${back ? `${back} 的隔天` : "上一個抄見日"} 重新起算\n` +
        `· 要加回來：在日期欄重新寫入 ${date} 即可\n\n` +
        `（沒有寫入任何電費資料，只是把清單還原。）`;
    }

    if (action === "recompute") {
      return dataDate
        ? `確定要用目前的統計重新計算 ${dataDate} 這期？既有紀錄會被覆寫。`
        : "確定要用目前的統計重新計算最近一期？既有紀錄會被覆寫。";
    }

    return null;
  }

  async _handle(action, entity, btn) {
    if (!action || !entity) return;
    const ui = this._uiFor(entity);
    ui.error = "";
    try {
      if (action === "toggle-hist") {
        ui.histOpen = !ui.histOpen;
      } else if (action === "toggle-tiers") {
        ui.tiersOpen = !ui.tiersOpen;
      } else if (action === "confirm") {
        // This is the one card action that writes to the ledger, so it is the
        // one that asks first -- and it states what it is about to record.
        if (!window.confirm(this._confirmMessage("confirm", entity, btn))) return;
        await this._call(entity, "confirm_close", {});
        ui.readDate = "";
        ui.actualKwh = "";
        ui.actualBill = "";
        ui.force = false;
      } else if (action === "cancel") {
        await this._call(entity, "cancel_close", {});
      } else if (action === "close") {
        await this._close(entity, ui);
      } else if (action === "undo") {
        if (window.confirm(this._confirmMessage("undo", entity, btn))) {
          await this._call(entity, "undo_close_period", {});
        }
      } else if (action === "recompute") {
        if (window.confirm(this._confirmMessage("recompute", entity, btn))) {
          const date = str(btn.getAttribute("data-date"));
          const data = { recompute: true };
          if (date) data.date = date;
          await this._call(entity, "update_reading", data);
        }
      } else {
        return;
      }
    } catch (err) {
      ui.error = str((err && err.message) || err, "操作失敗");
    }
    this._sigs[entity] = null; // force this block to rebuild
    this._render();
  }

  async _close(entity, ui) {
    const st = this._hass && this._hass.states[entity];
    const attrs = (st && st.attributes) || {};
    const date = str(ui.readDate) || str(attrs.suggested_read_date) || str(attrs.next_read_date);
    if (!date) {
      ui.error = "請先選擇抄見日";
      return;
    }
    const data = { read_date: date, confirm: false };
    const kwh = num(ui.actualKwh);
    if (kwh !== null) data.actual_kwh = kwh;
    const bill = num(ui.actualBill);
    if (bill !== null) data.actual_bill = bill;
    if (ui.force) data.force = true;
    await this._call(entity, "close_period", data);
    ui.readDate = "";
    ui.actualKwh = "";
    ui.actualBill = "";
    ui.force = false;
  }

  /* ---------------------------------------------------------------- render */

  _sig(attrs, state) {
    try {
      return `${state}|${JSON.stringify(attrs)}`;
    } catch (err) {
      return `err|${Math.random()}`;
    }
  }

  _render() {
    if (!this._config || !this._body) return;
    const title = str(this._config.title, "台電電費");
    if (this._titleEl) {
      this._titleEl.textContent = title;
      this._titleEl.style.display = title ? "" : "none";
    }
    const seen = new Set();

    for (const hh of this._households) {
      seen.add(hh.entity);
      let node = this._nodes[hh.entity];
      if (!node) {
        node = document.createElement("div");
        node.className = "hh";
        this._body.appendChild(node);
        this._nodes[hh.entity] = node;
      }
      const st = this._hass && this._hass.states ? this._hass.states[hh.entity] : null;
      const attrs = (st && st.attributes) || {};
      const sig = this._sig(attrs, st ? st.state : "missing");
      if (this._sigs[hh.entity] === sig) continue;
      this._sigs[hh.entity] = sig;
      node.innerHTML = this._renderHousehold(hh, attrs, st);
    }

    for (const key of Object.keys(this._nodes)) {
      if (!seen.has(key)) {
        this._nodes[key].remove();
        delete this._nodes[key];
        delete this._sigs[key];
      }
    }
  }

  _renderHousehold(hh, a, st) {
    const entity = hh.entity;
    const ui = this._uiFor(entity);
    // The card's own label wins: the sensor attribute is the integration's
    // internal 戶名 (``no17``), and a household configured as ``17 號`` should
    // read ``17 號``.  Fall back to the attribute, then a generic word.
    const label = str(hh.label) || str(a.label) || "戶別";

    if (!st) {
      return `<div class="section-head"><span>${esc(label)}</span><span class="badge">找不到實體</span></div>
        <div class="warn">找不到 ${esc(entity)}，請確認這個感測器仍存在。</div>`;
    }

    const cycle = num(a.cycle_days) || 60;
    const elapsed = num(a.days_elapsed) || 0;
    const remaining = num(a.days_remaining);
    const pct = cycle ? Math.max(0, Math.min(100, (elapsed / cycle) * 100)) : 0;
    const src = str(a.usage_source, "unknown");
    const issues = arr(a.config_issues);
    const history = arr(a.history);
    const histCount = num(a.history_len);
    const totalHist = histCount === null ? history.length : histCount;
    const readings = arr(a.readings);
    const tiers = arr(a.tiers);
    const segments = arr(a.segments);
    const pending = a.pending_close && typeof a.pending_close === "object" ? a.pending_close : null;

    let html = "";

    /* 1. header */
    html += `<div class="head">
      <h1>${esc(label)}</h1>
      <span class="badge">${esc(sourceLabel(src))}</span>
    </div>
    <div class="window">本期 <b>${esc(str(a.period_start, "—"))}</b> → <b>${esc(
      str(a.next_read_date, "—")
    )}</b>　已 <b>${elapsed}</b>／${cycle} 天　距抄表 <b>${
      remaining === null ? "—" : remaining
    }</b> 天</div>
    <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>`;

    /* 8a. config warnings */
    if (str(a.config_warning) || issues.length) {
      html += `<div class="alert"><b>設定有問題，數值可能不可用</b>${
        str(a.config_warning) ? `<div>${esc(a.config_warning)}</div>` : ""
      }${
        issues.length
          ? `<ul>${issues.map((i) => `<li>${esc(issueText(i))}</li>`).join("")}</ul>`
          : ""
      }</div>`;
    }

    /* 2. current-period summary */
    html += `<div class="row">
      <div>
        <span class="big">${fmt(a.kwh, 1)}<small> kWh</small></span>
        <div class="cap">本期用電量</div>
      </div>
      <div style="text-align:right">
        <div class="money">${money(a.cost)}</div>
        <div class="cap">本期電費（推定）</div>
      </div>
    </div>
    <div class="meta" style="margin-top:10px">
      <span>平均每日 <b>${fmt(a.avg_daily_kwh, 1)}</b> 度</span>
      <span>每度均價 <b>${price(a.avg_price_per_kwh)}</b> 元</span>
      <span>下一度 <b>${price(a.marginal_price_per_kwh)}</b> 元</span>
      ${
        /* Projecting a 63-day bill off the first few days is misleading: an
           early-period sample (3 days in October) projected ~$1,052 against real
           bills of $5,000-7,000.  Below a week of data we say so instead of
           printing a number that looks like a bug. */
        elapsed < 7
          ? `<span class="badge">推估資料不足（僅 ${elapsed} 天）</span>`
          : `<span>推估總度數 <b>${fmt(a.projected_kwh, 1)}</b> 度</span>
      <span>推估總電費 <b>${money(a.projected_cost)}</b></span>`
      }
      ${
        num(a.actual_bill) !== null
          ? `<span>帳單金額 <b>${money(a.actual_bill)}</b></span>`
          : ""
      }
      ${
        num(a.bill_variance) !== null
          ? `<span>差額 <b class="${num(a.bill_variance) >= 0 ? "pos" : "neg"}">${signed(
              a.bill_variance
            )}</b> 元</span>`
          : ""
      }
    </div>`;

    /* 3. pending panel OR close form */
    if (pending) {
      const left = untilText(pending.expires_at || a.pending_expires_at);
      const pb = pending.bill && typeof pending.bill === "object" ? pending.bill : {};
      html += `<div class="pending">
        <div class="ptitle">待確認：這筆結算還沒寫入</div>
        <div class="prow">
          <span>抄見日 <b>${esc(str(pending.read_date, "—"))}</b></span>
          <span>本期度數 <b>${fmt(pending.kwh, 1)}</b> 度</span>
          <span>推定電費 <b>${money(pb.estimated_total)}</b></span>
          ${pending.forced ? `<span><b>強制結算</b></span>` : ""}
          ${
            left
              ? `<span>有效期限 <b>${
                  left === "已過期" ? "已過期" : `剩 ${left}`
                }</b></span>`
              : ""
          }
        </div>
        <div class="btns">
          <button data-action="confirm" data-entity="${esc(entity)}">確認寫入</button>
          <button class="ghost" data-action="cancel" data-entity="${esc(entity)}">取消</button>
        </div>
      </div>`;
    } else {
      const readDate = ui.readDate || str(a.suggested_read_date) || str(a.next_read_date);
      html += `<div class="form">
        <label>抄見日（可改）
          <input type="date" data-field="readDate" data-entity="${esc(entity)}" value="${esc(
        readDate
      )}">
        </label>
        <label>實際度數（選填）
          <input type="number" step="0.1" min="0" placeholder="留空用目前度數" data-field="actualKwh" data-entity="${esc(
            entity
          )}" value="${esc(ui.actualKwh)}">
        </label>
        <label>電費單金額（選填）
          <input type="number" step="1" min="0" placeholder="TWD" data-field="actualBill" data-entity="${esc(
            entity
          )}" value="${esc(ui.actualBill)}">
        </label>
        <label class="check"><input type="checkbox" data-field="force" data-entity="${esc(
          entity
        )}" ${ui.force ? "checked" : ""}>強制結算</label>
      </div>
      <div class="btns">
        <button data-action="close" data-entity="${esc(entity)}">結算本期（先確認）</button>
      </div>
      <div class="hint">按鈕只是快捷：真正的依據是下面的抄見日清單。日期可改，晚幾天才按也不會跑掉；提前按會用你選的日期當抄見日，未滿一期時才需要勾「強制結算」。</div>`;
    }

    /* 3b. 撤銷最後一期 -- one predictable place for the rescue.
       A button (entity or card) cannot ask "are you sure?" on its own, so this
       one always opens the dialog above, which names the date it will remove.
       Shown only when there is something to take back, so it never looks like a
       no-op: ``last_read_date`` is the newest read date that has already
       happened, which is precisely what ``undo_close_period`` removes. */
    const lastRead = str(a.last_read_date);
    if (!pending && lastRead) {
      html += `<div class="safety">
        <div class="srow">
          <span>上一個抄見日 <b>${esc(lastRead)}</b></span>
          <span>本期從 <b>${esc(str(a.period_start, "—"))}</b> 起算</span>
        </div>
        <div class="btns">
          <button class="danger" data-action="undo" data-entity="${esc(
            entity
          )}" data-date="${esc(lastRead)}">撤銷最後一期（${esc(lastRead)}）</button>
        </div>
        <div class="hint">按下去會先跳確認對話框，確認後才移除 ${esc(
          lastRead
        )} 這筆抄見日；該期不會進歷史，本期會回到上一個抄見日的隔天。要加回來就重新寫入同一個日期。</div>
      </div>`;
    }

    if (ui.error) html += `<div class="err">${esc(ui.error)}</div>`;

    /* 4 + 6. history */
    if (history.length) {
      const rows = history.slice().reverse();
      const shown = ui.histOpen ? rows : rows.slice(0, 3);
      const body = shown
        .map((h) => {
          const v = num(h.variance);
          const cls = v === null ? "" : v >= 0 ? "pos" : "neg";
          return `<tr>
            <td>${esc(str(h.period_start, "—"))}</td>
            <td>${esc(str(h.period_end, "—"))}</td>
            <td>${fmt(h.days, 0)}</td>
            <td>${fmt(h.kwh, 1)}</td>
            <td>${money(h.estimated_total)}</td>
            <td>${money(h.actual_total)}</td>
            <td class="${cls}">${v === null ? "—" : signed(v)}</td>
            <td>${price(h.avg_price_per_kwh)}</td>
            <td>${
              h.period_end
                ? `<button class="tiny ghost" data-action="recompute" data-entity="${esc(
                    entity
                  )}" data-date="${esc(h.period_end)}">重算</button>`
                : ""
            }</td>
          </tr>`;
        })
        .join("");

      html += `<div class="section">
        <div class="section-head">
          <span>歷次帳期（${totalHist}）</span>
          ${
            rows.length > 3
              ? `<button class="tiny ghost" data-action="toggle-hist" data-entity="${esc(entity)}">${
                  ui.histOpen ? "收合" : `展開全部 ${rows.length} 期`
                }</button>`
              : ""
          }
        </div>
        <div class="hint">回看前幾個月：每一列是一期已寫入的帳期，最新在最上面。「重算」可用目前的統計覆寫該期。</div>
        <div class="scroll"><table>
          <thead><tr>
            <th>起算日</th><th>抄見日</th><th>天數</th><th>度數</th>
            <th>推定電費</th><th>實際電費</th><th>差額</th><th>每度均價</th><th></th>
          </tr></thead>
          <tbody>${body}</tbody>
        </table></div>
      </div>`;
    } else {
      html += `<div class="section">
        <div class="section-head"><span>歷次帳期（0）</span></div>
        <div class="empty">尚無歷史期。寫入第一次結算後，這裡會列出每一期的度數、推定與實際電費。</div>
      </div>`;
    }

    /* 5. readings */
    html += `<div class="section">
      <div class="section-head"><span>抄見日清單（${readings.length}）</span></div>
      <div class="hint">這份清單是唯一依據（歷次實際抄見日）：一期從「上一個抄見日 + 1 天」到這一天。上面的結算／撤銷／重算只是新增或改寫這裡某一筆的快捷。</div>
      ${
        readings.length
          ? readings
              .slice()
              .reverse()
              .map(
                (r) => `<div class="rd"><span><b>${esc(str(r.date, "—"))}</b>${
                  num(r.kwh) !== null ? `　實際 ${fmt(r.kwh, 1)} 度` : ""
                }${num(r.bill) !== null ? `　金額 ${money(r.bill)}` : ""}</span></div>`
              )
              .join("")
          : `<div class="empty">尚無抄見日紀錄。用上面的表單寫入第一筆，或讓整合自動推進。</div>`
      }
    </div>`;

    /* 7. tiers / segments */
    if (tiers.length || segments.length) {
      let detail = "";
      if (tiers.length) {
        detail += `<div class="scroll"><table>
          <thead><tr><th>級距</th><th>費率</th><th>度數</th><th>金額</th></tr></thead>
          <tbody>${tiers
            .map((t, i) => {
              // Accept both key spellings: the integration emits a cumulative
              // ladder (``up_to_kwh``/``price``), older payloads used
              // ``from_kwh``/``to_kwh``/``rate``.
              const raw = (o, a, b) => (o[a] !== undefined ? o[a] : o[b]);
              const to = num(raw(t, "to_kwh", "up_to_kwh"));
              const prev = i > 0 ? tiers[i - 1] : null;
              const explicitFrom = num(t.from_kwh);
              const prevTo = prev ? num(raw(prev, "to_kwh", "up_to_kwh")) : null;
              const from = explicitFrom !== null ? explicitFrom : prevTo === null ? 0 : prevTo;
              const range =
                to === null ? `${fmt(from, 0)} 以上` : `${fmt(from, 0)} – ${fmt(to, 0)}`;
              return `<tr><td>${range}</td><td>${price(raw(t, "rate", "price"))}</td><td>${fmt(
                t.kwh,
                1
              )}</td><td>${money(t.cost)}</td></tr>`;
            })
            .join("")}</tbody>
        </table></div>`;
      }
      if (segments.length) {
        // A cross-season period is billed by computing each season's FULL
        // progressive bill on the total kWh and weighting it by that season's
        // day share, so the meaningful column is the weighted amount (the
        // per-season kWh is not what the 台電 bill prints).
        detail += `<div class="scroll"><table>
          <thead><tr><th>季節</th><th>天數</th><th>占比</th><th>加權金額</th></tr></thead>
          <tbody>${segments
            .map((s) => {
              const share = num(s.day_share);
              const cost =
                num(s.cost) !== null
                  ? num(s.cost)
                  : num(s.weighted_cost) !== null
                  ? num(s.weighted_cost)
                  : num(s.full_season_cost);
              return `<tr><td>${esc(seasonLabel(s.season))}</td><td>${fmt(
                s.days,
                0
              )}</td><td>${
                share === null ? "—" : `${fmt(share * 100, 1)}%`
              }</td><td>${money(cost)}</td></tr>`;
            })
            .join("")}</tbody>
        </table></div>`;
      }
      html += `<div class="section">
        <div class="section-head">
          <span>明細</span>
          <button class="tiny ghost" data-action="toggle-tiers" data-entity="${esc(entity)}">${
        ui.tiersOpen ? "收合" : "展開"
      }</button>
        </div>
        ${ui.tiersOpen ? detail : ""}
      </div>`;
    }

    /* 8b. data-source warnings */
    if (src === "none") {
      html += `<div class="warn">來源無資料：Emporia 離線或尚未產生統計，度數會偏低；可勾「強制結算」並自行填入實際度數。</div>`;
    }
    if (src === "period-not-started") {
      html += `<div class="warn">本期視窗還沒開始（上期抄見日在未來），請先在抄見日清單修正日期。</div>`;
    }
    if (src === "inactive") {
      html += `<div class="warn">這個戶別尚未啟用或設定不完整。</div>`;
    }
    if (str(a.stats_error)) {
      html += `<div class="warn">長期統計讀取失敗（${esc(a.stats_error)}），本期度數可能偏低。</div>`;
    }
    if (str(a.stats_write_error)) {
      html += `<div class="warn">長期統計寫入失敗（${esc(a.stats_write_error)}），歷史曲線不會累積。</div>`;
    }
    const cov = str(a.stats_coverage_start);
    const pStart = str(a.period_start);
    if (cov && pStart && cov > pStart) {
      html += `<div class="warn">長期統計只涵蓋 ${esc(cov)} 之後，本期從 ${esc(
        pStart
      )} 起的前段度數未計入，結算時可自行填實際度數。</div>`;
    } else if (cov) {
      html += `<div class="src">統計回溯自 ${esc(cov)}，早於此日的度數未計入。</div>`;
    }
    if (str(a.stats_last_day)) {
      html += `<div class="src">統計最後一日 ${esc(a.stats_last_day)}。</div>`;
    }

    return html;
  }

  getCardSize() {
    return 6 + (this._households || []).length * 6;
  }
}

customElements.define("taipower-bill-card", TaipowerBillCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "taipower-bill-card",
  name: "Taipower Bill Card",
  description: "台電雙月電費：以歷次實際抄見日為依據，支援待確認結算、歷史回看與重算",
});
