"use strict";
/* paper·public — broker-style front end for the local paper account API */

const $ = (id) => document.getElementById(id);
const S = {
  sym: null, name: "", period: "DAY", exp: null,
  ticket: { symbol: null, label: "", isOption: false },
  side: "BUY", type: "MARKET", tq: null, symbols: [], positions: [], spot: null,
};

// ---------------------------------------------------------------- helpers
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `HTTP ${r.status}`);
  return body;
}
const n = (v, d = 2) => (v === null || v === undefined || isNaN(v) ? "–" : Number(v).toFixed(d));
const money = (v) => (v === null || v === undefined ? "–" :
  (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
const signed = (v) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : v < 0 ? "-" : "") + money(Math.abs(v)).replace("-", ""));
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const intc = (v) => (v === null || v === undefined ? "–" : Number(v).toLocaleString("en-US"));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const OSI = /^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;
function describe(sym) {
  const m = OSI.exec(sym || "");
  if (!m) return sym;
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${m[1]} ${months[+m[3] - 1]} ${+m[4]} '${m[2]} $${+m[6] / 1000} ${m[5] === "C" ? "Call" : "Put"}`;
}
const isOpt = (sym) => OSI.test(sym || "");
function orderLabel(o) {
  if (o.order_class === "MULTILEG") {
    const legs = (o.legs || []).map((l) => `${l.side === "BUY" ? "+" : "−"}${l.ratio !== 1 ? l.ratio + " " : ""}${esc(describe(l.symbol).replace(/^\S+ /, "") || l.symbol)}`).join(", ");
    return `<b>${esc(o.strategy || "Multi-leg")}</b> ${esc(o.symbol)}<div class="muted small">${legs}</div>`;
  }
  const tag = o.order_class === "BRACKET" ? ' <span class="tag">BRACKET</span>'
    : o.order_class === "BRACKET_EXIT" ? ` <span class="tag">${o.order_type === "STOP" ? "SL" : "TP"}</span>` : "";
  return esc(describe(o.symbol)) + tag;
}
function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 3000);
}
// Shift a UTC instant so the chart (which renders in UTC) shows New York wall-clock time.
const etFmt = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour12: false,
  year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
function etSeconds(iso) {
  const p = Object.fromEntries(etFmt.formatToParts(new Date(iso)).map((x) => [x.type, x.value]));
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
}

// ---------------------------------------------------------------- search
async function initSearch() {
  S.symbols = await fetch("/static/symbols.json").then((r) => r.json()).catch(() => []);
  const input = $("search"), list = $("searchResults");
  let items = [], active = 0;
  const render = () => {
    list.innerHTML = items.map((it, i) =>
      `<li data-s="${esc(it.symbol)}" class="${i === active ? "active" : ""}"><b>${esc(it.symbol)}</b><span>${esc(it.name)}</span></li>`).join("");
    list.hidden = items.length === 0;
  };
  input.addEventListener("input", () => {
    const q = input.value.trim().toUpperCase();
    if (!q) { items = []; render(); return; }
    const starts = S.symbols.filter((x) => x.symbol.startsWith(q));
    const named = S.symbols.filter((x) => !x.symbol.startsWith(q) && x.name.toUpperCase().includes(q));
    items = [...starts, ...named].slice(0, 10);
    if (!items.some((x) => x.symbol === q) && /^[A-Z.]{1,6}$/.test(q)) items.push({ symbol: q, name: "Look up this symbol" });
    active = 0; render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter" && items[active]) { pick(items[active].symbol); }
    else if (e.key === "Escape") { list.hidden = true; }
  });
  list.addEventListener("mousedown", (e) => { const li = e.target.closest("li"); if (li) pick(li.dataset.s); });
  input.addEventListener("blur", () => setTimeout(() => (list.hidden = true), 150));
  function pick(sym) { input.value = ""; list.hidden = true; input.blur(); goTrade(sym); }
}

// ---------------------------------------------------------------- symbol + quote
async function loadSymbol(sym) {
  sym = sym.toUpperCase();
  S.sym = sym;
  S.name = (S.symbols.find((x) => x.symbol === sym) || {}).name || "";
  try { localStorage.setItem("pp_sym", sym); } catch (e) { /* storage unavailable */ }
  $("qSym").textContent = sym; $("qName").textContent = S.name;
  $("qLast").textContent = "–"; $("qChg").textContent = "";
  setInstrument(sym);
  if ($("builder")) { $("builder").hidden = true; $("stratGrid").hidden = false; }
  refreshQuote(); if (S.chartSrc === "tv") renderTV(); else loadChart(); loadExpirations();
}

