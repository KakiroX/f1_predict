"""Event study: how do winner-market prices move around each radio message?

For every message we take three instants

    capture_utc    the clip was recorded (when the driver/engineer spoke)
    feed_utc       the clip was published on the F1 live-timing feed
    broadcast_utc  the clip was aired on TV (from broadcast.align or by hand)

and read the target driver's YES price on each venue at, before and after
them. The question is whether the price moves in the direction the message
implies *between* the moment you could know (t_avail) and the moment every
TV viewer knows (broadcast_utc), and whether that move survives spread and
fees (see backtest.py).

Moves are signed by the message direction, so a positive number always means
"the market moved the way the message predicted".
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS_S = (-120, -60, -30, 0, 15, 30, 60, 120, 180, 300)


def _ns(t):
    """pandas refuses to searchsorted a ns Timestamp into a s-resolution column; pin everything to ns."""
    return t.as_unit("ns") if isinstance(t, pd.Timestamp) else t


class PriceBook:
    """As-of lookups on per-(venue, driver) price series built from trades, else 1-minute data."""

    def __init__(self, trades: pd.DataFrame, minute: pd.DataFrame, quotes_at: str = "candle_end"):
        """`minute` holds 1-minute candles (quotes_at="candle_end") or live quotes (quotes_at="asof")."""
        self.quotes_at = quotes_at
        self.series: dict[tuple[str, int], pd.DataFrame] = {}
        self.quotes: dict[tuple[str, int], pd.DataFrame] = {}
        if not trades.empty:
            for (venue, drv), g in trades.groupby(["venue", "driver_number"]):
                g = g.sort_values("utc").assign(utc=lambda x: x["utc"].dt.as_unit("ns"))
                self.series[(venue, int(drv))] = g[["utc", "yes_price", "size", "notional"]]
        if not minute.empty:
            for (venue, drv), g in minute.groupby(["venue", "driver_number"]):
                g = g.sort_values("utc").assign(utc=lambda x: x["utc"].dt.as_unit("ns"))
                key = (venue, int(drv))
                if "yes_price" not in g or g["yes_price"].isna().all():
                    g["yes_price"] = (g["bid"] + g["ask"]) / 2
                if key not in self.series:
                    self.series[key] = g[["utc", "yes_price"]].dropna().assign(size=np.nan, notional=np.nan)
                if {"bid", "ask"} <= set(g.columns) and g["bid"].notna().any():
                    self.quotes[key] = g[["utc", "bid", "ask"]].dropna()

    def keys(self, driver: int) -> list[tuple[str, int]]:
        return [k for k in self.series if k[1] == driver]

    def price(self, key, t: pd.Timestamp) -> float:
        s = self.series.get(key)
        if s is None or pd.isna(t):
            return np.nan
        i = s["utc"].searchsorted(_ns(t), side="right") - 1
        return float(s["yes_price"].iloc[i]) if i >= 0 else np.nan

    def quote(self, key, t: pd.Timestamp) -> tuple[float, float]:
        """(bid, ask) at t: the candle containing t, or the last live quote at or before t."""
        q = self.quotes.get(key)
        if q is None or pd.isna(t):
            return np.nan, np.nan
        if self.quotes_at == "asof":
            i = q["utc"].searchsorted(_ns(t), side="right") - 1
        else:
            i = q["utc"].searchsorted(_ns(t), side="left")  # candle stamps are period *ends*
        if not 0 <= i < len(q):
            return np.nan, np.nan
        return float(q["bid"].iloc[i]), float(q["ask"].iloc[i])

    def volume(self, key, t1: pd.Timestamp, t2: pd.Timestamp) -> tuple[float, float, int]:
        """(contracts, notional $, trade count) in [t1, t2)."""
        s = self.series.get(key)
        if s is None or pd.isna(t1) or pd.isna(t2):
            return np.nan, np.nan, 0
        w = s[(s["utc"] >= t1) & (s["utc"] < t2)]
        return float(w["size"].sum()), float(w["notional"].sum()), len(w)


def confounders(t1: pd.Timestamp, t2: pd.Timestamp, driver: int, track_status: pd.DataFrame,
                pits: pd.DataFrame, race_control: pd.DataFrame) -> str:
    """Other public news in the window that could explain a price move."""
    found = []
    if not track_status.empty:
        ts = track_status[(track_status["feed_utc"] >= t1) & (track_status["feed_utc"] <= t2)]
        found += [f"track:{m}" for m in ts["message"].fillna(ts["status"])]
    if not pits.empty:
        p = pits[(pits["feed_utc"] >= t1) & (pits["feed_utc"] <= t2) & (pits["driver_number"] == driver)]
        found += ["pit"] * len(p)
    if not race_control.empty:
        rc = race_control[(race_control["feed_utc"] >= t1) & (race_control["feed_utc"] <= t2)]
        rc = rc[rc["message"].str.contains(r"INVESTIGAT|PENALTY|RETIRED|STOPPED|CAR \d+", case=False, na=False)]
        found += [f"rc:{m[:40]}" for m in rc["message"]]
    return "; ".join(found)


def run(radio: pd.DataFrame, book: PriceBook, t_avail_col: str = "feed_utc",
        default_delay_s: float = 120.0, track_status: pd.DataFrame | None = None,
        pits: pd.DataFrame | None = None, race_control: pd.DataFrame | None = None,
        race_window: tuple[pd.Timestamp, pd.Timestamp] | None = None,
        placebos: int = 200, seed: int = 0) -> pd.DataFrame:
    """One row per (message, venue)."""
    track_status = track_status if track_status is not None else pd.DataFrame()
    pits = pits if pits is not None else pd.DataFrame()
    race_control = race_control if race_control is not None else pd.DataFrame()
    rng = np.random.default_rng(seed)
    rows = []
    for _, r in radio.iterrows():
        t_av = r[t_avail_col]
        has_bc = "broadcast_utc" in r and pd.notna(r.get("broadcast_utc"))
        t_bc = r["broadcast_utc"] if has_bc else t_av + pd.Timedelta(seconds=default_delay_s)
        if race_window and not (race_window[0] <= t_av <= race_window[1]):
            continue
        target = int(r.get("target_driver", r["driver_number"]))
        direction = int(r.get("direction", 0))
        for key in book.keys(target):
            p = {h: book.price(key, t_av + pd.Timedelta(seconds=h)) for h in HORIZONS_S}
            p_cap = book.price(key, r["capture_utc"])
            p_bc = book.price(key, t_bc)
            p_post = book.price(key, t_bc + pd.Timedelta(seconds=120))
            contracts, notional, n = book.volume(key, t_av, t_bc)
            sgn = direction if direction else 1  # unsigned messages: report raw moves
            row = {
                "venue": key[0], "audio_file": r["audio_file"], "speaker": int(r["driver_number"]),
                "target_driver": target, "category": r.get("category", "other"), "direction": direction,
                "text": r.get("text", ""), "capture_utc": r["capture_utc"], "t_avail": t_av,
                "broadcast_utc": t_bc, "broadcast_known": has_bc,
                "avail_to_broadcast_s": (t_bc - t_av).total_seconds(),
                "p_capture": p_cap, "p_avail": p[0], "p_broadcast": p_bc, "p_post": p_post,
                # capture -> avail: did the market already know before the clip hit the feed?
                "move_capture_to_avail": sgn * (p[0] - p_cap),
                "move_pre_60s": sgn * (p[0] - p[-60]),
                "move_window": sgn * (p_bc - p[0]),           # the exploitable part
                "move_post_120s": sgn * (p_post - p_bc),
                "window_contracts": contracts, "window_notional": notional, "window_trades": n,
                "confounders": confounders(r["capture_utc"] - pd.Timedelta(seconds=30), t_bc, target,
                                           track_status, pits, race_control),
            }
            row |= {f"p_{h:+d}s": v for h, v in p.items()}
            if placebos and race_window:
                row["placebo_pctile"] = _placebo(book, key, row["move_window"] * sgn,
                                                 (t_bc - t_av), race_window, placebos, rng)
            rows.append(row)
    return pd.DataFrame(rows)


def _placebo(book, key, observed_move, length, window, n, rng) -> float:
    """Share of random same-length windows in the race with a smaller absolute move."""
    if pd.isna(observed_move):
        return np.nan
    span = (window[1] - window[0] - length).total_seconds()
    if span <= 0:
        return np.nan
    moves = []
    for u in rng.uniform(0, span, n):
        a = window[0] + pd.Timedelta(seconds=float(u))
        moves.append(abs(book.price(key, a + length) - book.price(key, a)))
    moves = np.array([m for m in moves if not np.isnan(m)])
    return float((moves < abs(observed_move)).mean()) if moves.size else np.nan


def summarise(es: pd.DataFrame) -> pd.DataFrame:
    """Mean signed moves with a t-stat, per venue and category, directional messages only."""
    d = es[es["direction"] != 0]
    if d.empty:
        return pd.DataFrame()

    def agg(g):
        w = g["move_window"].dropna()
        se = w.std(ddof=1) / np.sqrt(len(w)) if len(w) > 1 else np.nan
        return pd.Series({
            "n": len(g), "n_priced": len(w),
            "median_avail_to_broadcast_s": g["avail_to_broadcast_s"].median(),
            "mean_move_capture_to_avail": g["move_capture_to_avail"].mean(),
            "mean_move_window": w.mean(), "t_stat_window": w.mean() / se if se else np.nan,
            "hit_rate_window": (w > 0).mean() if len(w) else np.nan,
            "mean_move_post_120s": g["move_post_120s"].mean(),
            "median_window_notional": g["window_notional"].median(),
            "share_confounded": (g["confounders"] != "").mean(),
        })

    return d.groupby(["venue", "category"]).apply(agg, include_groups=False).reset_index()
