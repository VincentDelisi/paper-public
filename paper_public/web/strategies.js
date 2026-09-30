"use strict";
/* Strategy cards, strategy builder with payoff chart, and the analytics tab.
   Shares helpers and state (S, api, n, money, ...) with app.js. */

// ------------------------------------------------------------------ templates
// kind: C call, P put, S stock. off = strike offset in chain steps from ATM.
const STRATS = [
  { group: "Fundamentals", name: "Long Call", bias: "bull", legs: [{ kind: "C", side: "BUY", off: 0 }] },
  { group: "Fundamentals", name: "Long Put", bias: "bear", legs: [{ kind: "P", side: "BUY", off: 0 }] },
  { group: "Fundamentals", name: "Covered Call", bias: "bull", legs: [{ kind: "S", side: "BUY", ratio: 100 }, { kind: "C", side: "SELL", off: 2 }] },
  { group: "Fundamentals", name: "Cash-Secured Put", bias: "bull", legs: [{ kind: "P", side: "SELL", off: -2 }] },
  { group: "Fundamentals", name: "Covered Put", bias: "bear", legs: [{ kind: "S", side: "SELL", ratio: 100 }, { kind: "P", side: "SELL", off: -2 }] },
  { group: "Fundamentals", name: "Protective Put", bias: "bull", legs: [{ kind: "S", side: "BUY", ratio: 100 }, { kind: "P", side: "BUY", off: -2 }] },
  { group: "Straddles & Strangles", name: "Long Straddle", bias: "vol", legs: [{ kind: "C", side: "BUY", off: 0 }, { kind: "P", side: "BUY", off: 0 }] },
  { group: "Straddles & Strangles", name: "Long Strangle", bias: "vol", legs: [{ kind: "C", side: "BUY", off: 2 }, { kind: "P", side: "BUY", off: -2 }] },
  { group: "Vertical Spreads", name: "Call Debit Spread", bias: "bull", legs: [{ kind: "C", side: "BUY", off: 0 }, { kind: "C", side: "SELL", off: 2 }] },
  { group: "Vertical Spreads", name: "Call Credit Spread", bias: "bear", legs: [{ kind: "C", side: "SELL", off: 1 }, { kind: "C", side: "BUY", off: 3 }] },
  { group: "Vertical Spreads", name: "Put Debit Spread", bias: "bear", legs: [{ kind: "P", side: "BUY", off: 0 }, { kind: "P", side: "SELL", off: -2 }] },
  { group: "Vertical Spreads", name: "Put Credit Spread", bias: "bull", legs: [{ kind: "P", side: "SELL", off: -1 }, { kind: "P", side: "BUY", off: -3 }] },
  { group: "Vertical Spreads", name: "Synthetic Long", bias: "bull", legs: [{ kind: "C", side: "BUY", off: 0 }, { kind: "P", side: "SELL", off: 0 }] },
  { group: "Vertical Spreads", name: "Synthetic Short", bias: "bear", legs: [{ kind: "P", side: "BUY", off: 0 }, { kind: "C", side: "SELL", off: 0 }] },
];
const BIAS = { bull: ["Bullish ↑", "bull"], bear: ["Bearish ↓", "bear"], vol: ["Volatile ⇅", "vol"] };

// ------------------------------------------------------------------ math
function ncdf(x) { // Abramowitz-Stegun erf
  const t = 1 / (1 + 0.3275911 * Math.abs(x / Math.SQRT2));
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-(x * x) / 2);
  return x >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y);
}
function bs(S0, K, T, sig, right) {
  const d1 = (Math.log(S0 / K) + 0.5 * sig * sig * T) / (sig * Math.sqrt(T)), d2 = d1 - sig * Math.sqrt(T);
  return right === "C" ? S0 * ncdf(d1) - K * ncdf(d2) : K * ncdf(-d2) - S0 * ncdf(-d1);
}
// Value at expiration of one leg unit (per share for stock, per contract-share for options)
const legValue = (leg, P) => (leg.kind === "S" ? P : leg.kind === "C" ? Math.max(P - leg.strike, 0) : Math.max(leg.strike - P, 0));
const legMult = (leg) => (leg.kind === "S" ? 1 : 100);
const legSign = (leg) => (leg.side === "BUY" ? 1 : -1);
const legRatio = (leg) => leg.ratio || 1;

