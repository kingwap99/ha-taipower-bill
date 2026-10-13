/**
 * taipower-bill-card
 *
 * Plain-DOM Lovelace card (no lit, no build step, no external deps).
 * Reads the attributes published by the taipower_bill integration.
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
 * Each household block derives its sibling entities by suffix, so you only
 * need to point at the period_kwh sensor.
 */

const SUFFIXES = {
  kwh: "period_kwh",
  cost: "period_cost",
  avgDaily: "avg_daily_kwh",
  projKwh: "projected_period_kwh",
  projCost: "projected_period_cost",
  avgPrice: "avg_price_per_kwh",
  marginal: "marginal_price_per_kwh",
  daysLeft: "days_remaining",
  nextRead: "next_read_date",
  variance: "bill_variance",
  closed: "closed_periods",
};

function baseOf(entityId) {
  for (const suffix of Object.values(SUFFIXES)) {
    if (entityId.endsWith(`_${suffix}`)) {
      return entityId.slice(0, -(suffix.length + 1));
    }
  }
  return entityId;
}

const fmt = (n, d = 0) =>
  n === null || n === undefined || Number.isNaN(Number(n))
    ? "—"
    : Number(n).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });

const money = (n) => (n === null || n === undefined ? "—" : `$${fmt(n, 0)}`);

