"use strict";
/* Shadow Book agents page: sample bots on their own paper books.
   Uses helpers from app.js ($, api, money, signed, cls, esc, n, toast). */

const AG = { list: [], sel: null, arm: null, feedTop: null };
const KIND = {
  signal: ["Signal", "k-signal"], order: ["Order", "k-order"], fill: ["Fill", "k-fill"],
  exit: ["Exit", "k-exit"], win: ["Closed", "k-win"], loss: ["Closed", "k-loss"],
  error: ["Error", "k-error"], system: ["System", "k-system"], watch: ["Watching", "k-watch"],
  info: ["Note", "k-info"],
};
const AG_HUE = { "dip-buyer": 205, "momentum-calls": 280, "put-spreads": 150, "covered-calls": 32 };

const tOnly = (iso) => new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", second: "2-digit" });
const agTag = (id, name) => `<span class="ag-tag" style="--h:${AG_HUE[id] ?? 210}">${esc(name)}</span>`;
function kindChip(k) { const [label, c] = KIND[k] || KIND.info; return `<span class="kchip ${c}">${label}</span>`; }

// ------------------------------------------------------------------ cards
function agCard(a) {
  const on = a.enabled;
  const pct = a.starting_cash ? (a.total_pnl / a.starting_cash) * 100 : 0;
  return `<article class="card ag-card${AG.sel === a.id ? " sel" : ""}" data-id="${esc(a.id)}" style="--h:${AG_HUE[a.id] ?? 210}">
    <div class="ag-top">
      <div class="ag-ico">${esc(a.name.split(" ").map((w) => w[0]).join("").slice(0, 2))}</div>
      <div class="ag-name"><b>${esc(a.name)}</b><div class="muted small">${esc(a.tagline)}</div></div>
      <span class="spacer"></span>
      <span class="pill ${on ? "open" : ""}">${on ? "● Running" : "Paused"}</span>
    </div>
    <div class="ag-money mono">
      <div><div class="ag-eq">${money(a.equity)}</div>
        <div class="${cls(a.total_pnl)}">${signed(a.total_pnl)} <span class="small">(${pct >= 0 ? "+" : ""}${n(pct)}%)</span> <span class="muted small">all time</span></div></div>
      <div class="ag-day"><label>Today</label><b class="${cls(a.day_pnl)}">${signed(a.day_pnl)}</b></div>
    </div>
    <div class="ag-stats">
      <div><label>Positions</label><b>${a.open_positions}</b></div>
      <div><label>Open orders</label><b>${a.open_orders}</b></div>
      <div><label>Closed trades</label><b>${a.closed_trades}</b></div>
      <div><label>Win rate</label><b>${a.win_rate === null ? "–" : a.win_rate + "%"}</b></div>
    </div>
    <div class="ag-last">${a.last ? `${kindChip(a.last.kind)} <span>${esc(a.last.message)}</span>` : `<span class="muted">No activity yet. Press Start.</span>`}</div>
    <div class="ag-actions">
      <button class="btn sm ${on ? "" : "primary"}" data-act="${on ? "stop" : "start"}">${on ? "Pause" : "Start"}</button>
      <button class="btn sm ghost" data-act="open">${AG.sel === a.id ? "Hide details" : "Details"}</button>
    </div>
  </article>`;
}
function renderCards() { $("agGrid").innerHTML = AG.list.map(agCard).join(""); }

async function loadAgents() {
  let r; try { r = await api("/api/agents"); } catch (e) { return; }
  AG.list = r.agents; renderCards();
  loadFeed();
  if (AG.sel) loadAgentDetail();
}

// ------------------------------------------------------------------ feed
async function loadFeed() {
  let f; try { f = await api("/api/agents/feed?limit=80"); } catch (e) { return; }
  const top = f.length ? `${f[0].agent}:${f[0].id}` : null;
  const fresh = top !== AG.feedTop; AG.feedTop = top;
  $("agLive").classList.toggle("on", AG.list.some((a) => a.enabled));
  $("agFeed").innerHTML = f.length ? f.map((x, i) => `<li class="${fresh && i === 0 ? "new" : ""} ${x.kind === "watch" ? "dim" : ""}">
      <div class="f-meta">${agTag(x.agent, x.agent_name)} ${kindChip(x.kind)} <span class="spacer"></span><span class="muted mono small">${tOnly(x.ts)}</span></div>
      <div class="f-msg">${esc(x.message)}</div></li>`).join("")
    : `<li class="muted small">Nothing yet. Start an agent and its decisions show up here, in plain English.</li>`;
}