async function refreshQuote() {
  if (!S.sym) return;
  const syms = [S.sym]; if (S.ticket.symbol && S.ticket.symbol !== S.sym) syms.push(S.ticket.symbol);
  let q;
  try { q = await api(`/api/quote?symbols=${encodeURIComponent(syms.join(","))}`); } catch (e) { return; }
  const u = q[S.sym];
  if (u) {
    const last = u.last ?? u.mid;
    S.spot = last;
    $("qLast").textContent = n(last);
    $("qBid").textContent = n(u.bid); $("qAsk").textContent = n(u.ask); $("qSpr").textContent = n(u.ask - u.bid);
    if (u.previous_close) {
      const ch = last - u.previous_close, pct = (ch / u.previous_close) * 100;
      $("qChg").innerHTML = `<span class="${cls(ch)}">${ch >= 0 ? "+" : ""}${n(ch)} (${ch >= 0 ? "+" : ""}${n(pct)}%)</span> <span class="muted">today</span>`;
    }
  } else {
    $("qLast").textContent = "no quote";
  }
  S.tq = q[S.ticket.symbol] || null;
  renderTicketQuote();
}

// ---------------------------------------------------------------- chart
let chart, series;
function initChart() {
  const el = $("chart");
  chart = LightweightCharts.createChart(el, {
    layout: { background: { color: "transparent" }, textColor: "#a7b3c2", fontFamily: "Inter, sans-serif" },
    grid: { vertLines: { color: "rgba(36,48,64,0.35)" }, horzLines: { color: "rgba(36,48,64,0.35)" } },
    rightPriceScale: { borderColor: "#243040" },
    timeScale: { borderColor: "#243040", timeVisible: true, secondsVisible: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    localization: { locale: "en-US" },
    autoSize: true,
  });
  series = chart.addCandlestickSeries({
    upColor: "#22c55e", downColor: "#f0525b", borderVisible: false,
    wickUpColor: "#22c55e", wickDownColor: "#f0525b",
  });
  $("ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#ranges button").forEach((x) => x.classList.toggle("on", x === b));
    S.period = b.dataset.p; loadChart();
  });
}
async function loadChart() {
  if (!S.sym || !series) return;
  let bars;
  try { bars = await api(`/api/bars?symbol=${encodeURIComponent(S.sym)}&period=${S.period}`); } catch (e) { return; }
  if (!bars.length) return;           // slow/failed fetch: keep what's on screen
  const seen = new Set();
  const data = bars.map((b) => ({ time: etSeconds(b.t), open: b.o, high: b.h, low: b.l, close: b.c }))
    .filter((b) => (seen.has(b.time) ? false : seen.add(b.time))).sort((a, b) => a.time - b.time);
  series.setData(data);
  chart.timeScale().fitContent();
}

// ---------------------------------------------------------------- TradingView chart
S.chartSrc = "tv";
try { S.chartSrc = localStorage.getItem("pp_chart") || "tv"; } catch (e) { /* storage unavailable */ }
function tvAvailable() { return typeof window.TradingView !== "undefined" && window.TradingView.widget; }
function renderTV() {
  if (!S.sym || S.chartSrc !== "tv") return;
  if (!tvAvailable()) { window.__tvReady = renderTV; return; }   // script still loading
  const el = $("tvchart"); el.innerHTML = "";
  const inner = document.createElement("div"); inner.id = "tvchart_inner"; inner.style.height = "100%";
  el.appendChild(inner);
  new window.TradingView.widget({
    container_id: "tvchart_inner", autosize: true, symbol: S.sym, interval: "5",
    timezone: "America/New_York", theme: "dark", style: "1", locale: "en",
    backgroundColor: "#121821", gridColor: "rgba(36,48,64,0.35)",
    withdateranges: true, hide_side_toolbar: false, allow_symbol_change: false,
    details: false, save_image: true,
  });
}
function applyChartSource() {
  const tv = S.chartSrc === "tv";
  document.querySelectorAll("#chartSrc button").forEach((b) => b.classList.toggle("on", b.dataset.v === S.chartSrc));
  $("tvchart").hidden = !tv; $("chart").hidden = tv; $("ranges").hidden = tv;
  if (tv) renderTV(); else loadChart();
}
function initChartSource() {
  $("chartSrc").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    S.chartSrc = b.dataset.v;
    try { localStorage.setItem("pp_chart", S.chartSrc); } catch (err) { /* storage unavailable */ }
    applyChartSource();
  });
  // If TradingView can't load (offline / blocked), fall back to the basic chart.
  setTimeout(() => { if (S.chartSrc === "tv" && !tvAvailable()) { S.chartSrc = "basic"; applyChartSource(); if (S.view === "trade") toast("TradingView chart unavailable, showing basic chart"); } }, 8000);
}

