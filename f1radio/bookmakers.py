"""Bookmakers and betting exchanges: odds import, Betfair history, and a fixed-odds backtest.

Prediction markets have a history API; bookmakers do not. Three ways in:

* any bookmaker (Winline, Pinnacle, bet365, ...): a CSV of odds you captured
  yourself, imported with `import-odds`. Columns:

      utc, selection, odds            required (decimal, "5/2" or "+150"/"-200")
      bookmaker                       or pass --bookmaker
      lay_odds                        exchanges: best price to lay (odds = best to back)
      back_size, lay_size             exchanges: money at the best prices
      suspended                       1 while the market is closed to bets

  Rows can be one selection at a time; odds are carried forward until they change.
* Betfair Exchange history files (historicdata.betfair.com, .bz2 stream
  files), imported with `import-betfair`. ADVANCED/PRO files have best
  back/lay to the second; BASIC files only have last traded price per minute.
* Betfair live, with `record-betfair` (needs an app key and session token).

Odds become probabilities with the margin removed proportionally
(fair_i = (1/odds_i) / sum_j(1/odds_j)) whenever the captured field is
complete enough to carry a margin (sum >= 1). A partial field (only the top
five drivers, say) keeps the raw 1/odds.

A fixed-odds bookmaker is not a market you can trade in and out of:

* you can only back. Bad news for driver X means backing whoever gains
  most, here the next most likely winner;
* the bet is accepted after a delay (typically several seconds in-play), at
  the odds at that moment, and refused while the market is suspended;
* there is no exit at broadcast time. The backtest reports the expected
  value at the post-broadcast fair price, and the settled result.
"""
from __future__ import annotations

import bz2
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .markets import match_driver
from .study import _ns

log = logging.getLogger(__name__)

STANDARD = ["utc", "venue", "kind", "driver_number", "selection", "odds", "lay_odds", "back_size",
            "lay_size", "suspended", "implied", "yes_price", "bid", "ask"]


