"use strict";
/* Shadow Book home: balance + account value chart, portfolio table,
   watchlist, market strip and recent activity. Uses helpers from app.js. */

const H = { range: "1D", filter: "all", chart: null, series: null, baseLine: null, editing: false, closeArm: null };

// ------------------------------------------------------------------ market strip
async function loadMarket() {
  let m; try { m = await api("/api/market"); } catch (e) { return; }
  $("mstrip").innerHTML = m.map((x) => `<span class="mi"><b>${esc(x.label)}</b> <span class="muted">${esc(x.symbol)}</span>
    <span>${n(x.last)}</span> <span class="${cls(x.change_pct)}">${x.change_pct === null ? "" : (x.change_pct >= 0 ? "▲ +" : "▼ ") + n(x.change_pct) + "%"}</span></span>`).join("");
}

// ------------------------------------------------------------------ account value
function initHomeChart() {
  H.chart = LightweightCharts.createChart($("homeChart"), {
    layout: { background: { color: "transparent" }, textColor: "#6f7d8f", fontFamily: "Inter, sans-serif" },
    grid: { vertLines: { visible: false }, horzLines: { visible: false } },
    rightPriceScale: { visible: false }, leftPriceScale: { visible: false },
    timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
    crosshair: { horzLine: { visible: false, labelVisible: false }, vertLine: { labelVisible: true } },
    handleScroll: false, handleScale: false, localization: { locale: "en-US", priceFormatter: (v) => money(v) },
    autoSize: true,
  });
  H.series = H.chart.addAreaSeries({ lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerRadius: 4 });
  $("homeRanges").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-r]"); if (!b) return;
    H.range = b.dataset.r;
    document.querySelectorAll("#homeRanges button").forEach((x) => x.classList.toggle("on", x === b));
    loadEquity();
  });
}
async function loadEquity() {
  let e; try { e = await api(`/api/equity?range=${H.range}`); } catch (err) { return; }
  $("hVal").textContent = money(e.equity);
  const lbl = { "1D": "today", "1W": "past week", "1M": "past month", "3M": "past 3 months", YTD: "year to date", ALL: "all time" }[e.range];
  $("hChg").innerHTML = `<span class="${cls(e.change)}">${e.change >= 0 ? "▲" : "▼"} ${signed(e.change)} (${e.change_pct >= 0 ? "+" : ""}${n(e.change_pct)}%)</span> <span class="muted">${lbl}</span>`;
  const up = e.change >= 0;
  const col = up ? "#22c55e" : "#f0525b";
  H.series.applyOptions({ lineColor: col, topColor: up ? "rgba(34,197,94,0.18)" : "rgba(240,82,91,0.18)", bottomColor: "rgba(0,0,0,0)" });
  const seen = new Set();
  const pts = e.points.map((p) => ({ time: etSeconds(p.ts), value: p.equity }))
    .filter((p) => (seen.has(p.time) ? false : seen.add(p.time))).sort((a, b) => a.time - b.time);
  if (pts.length === 1) pts.unshift({ time: pts[0].time - 60, value: e.baseline });
  H.series.setData(pts);
  if (H.baseLine) H.series.removePriceLine(H.baseLine);
  H.baseLine = H.series.createPriceLine({ price: e.baseline, color: "#3a4758", lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
  H.chart.timeScale().fitContent();
  $("homeChartNote").textContent = e.points.length < 3 ? "Chart fills in as Shadow Book runs (one point a minute during market hours)" : "";
}

// ------------------------------------------------------------------ portfolio
const pctTxt = (v) => (v === null || v === undefined ? "" : `${v >= 0 ? "+" : ""}${n(v)}%`);
function retCell(amt, pct) {
  if (amt === null || amt === undefined) return `<td class="muted">–</td>`;
  return `<td><div class="${cls(amt)}">${pctTxt(pct) || signed(amt)}</div><div class="sub ${cls(amt)}">${signed(amt)}</div></td>`;
}
function logoFor(sym) {
  const s = (sym || "?").replace(/[^A-Z]/g, "");
  let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `<span class="tlogo" style="--h:${h}">${esc(s.slice(0, 2))}</span>`;
}
function pfRow(r) {
  const ch = r.price_change;
  return `<tr data-und="${esc(r.underlying)}">
    <td class="l"><div class="nm">${logoFor(r.underlying)}<div><div class="t">${esc(r.title)}${r.kind === "group" ? ' <span class="tag">SPREAD</span>' : ""}</div>
      <div class="sub">${esc(r.subtitle || nameOf(r.underlying))}</div></div></div></td>
    <td><div>${r.price === null ? "–" : n(r.price)}</div><div class="sub ${cls(ch)}">${ch === null || ch === undefined ? "" : `${ch >= 0 ? "+" : ""}${n(ch)} (${pctTxt(r.price_change_pct)})`}</div></td>
    <td><div>${money(r.holdings)}</div><div class="sub">${esc(r.holdings_sub)}</div></td>
    <td><div>${money(r.cost)}</div><div class="sub">${esc(r.cost_sub)}</div></td>
    ${retCell(r.day_return, r.day_return_pct)}
    ${retCell(r.unrealized, r.unrealized_pct)}
    <td class="act"><button class="btn xs" data-close='${esc(JSON.stringify(r.close))}'>Close</button></td></tr>`;
}
const nameOf = (sym) => (S.symbols.find((x) => x.symbol === sym) || {}).name || "";
async function loadPortfolio() {
  let p; try { p = await api("/api/portfolio"); } catch (e) { return; }
  H.pf = p; renderPortfolio();
}
function renderPortfolio() {
  const p = H.pf; if (!p) return;
  const body = $("pfTbl").querySelector("tbody");
  const parts = [];
  const sec = (label, rows) => rows.length ? `<tr class="sec"><td class="l" colspan="7">${label}</td></tr>${rows.map(pfRow).join("")}` : "";
  if (H.filter !== "equities") parts.push(sec("Options", p.options));
  if (H.filter !== "options") parts.push(sec("Equities", p.equities));
  const html = parts.join("");
  body.innerHTML = html || `<tr><td colspan="7" class="empty-pf">
      <div class="empty-big">Your $25,000 paper account is ready.</div>
      <div class="muted">Search a ticker to trade shares or options, or build a spread from the Strategies tab.</div>
      <div class="empty-actions"><button class="btn primary sm" data-go="QQQ">Trade QQQ</button><button class="btn sm" data-go="SPY">Trade SPY</button><button class="btn sm" data-strat="1">Browse strategies</button></div></td></tr>`;
}
function initPortfolio() {
  $("pfChips").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]"); if (!b) return;
    H.filter = b.dataset.f;
    document.querySelectorAll("#pfChips button").forEach((x) => x.classList.toggle("on", x === b));
    renderPortfolio();
  });
  $("pfTbl").addEventListener("click", async (e) => {
    const go = e.target.closest("[data-go]");
    if (go) return goTrade(go.dataset.go);
    if (e.target.closest("[data-strat]")) { goTrade(S.sym || "QQQ"); setTimeout(() => document.querySelector('#tabs button[data-t="strategies"]').click(), 50); return; }
    const cb = e.target.closest("button[data-close]");
    if (cb) {
      e.stopPropagation();
      if (H.closeArm !== cb) {                  // first click arms, second confirms
        if (H.closeArm) { H.closeArm.textContent = "Close"; H.closeArm.classList.remove("armed"); }
        H.closeArm = cb; cb.textContent = "Confirm close"; cb.classList.add("armed");
        setTimeout(() => { if (H.closeArm === cb) { cb.textContent = "Close"; cb.classList.remove("armed"); H.closeArm = null; } }, 4000);
        return;
      }
      H.closeArm = null; cb.disabled = true;
      try {
        const c = JSON.parse(cb.dataset.close);
        const o = await api("/api/close", { method: "POST", body: JSON.stringify(c) });
        toast(o.status === "FILLED" ? "Position closed" : `Close order ${o.status.toLowerCase()}`);
      } catch (err) { toast(err.message); }
      loadHome(); refreshAccount();
      return;
    }
    const tr = e.target.closest("tr[data-und]");
    if (tr) goTrade(tr.dataset.und);
  });
}