// ---------------------------------------------------------------- options chain
async function loadExpirations() {
  const sel = $("expSel");
  sel.innerHTML = "<option>loading…</option>";
  let exps = [];
  try { exps = await api(`/api/expirations?symbol=${encodeURIComponent(S.sym)}`); } catch (e) { /* none */ }
  if (!exps.length) {
    S.exp = null;
    const symAtCall = S.sym;
    S.expTries = (S.expSym === S.sym ? (S.expTries || 0) : 0) + 1; S.expSym = S.sym;
    if (S.expTries >= 3) {
      sel.innerHTML = "<option>none</option>";
      $("chainTbl").querySelector("tbody").innerHTML = `<tr><td colspan="19" class="empty">No listed options for ${esc(S.sym)}</td></tr>`;
      return;
    }
    sel.innerHTML = "<option>retrying…</option>";
    setTimeout(() => { if (S.sym === symAtCall && !S.exp) loadExpirations(); }, 5000);
    $("chainTbl").querySelector("tbody").innerHTML = `<tr><td colspan="19" class="empty">No options found for ${esc(S.sym)} yet (retrying)</td></tr>`;
    return;
  }
  const today = new Date();
  sel.innerHTML = exps.slice(0, 40).map((e) => {
    const d = new Date(e + "T16:00:00");
    const dte = Math.max(0, Math.round((d - today) / 86400000));
    return `<option value="${e}">${d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" })} · ${dte}d</option>`;
  }).join("");
  S.exp = exps[0]; sel.value = S.exp; S.expTries = 0;
  loadChain();
}

async function loadChain() {
  if (!S.sym || !S.exp) return;
  const strikes = $("strikeSel").value;
  let c;
  const body = $("chainTbl").querySelector("tbody");
  const hasTable = !!body.querySelector("td.strike");
  try { c = await api(`/api/chain?symbol=${encodeURIComponent(S.sym)}&expiration=${S.exp}&strikes=${strikes}`); }
  catch (e) { $("chainStale").hidden = !hasTable; return; }
  if (!c.rows.length) {
    if (hasTable && c.expiration === S.exp) { $("chainStale").hidden = false; return; }   // keep last good chain
    body.innerHTML = `<tr><td colspan="19" class="empty">Loading chain… (Public API is slow right now, retrying)</td></tr>`;
    return;
  }
  $("chainStale").hidden = true;
  const spot = c.spot;
  $("chainInfo").textContent = spot ? `Underlying ${n(spot)} · ${c.dte}d to expiry` : "";
  let atmIdx = -1, best = Infinity;
  c.rows.forEach((r, i) => { const d = Math.abs(r.strike - spot); if (d < best) { best = d; atmIdx = i; } });
  const g = (v, d) => (v === null || v === undefined ? `<td class="dim">–</td>` : `<td>${n(v, d)}</td>`);
  const ivc = (v) => (v ? `<td>${n(v * 100, 1)}%</td>` : `<td class="dim">–</td>`);
  const q = (leg, kind) => {
    if (!leg) return `<td class="dim">–</td>`;
    const px = kind === "bid" ? leg.bid : leg.ask;
    const sel = S.ticket.symbol === leg.symbol ? " sel" : "";
    return px > 0
      ? `<td class="q ${kind}${sel}" data-sym="${leg.symbol}" data-kind="${kind}" data-px="${px}">${n(px)}</td>`
      : `<td class="dim">–</td>`;
  };
  body.innerHTML = c.rows.map((r, i) => {
    const cl = r.call, pt = r.put;
    const callItm = spot && r.strike < spot, putItm = spot && r.strike > spot;
    const callCells = cl
      ? [g(cl.delta, 2), g(cl.gamma, 3), g(cl.theta, 2), g(cl.vega, 2), ivc(cl.iv),
         `<td>${intc(cl.open_interest)}</td>`, `<td>${intc(cl.volume)}</td>`, q(cl, "bid"), q(cl, "ask")]
      : Array(9).fill(`<td class="dim">–</td>`);
    const putCells = pt
      ? [q(pt, "bid"), q(pt, "ask"), `<td>${intc(pt.volume)}</td>`, `<td>${intc(pt.open_interest)}</td>`,
         ivc(pt.iv), g(pt.delta, 2), g(pt.gamma, 3), g(pt.theta, 2), g(pt.vega, 2)]
      : Array(9).fill(`<td class="dim">–</td>`);
    const shade = (cells, on) => (on ? cells.map((x) => x.replace("<td", '<td data-itm="1"')) : cells);
    return `<tr class="${i === atmIdx ? "atm" : ""}">${shade(callCells, callItm).join("")}<td class="strike">${n(r.strike, r.strike % 1 ? 2 : 0)}</td>${shade(putCells, putItm).join("")}</tr>`;
  }).join("").replaceAll('data-itm="1" class="', 'class="itm ').replaceAll('<td data-itm="1">', '<td class="itm">');
}

