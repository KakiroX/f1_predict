"""Record order books live during a race, with local receive timestamps.

History is the weak part of a backtest: Polymarket keeps no historical book
and Kalshi keeps only 1-minute candles. Run this during the next races
(alongside a recording of the live broadcast you actually watch) to get
second-level bid/ask on both venues:

    python -m f1radio record --poly-slug <event-slug> --kalshi-event KXF1RACE-USGP26 --out data/live/austin26

Polymarket: public market websocket (book, price_change, last_trade_price).
Kalshi:     the websocket needs an API key, so we poll the public
            /markets?event_ticker= endpoint, one request per second for the
            whole field.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import pandas as pd

from .http import SESSION
from .markets import GAMMA, KALSHI

log = logging.getLogger(__name__)

POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _write(fh, payload) -> None:
    fh.write(json.dumps({"recv_ns": time.time_ns(), "msg": payload}) + "\n")
    fh.flush()


async def _poly(tokens: list[str], path: Path) -> None:
    import websockets  # optional dependency, only needed for live recording

    with path.open("a") as fh:
        while True:
            try:
                async with websockets.connect(POLY_WS, ping_interval=None) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))

                    async def keepalive():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")

                    ka = asyncio.create_task(keepalive())
                    try:
                        async for raw in ws:
                            if raw != "PONG":
                                _write(fh, json.loads(raw))
                    finally:
                        ka.cancel()
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                log.warning("polymarket ws dropped (%s); reconnecting", exc)
                await asyncio.sleep(1)


async def _kalshi(event_ticker: str, path: Path, interval_s: float) -> None:
    with path.open("a") as fh:
        while True:
            t = time.monotonic()
            try:
                res = await asyncio.to_thread(
                    lambda: SESSION.get(f"{KALSHI}/markets", params={"event_ticker": event_ticker, "limit": 100},
                                        timeout=10).json())
                _write(fh, [{k: m.get(k) for k in ("ticker", "yes_bid", "yes_ask", "yes_bid_dollars",
                                                     "yes_ask_dollars", "last_price", "last_price_dollars",
                                                     "volume", "volume_fp")} for m in res.get("markets", [])])
            except Exception as exc:  # noqa: BLE001
                log.warning("kalshi poll failed: %s", exc)
            await asyncio.sleep(max(0.0, interval_s - (time.monotonic() - t)))


def record(out: Path, poly_slug: str | None, kalshi_event: str | None, interval_s: float = 1.0) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tasks = []
    if poly_slug:
        ev = SESSION.get(f"{GAMMA}/events", params={"slug": poly_slug}, timeout=30).json()[0]
        tokens, labels = [], {}
        for m in ev["markets"]:
            ids = json.loads(m["clobTokenIds"])
            tokens += ids
            labels[ids[0]] = m.get("groupItemTitle")  # YES token
        (out / "poly_tokens.json").write_text(json.dumps(labels, indent=1))
        tasks.append(_poly(tokens, out / "poly.jsonl"))
    if kalshi_event:
        tasks.append(_kalshi(kalshi_event, out / "kalshi.jsonl", interval_s))
    if not tasks:
        raise SystemExit("nothing to record: pass --poly-slug and/or --kalshi-event")

    async def main():
        await asyncio.gather(*tasks)

    log.info("recording to %s, Ctrl-C to stop", out)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def load(out: Path, drivers: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recordings -> (trades, quotes) in the same shape as markets.fetch_all."""
    from .markets import match_driver

    trades, quotes = [], []
    poly = out / "poly.jsonl"
    if poly.exists():
        yes_labels = json.loads((out / "poly_tokens.json").read_text())
        num = {tok: match_driver(lab or "", drivers) for tok, lab in yes_labels.items()}
        for line in poly.open():
            rec = json.loads(line)
            utc = pd.Timestamp(rec["recv_ns"], unit="ns", tz="UTC")
            for m in rec["msg"] if isinstance(rec["msg"], list) else [rec["msg"]]:
                if m.get("event_type") == "last_trade_price" and m.get("asset_id") in num:
                    trades.append({"venue": "polymarket", "driver_number": num[m["asset_id"]], "utc": utc,
                                   "yes_price": float(m["price"]), "size": float(m.get("size", 0)),
                                   "notional": float(m["price"]) * float(m.get("size", 0))})
                for pc in m.get("price_changes", []):
                    if pc.get("asset_id") in num and pc.get("best_bid") is not None:
                        quotes.append({"venue": "polymarket", "driver_number": num[pc["asset_id"]], "utc": utc,
                                       "bid": float(pc["best_bid"]), "ask": float(pc["best_ask"])})
    kal = out / "kalshi.jsonl"
    if kal.exists():
        for line in kal.open():
            rec = json.loads(line)
            utc = pd.Timestamp(rec["recv_ns"], unit="ns", tz="UTC")
            for m in rec["msg"]:
                drv = match_driver(m["ticker"].rsplit("-", 1)[-1], drivers)

                def d(k):
                    v = m.get(f"{k}_dollars")
                    return float(v) if v is not None else (m.get(k) or 0) / 100

                if drv is not None:
                    quotes.append({"venue": "kalshi", "driver_number": drv, "utc": utc,
                                   "bid": d("yes_bid"), "ask": d("yes_ask"), "yes_price": d("last_price")})
    return pd.DataFrame(trades), pd.DataFrame(quotes)