/** P&L at expiration for `qty` spreads, given signed per-share net (+debit / -credit). */
function pnlAt(legs, net, qty, P) {
  let v = 0;
  for (const l of legs) v += legSign(l) * legRatio(l) * legMult(l) * legValue(l, P);
  return qty * (v - 100 * net);
}
function analyze(legs, net, qty, spot) {
  const strikes = legs.filter((l) => l.kind !== "S").map((l) => l.strike);
  const hi = Math.max(spot, ...strikes, 1) * 3;
  const pts = [...new Set([0, ...strikes, spot, hi])].sort((a, b) => a - b);
  const vals = pts.map((P) => pnlAt(legs, net, qty, P));
  const slopeR = pnlAt(legs, net, qty, hi + 1) - pnlAt(legs, net, qty, hi);
  let maxP = Math.max(...vals), maxL = Math.min(...vals);
  const unlimitedProfit = slopeR > 1e-9, unlimitedLoss = slopeR < -1e-9;
  const bes = [];
  for (let i = 0; i < pts.length - 1; i++) {
    const a = vals[i], b = vals[i + 1];
    if (a === 0) bes.push(pts[i]);
    else if ((a < 0 && b > 0) || (a > 0 && b < 0)) bes.push(pts[i] + (pts[i + 1] - pts[i]) * (-a / (b - a)));
  }
  // collateral: worst settlement shortfall over 0..1.5x spot, premiums excluded (matches the server)
  const grid = [...new Set([0, spot, spot * 1.5, ...strikes.filter((k) => k <= spot * 1.5)])];
  let worst = 0;
  for (const P of grid) {
    let v = 0; for (const l of legs) v += legSign(l) * legRatio(l) * legMult(l) * legValue(l, P);
    worst = Math.min(worst, v);
  }
  const bpNeeded = Math.max(0, qty * (100 * net - worst));
  return { maxP, maxL, unlimitedProfit, unlimitedLoss, bes: [...new Set(bes.map((x) => +x.toFixed(2)))], bpNeeded };
}

