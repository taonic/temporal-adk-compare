"""Mock business systems: a payment provider, an order system and an email sender.

Each call is written to SQLite, so the demo can show exactly how many times
each side effect really happened, independent of what either runtime believes.

The payment provider honours an `Idempotency-Key` header the way real ones do
(Stripe, Adyen and others): a repeated key returns the original refund instead
of moving money again. The repeat is still logged, marked as replayed.
"""

import asyncio
import sqlite3
import threading
import time
import uuid

from fastapi import FastAPI, Header
from pydantic import BaseModel

from .. import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS refunds (
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, run_id TEXT, order_id TEXT, amount_cents INTEGER,
  reason TEXT, refund_id TEXT, idempotency_key TEXT, replayed INTEGER);
CREATE TABLE IF NOT EXISTS order_refunds (
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, run_id TEXT, order_id TEXT, refund_id TEXT, amount_cents INTEGER);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, run_id TEXT, order_id TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY, ts REAL, side TEXT, run_id TEXT, order_id TEXT, amount_cents INTEGER, reason TEXT);
"""
TABLES = ("refunds", "order_refunds", "notifications", "escalations")


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def insert(self, table: str, **row) -> None:
        row.setdefault("ts", time.time())
        cols = ", ".join(row)
        with self._lock:
            self._db.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))

    def find_refund(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM refunds WHERE idempotency_key = ? AND replayed = 0", (key,)).fetchone()

    def rows(self, table: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(f"SELECT * FROM {table} ORDER BY id")]

    def reset(self) -> None:
        with self._lock:
            for table in TABLES:
                self._db.execute(f"DELETE FROM {table}")


class RefundIn(BaseModel):
    side: str
    run_id: str
    order_id: str
    amount_cents: int
    reason: str = ""


class OrderRefundIn(BaseModel):
    side: str
    run_id: str
    refund_id: str
    amount_cents: int


class NotificationIn(BaseModel):
    side: str
    run_id: str
    order_id: str
    message: str


class EscalationIn(BaseModel):
    side: str
    run_id: str
    order_id: str
    amount_cents: int
    reason: str


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store(config.BUSINESS_DB)
    app = FastAPI(title="Mock business systems")

    @app.post("/payments/refunds")
    async def refund(body: RefundIn, idempotency_key: str | None = Header(default=None)):
        await asyncio.sleep(config.BUSINESS_DELAY)
        if idempotency_key and (original := store.find_refund(idempotency_key)):
            store.insert("refunds", **body.model_dump(), refund_id=original["refund_id"],
                         idempotency_key=idempotency_key, replayed=1)
            return {"refund_id": original["refund_id"], "replayed": True}
        refund_id = f"re_{uuid.uuid4().hex[:8]}"
        store.insert("refunds", **body.model_dump(), refund_id=refund_id, idempotency_key=idempotency_key, replayed=0)
        return {"refund_id": refund_id, "replayed": False}

    @app.post("/orders/{order_id}/refunds")
    async def record(order_id: str, body: OrderRefundIn):
        await asyncio.sleep(config.BUSINESS_DELAY)
        store.insert("order_refunds", order_id=order_id, **body.model_dump())
        return {"recorded": True}

    @app.post("/notifications")
    async def notify(body: NotificationIn):
        await asyncio.sleep(config.BUSINESS_DELAY)
        store.insert("notifications", **body.model_dump())
        return {"sent": True}

    @app.post("/escalations")
    async def escalate(body: EscalationIn):
        await asyncio.sleep(config.BUSINESS_DELAY)
        store.insert("escalations", **body.model_dump())
        return {"ticket": f"MGR-{uuid.uuid4().hex[:5].upper()}"}

    @app.get("/state")
    async def state():
        return {table: store.rows(table) for table in TABLES}

    @app.post("/reset")
    async def reset():
        store.reset()
        return {"ok": True}

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=config.BUSINESS_PORT, log_level="warning")
