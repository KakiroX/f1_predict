"""Trade every directional radio message and count what is left after costs.

Rule: at t_avail + reaction, buy YES on the target driver for good news or
buy NO for bad news; sell at broadcast_utc + hold. Execution is the weak
spot of any historical test here:

* Kalshi 1-minute candles carry bid/ask, so we cross the spread with them
  (at minute resolution, which flatters or punishes fills at random).
* Polymarket has no historical order book, so we pay last trade +/- a
  half-spread assumption (`poly_half_spread`). Live recordings from
  recorder.py carry real bid/ask for both venues and replace both guesses.
* Size is capped at a share of what actually traded in the window, since a
  thin in-race book is the real limit on this strategy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .study import PriceBook


@dataclass
class Params:
    reaction_s: float = 5.0          # hear the clip, decide, click
    hold_s: float = 30.0             # exit this long after the broadcast
    default_delay_s: float = 120.0   # used when broadcast time is unknown
    stake: float = 500.0             # $ per signal before the capacity cap
    participation: float = 0.25      # max share of window volume we assume we could take
    poly_half_spread: float = 0.01
    poly_fee_rate: float = 0.0       # taker fee as a share of notional; check the market's fee schedule
    kalshi_fee_coef: float = 0.07    # Kalshi taker fee: ceil(0.07 * C * P * (1-P)) in cents per order
    min_price: float = 0.03          # skip near-certain markets: nothing left to move
    max_price: float = 0.97


def kalshi_fee(contracts: float, price: float, coef: float = 0.07) -> float:
    return math.ceil(coef * contracts * price * (1 - price) * 100 - 1e-9) / 100


def _fill(book: PriceBook, key, t, buy_yes: bool, opening: bool, prm: Params) -> float:
    """Price per contract of the side we hold (YES, or NO = 1 - YES)."""
    bid, ask = book.quote(key, t)
    if not (np.isnan(bid) or np.isnan(ask)):
        yes = ask if opening == buy_yes else bid  # buy YES / close NO at ask; else at bid
    else:
        last = book.price(key, t)
        hs = prm.poly_half_spread if key[0] == "polymarket" else 0.02
        yes = last + hs if opening == buy_yes else last - hs
    yes = min(max(yes, 0.0), 1.0)
    return yes if buy_yes else 1 - yes


def _fee(venue: str, contracts: float, px: float, prm: Params) -> float:
    if venue == "kalshi":
        return kalshi_fee(contracts, px, prm.kalshi_fee_coef)
    return prm.poly_fee_rate * contracts * px


def run(es: pd.DataFrame, book: PriceBook, prm: Params = Params()) -> pd.DataFrame:
    trades = []
    for _, e in es[es["direction"] != 0].iterrows():
        key = (e["venue"], int(e["target_driver"]))
        mid = e["p_avail"]
        if np.isnan(mid) or not (prm.min_price <= mid <= prm.max_price):
            continue
        buy_yes = e["direction"] > 0
        t_in = e["t_avail"] + pd.Timedelta(seconds=prm.reaction_s)
        t_out = e["broadcast_utc"] + pd.Timedelta(seconds=prm.hold_s)
        if t_out <= t_in:
            continue  # aired before we could react: no edge by construction
        px_in = _fill(book, key, t_in, buy_yes, True, prm)
        px_out = _fill(book, key, t_out, buy_yes, False, prm)
        if np.isnan(px_in) or np.isnan(px_out) or px_in <= 0:
            continue
        window_contracts, _, n = book.volume(key, t_in, t_out + pd.Timedelta(seconds=60))
        cap = prm.participation * window_contracts if not np.isnan(window_contracts) else 0.0
        contracts = math.floor(min(prm.stake / px_in, cap))
        fees = _fee(key[0], contracts, px_in, prm) + _fee(key[0], contracts, px_out, prm) if contracts else 0.0
        trades.append({
            "venue": key[0], "audio_file": e["audio_file"], "target_driver": key[1],
            "category": e["category"], "side": "YES" if buy_yes else "NO",
            "t_in": t_in, "t_out": t_out, "px_in": px_in, "px_out": px_out,
            "edge_per_contract": px_out - px_in, "capacity_contracts": cap, "contracts": contracts,
            "fees": fees, "pnl": contracts * (px_out - px_in) - fees, "window_trades": n,
            "confounders": e["confounders"],
        })
    return pd.DataFrame(trades)


def summarise(bt: pd.DataFrame) -> pd.DataFrame:
    if bt.empty:
        return pd.DataFrame()
    g = bt.groupby("venue")
    return pd.DataFrame({
        "signals": g.size(),
        "filled": g["contracts"].apply(lambda c: int((c > 0).sum())),
        "mean_edge_per_contract": g["edge_per_contract"].mean(),
        "hit_rate": g["edge_per_contract"].apply(lambda x: float((x > 0).mean())),
        "total_contracts": g["contracts"].sum(),
        "total_fees": g["fees"].sum(),
        "total_pnl": g["pnl"].sum(),
        "pnl_ex_confounded": bt[bt["confounders"] == ""].groupby("venue")["pnl"].sum(),
    }).fillna({"pnl_ex_confounded": 0.0}).reset_index()