def parse_odds(v) -> float:
    """Decimal 3.5, fractional '5/2', or American '+150' / '-200' -> decimal odds."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    s = str(v).strip()
    if not s:
        return np.nan
    if "/" in s:
        num, den = s.split("/")
        return 1 + float(num) / float(den)
    if s[0] in "+-":  # an explicit sign means American odds
        a = float(s)
        return 1 + a / 100 if a > 0 else 1 + 100 / -a
    return float(s)


def normalise(raw: pd.DataFrame, venue: str, kind: str, drivers: pd.DataFrame) -> pd.DataFrame:
    """Raw odds rows -> STANDARD rows: one per (timestamp, driver), carried forward."""
    df = raw.copy()
    df["driver_number"] = [match_driver(str(s), drivers) for s in df["selection"]]
    missing = sorted(set(df.loc[df["driver_number"].isna(), "selection"].astype(str)))
    if missing:
        log.warning("%s: selections with no driver match (ignored): %s", venue, missing)
    df = df.dropna(subset=["driver_number"]).copy()
    df["driver_number"] = df["driver_number"].astype(int)
    for c in ("odds", "lay_odds"):
        df[c] = df[c].map(parse_odds) if c in df else np.nan
    for c in ("back_size", "lay_size"):
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
    df["suspended"] = (pd.to_numeric(df["suspended"], errors="coerce").fillna(0).astype(int)
                       if "suspended" in df else 0)
    df = df.sort_values("utc")

    # Carry every selection's last quote forward onto every timestamp, so the
    # margin is removed across the whole field at each instant.
    # A selection's row states its full quote: an empty price there means "nothing
    # on offer", not "unchanged". Only timestamps without a row are carried forward.
    index = pd.DatetimeIndex(sorted(df["utc"].unique()), name="utc")
    drivers_seen = sorted(df["driver_number"].unique())
    last = df.drop_duplicates(["utc", "driver_number"], keep="last")
    empty = -1.0  # sentinel for an explicit blank, so ffill does not paper over it

    def carry(c: str) -> pd.DataFrame:
        w = last.assign(**{c: last[c].fillna(empty)}).pivot(index="utc", columns="driver_number", values=c)
        w = w.reindex(index=index, columns=drivers_seen).ffill()
        return w.mask(w == empty)

    wide = {c: carry(c) for c in ("odds", "lay_odds", "back_size", "lay_size") if df[c].notna().any()}
    seen = last.pivot(index="utc", columns="driver_number", values="suspended").reindex(
        index=index, columns=drivers_seen).notna().cummax()  # rows after each selection first appears
    susp = df.groupby("utc")["suspended"].max().reindex(index).ffill().fillna(0).astype(int)
    back = wide["odds"]
    lay = wide.get("lay_odds")
    mid_odds = back if lay is None else back.where(lay.isna(), 2 / (1 / back + 1 / lay))
    implied = 1 / mid_odds
    total = implied.sum(axis=1)
    fair = implied.div(total.where(total >= 1, 1.0), axis=0)

    out = []
    for drv in back.columns:
        sel = df.loc[df["driver_number"] == drv, "selection"].iloc[0]
        part = pd.DataFrame({"utc": back.index, "venue": venue, "kind": kind, "driver_number": int(drv),
                             "selection": sel, "odds": back[drv].values,
                             "lay_odds": lay[drv].values if lay is not None else np.nan,
                             "suspended": susp.values,
                             "implied": implied[drv].values, "yes_price": fair[drv].values})
        for c in ("back_size", "lay_size"):
            part[c] = wide[c][drv].values if c in wide else np.nan
        if kind == "exchange" and lay is not None:
            part["ask"] = 1 / part["odds"]      # backing = buying YES at 1/back odds
            part["bid"] = 1 / part["lay_odds"]  # laying  = selling YES at 1/lay odds
        else:
            part["bid"] = part["ask"] = np.nan
        out.append(part[seen[drv].values])
    res = pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=STANDARD)
    return res[STANDARD].sort_values(["utc", "driver_number"]).reset_index(drop=True)


def read_odds_csv(path: Path, bookmaker: str | None, tz: str | None) -> pd.DataFrame:
    raw = pd.read_csv(path)
    ts_col = "utc" if "utc" in raw else "timestamp"
    ts = pd.to_datetime(raw[ts_col], format="ISO8601")
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(tz or "UTC")
    raw["utc"] = ts.dt.tz_convert("UTC").dt.as_unit("ns")
    if "bookmaker" not in raw:
        if not bookmaker:
            raise SystemExit("the CSV has no `bookmaker` column; pass --bookmaker")
        raw["bookmaker"] = bookmaker
    if "odds" not in raw and "back_odds" in raw:
        raw = raw.rename(columns={"back_odds": "odds"})
    return raw


# --------------------------------------------------------------------------- Betfair

def _best(levels: dict[int, tuple[float, float]]) -> tuple[float, float]:
    return levels[0] if 0 in levels else (np.nan, np.nan)


def read_betfair_stream(path: Path, market_id: str | None = None) -> pd.DataFrame:
    """Betfair historical stream file (.bz2 or plain) -> raw odds rows for one market."""
    opener = bz2.open if str(path).endswith(".bz2") else open
    defs: dict[str, dict] = {}
    settled: dict[str, str] = {}
    state: dict[tuple[str, int], dict] = {}
    rows = []
    with opener(path, "rt") as fh:
        for line in fh:
            msg = json.loads(line)
            if msg.get("op") != "mcm":
                continue
            utc = pd.Timestamp(msg["pt"], unit="ms", tz="UTC")
            for mc in msg.get("mc", []):
                mid = mc["id"]
                if "marketDefinition" in mc:
                    defs[mid] = mc["marketDefinition"]
                d = defs.get(mid, {})
                names = {r["id"]: r.get("name", str(r["id"])) for r in d.get("runners", [])}
                for r in d.get("runners", []):
                    if r.get("status") == "WINNER":
                        settled[mid] = names[r["id"]]
                suspended = int(d.get("status", "OPEN") != "OPEN")
                touched = {rc["id"] for rc in mc.get("rc", [])}
                if "marketDefinition" in mc:  # status may have changed for everyone
                    touched |= {sid for (m, sid) in state if m == mid}
                for rc in mc.get("rc", []):
                    st = state.setdefault((mid, rc["id"]), {"ltp": np.nan, "b": {}, "l": {}, "atb": {}, "atl": {}})
                    if "ltp" in rc:
                        st["ltp"] = rc["ltp"]
                    for key, book in (("batb", "b"), ("batl", "l")):
                        for lvl, price, size in rc.get(key, []):
                            if size == 0:
                                st[book].pop(lvl, None)
                            else:
                                st[book][lvl] = (price, size)
                    for key in ("atb", "atl"):  # PRO files: full ladder [price, size]
                        for price, size in rc.get(key, []):
                            if size == 0:
                                st[key].pop(price, None)
                            else:
                                st[key][price] = size
                for sid in touched:
                    st = state.get((mid, sid))
                    if st is None:
                        continue
                    (bp, bs), (lp, ls) = _best(st["b"]), _best(st["l"])
                    if np.isnan(bp) and st["atb"]:
                        bp = max(st["atb"])
                        bs = st["atb"][bp]
                    if np.isnan(lp) and st["atl"]:
                        lp = min(st["atl"])
                        ls = st["atl"][lp]
                    rows.append({"market_id": mid, "market_name": d.get("name", ""), "utc": utc,
                                 "selection": names.get(sid, str(sid)),
                                 "odds": bp if not np.isnan(bp) else st["ltp"], "lay_odds": lp,
                                 "back_size": bs, "lay_size": ls, "ltp": st["ltp"], "suspended": suspended,
                                 "in_play": int(bool(d.get("inPlay")))})
    df = pd.DataFrame(rows)
    if df.empty:
        raise ValueError(f"no market data in {path}")
    markets = df.groupby("market_id")["market_name"].first()
    if market_id is None:
        winner_markets = markets[markets.str.contains("winner", case=False)]
        if len(markets) == 1:
            market_id = markets.index[0]
        elif len(winner_markets) == 1:
            market_id = winner_markets.index[0]
        else:
            raise SystemExit(f"several markets in the file, pass --market-id: {markets.to_dict()}")
    df = df[df["market_id"] == market_id].copy()
    df.attrs["winner"] = settled.get(market_id)  # settled runner, if the file runs to settlement
    df["bookmaker"] = "betfair"
    df["utc"] = df["utc"].dt.as_unit("ns")
    return df


BETFAIR_API = "https://api.betfair.com/exchange/betting/rest/v1.0/"


def record_betfair(market_id: str, out: Path, app_key: str, session_token: str, interval_s: float = 1.0,
                   api: str = BETFAIR_API) -> None:
    """Poll listMarketBook and append raw odds rows (import them later with import-odds)."""
    import requests

    hdr = {"X-Application": app_key, "X-Authentication": session_token, "Content-Type": "application/json"}
    cat = requests.post(api + "listMarketCatalogue/", headers=hdr, timeout=20, json={
        "filter": {"marketIds": [market_id]}, "marketProjection": ["RUNNER_DESCRIPTION"], "maxResults": 1}).json()
    names = {r["selectionId"]: r["runnerName"] for r in cat[0]["runners"]}
    out.parent.mkdir(parents=True, exist_ok=True)
    new = not out.exists()
    with out.open("a") as fh:
        if new:
            fh.write("utc,bookmaker,selection,odds,lay_odds,back_size,lay_size,suspended\n")
        log.info("recording Betfair %s to %s, Ctrl-C to stop", market_id, out)
        try:
            while True:
                t = time.monotonic()
                try:
                    books = requests.post(api + "listMarketBook/", headers=hdr, timeout=10, json={
                        "marketIds": [market_id], "priceProjection": {"priceData": ["EX_BEST_OFFERS"]}}).json()
                    now = pd.Timestamp.now(tz="UTC").isoformat()
                    for b in books:
                        susp = int(b.get("status") != "OPEN")
                        for r in b.get("runners", []):
                            atb = (r.get("ex", {}).get("availableToBack") or [{}])[0]
                            atl = (r.get("ex", {}).get("availableToLay") or [{}])[0]
                            name = names.get(r["selectionId"], r["selectionId"])
                            fh.write(f'{now},betfair,"{name}",{atb.get("price", "")},{atl.get("price", "")},'
                                     f'{atb.get("size", "")},{atl.get("size", "")},{susp}\n')
                    fh.flush()
                except Exception as exc:  # noqa: BLE001 - keep recording through blips
                    log.warning("betfair poll failed: %s", exc)
                time.sleep(max(0.0, interval_s - (time.monotonic() - t)))
        except KeyboardInterrupt:
            pass


# --------------------------------------------------------------------------- fixed-odds backtest

@dataclass
class BookieParams:
    reaction_s: float = 5.0
    acceptance_delay_s: float = 7.0  # in-play bet delay; varies by bookmaker and sport
    hold_s: float = 30.0             # when to value the bet after the broadcast
    stake: float = 100.0
    max_stake: float = 100.0         # bookmakers cap in-play stakes, often far below this for winners


class OddsBook:
    """As-of odds, fair probability and suspension per (venue, driver)."""

    def __init__(self, odds: pd.DataFrame):
        self.s = {(v, int(d)): g.sort_values("utc").reset_index(drop=True)
                  for (v, d), g in odds.groupby(["venue", "driver_number"])}

    def at(self, venue: str, drv: int, t: pd.Timestamp) -> dict | None:
        g = self.s.get((venue, drv))
        if g is None or pd.isna(t):
            return None
        i = g["utc"].searchsorted(_ns(t), side="right") - 1
        return g.iloc[i].to_dict() if i >= 0 else None

    def field(self, venue: str, t: pd.Timestamp) -> dict[int, dict]:
        return {d: q for (v, d) in self.s if v == venue and (q := self.at(v, d, t))}


def backtest(es: pd.DataFrame, odds: pd.DataFrame, winner: int | None, prm: BookieParams = BookieParams()
             ) -> pd.DataFrame:
    """One bet per (directional message, bookmaker)."""
    ob = OddsBook(odds)
    venues = sorted(odds["venue"].unique()) if not odds.empty else []
    events = es[es["direction"] != 0].drop_duplicates("audio_file")
    rows = []
    for _, e in events.iterrows():
        target = int(e["target_driver"])
        t_req = e["t_avail"] + pd.Timedelta(seconds=prm.reaction_s)
        t_acc = t_req + pd.Timedelta(seconds=prm.acceptance_delay_s)
        t_val = e["broadcast_utc"] + pd.Timedelta(seconds=prm.hold_s)
        for v in venues:
            field = ob.field(v, t_req)
            if not field:
                continue
            if e["direction"] > 0:
                pick = target
            else:  # back the most likely winner other than the driver with bad news
                others = {d: q for d, q in field.items() if d != target}
                if not others:
                    continue
                pick = max(others, key=lambda d: others[d]["yes_price"])
            q_req, q_acc, q_val = ob.at(v, pick, t_req), ob.at(v, pick, t_acc), ob.at(v, pick, t_val)
            row = {"venue": v, "audio_file": e["audio_file"], "category": e["category"], "target_driver": target,
                   "direction": int(e["direction"]), "backed": pick, "t_request": t_req, "t_accept": t_acc,
                   "odds_request": q_req["odds"] if q_req else np.nan, "status": "accepted",
                   "confounders": e.get("confounders", "")}
            if not q_req or not q_acc or pd.isna(q_acc["odds"]):
                row["status"] = "no_odds"
            elif q_req["suspended"] or q_acc["suspended"]:
                row["status"] = "suspended"
            elif t_acc >= e["broadcast_utc"]:
                row["status"] = "accepted_after_broadcast"
            if row["status"] in ("accepted", "accepted_after_broadcast"):
                stake = min(prm.stake, prm.max_stake)
                o = q_acc["odds"]
                fair_val = q_val["yes_price"] if q_val else np.nan
                row |= {"odds_taken": o, "stake": stake, "fair_p_at_bet": q_acc["yes_price"],
                        "fair_p_after_broadcast": fair_val,
                        # value of the ticket at the fair price once everyone knows
                        "ev_after_broadcast": stake * (o * fair_val - 1),
                        "settled_pnl": (stake * (o - 1) if pick == winner else -stake) if winner else np.nan}
            rows.append(row)
    return pd.DataFrame(rows)


def summarise(bt: pd.DataFrame) -> pd.DataFrame:
    if bt.empty:
        return pd.DataFrame()
    out = []
    for v, g in bt.groupby("venue"):
        acc = g[g["status"] == "accepted"]
        out.append({"venue": v, "signals": len(g), "accepted": len(acc),
                    "suspended": int((g["status"] == "suspended").sum()),
                    "too_late": int((g["status"] == "accepted_after_broadcast").sum()),
                    "mean_odds_drift": (acc["odds_taken"] / acc["odds_request"] - 1).mean() if len(acc) else np.nan,
                    "total_staked": acc["stake"].sum() if len(acc) else 0.0,
                    "ev_after_broadcast": acc["ev_after_broadcast"].sum() if len(acc) else 0.0,
                    "settled_pnl": acc["settled_pnl"].sum(min_count=1) if len(acc) else np.nan})
    return pd.DataFrame(out)
