"""Historical prices and trades for race-winner markets on Polymarket and Kalshi.

Both venues list one binary market per driver ("Will X win the Singapore GP?").
Everything here is normalised to the YES price in [0, 1] with UTC timestamps,
so the two venues can be compared on one axis.

Polymarket (no auth needed):
  gamma-api   event / market metadata (slug, conditionId, clobTokenIds)
  data-api    individual trades, second resolution
  clob        /prices-history, 1-minute resolution
Kalshi (no auth needed for market data):
  /events?series_ticker=KXF1RACE     race-winner events, one market per driver
  /markets/trades                    individual trades
  /series/../candlesticks            1-minute OHLC of bid, ask and last price
"""
from __future__ import annotations

import json
import logging
import unicodedata

import pandas as pd

from .http import get_json

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXF1RACE"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().strip()


def match_driver(label: str, drivers: pd.DataFrame) -> int | None:
    """Map a market label ("Max Verstappen", "VER", "Kimi Antonelli") to a car number."""
    lab = _norm(label)
    for _, d in drivers.iterrows():
        if lab == _norm(d["tla"]) or _norm(d["last_name"]) in lab.split() or lab.endswith(_norm(d["last_name"])):
            return int(d["driver_number"])
    return None


def _ts(t: pd.Timestamp) -> int:
    return int(t.timestamp())


# --------------------------------------------------------------------------- Polymarket

def polymarket_event(query: str | None = None, year: int | None = None, slug: str | None = None) -> dict:
    """Find the race-winner event by slug (most reliable) or by text search."""
    if slug:
        events = get_json(f"{GAMMA}/events", {"slug": slug})
        if events:
            return events[0]
        raise LookupError(f"no Polymarket event with slug {slug!r}")
    res = get_json(f"{GAMMA}/public-search", {"q": query, "events_status": "closed", "limit_per_type": 50})
    q = [_norm(w) for w in query.split()]
    for ev in res.get("events", []):
        title = _norm(ev.get("title", ""))
        same_year = year is None or str(year) in (ev.get("endDate", "") + ev.get("title", "") + ev["slug"])
        if all(w in title for w in q) and "winner" in title and same_year:
            # Search results are abbreviated; refetch for the full market list.
            return get_json(f"{GAMMA}/events", {"slug": ev["slug"]})[0]
    raise LookupError(f"no Polymarket winner event matching {query!r}; pass --poly-slug")