function initChain() {
  $("expSel").addEventListener("change", (e) => { S.exp = e.target.value; loadChain(); });
  $("strikeSel").addEventListener("change", loadChain);
  $("chainTbl").addEventListener("click", (e) => {
    const td = e.target.closest("td.q"); if (!td) return;
    setInstrument(td.dataset.sym);
    setSide(td.dataset.kind === "ask" ? "BUY" : "SELL");
    setType("LIMIT");
    $("tLimit").value = td.dataset.px;
    updateEst(); refreshQuote(); loadChain();
  });
}

// ---------------------------------------------------------------- ticket
function setInstrument(sym) {
  S.ticket = { symbol: sym, label: describe(sym), isOption: isOpt(sym) };
  $("tInstr").textContent = S.ticket.label;
  $("tQtyUnit").textContent = S.ticket.isOption ? "contracts (×100)" : "shares";
  $("tQty").step = "1";
  S.tq = null; renderTicketQuote(); hideConfirm();
}
function setSide(v) {
  S.side = v;
  document.querySelectorAll("#tSide button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("tStopHint").textContent = v === "BUY" ? "(triggers when ask ≥ stop)" : "(triggers when bid ≤ stop)";
  hideConfirm(); updateEst();
}
function setType(v) {
  S.type = v;
  document.querySelectorAll("#tType button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("tLimitWrap").hidden = !(v === "LIMIT" || v === "STOP_LIMIT");
  $("tStopWrap").hidden = !(v === "STOP" || v === "STOP_LIMIT");
  $("tStopHint").textContent = S.side === "BUY" ? "(triggers when ask ≥ stop)" : "(triggers when bid ≤ stop)";
  hideConfirm(); updateEst();
}
function renderTicketQuote() {
  const q = S.tq;
  $("tQuote").textContent = q ? `Bid ${n(q.bid)} · Ask ${n(q.ask)} · Mid ${n(q.mid)}` : "";
  updateEst();
}
function estPrice() {
  if (S.type === "LIMIT" || S.type === "STOP_LIMIT") return parseFloat($("tLimit").value) || null;
  if (S.type === "STOP") return parseFloat($("tStop").value) || null;
  if (!S.tq) return null;
  return S.side === "BUY" ? S.tq.ask : S.tq.bid;
}
function updateEst() {
  const px = estPrice(), qty = parseFloat($("tQty").value) || 0, mult = S.ticket.isOption ? 100 : 1;
  $("tEstLabel").textContent = S.side === "BUY" ? "Est. cost" : "Est. credit";
  $("tEst").textContent = px ? money(px * qty * mult) : "–";
  const held = S.positions.find((p) => p.symbol === S.ticket.symbol);
  $("tNote").textContent = [
    { MARKET: `Market orders fill at the ${S.side === "BUY" ? "ask" : "bid"}.`,
      LIMIT: "Fills when the live quote reaches your limit.",
      STOP: "Becomes a market order once the stop price trades.",
      STOP_LIMIT: "Becomes a limit order once the stop price trades." }[S.type],
    held ? `You're ${held.quantity > 0 ? "long" : "short"} ${Math.abs(held.quantity)}.`
      : (S.side === "SELL" ? (S.ticket.isOption ? "Selling with no position opens a short (sell to open)." : "Selling with no position opens a short sale.") : ""),
  ].filter(Boolean).join(" ");
}
function hideConfirm() { $("tConfirm").hidden = true; $("tReview").hidden = false; }
function showMsg(text, ok) { const m = $("tMsg"); m.textContent = text; m.className = "msg " + (ok ? "ok" : "err"); m.hidden = false; clearTimeout(showMsg._t); showMsg._t = setTimeout(() => (m.hidden = true), 7000); }

function initTicket() {
  $("tSide").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setSide(b.dataset.v); });
  $("tType").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    setType(b.dataset.v);
    if (b.dataset.v === "LIMIT" && !$("tLimit").value && S.tq) $("tLimit").value = n(S.tq.mid);
  });
  ["tQty", "tLimit", "tStop", "tTP", "tSL"].forEach((id) => $(id).addEventListener("input", () => { hideConfirm(); updateEst(); }));
  $("tBracket").addEventListener("change", () => { $("tBrFields").hidden = !$("tBracket").checked; hideConfirm(); });
  $("tReview").addEventListener("click", () => {
    const qty = parseFloat($("tQty").value);
    if (!S.ticket.symbol) return showMsg("Pick a ticker first.", false);
    if (!(qty > 0)) return showMsg("Enter a quantity.", false);
    if ((S.type === "LIMIT" || S.type === "STOP_LIMIT") && !(parseFloat($("tLimit").value) > 0)) return showMsg("Enter a limit price.", false);
    if ((S.type === "STOP" || S.type === "STOP_LIMIT") && !(parseFloat($("tStop").value) > 0)) return showMsg("Enter a stop price.", false);
    const br = $("tBracket").checked, tp = parseFloat($("tTP").value), sl = parseFloat($("tSL").value);
    if (br && !(tp > 0) && !(sl > 0)) return showMsg("Enter a take-profit and/or stop-loss price.", false);
    const px = estPrice(), mult = S.ticket.isOption ? 100 : 1;
    $("tSummary").innerHTML =
      `<b class="${S.side === "BUY" ? "up" : "down"}">${S.side}</b> ${qty} ${S.ticket.isOption ? "contract" + (qty === 1 ? "" : "s") : "share" + (qty === 1 ? "" : "s")} of <b>${esc(S.ticket.label)}</b><br>` +
      `${{ MARKET: "Market", LIMIT: `Limit ${n(parseFloat($("tLimit").value))}`, STOP: `Stop ${n(parseFloat($("tStop").value))}`,
          STOP_LIMIT: `Stop ${n(parseFloat($("tStop").value))} / limit ${n(parseFloat($("tLimit").value))}` }[S.type]} · ${$("tTif").value}` +
      (br ? `<br>Bracket: ${tp > 0 ? `take-profit ${n(tp)}` : ""}${tp > 0 && sl > 0 ? " · " : ""}${sl > 0 ? `stop-loss ${n(sl)}` : ""}` : "") +
      (px ? `<br>Est. ${S.side === "BUY" ? "cost" : "credit"} <b>${money(px * qty * mult)}</b>` : "");
    $("tConfirm").hidden = false; $("tReview").hidden = true;
  });
  $("tBack").addEventListener("click", hideConfirm);
  $("tSubmit").addEventListener("click", async () => {
    const btn = $("tSubmit"); btn.disabled = true;
    try {
      const o = await api("/api/orders", { method: "POST", body: JSON.stringify({
        symbol: S.ticket.symbol, side: S.side, quantity: parseFloat($("tQty").value),
        order_type: S.type,
        limit_price: (S.type === "LIMIT" || S.type === "STOP_LIMIT") ? parseFloat($("tLimit").value) : null,
        stop_price: (S.type === "STOP" || S.type === "STOP_LIMIT") ? parseFloat($("tStop").value) : null,
        take_profit: $("tBracket").checked && parseFloat($("tTP").value) > 0 ? parseFloat($("tTP").value) : null,
        stop_loss: $("tBracket").checked && parseFloat($("tSL").value) > 0 ? parseFloat($("tSL").value) : null,
        time_in_force: $("tTif").value }) });
      showMsg(o.status === "FILLED"
        ? `Filled: ${o.side} ${o.quantity} @ ${n(o.avg_fill_price)}`
        : (o.order_type === "STOP" || o.order_type === "STOP_LIMIT")
          ? `Stop order working at ${n(o.stop_price)}.`
          : `Order placed, resting at ${n(o.limit_price)}. It fills when the market reaches your price.`, true);
      if ($("tBracket").checked && o.status === "FILLED") toast("Bracket exits placed (see Orders)");
      hideConfirm(); refreshAccount();
    } catch (e) { showMsg(e.message, false); }
    finally { btn.disabled = false; }
  });
}