// ------------------------------------------------------------------ cards
function thumbSVG(t) {
  const S0 = 100, step = 5, T = 30 / 365, iv = 0.3;
  const legs = t.legs.map((l) => {
    const strike = l.kind === "S" ? null : S0 + (l.off || 0) * step;
    const px = l.kind === "S" ? S0 : bs(S0, strike, T, iv, l.kind);
    return { ...l, strike, px };
  });
  const net = legs.reduce((a, l) => a + legSign(l) * legRatio(l) * legMult(l) * l.px / 100, 0);
  const xs = [], ys = [];
  for (let i = 0; i <= 60; i++) { const P = 70 + i; xs.push(P); ys.push(pnlAt(legs, net, 1, P)); }
  const lo = Math.min(...ys, 0), hiY = Math.max(...ys, 0), pad = (hiY - lo) * 0.12 || 1;
  const W = 150, H = 72, X = (P) => ((P - 70) / 60) * W, Y = (v) => H - ((v - (lo - pad)) / (hiY - lo + 2 * pad)) * H;
  const line = xs.map((P, i) => `${i ? "L" : "M"}${X(P).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join("");
  const area = `${line}L${W},${Y(0)}L0,${Y(0)}Z`;
  const kinks = legs.filter((l) => l.strike).map((l) => `<circle cx="${X(l.strike)}" cy="${Y(pnlAt(legs, net, 1, l.strike))}" r="3" fill="#1f3fbf"/>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${area}" fill="rgba(76,120,255,0.13)"/>
    <line x1="0" x2="${W}" y1="${Y(0)}" y2="${Y(0)}" stroke="#9aa6b8" stroke-dasharray="3 3" stroke-width="1"/>
    <path d="${line}" fill="none" stroke="#1f3fbf" stroke-width="2"/>${kinks}</svg>`;
}
function renderStratGrid() {
  const groups = [...new Set(STRATS.map((s) => s.group))];
  $("stratGrid").innerHTML = groups.map((g) => `<section class="strat-section"><h4>${g}</h4><div class="strat-cards">${
    STRATS.filter((s) => s.group === g).map((s) => `<button class="scard" data-s="${s.name}">${thumbSVG(s)}
      <div class="sname">${s.name}</div><div class="sbias ${BIAS[s.bias][1]}">${BIAS[s.bias][0]}</div></button>`).join("")
  }</div></section>`).join("");
}

// ------------------------------------------------------------------ builder
const B = { t: null, rows: [], legs: [], exp: null, stock: null, type: "LIMIT" };

async function openBuilder(name) {
  B.t = STRATS.find((s) => s.name === name);
  $("stratGrid").hidden = true; $("builder").hidden = false;
  $("bTitle").textContent = `${B.t.name} · ${S.sym}`;
  $("bBias").textContent = BIAS[B.t.bias][0]; $("bBias").className = "b-bias " + BIAS[B.t.bias][1];
  $("bMsg").hidden = true; bHideConfirm();
  const opts = [...$("expSel").options].filter((o) => /^\d{4}-/.test(o.value));
  $("bExp").innerHTML = opts.map((o) => `<option value="${o.value}">${o.textContent}</option>`).join("");
  // default: first expiration at least 7 days out for spreads, nearest for single legs
  const pick = opts.find((o) => (new Date(o.value + "T16:00:00") - new Date()) / 86400000 >= 7) || opts[0];
  B.exp = pick ? pick.value : null;
  if (B.exp) $("bExp").value = B.exp;
  await loadBuilderChain(true);
}

async function loadBuilderChain(resetStrikes) {
  if (!B.exp) { $("bLegs").querySelector("tbody").innerHTML = `<tr><td colspan="7" class="empty">No expirations loaded yet</td></tr>`; return; }
  const [c, q] = await Promise.all([
    api(`/api/chain?symbol=${encodeURIComponent(S.sym)}&expiration=${B.exp}&strikes=60`).catch(() => null),
    api(`/api/quote?symbols=${encodeURIComponent(S.sym)}`).catch(() => ({})),
  ]);
  if (c && c.rows.length) B.rows = c.rows;
  B.spot = (c && c.spot) || S.spot;
  B.stock = q[S.sym] || B.stock;
  if (!B.rows.length) return;
  const strikes = B.rows.map((r) => r.strike);
  const atm = strikes.reduce((bi, k, i) => (Math.abs(k - B.spot) < Math.abs(strikes[bi] - B.spot) ? i : bi), 0);
  if (resetStrikes || !B.legs.length || B.legs.length !== B.t.legs.length) {
    B.legs = B.t.legs.map((l) => ({ ...l, strike: l.kind === "S" ? null : strikes[Math.min(strikes.length - 1, Math.max(0, atm + (l.off || 0)))] }));
  }
  refreshLegQuotes();
  renderBuilder(true);
}

function refreshLegQuotes() {
  for (const l of B.legs) {
    if (l.kind === "S") {
      const q = B.stock || {};
      Object.assign(l, { symbol: S.sym, bid: q.bid, ask: q.ask, delta: 1 });
    } else {
      const row = B.rows.find((r) => r.strike === l.strike) || {};
      const leg = (l.kind === "C" ? row.call : row.put) || {};
      Object.assign(l, { symbol: leg.symbol, bid: leg.bid, ask: leg.ask, delta: leg.delta });
    }
  }
}
const legNat = (l) => (l.side === "BUY" ? l.ask : l.bid);
const legMid = (l) => ((l.bid || 0) + (l.ask || 0)) / 2;
const netOf = (fn) => B.legs.reduce((a, l) => a + legSign(l) * legRatio(l) * legMult(l) * (fn(l) || 0) / 100, 0);

function renderBuilder(resetNet) {
  const qty = Math.max(1, parseInt($("bQty").value) || 1);
  const strikes = B.rows.map((r) => r.strike);
  $("bLegs").querySelector("tbody").innerHTML = B.legs.map((l, i) => `<tr>
    <td class="${l.side === "BUY" ? "up" : "down"}">${l.side === "BUY" ? "Buy" : "Sell"}</td>
    <td class="r">${legRatio(l) * qty}</td>
    <td>${l.kind === "S" ? `${esc(S.sym)} shares` : `${esc(S.sym)} ${l.kind === "C" ? "Call" : "Put"}`}</td>
    <td>${l.kind === "S" ? "–" : `<select data-leg="${i}">${strikes.map((k) => `<option value="${k}" ${k === l.strike ? "selected" : ""}>${n(k, k % 1 ? 2 : 0)}</option>`).join("")}</select>`}</td>
    <td class="r">${n(l.bid)}</td><td class="r">${n(l.ask)}</td><td class="r">${l.delta === undefined || l.delta === null ? "–" : n(l.delta, 2)}</td></tr>`).join("");
  const nat = netOf(legNat), mid = netOf(legMid);
  if (resetNet || !$("bNet").value) $("bNet").value = Math.abs(Math.round(mid * 100) / 100).toFixed(2);
  bUpdate(nat, mid);
}

function bSignedNet(nat, mid) {
  if (B.type === "MARKET") return nat;
  const v = parseFloat($("bNet").value) || 0;
  return (mid >= 0 ? 1 : -1) * v;
}

function bUpdate(nat, mid) {
  if (nat === undefined) { nat = netOf(legNat); mid = netOf(legMid); }
  const qty = Math.max(1, parseInt($("bQty").value) || 1);
  const debit = mid >= 0;
  $("bNetLabel").textContent = debit ? "Net debit" : "Net credit";
  $("bNet").disabled = B.type === "MARKET";
  $("bNetHint").textContent = `Natural ${debit ? "debit" : "credit"} ${n(Math.abs(nat))} · mid ${n(Math.abs(mid))}`;
  const net = bSignedNet(nat, mid);
  const a = analyze(B.legs, net, qty, B.spot || S.spot || 0);
  $("bTotLabel").textContent = net >= 0 ? "Total cost" : "Total credit";
  $("bTot").textContent = money(Math.abs(net) * 100 * qty);
  const tile = (k, v, c = "") => `<div class="stat"><label>${k}</label><b class="${c}">${v}</b></div>`;
  $("bStats").innerHTML = [
    tile("Max profit", a.unlimitedProfit ? "Unlimited" : signed(a.maxP), "up"),
    tile("Max loss", a.unlimitedLoss ? "Unlimited" : signed(a.maxL), "down"),
    tile(a.bes.length > 1 ? "Breakevens" : "Breakeven", a.bes.length ? a.bes.map((x) => n(x)).join(" / ") : "–"),
    tile("Buying power used", money(a.bpNeeded)),
  ].join("");
  $("bPayNote").textContent = `· ${qty} × ${B.t.name}, ${B.type === "MARKET" ? "at natural" : "at your limit"}`;
  drawPayoff(B.legs, net, qty, B.spot || S.spot, a);
}

// ------------------------------------------------------------------ payoff chart (SVG)
function drawPayoff(legs, net, qty, spot, a) {
  const svg = $("payoff");
  const W = svg.clientWidth || 600, H = svg.clientHeight || 240, L = 56, R = 12, T = 12, Bm = 26;
  const strikes = legs.filter((l) => l.strike).map((l) => l.strike);
  const kLo = Math.min(spot, ...strikes), kHi = Math.max(spot, ...strikes);
  const xPad = Math.max((kHi - kLo) * 1.2, spot * 0.04);
  const lo = Math.max(0, kLo - xPad), hi = kHi + xPad;
  const N = 240, xs = [], ys = [];
  for (let i = 0; i <= N; i++) { const P = lo + ((hi - lo) * i) / N; xs.push(P); ys.push(pnlAt(legs, net, qty, P)); }
  let yMin = Math.min(...ys, 0), yMax = Math.max(...ys, 0);
  const pad = (yMax - yMin) * 0.1 || 10; yMin -= pad; yMax += pad;
  const X = (P) => L + ((P - lo) / (hi - lo)) * (W - L - R), Y = (v) => T + ((yMax - v) / (yMax - yMin)) * (H - T - Bm);
  const line = xs.map((P, i) => `${i ? "L" : "M"}${X(P).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join("");
  const area = `${line}L${X(hi)},${Y(0)}L${X(lo)},${Y(0)}Z`;
  const yTicks = niceTicks(yMin, yMax, 5), xTicks = niceTicks(lo, hi, 6);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = `
    <defs>
      <clipPath id="cpUp"><rect x="0" y="0" width="${W}" height="${Y(0)}"/></clipPath>
      <clipPath id="cpDn"><rect x="0" y="${Y(0)}" width="${W}" height="${H}"/></clipPath>
    </defs>
    ${yTicks.map((v) => `<line x1="${L}" x2="${W - R}" y1="${Y(v)}" y2="${Y(v)}" stroke="rgba(36,48,64,0.6)"/>
      <text x="${L - 6}" y="${Y(v) + 4}" text-anchor="end" fill="#6f7d8f" font-size="11">${fmtK(v)}</text>`).join("")}
    ${xTicks.map((v) => `<text x="${X(v)}" y="${H - 8}" text-anchor="middle" fill="#6f7d8f" font-size="11">${n(v, v >= 100 ? 0 : 2)}</text>`).join("")}
    <path d="${area}" fill="rgba(34,197,94,0.16)" clip-path="url(#cpUp)"/>
    <path d="${area}" fill="rgba(240,82,91,0.16)" clip-path="url(#cpDn)"/>
    <line x1="${L}" x2="${W - R}" y1="${Y(0)}" y2="${Y(0)}" stroke="#a7b3c2" stroke-width="1"/>
    <line x1="${X(spot)}" x2="${X(spot)}" y1="${T}" y2="${H - Bm}" stroke="#a7b3c2" stroke-dasharray="4 4"/>
    <text x="${X(spot) + 4}" y="${T + 10}" fill="#a7b3c2" font-size="11">Now ${n(spot)}</text>
    <path d="${line}" fill="none" stroke="#4c9dff" stroke-width="2"/>
    ${a.bes.filter((b) => b >= lo && b <= hi).map((b) => `<circle cx="${X(b)}" cy="${Y(0)}" r="4" fill="#0b0f14" stroke="#e6edf5" stroke-width="2"/>`).join("")}
    <line id="payX" x1="0" x2="0" y1="${T}" y2="${H - Bm}" stroke="#e6edf5" stroke-width="1" opacity="0"/>
    <circle id="payDot" r="4" fill="#4c9dff" stroke="#0b0f14" stroke-width="2" opacity="0"/>
    <rect x="${L}" y="${T}" width="${W - L - R}" height="${H - T - Bm}" fill="transparent" id="payHit"/>`;
  const tip = $("payTip"), hit = svg.querySelector("#payHit");
  hit.onmousemove = (e) => {
    const r = svg.getBoundingClientRect(), px = ((e.clientX - r.left) / r.width) * W;
    const P = lo + ((px - L) / (W - L - R)) * (hi - lo), v = pnlAt(legs, net, qty, P);
    svg.querySelector("#payX").setAttribute("x1", px); svg.querySelector("#payX").setAttribute("x2", px);
    svg.querySelector("#payX").setAttribute("opacity", 0.5);
    const d = svg.querySelector("#payDot"); d.setAttribute("cx", px); d.setAttribute("cy", Y(v)); d.setAttribute("opacity", 1);
    tip.hidden = false;
    tip.innerHTML = `${S.sym} at ${n(P)} → <b class="${cls(v)}">${signed(v)}</b>`;
    tip.style.left = Math.min(r.width - 180, Math.max(0, (px / W) * r.width + 10)) + "px";
    tip.style.top = ((Y(v) / H) * r.height + 24) + "px";
  };
  hit.onmouseleave = () => { tip.hidden = true; svg.querySelector("#payX").setAttribute("opacity", 0); svg.querySelector("#payDot").setAttribute("opacity", 0); };
}
function niceTicks(lo, hi, count) {
  const span = hi - lo, raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(+v.toFixed(6));
  return out;
}
const fmtK = (v) => (Math.abs(v) >= 1000 ? `${v < 0 ? "-" : ""}$${(Math.abs(v) / 1000).toFixed(1)}k` : `${v < 0 ? "-" : ""}$${Math.abs(v).toFixed(0)}`);

function bHideConfirm() { $("bConfirm").hidden = true; $("bReview").hidden = false; }
function bMsg(text, ok) { const m = $("bMsg"); m.textContent = text; m.className = "msg " + (ok ? "ok" : "err"); m.hidden = false; }

async function submitStrategy() {
  const qty = Math.max(1, parseInt($("bQty").value) || 1);
  const nat = netOf(legNat), mid = netOf(legMid), net = bSignedNet(nat, mid);
  const btn = $("bSubmit"); btn.disabled = true;
  try {
    let o;
    if (B.legs.length === 1) {
      const l = B.legs[0];
      o = await api("/api/orders", { method: "POST", body: JSON.stringify({
        symbol: l.symbol, side: l.side, quantity: qty, order_type: B.type,
        limit_price: B.type === "LIMIT" ? Math.abs(net) : null, time_in_force: $("bTif").value }) });
    } else {
      o = await api("/api/multileg", { method: "POST", body: JSON.stringify({
        legs: B.legs.map((l) => ({ symbol: l.symbol, side: l.side, ratio: legRatio(l) })),
        quantity: qty, order_type: B.type, net_price: B.type === "LIMIT" ? net : null,
        time_in_force: $("bTif").value, strategy: B.t.name }) });
    }
    bMsg(o.status === "FILLED"
      ? `Filled ${B.t.name}: net ${o.net_fill !== null && o.net_fill !== undefined ? (o.net_fill >= 0 ? "debit " : "credit ") + n(Math.abs(o.net_fill)) : n(o.avg_fill_price)}`
      : `${B.t.name} order resting. It fills when the combined price reaches your limit.`, true);
    bHideConfirm(); refreshAccount();
  } catch (e) { bMsg(e.message, false); }
  finally { btn.disabled = false; }
}

function initStrategies() {
  renderStratGrid();
  $("stratGrid").addEventListener("click", (e) => { const c = e.target.closest(".scard"); if (c) openBuilder(c.dataset.s); });
  $("bBack").addEventListener("click", () => { $("builder").hidden = true; $("stratGrid").hidden = false; });
  $("bExp").addEventListener("change", (e) => { B.exp = e.target.value; loadBuilderChain(false); });
  $("bQty").addEventListener("input", () => { bHideConfirm(); renderBuilder(false); });
  $("bNet").addEventListener("input", () => { bHideConfirm(); bUpdate(); });
  $("bLegs").addEventListener("change", (e) => {
    const s = e.target.closest("select[data-leg]"); if (!s) return;
    B.legs[+s.dataset.leg].strike = parseFloat(s.value);
    refreshLegQuotes(); bHideConfirm(); renderBuilder(true);
  });
  $("bType").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    B.type = b.dataset.v;
    document.querySelectorAll("#bType button").forEach((x) => x.classList.toggle("on", x === b));
    bHideConfirm(); bUpdate();
  });
  $("bReview").addEventListener("click", () => {
    if (B.legs.some((l) => !l.symbol || !(l.bid > 0 || l.ask > 0))) return bMsg("Waiting for quotes on every leg…", false);
    const qty = Math.max(1, parseInt($("bQty").value) || 1);
    const nat = netOf(legNat), mid = netOf(legMid), net = bSignedNet(nat, mid);
    const a = analyze(B.legs, net, qty, B.spot || S.spot);
    $("bSummary").innerHTML = `<b>${qty} × ${esc(B.t.name)}</b> on ${esc(S.sym)}, ${esc($("bExp").selectedOptions[0]?.textContent || "")}<br>` +
      B.legs.map((l) => `${l.side === "BUY" ? "Buy" : "Sell"} ${legRatio(l) * qty} ${l.kind === "S" ? "shares" : `${n(l.strike, l.strike % 1 ? 2 : 0)} ${l.kind === "C" ? "call" : "put"}`}`).join(" · ") +
      `<br>${B.type === "MARKET" ? "Market" : `Limit ${net >= 0 ? "debit" : "credit"} ${n(Math.abs(net))}`} · ` +
      `max loss ${a.unlimitedLoss ? "unlimited" : money(-a.maxL)}`;
    $("bConfirm").hidden = false; $("bReview").hidden = true;
  });
  $("bEdit").addEventListener("click", bHideConfirm);
  $("bSubmit").addEventListener("click", submitStrategy);
  setInterval(() => { if (S.view === "trade" && !$("builder").hidden && !$("tab-strategies").hidden) loadBuilderChain(false); }, 15000);
}