// ------------------------------------------------------------------ watchlist
async function loadWatch() {
  let w; try { w = await api("/api/watchlist"); } catch (e) { return; }
  $("wList").innerHTML = w.length ? w.map((x) => `<li data-s="${esc(x.symbol)}">
      <div class="w-sym"><b>${esc(x.symbol)}</b><span class="muted">${esc(nameOf(x.symbol))}</span></div>
      <div class="w-px mono"><div>${n(x.last)}</div><div class="${cls(x.change_pct)}">${x.change_pct === null ? "" : pctTxt(x.change_pct)}</div></div>
      ${H.editing ? `<button class="w-rm" data-rm="${esc(x.symbol)}" aria-label="Remove ${esc(x.symbol)}">×</button>` : ""}</li>`).join("")
    : `<li class="muted small">Add tickers to track them here.</li>`;
  H.watch = w.map((x) => x.symbol);
}
function initWatch() {
  $("wEdit").addEventListener("click", () => {
    H.editing = !H.editing; $("wEdit").textContent = H.editing ? "Done" : "Edit";
    $("wAdd").hidden = !H.editing; loadWatch();
  });
  $("wAdd").addEventListener("submit", async (e) => {
    e.preventDefault();
    const s = $("wAddIn").value.trim().toUpperCase(); if (!s) return;
    await api("/api/watchlist", { method: "PUT", body: JSON.stringify({ symbols: [...(H.watch || []), s] }) });
    $("wAddIn").value = ""; loadWatch();
  });
  $("wList").addEventListener("click", async (e) => {
    const rm = e.target.closest("[data-rm]");
    if (rm) {
      await api("/api/watchlist", { method: "PUT", body: JSON.stringify({ symbols: (H.watch || []).filter((s) => s !== rm.dataset.rm) }) });
      return loadWatch();
    }
    const li = e.target.closest("li[data-s]"); if (li && !H.editing) goTrade(li.dataset.s);
  });
}