// ------------------------------------------------------------------ detail
function agPosRows(p) {
  const rows = [...p.options, ...p.equities];
  if (!rows.length) return `<tr><td colspan="4" class="empty">No open positions.</td></tr>`;
  return rows.map((r) => `<tr><td class="l"><div class="t">${esc(r.title)}${r.kind === "group" ? ' <span class="tag">SPREAD</span>' : ""}</div><div class="sub">${esc(r.subtitle || "")}</div></td>
    <td><div>${money(r.holdings)}</div><div class="sub">${esc(r.holdings_sub)}</div></td>
    <td><div>${money(r.cost)}</div><div class="sub">${esc(r.cost_sub)}</div></td>
    <td><div class="${cls(r.unrealized)}">${signed(r.unrealized)}</div><div class="sub ${cls(r.unrealized)}">${r.unrealized_pct === null ? "" : (r.unrealized_pct >= 0 ? "+" : "") + n(r.unrealized_pct) + "%"}</div></td></tr>`).join("");
}
function agOrderRows(orders) {
  if (!orders.length) return `<tr><td colspan="4" class="empty">No orders yet.</td></tr>`;
  const st = (s) => `<span class="st st-${s.toLowerCase()}">${s[0] + s.slice(1).toLowerCase()}</span>`;
  const px = (o) => o.order_class === "MULTILEG"
    ? (o.net_fill !== null ? `${money(Math.abs(o.net_fill))} ${o.net_fill >= 0 ? "debit" : "credit"}` : o.limit_price !== null ? `${money(o.limit_price)} ${o.side === "CREDIT" ? "credit" : "debit"} limit` : "Market")
    : (o.avg_fill_price !== null ? money(o.avg_fill_price) : o.order_type === "STOP" ? `Stop ${money(o.stop_price)}` : o.limit_price !== null ? `Limit ${money(o.limit_price)}` : "Market");
  return orders.slice(0, 12).map((o) => `<tr><td class="l">${orderLabel(o)}</td>
    <td>${o.order_class === "MULTILEG" ? "" : o.side === "BUY" ? "Buy" : "Sell"} ${o.quantity}</td><td>${px(o)}</td><td>${st(o.status)}</td></tr>`).join("");
}
async function loadAgentDetail() {
  const id = AG.sel; if (!id || AG.arm) return;         // don't redraw over an armed button
  let d; try { d = await api(`/api/agents/${encodeURIComponent(id)}`); } catch (e) { return; }
  if (AG.sel !== id) return;
  const box = $("agDetail");
  const logEl = box.querySelector(".ag-log");
  const keepScroll = logEl ? logEl.scrollTop : 0;
  box.hidden = false;
  box.style.setProperty("--h", AG_HUE[id] ?? 210);
  box.innerHTML = `
    <div class="agd-head"><div><h3>${esc(d.name)}</h3><div class="muted small">${d.enabled ? "Running" : "Paused"} · own paper book · cash ${money(d.cash)} · buying power ${money(d.buying_power)}</div></div>
      <span class="spacer"></span>
      <button class="btn sm" data-act="${d.enabled ? "stop" : "start"}">${d.enabled ? "Pause" : "Start"}</button>
      <button class="btn sm" data-act="flatten" data-arm="Confirm: close all">Close all</button>
      <button class="btn sm ghost" data-act="reset" data-arm="Confirm reset">Reset book</button></div>
    <div class="agd-grid">
      <div><h4>Rules</h4><ol class="ag-rules">${d.rules.map((r) => `<li>${esc(r)}</li>`).join("")}</ol></div>
      <div><h4>Decision log</h4><ul class="ag-log">${d.log.length ? d.log.map((x) => `<li class="${x.kind === "watch" ? "dim" : ""}"><div class="f-meta">${kindChip(x.kind)}<span class="spacer"></span><span class="muted mono small">${tOnly(x.ts)}</span></div><div class="f-msg">${esc(x.message)}</div></li>`).join("") : `<li class="muted small">Nothing logged yet.</li>`}</ul></div>
    </div>
    <h4 class="agd-h">Positions</h4><div class="table-wrap"><table class="pf mono agd-tbl"><thead><tr><th class="l">Name</th><th>Holdings</th><th>Cost</th><th>Unrealized</th></tr></thead><tbody>${agPosRows(d.portfolio)}</tbody></table></div>
    <h4 class="agd-h">Orders</h4><div class="table-wrap"><table class="pf mono agd-tbl"><thead><tr><th class="l">Order</th><th>Qty</th><th>Price</th><th>Status</th></tr></thead><tbody>${agOrderRows(d.orders)}</tbody></table></div>`;
  const nl = box.querySelector(".ag-log"); if (nl) nl.scrollTop = keepScroll;
}

// ------------------------------------------------------------------ actions
async function agAct(id, act, btn) {
  if (btn && btn.dataset.arm && AG.arm !== btn.dataset.act + id) {     // two-click confirm
    AG.arm = btn.dataset.act + id; btn.textContent = btn.dataset.arm; btn.classList.add("armed");
    setTimeout(() => { if (AG.arm === btn.dataset.act + id) { AG.arm = null; loadAgentDetail(); } }, 4000);
    return;
  }
  AG.arm = null;
  try {
    const r = await api(`/api/agents/${encodeURIComponent(id)}/${act}`, { method: "POST" });
    if (act === "flatten") toast(`Closed ${r.result.closed} position${r.result.closed === 1 ? "" : "s"}${r.result.errors.length ? " (some could not close)" : ""}`);
    if (act === "reset") toast("Book reset to $25,000");
  } catch (e) { toast(e.message); }
  loadAgents();
}
(function initAgents() {
  $("agGrid").addEventListener("click", (e) => {
    const card = e.target.closest(".ag-card"); if (!card) return;
    const id = card.dataset.id;
    const b = e.target.closest("button[data-act]");
    if (b && b.dataset.act !== "open") return agAct(id, b.dataset.act, b);
    AG.sel = AG.sel === id ? null : id;
    if (!AG.sel) $("agDetail").hidden = true;
    renderCards(); loadAgentDetail();
    if (AG.sel) setTimeout(() => $("agDetail").scrollIntoView({ behavior: "smooth", block: "start" }), 150);
  });
  $("agDetail").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]"); if (b && AG.sel) agAct(AG.sel, b.dataset.act, b);
  });
  $("agStartAll").addEventListener("click", async () => { await api("/api/agents/all/start", { method: "POST" }); loadAgents(); });
  $("agStopAll").addEventListener("click", async () => { await api("/api/agents/all/stop", { method: "POST" }); loadAgents(); });
  setInterval(() => { if (S.view === "agents") loadAgents(); }, 3000);
})();
