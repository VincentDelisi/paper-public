"""SQLite storage for the paper account. One file = one paper account."""
from __future__ import annotations

import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, symbol TEXT, side TEXT,
    quantity REAL, order_type TEXT, limit_price REAL, time_in_force TEXT,
    status TEXT, filled_quantity REAL DEFAULT 0, avg_fill_price REAL, reason TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts TEXT, trade_date TEXT,
    symbol TEXT, side TEXT, quantity REAL, price REAL, fee REAL, realized_pnl REAL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, quantity REAL, avg_cost REAL, opened_at TEXT
);
CREATE TABLE IF NOT EXISTS day_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, symbol TEXT, order_id TEXT
);
CREATE TABLE IF NOT EXISTS equity_history (ts TEXT PRIMARY KEY, equity REAL);
CREATE TABLE IF NOT EXISTS agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT
);
"""

# Columns added after v0.1. Existing account files are upgraded in place.
MIGRATIONS = {
    "orders": {
        "legs": "TEXT", "order_class": "TEXT", "strategy": "TEXT", "stop_price": "REAL",
        "trigger_state": "TEXT", "parent_id": "TEXT", "oco_group": "TEXT",
        "take_profit": "REAL", "stop_loss": "REAL", "reserved": "REAL", "net_limit": "REAL",
        "net_fill": "REAL",
    },
    "fills": {"effect": "TEXT"},
}


class Ledger:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # meta
    def get(self, key: str, default: Any = None) -> Any:
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        r = self.db.execute(sql, params).fetchone()
        return dict(r) if r else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.db.execute(sql, params)

    def commit(self) -> None:
        self.db.commit()

    def wipe(self) -> None:
        for t in ("meta", "orders", "fills", "positions", "day_trades", "equity_history", "agent_log"):
            self.db.execute(f"DELETE FROM {t}")
        self.db.commit()
