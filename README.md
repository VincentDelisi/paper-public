# Shadow Book

Paper trading on real Public.com market data. (Python package: `paper_public`.)

**Unofficial** paper trading for the Public.com API. Live market data from
Public; orders, fills and positions are simulated locally. Nothing is ever
sent to Public's order endpoints. Not affiliated with or endorsed by Public.com.

## How fills work (conservative on purpose)
- Market BUY fills at the **ask**, market SELL at the **bid** (plus optional slippage).
- Limit orders rest and fill only when the live quote reaches your price.
- Options: 100x multiplier, whole contracts, expired contracts settle to intrinsic value in cash.
- Rules enforced: buying power (resting buys reserve cash), no overselling,
  PDT (blocks new opens after 3 day trades in 5 business days under $25k),
  optional daily loss limit, DAY orders expire at 4:00pm ET.
- Long and short: buy/sell to open or close, short stock, sell options to open.
- Multi-leg orders (2-4 legs) fill all-or-none at a net debit/credit limit.
- Order types: market, limit, stop, stop-limit, and brackets (take-profit + stop-loss, one-cancels-other).
- Buying power is risk-based: cash-secured puts reserve strike x 100, credit spreads reserve the width,
  covered calls need nothing extra, short stock reserves 150%, naked calls are stress-tested at +50%.

## Setup (Windows PowerShell)
    py -m venv venv
    venv\Scripts\activate
    pip install -r requirements.txt
    # put PUBLIC_API_KEY=... in a file named .env

## Web app

Portfolio home (balance, account value chart, positions grouped by strategy with one-click close,
watchlist, market strip, recent activity) and a Trade screen per ticker.

## Trading screen
    python -m paper_public.server           # live Public data, opens http://127.0.0.1:8000
    python -m paper_public.server --demo    # synthetic prices, no API key, market always open

Strategy builder with Public-style cards (long call/put, covered call, cash-secured put,
covered/protective put, straddle, strangle, the four verticals, synthetic long/short): pick
expiration and strikes, see max profit/loss, breakevens, buying power and the payoff chart, then
place it as one order. Analytics tab: equity curve, win rate, profit factor, drawdown, P&L by ticker.

Search any ticker, TradingView chart, options chain with IV and
Greeks (Black-Scholes from the live bid/ask), order ticket with review/confirm,
positions with one-click close, open orders with cancel, fill history, account
summary and reset. Runs on your computer only (bound to 127.0.0.1).

## Command line
    python -m paper_public account
    python -m paper_public quote QQQ
    python -m paper_public buy QQQ 10
    python -m paper_public buy QQQ261002C00740000 1 --limit 3.40
    python -m paper_public positions
    python -m paper_public process        # fill resting limits / expire / settle
    python -m paper_public reset --cash 25000

## In code (what an agent uses)
    from paper_public import PaperBroker, PublicMarketData
    broker = PaperBroker(PublicMarketData(), starting_cash=25_000, daily_loss_limit=500)
    broker.place_order("QQQ", "BUY", 10)                       # market
    broker.place_order("QQQ", "BUY", 10, "LIMIT", 739.50)      # limit
    broker.process()                                           # call on a timer
    broker.account(); broker.positions()

## Agents (sample bots)

Open **Agents** in the top bar. Four demo agents each trade their own $25,000
paper book (files in `agent_books/`, or `demo_agent_books/` in demo mode), so
their results never touch your account or each other's:

| Agent | What it shows off |
|---|---|
| Dip Buyer | Market orders on SPY shares, take-profit and stop exits |
| Momentum Call Scalper | QQQ call bought with a bracket (TP +25%, SL -20%, OCO) |
| Put Credit Spread Seller | Multi-leg limit at the mid, walked toward the bid in 3 steps |
| Covered Call Writer | 100 SOFI + short call as one order, buyback at 80%, re-sell |

They start **paused** every time the server starts. Pausing stops new
decisions; open positions and bracket orders stay in place. "Close all"
cancels the agent's orders and closes its positions; "Reset book" wipes it
back to $25,000. Every decision is logged in plain English, and the live feed
shows all agents together. In demo mode the time-of-day rules are ignored so
they trade at any hour. These are demonstrations of execution, not strategies
with an edge.

Write your own: subclass `paper_public.agents.Agent`, implement
`tick(ctx)` (use `ctx.price`, `ctx.chain`, `ctx.broker.place_order`,
`ctx.log`), and add it to `default_agents()` in `agents/demos.py`.

## Tests
    python -m pytest -q