def polymarket_markets(event: dict, drivers: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in event.get("markets", []):
        tokens = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds")
        outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes")
        if not tokens:
            continue
        label = m.get("groupItemTitle") or m.get("question", "")
        yes_idx = [o.lower() for o in outcomes].index("yes") if outcomes else 0
        rows.append({"venue": "polymarket", "label": label, "driver_number": match_driver(label, drivers),
                     "market_id": m["conditionId"], "yes_token": tokens[yes_idx],
                     "question": m.get("question")})
    df = pd.DataFrame(rows)
    unmatched = df[df["driver_number"].isna()]["label"].tolist() if not df.empty else []
    if unmatched:
        log.warning("Polymarket markets with no driver match (ignored): %s", unmatched)
    return df


def polymarket_trades(condition_id: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """All taker trades in [start, end]. The API returns newest first, offset-paginated."""
    rows, offset, limit = [], 0, 500
    while True:
        page = get_json(f"{DATA}/trades", {"market": condition_id, "limit": limit, "offset": offset})
        if not page:
            break
        rows.extend(page)
        if page[-1]["timestamp"] < _ts(start):
            break
        offset += limit
        if offset > 10_000:
            log.warning("Polymarket trade pagination capped for %s; early trades may be missing", condition_id)
            break
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df[(df["utc"] >= start) & (df["utc"] <= end)].copy()
    is_no = df["outcome"].str.lower().eq("no")
    df["yes_price"] = df["price"].astype(float).where(~is_no, 1 - df["price"].astype(float))
    df["size"] = df["size"].astype(float)
    df["notional"] = df["size"] * df["price"].astype(float)
    return df[["utc", "yes_price", "size", "notional", "side", "outcome"]].sort_values("utc")


def polymarket_history(yes_token: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    res = get_json(f"{CLOB}/prices-history",
                   {"market": yes_token, "startTs": _ts(start), "endTs": _ts(end), "fidelity": 1})
    df = pd.DataFrame(res.get("history", []))
    if df.empty:
        return df
    return pd.DataFrame({"utc": pd.to_datetime(df["t"], unit="s", utc=True), "yes_price": df["p"].astype(float)})


# --------------------------------------------------------------------------- Kalshi

def kalshi_event(query: str, year: int, event_ticker: str | None = None, series: str = KALSHI_SERIES) -> dict:
    if event_ticker:
        res = get_json(f"{KALSHI}/events/{event_ticker}", {"with_nested_markets": "true"})
        ev = res["event"]
        ev.setdefault("markets", res.get("markets", []))
        return ev
    cursor, yy = None, str(year)[2:]
    q = _norm(query)
    while True:
        params = {"series_ticker": series, "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        res = get_json(f"{KALSHI}/events", params)
        for ev in res.get("events", []):
            if q in _norm(ev.get("title", "")) and yy in ev["event_ticker"]:
                return ev
        cursor = res.get("cursor")
        if not cursor:
            raise LookupError(f"no Kalshi {series} event matching {query!r} {year}; pass --kalshi-event")


def kalshi_markets(event: dict, drivers: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for m in event.get("markets", []):
        label = m.get("yes_sub_title") or m.get("subtitle") or m["ticker"].rsplit("-", 1)[-1]
        num = match_driver(label, drivers) or match_driver(m["ticker"].rsplit("-", 1)[-1], drivers)
        rows.append({"venue": "kalshi", "label": label, "driver_number": num, "market_id": m["ticker"],
                     "series": event.get("series_ticker", KALSHI_SERIES), "question": m.get("title")})
    return pd.DataFrame(rows)


def _dollars(row: dict, key: str) -> float:
    if f"{key}_dollars" in row and row[f"{key}_dollars"] is not None:
        return float(row[f"{key}_dollars"])
    return float(row[key]) / 100.0


def kalshi_trades(ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for path in ("/markets/trades", "/historical/trades"):  # settled markets may be archived
        cursor = None
        while True:
            params = {"ticker": ticker, "min_ts": _ts(start), "max_ts": _ts(end), "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                res = get_json(KALSHI + path, params)
            except Exception as exc:  # noqa: BLE001 - the archive endpoint may not exist
                log.debug("%s unavailable: %s", path, exc)
                break
            rows.extend(res.get("trades", []))
            cursor = res.get("cursor")
            if not cursor:
                break
        if rows:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame({
        "utc": pd.to_datetime([r["created_time"] for r in rows], utc=True, format="ISO8601"),
        "yes_price": [_dollars(r, "yes_price") for r in rows],
        "size": [float(r.get("count_fp") or r.get("count") or 0) for r in rows],
        "side": [r.get("taker_side") for r in rows],
    })
    df["notional"] = df["size"] * df["yes_price"]
    return df.sort_values("utc")


def kalshi_candles(series: str, ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    res = get_json(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
                   {"start_ts": _ts(start), "end_ts": _ts(end), "period_interval": 1})
    rows = []
    for c in res.get("candlesticks", []):
        def close(side):
            d = c.get(side) or {}
            v = d.get("close_dollars", d.get("close"))
            if v is None:
                return None
            return float(v) if "close_dollars" in d else float(v) / 100.0
        rows.append({"utc": pd.to_datetime(c["end_period_ts"], unit="s", utc=True),
                     "bid": close("yes_bid"), "ask": close("yes_ask"), "last": close("price"),
                     "volume": c.get("volume")})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- both

def fetch_all(query: str, year: int, drivers: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
              poly_slug: str | None = None, kalshi_event_ticker: str | None = None) -> dict[str, pd.DataFrame]:
    """Fetch everything for one race. A venue that fails is logged and skipped."""
    markets, trades, minute = [], [], []

    try:
        ev = polymarket_event(f"{query} grand prix", year, slug=poly_slug)
        pm = polymarket_markets(ev, drivers).dropna(subset=["driver_number"])
        markets.append(pm)
        for _, m in pm.iterrows():
            t = polymarket_trades(m["market_id"], start, end)
            if not t.empty:
                trades.append(t.assign(venue="polymarket", driver_number=m["driver_number"]))
            h = polymarket_history(m["yes_token"], start, end)
            if not h.empty:
                minute.append(h.assign(venue="polymarket", driver_number=m["driver_number"]))
    except Exception as exc:  # noqa: BLE001
        log.error("Polymarket fetch failed: %s", exc)

    try:
        ev = kalshi_event(query, year, kalshi_event_ticker)
        km = kalshi_markets(ev, drivers).dropna(subset=["driver_number"])
        markets.append(km)
        for _, m in km.iterrows():
            t = kalshi_trades(m["market_id"], start, end)
            if not t.empty:
                trades.append(t.assign(venue="kalshi", driver_number=m["driver_number"]))
            c = kalshi_candles(m["series"], m["market_id"], start, end)
            if not c.empty:
                minute.append(c.assign(venue="kalshi", driver_number=m["driver_number"],
                                       yes_price=c["last"]))
    except Exception as exc:  # noqa: BLE001
        log.error("Kalshi fetch failed: %s", exc)

    cat = lambda xs: pd.concat(xs, ignore_index=True) if xs else pd.DataFrame()  # noqa: E731
    return {"markets": cat(markets), "trades": cat(trades), "minute": cat(minute)}