// ---------------------------------------------------------------- account panels
async function refreshAccount() {
  try {
    const [a, pos, ords, fills, meta] = await Promise.all([
      api("/api/account"), api("/api/positions"), api("/api/orders"), api("/api/fills"), api("/api/meta")]);
    S.positions = pos;
    $("hEquity").textContent = money(a.equity);
    $("hDay").innerHTML = `<span class="${cls(a.day_pnl)}">${signed(a.day_pnl)}</span>`;
    $("hBP").textContent = money(a.buying_power);
    if ($("hBP2")) $("hBP2").textContent = money(a.buying_power);
    const pill = $("mktPill");
    const slow = meta.data && meta.data.backing_off;
    pill.textContent = slow ? `Public API slow · retry in ${Math.ceil(meta.data.resume_in)}s`
      : meta.market_open ? "Market open" : "Market closed";
    pill.classList.toggle("open", meta.market_open && !slow);
    pill.classList.toggle("slow", !!slow);
    $("demoBadge").hidden = !meta.demo;
    $("acctDl").innerHTML = [
      ["Cash", money(a.cash)], ["Positions value", money(a.positions_value)], ["Equity", money(a.equity)],
      ["Options collateral", money(a.collateral)],
      ["Buying power", money(a.buying_power)],
      ["Day P&L", `<span class="${cls(a.day_pnl)}">${signed(a.day_pnl)}</span>`],
      ["Total P&L", `<span class="${cls(a.total_pnl)}">${signed(a.total_pnl)}</span>`],
    ].map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

    $("nPos").textContent = pos.length || "";
    $("posTbl").querySelector("tbody").innerHTML = pos.length ? pos.map((p) => `<tr>
      <td>${esc(p.description)} <span class="tag">${p.quantity > 0 ? "LONG" : "SHORT"}</span></td><td class="r">${p.quantity}</td><td class="r">${n(p.avg_cost)}</td>
      <td class="r">${n(p.mark)}</td><td class="r">${money(p.market_value)}</td>
      <td class="r ${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>
      <td class="r"><button class="btn xs" data-close="${esc(p.symbol)}" data-qty="${p.quantity}">Close</button></td></tr>`).join("")
      : `<tr><td colspan="7" class="empty">No positions. Pick a ticker and place a paper trade.</td></tr>`;

    const open = ords.filter((o) => o.status === "OPEN");
    $("nOrd").textContent = open.length || "";
    $("ordTbl").querySelector("tbody").innerHTML = ords.length ? ords.map((o) => `<tr>
      <td>${new Date(o.created_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
      <td>${orderLabel(o)}</td><td class="${o.side === "BUY" || o.side === "DEBIT" ? "up" : "down"}">${o.side}</td>
      <td class="r">${o.quantity}</td><td>${o.order_type.replace("_", " ")}${o.trigger_state === "TRIGGERED" ? ' <span class="tag">TRIGGERED</span>' : ""}</td>
      <td class="r">${o.stop_price ? `stop ${n(o.stop_price)}${o.limit_price ? " / " + n(o.limit_price) : ""}` : (o.limit_price ? n(o.limit_price) : "–")}</td>
      <td>${o.status}</td><td class="r">${o.avg_fill_price ? n(o.avg_fill_price) : "–"}</td>
      <td class="muted">${esc(o.reason || "")}</td>
      <td class="r">${o.status === "OPEN" ? `<button class="btn xs" data-cancel="${o.id}">Cancel</button>` : ""}</td></tr>`).join("")
      : `<tr><td colspan="10" class="empty">No orders yet.</td></tr>`;

    $("fillTbl").querySelector("tbody").innerHTML = fills.length ? fills.map((f) => `<tr>
      <td>${new Date(f.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
      <td>${esc(describe(f.symbol))}</td><td>${f.side}</td><td class="r">${f.quantity}</td>
      <td class="r">${n(f.price)}</td><td class="r ${cls(f.realized_pnl)}">${f.effect === "OPEN" ? "–" : signed(f.realized_pnl)}</td></tr>`).join("")
      : `<tr><td colspan="6" class="empty">No fills yet.</td></tr>`;
    updateEst();
  } catch (e) { /* server restarting */ }
}