// ------------------------------------------------------------------ analytics
let eqChart, eqSeries;
async function loadAnalytics() {
  let a; try { a = await api("/api/analytics"); } catch (e) { return; }
  const tile = (k, v, c = "") => `<div class="stat"><label>${k}</label><b class="${c}">${v}</b></div>`;
  const pct = (v) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${n(v)}%`);
  $("aTiles").innerHTML = [
    tile("Total return", pct(a.total_return_pct), cls(a.total_return_pct)),
    tile("Realized P&L", signed(a.realized_total), cls(a.realized_total)),
    tile("Closed trades", a.closed_trades),
    tile("Win rate", a.win_rate === null ? "–" : `${n(a.win_rate, 1)}%`),
    tile("Profit factor", a.profit_factor === null ? "–" : n(a.profit_factor)),
    tile("Avg win", a.avg_win === null ? "–" : signed(a.avg_win), "up"),
    tile("Avg loss", a.avg_loss === null ? "–" : signed(a.avg_loss), "down"),
    tile("Max drawdown", a.max_drawdown ? money(-a.max_drawdown) : "$0.00"),
  ].join("");
  $("aByTbl").querySelector("tbody").innerHTML = a.by_underlying.length ? a.by_underlying.map((r) => `<tr>
    <td>${esc(r.underlying)}</td><td class="r">${r.trades}</td><td class="r">${n((r.wins / r.trades) * 100, 0)}%</td>
    <td class="r ${cls(r.realized)}">${signed(r.realized)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">Close a trade to see results here.</td></tr>`;
  if (!eqChart) {
    eqChart = LightweightCharts.createChart($("eqChart"), {
      layout: { background: { color: "transparent" }, textColor: "#a7b3c2", fontFamily: "Inter, sans-serif" },
      grid: { vertLines: { visible: false }, horzLines: { color: "rgba(36,48,64,0.35)" } },
      rightPriceScale: { borderColor: "#243040" }, timeScale: { borderColor: "#243040", timeVisible: true },
      localization: { locale: "en-US" }, autoSize: true,
    });
    eqSeries = eqChart.addAreaSeries({ lineColor: "#4c9dff", topColor: "rgba(76,157,255,0.25)", bottomColor: "rgba(76,157,255,0.02)", lineWidth: 2 });
  }
  const seen = new Set();
  const pts = a.equity_curve.map((h) => ({ time: etSeconds(h.ts), value: h.equity }))
    .filter((p) => (seen.has(p.time) ? false : seen.add(p.time))).sort((x, y) => x.time - y.time);
  if (!pts.length) pts.push({ time: Math.floor(Date.now() / 1000), value: a.equity });
  eqSeries.setData(pts); eqChart.timeScale().fitContent();
}

initStrategies();
