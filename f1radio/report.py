"""Markdown report (and price charts if matplotlib is installed) for one race."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest, study

log = logging.getLogger(__name__)


def md_table(df: pd.DataFrame, floatfmt: str = "{:.3f}") -> str:
    if df is None or df.empty:
        return "_(no rows)_\n"
    def fmt(v):
        if isinstance(v, float):
            if np.isnan(v):
                return ""
            return f"{v:.0f}" if v.is_integer() and abs(v) >= 1 else floatfmt.format(v)
        if isinstance(v, pd.Timestamp):
            return v.strftime("%H:%M:%S")
        return str(v).replace("|", "/").replace("\n", " ")
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines) + "\n"


def _dist(s: pd.Series) -> str:
    s = s.dropna()
    if s.empty:
        return "n/a"
    return (f"n={len(s)}, median {s.median():.0f}s, p10 {s.quantile(.1):.0f}s, "
            f"p90 {s.quantile(.9):.0f}s, min {s.min():.0f}s, max {s.max():.0f}s")


def plot_events(d: Path, es: pd.DataFrame, book: study.PriceBook, top: int = 12) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.info("matplotlib not installed; skipping charts")
        return []
    out = []
    pick = es[es["direction"] != 0].assign(a=lambda x: x["move_window"].abs()).nlargest(top, "a")
    for _, e in pick.iterrows():
        key = (e["venue"], int(e["target_driver"]))
        s = book.series[key]
        lo, hi = e["capture_utc"] - pd.Timedelta(minutes=3), e["broadcast_utc"] + pd.Timedelta(minutes=4)
        w = s[(s["utc"] >= lo) & (s["utc"] <= hi)]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        ax.step(w["utc"], w["yes_price"], where="post", lw=1.4)
        for t, lab, ls in ((e["capture_utc"], "spoken", ":"), (e["t_avail"], "available", "--"),
                           (e["broadcast_utc"], "on TV" if e["broadcast_known"] else "on TV (assumed)", "-")):
            ax.axvline(t, ls=ls, color="k", lw=0.8)
            ax.text(t, ax.get_ylim()[1], f" {lab}", va="top", fontsize=8)
        ax.set_title(f"{e['venue']} #{key[1]} {e['category']}: {str(e['text'])[:70]}", fontsize=9)
        ax.set_ylabel("YES price")
        fig.autofmt_xdate()
        fig.tight_layout()
        p = d / "charts" / f"{e['venue']}_{Path(e['audio_file']).stem}.png"
        p.parent.mkdir(exist_ok=True)
        fig.savefig(p, dpi=110)
        plt.close(fig)
        out.append(p)
    return out


def write(d: Path, radio: pd.DataFrame, es: pd.DataFrame, bt: pd.DataFrame, book: study.PriceBook,
          t_avail: str, prm: backtest.Params) -> Path:
    lines = [f"# Radio vs. winner markets: {d.name}\n"]
    meta_path = d / "meta.json"
    source = json.loads(meta_path.read_text()).get("source", "f1") if meta_path.exists() else "f1"
    lines.append(f"Race data source: `{source}`. Availability time used: `{t_avail}`. "
                 f"Backtest params: `{asdict(prm)}`\n")
    if source == "openf1":
        lines.append("> OpenF1 has no feed publish times, so availability = the moment the clip was "
                     "recorded. That is the best case for a trader, not what a public-data trader "
                     "could actually do.\n")
    lines.append("## Timing chain\n")
    lines.append(f"- spoken -> live-timing feed: {_dist(radio['feed_lag_s'])}")
    if radio["broadcast_utc"].notna().any():
        lines.append(f"- feed -> TV: {_dist((radio['broadcast_utc'] - radio['feed_utc']).dt.total_seconds())}")
        lines.append(f"- spoken -> TV: {_dist((radio['broadcast_utc'] - radio['capture_utc']).dt.total_seconds())}")
        lines.append(f"- clips found on TV: {radio['broadcast_utc'].notna().sum()} / {len(radio)}")
    else:
        lines.append(f"- feed -> TV: **unknown, assumed {prm.default_delay_s:.0f}s** "
                     "(run `align` or fill annotations/ to measure it)")
    lines.append("")
    lines.append("## Messages by category\n")
    lines.append(md_table(radio.groupby(["category", "direction"]).size().rename("n").reset_index()))
    lines.append("## Event study (directional messages)\n")
    lines.append("Moves are YES-price changes signed by the message direction: positive = the "
                 "market moved the way the message predicted. `window` = from availability to "
                 "broadcast, i.e. the part a faster trader could capture.\n")
    lines.append(md_table(study.summarise(es) if not es.empty else pd.DataFrame()))
    if not es.empty:
        cols = ["venue", "target_driver", "category", "t_avail", "avail_to_broadcast_s", "p_avail",
                "move_capture_to_avail", "move_window", "move_post_120s", "window_notional",
                "placebo_pctile", "confounders", "text"]
        big = es[es["direction"] != 0].assign(a=lambda x: x["move_window"].abs()).nlargest(15, "a")
        lines.append("### Largest moves inside the window\n")
        lines.append(md_table(big[[c for c in cols if c in big]]))
    lines.append("## Backtest\n")
    lines.append(md_table(backtest.summarise(bt)))
    if not bt.empty:
        lines.append(md_table(bt.sort_values("pnl")[["venue", "target_driver", "category", "side", "px_in",
                                                     "px_out", "contracts", "fees", "pnl", "confounders"]]))
    charts = plot_events(d, es, book) if not es.empty else []
    if charts:
        lines.append("## Charts\n")
        lines += [f"![{p.stem}](charts/{p.name})" for p in charts]
    path = d / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path
