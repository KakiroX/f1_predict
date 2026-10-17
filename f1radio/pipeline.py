"""Glue: fetch -> audio -> transcripts -> broadcast times -> event study -> backtest -> report."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from . import backtest, broadcast, classify, f1feed, markets, report, study

log = logging.getLogger(__name__)

RACES = {"singapore": "Singapore", "baku": "Azerbaijan"}
DATA = Path("data")
ANNOTATIONS = Path("annotations")


def race_dir(year: int, race: str) -> Path:
    return DATA / f"{year}_{race}"


def save(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def load(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= 1:
        return pd.DataFrame()
    df = pd.read_csv(path)
    for c in df.columns:
        if c == "utc" or c.endswith("_utc") or c in ("t_avail", "t_in", "t_out"):
            df[c] = pd.to_datetime(df[c], utc=True, format="ISO8601").dt.as_unit("ns")
    for c in ("confounders", "text"):  # empty strings come back from CSV as NaN
        if c in df:
            df[c] = df[c].fillna("")
    return df


def race_window(d: Path) -> tuple[pd.Timestamp, pd.Timestamp]:
    meta = json.loads((d / "meta.json").read_text())
    return pd.Timestamp(meta["race_start_utc"]), pd.Timestamp(meta["race_end_utc"])


# --------------------------------------------------------------------------- steps

def fetch(year: int, race: str, poly_slug: str | None = None, kalshi_event: str | None = None,
          pad_min: int = 30) -> Path:
    d = race_dir(year, race)
    f1 = f1feed.fetch_all(year, RACES.get(race, race))
    for name in ("drivers", "radio", "track_status", "session_status", "race_control", "laps", "pits"):
        save(f1[name], d / f"{name}.csv")

    ss = f1["session_status"]
    started = ss.loc[ss["status"] == "Started", "feed_utc"]
    start = started.iloc[0] if len(started) else f1["session"].scheduled_start_utc
    finished = ss.loc[(ss["status"].isin(["Finished", "Finalised", "Ends"])) & (ss["feed_utc"] > start), "feed_utc"]
    end = finished.iloc[0] if len(finished) else start + pd.Timedelta(hours=2, minutes=30)
    s = f1["session"]
    (d / "meta.json").write_text(json.dumps({
        "year": year, "race": race, "meeting": s.meeting, "path": s.path, "stream_t0_utc": str(f1["t0"]),
        "race_start_utc": str(start), "race_end_utc": str(end),
    }, indent=1))
    log.info("%s: %d radio clips, race %s -> %s", s.meeting, len(f1["radio"]), start, end)

    mk = markets.fetch_all(RACES.get(race, race), year, f1["drivers"],
                           start - pd.Timedelta(minutes=pad_min), end + pd.Timedelta(minutes=pad_min),
                           poly_slug=poly_slug, kalshi_event_ticker=kalshi_event)
    for name, df in mk.items():
        save(df, d / f"{name}.csv")
    log.info("markets: %d markets, %d trades, %d minute rows", len(mk["markets"]), len(mk["trades"]),
             len(mk["minute"]))
    return d


def audio(year: int, race: str) -> None:
    from .http import download

    d = race_dir(year, race)
    radio = load(d / "radio.csv")
    for _, r in radio.iterrows():
        download(r["audio_url"], d / "audio" / r["audio_file"])
    log.info("downloaded %d clips to %s", len(radio), d / "audio")


def transcribe(year: int, race: str, model: str) -> None:
    from .transcribe import transcribe as run

    d = race_dir(year, race)
    run(load(d / "radio.csv"), d / "audio", d / "transcripts.csv", model)


def anchor_utc(d: Path, event: str) -> pd.Timestamp:
    """UTC of a moment you can spot in the broadcast replay: 'lap:N' or 'session_started'."""
    if event == "session_started":
        ss = load(d / "session_status.csv")
        return ss.loc[ss["status"] == "Started", "feed_utc"].iloc[0]
    if event.startswith("lap:"):
        laps = load(d / "laps.csv")
        return laps.loc[laps["lap"] >= int(event[4:]), "feed_utc"].iloc[0]
    return pd.Timestamp(event, tz="UTC") if pd.Timestamp(event).tz is None else pd.Timestamp(event)


def align(year: int, race: str, recording: str, wallclock_start: str | None, anchor_video: str | None,
          anchor_event: str, viewer_latency_s: float, max_delay_s: float) -> None:
    d = race_dir(year, race)
    if wallclock_start:
        clock = broadcast.linear_clock(0.0, pd.Timestamp(wallclock_start).tz_convert("UTC"))
    elif anchor_video:
        clock = broadcast.linear_clock(broadcast.parse_clock(anchor_video), anchor_utc(d, anchor_event),
                                       viewer_latency_s)
    else:
        raise SystemExit("pass --wallclock-start (live recording) or --anchor-video (replay)")
    out = broadcast.align(load(d / "radio.csv"), recording, d / "audio", *clock, max_delay_s=max_delay_s)
    save(out[["audio_file", "broadcast_utc", "match_score", "airings"]], d / "broadcast_times.csv")
    found = out["broadcast_utc"].notna()
    log.info("found %d/%d clips in the broadcast; median feed->broadcast %.0fs", found.sum(), len(out),
             out.loc[found, "feed_to_broadcast_s"].median() if found.any() else float("nan"))


def radio_table(d: Path) -> pd.DataFrame:
    """radio.csv + transcripts + broadcast times (auto, then manual annotations win) + tags."""
    radio = load(d / "radio.csv")
    tr = load(d / "transcripts.csv")
    if not tr.empty:
        radio = radio.merge(tr, on="audio_file", how="left")
    else:
        radio["text"] = ""
    bt = load(d / "broadcast_times.csv")
    radio["broadcast_utc"] = pd.NaT
    if not bt.empty:
        radio = radio.drop(columns="broadcast_utc").merge(bt[["audio_file", "broadcast_utc"]],
                                                          on="audio_file", how="left")
    manual = load(ANNOTATIONS / f"{d.name}_broadcast.csv")
    if not manual.empty:
        m = manual.dropna(subset=["broadcast_utc"]).set_index("audio_file")["broadcast_utc"]
        radio["broadcast_utc"] = radio["audio_file"].map(m).fillna(radio["broadcast_utc"])
    radio["broadcast_utc"] = pd.to_datetime(radio["broadcast_utc"], utc=True).dt.as_unit("ns")
    return classify.tag(radio, load(d / "drivers.csv"))


def analyze(year: int, race: str, t_avail: str, live: str | None, prm: backtest.Params) -> Path:
    d = race_dir(year, race)
    drivers = load(d / "drivers.csv")
    if live:
        from .recorder import load as load_live

        trades, quotes = load_live(Path(live), drivers)
        book = study.PriceBook(trades, quotes, quotes_at="asof")
    else:
        book = study.PriceBook(load(d / "trades.csv"), load(d / "minute.csv"))
    radio = radio_table(d)
    es = study.run(radio, book, t_avail_col=t_avail, default_delay_s=prm.default_delay_s,
                   track_status=load(d / "track_status.csv"), pits=load(d / "pits.csv"),
                   race_control=load(d / "race_control.csv"), race_window=race_window(d))
    bt = backtest.run(es, book, prm) if not es.empty else pd.DataFrame()
    save(radio, d / "radio_tagged.csv")
    save(es, d / "event_study.csv")
    save(study.summarise(es) if not es.empty else pd.DataFrame(), d / "event_summary.csv")
    save(bt, d / "backtest.csv")
    path = report.write(d, radio, es, bt, book, t_avail, prm)
    log.info("report: %s", path)
    return path