function initPanels() {
  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
    ["chain", "strategies", "positions", "orders", "fills", "analytics"].forEach((t) => ($("tab-" + t).hidden = t !== b.dataset.t));
    if (b.dataset.t === "analytics") loadAnalytics();
  });
  $("posTbl").addEventListener("click", (e) => {
    const b = e.target.closest("[data-close]"); if (!b) return;
    const q = parseFloat(b.dataset.qty);
    setInstrument(b.dataset.close); setSide(q > 0 ? "SELL" : "BUY"); setType("MARKET");
    $("tBracket").checked = false; $("tBrFields").hidden = true;
    $("tQty").value = Math.abs(q); updateEst(); refreshQuote();
    $("tReview").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("ordTbl").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-cancel]"); if (!b) return;
    try { await api(`/api/orders/${b.dataset.cancel}`, { method: "DELETE" }); toast("Order cancelled"); refreshAccount(); }
    catch (err) { toast(err.message); }
  });
  $("tradeStock").addEventListener("click", () => { if (S.sym) { setInstrument(S.sym); setType("MARKET"); refreshQuote(); loadChain(); } });
  $("resetBtn").addEventListener("click", async () => {
    const v = prompt("Reset the paper account. This deletes all positions, orders and history.\nStarting cash:", "25000");
    if (v === null) return;
    const cash = parseFloat(v);
    if (!(cash > 0)) return toast("Enter a positive amount");
    await api("/api/reset", { method: "POST", body: JSON.stringify({ cash }) });
    toast(`Account reset to ${money(cash)}`); refreshAccount();
  });
}