class TaipowerBillCard extends HTMLElement {
  setConfig(config) {
    if (!config.households || !config.households.length) {
      throw new Error("taipower-bill-card: 需要 households 設定");
    }
    this._config = config;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) {
      this.attachShadow({ mode: "open" });
      this._root = document.createElement("div");
      this._root.className = "wrap";
      this._root.innerHTML = `<style>${this._styles()}</style>`;
      this._body = document.createElement("div");
      this._root.appendChild(this._body);
      this.appendChild(this._root);
    }
    this._render(hass);
  }

  _styles() {
    return `
      .wrap{font-family:var(--paper-font-body1,-apple-system,sans-serif);padding:0}
      .head{display:flex;align-items:baseline;justify-content:space-between;padding:12px 16px 4px}
      .head h1{font-size:15px;font-weight:600;margin:0;color:var(--primary-text-color)}
      .hh{padding:12px 16px;border-top:1px solid var(--divider-color,rgba(0,0,0,.08))}
      .row{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
      .big{font-size:28px;font-weight:600;color:var(--primary-text-color);line-height:1.1}
      .big small{font-size:13px;font-weight:400;color:var(--secondary-text-color)}
      .money{font-size:22px;font-weight:600;color:var(--accent-color,var(--primary-color))}
      .bar{height:8px;border-radius:4px;background:var(--disabled-text-color,rgba(0,0,0,.12));margin:10px 0 6px;overflow:hidden}
      .bar>i{display:block;height:100%;border-radius:4px;background:var(--primary-color,#03a9f4)}
      .meta{display:flex;flex-wrap:wrap;gap:10px 18px;font-size:12.5px;color:var(--secondary-text-color)}
      .meta b{color:var(--primary-text-color);font-weight:600}
      .tiers{margin-top:10px;font-size:12px}
      .tier{display:grid;grid-template-columns:70px 1fr 62px 62px;gap:8px;padding:3px 0;border-bottom:1px dashed var(--divider-color,rgba(0,0,0,.06))}
      .tier span:last-child{text-align:right}
      .hist{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
      .hist th{text-align:right;font-weight:500;color:var(--secondary-text-color);padding:4px 6px;border-bottom:1px solid var(--divider-color,rgba(0,0,0,.1))}
      .hist td{text-align:right;padding:4px 6px;border-bottom:1px solid var(--divider-color,rgba(0,0,0,.05))}
      .hist th:first-child,.hist td:first-child{text-align:left}
      .pos{color:#d64000}.neg{color:#2e7d32}
      .warn{font-size:12px;color:#b26a00;margin-top:6px}
      .src{font-size:11px;color:var(--disabled-text-color)}
      .alert{font-size:12.5px;line-height:1.5;margin-top:8px;padding:8px 10px;border-radius:6px;
        background:var(--error-color,#b00020);color:#fff}
      .alert b{font-weight:600}
    `;
  }

  _render(hass) {
    const title = this._config.title || "台電電費";
    let html = `<div class="head"><h1>${title}</h1><span class="src">${new Date().toLocaleTimeString("zh-TW")}</span></div>`;

    for (const hh of this._config.households) {
      const base = baseOf(hh.entity);
      const get = (key) => hass.states[`${base}_${SUFFIXES[key]}`];
      const kwhState = get("kwh");
      const attrs = (kwhState && kwhState.attributes) || {};

      const kwh = kwhState && kwhState.state !== "unavailable" ? Number(kwhState.state) : null;
      const cost = get("cost") && get("cost").state !== "unavailable" ? Number(get("cost").state) : null;
      const daysLeft = get("daysLeft") ? Number(get("daysLeft").state) : null;
      const cycle = attrs.cycle_days || 60;
      const elapsed = attrs.days_elapsed || 0;
      const pct = cycle ? Math.min(100, (elapsed / cycle) * 100) : 0;
      const projCost = get("projCost") ? Number(get("projCost").state) : null;
      const avgPrice = get("avgPrice") ? Number(get("avgPrice").state) : null;
      const marginal = get("marginal") ? Number(get("marginal").state) : null;
      const variance = get("variance") && get("variance").state !== "unavailable" ? Number(get("variance").state) : null;
      const nextRead = get("nextRead") ? get("nextRead").state : "—";

      const tiers = attrs.tiers || [];
      const segs = attrs.segments || [];
      const closedState = get("closed");
      const history = (closedState && closedState.attributes && closedState.attributes.history) || [];

      const crossSeason = segs.length > 1;
      const src = attrs.usage_source || "unknown";
      const cfgWarn = attrs.config_warning;

      html += `<div class="hh">
        ${cfgWarn ? `<div class="alert"><b>設定有問題，數值不可用</b><br>${cfgWarn}</div>` : ""}
        <div class="row">
          <div><span class="big">${fmt(kwh, 0)}<small> kWh</small></span></div>
          <div class="money">${money(cost)}</div>
        </div>
        <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>
        <div class="meta">
          <span>已 <b>${elapsed}</b>／${cycle} 天</span>
          <span>距抄表 <b>${daysLeft === null ? "—" : daysLeft}</b> 天</span>
          <span>下次 <b>${nextRead}</b></span>
          <span>日均 <b>${fmt(attrs.avg_daily_kwh ?? (get("avgDaily") ? Number(get("avgDaily").state) : null), 1)}</b></span>
          <span>推估 <b>${fmt(get("projKwh") ? Number(get("projKwh").state) : null, 0)}</b> kWh / <b>${money(projCost)}</b></span>
          <span>平均 <b>${avgPrice === null ? "—" : fmt(avgPrice, 3)}</b> 元/度</span>
          <span>下一度 <b>${marginal === null ? "—" : fmt(marginal, 2)}</b> 元</span>
          ${variance !== null ? `<span>對帳 <b class="${variance >= 0 ? "pos" : "neg"}">${variance >= 0 ? "+" : ""}${fmt(variance, 0)}</b></span>` : ""}
        </div>
        ${crossSeason ? `<div class="warn">跨季分攤：${segs.map((s) => `${s.season === "summer" ? "夏月" : "非夏月"} ${s.days}天×${(s.day_share * 100).toFixed(0)}%`).join(" + ")}</div>` : ""}
        ${tiers.length ? `<div class="tiers">${tiers.map((t) => `<div class="tier"><span>第${t.tier}段</span><span>${t.up_to_kwh ? "≤" + t.up_to_kwh + "度" : "超過"} </span><span>${fmt(t.kwh, 0)}度</span><span>${money(t.cost)}</span></div>`).join("")}</div>` : ""}
        ${history.length ? `<table class="hist"><thead><tr><th>期起</th><th>度數</th><th>推定</th><th>實際</th><th>元/度</th></tr></thead><tbody>${history
          .slice(-8)
          .reverse()
          .map(
            (h) =>
              `<tr><td>${h.period_start}</td><td>${fmt(h.kwh, 0)}</td><td>${money(h.estimated_total)}</td><td>${money(h.actual_total)}</td><td>${h.avg_price_per_kwh ? fmt(h.avg_price_per_kwh, 2) : "—"}</td></tr>`
          )
          .join("")}</tbody></table>` : '<div class="src">尚無歷史期，收到第一張電費單後按「結算本期」開始累積</div>'}
        ${src === "none" ? '<div class="warn">來源無資料：Emporia 離線或尚未產生統計，可改用手動模式填度數</div>' : ""}
        ${src === "period-not-started" ? '<div class="warn">本期視窗還沒開始（上期抄見日在未來），請先修正抄見日</div>' : ""}
        ${attrs.stats_coverage_start ? `<div class="src">統計只回溯到 ${attrs.stats_coverage_start}，早於此日的度數未計入</div>` : ""}
      </div>`;
    }

    this._body.innerHTML = html;
  }

  getCardSize() {
    return 4 + (this._config.households || []).length * 3;
  }
}

customElements.define("taipower-bill-card", TaipowerBillCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "taipower-bill-card",
  name: "Taipower Bill Card",
  description: "台電雙月電費當期進度與歷史回看",
});