// ------------------------------------------------------------------ activity
async function loadActivity() {
  let f; try { f = await api("/api/fills"); } catch (e) { return; }
  const verb = (x) => ({ BUY: x.effect === "CLOSE" ? "Bought to close" : "Bought", SELL: x.effect === "CLOSE" ? "Sold to close" : (isOpt(x.symbol) ? "Sold to open" : "Sold"),
    EXPIRE: x.effect === "ASSIGNED" ? "Assigned" : x.effect === "EXERCISED" ? "Exercised" : "Expired" }[x.side] || x.side);
  $("actList").innerHTML = f.length ? f.slice(0, 8).map((x) => `<li>
      <div><b>${verb(x)}</b> ${x.quantity} ${esc(describe(x.symbol))} <span class="muted">@ ${n(x.price)}</span></div>
      <div class="muted small">${new Date(x.ts).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
      ${x.effect !== "OPEN" && x.realized_pnl ? ` · <span class="${cls(x.realized_pnl)}">${signed(x.realized_pnl)}</span>` : ""}</div></li>`).join("")
    : `<li class="muted small">No trades yet.</li>`;
}

// ------------------------------------------------------------------ boot
function loadHome() { loadEquity(); loadPortfolio(); loadWatch(); loadActivity(); }
(function initHome() {
  initHomeChart(); initPortfolio(); initWatch();
  loadMarket();
  if (S.view === "home") loadHome();
  setInterval(loadMarket, 15000);
  setInterval(() => { if (S.view === "home") { loadEquity(); loadPortfolio(); loadWatch(); loadActivity(); } }, 6000);
})();