// ---------------------------------------------------------------- views / routing
function goTrade(sym) { location.hash = sym ? `#/trade/${encodeURIComponent(sym.toUpperCase())}` : "#/trade"; }
function showView(v) {
  S.view = v;
  $("viewHome").hidden = v !== "home";
  $("viewTrade").hidden = v !== "trade";
  $("viewAgents").hidden = v !== "agents";
  document.querySelectorAll("#mainnav a").forEach((a) => a.classList.toggle("on", a.dataset.v === v));
  if (v === "home" && typeof loadHome === "function") loadHome();
  if (v === "agents" && typeof loadAgents === "function") loadAgents();
}
function route() {
  const m = /^#\/trade(?:\/([^/?#]+))?/.exec(location.hash);
  if (m) {
    const sym = m[1] ? decodeURIComponent(m[1]).toUpperCase() : (S.sym || S.startSym);
    const firstShow = S.view !== "trade";
    showView("trade");
    if (sym !== S.sym) loadSymbol(sym);
    else if (firstShow && S.chartSrc === "tv") renderTV();   // size the chart now that it's visible
  } else if (/^#\/agents/.test(location.hash)) {
    showView("agents");
  } else {
    showView("home");
  }
}
window.addEventListener("hashchange", route);

// ---------------------------------------------------------------- boot
(async function boot() {
  initChart(); initTicket(); initChain(); initPanels(); initChartSource(); applyChartSource();
  await initSearch();
  let start = "QQQ";
  try { start = localStorage.getItem("pp_sym") || "QQQ"; } catch (e) { /* storage unavailable */ }
  S.startSym = start;
  route();
  refreshAccount();
  setInterval(() => { if (S.view === "trade") refreshQuote(); }, 4000);
  setInterval(refreshAccount, 5000);
  setInterval(() => { if (S.view === "trade" && !$("tab-chain").hidden) loadChain(); }, 15000);
  setInterval(() => { if (S.view === "trade" && S.chartSrc !== "tv") loadChart(); }, 60000);
})();
