"""python -m f1radio <command> --year 2026 --race singapore"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import backtest, pipeline


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="f1radio", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def race_args(p):
        p.add_argument("--year", type=int, default=2026)
        p.add_argument("--race", default="singapore", help="singapore, baku, or any meeting name")
        return p

    p = race_args(sub.add_parser("fetch", help="F1 archive + Polymarket/Kalshi history"))
    p.add_argument("--poly-slug", help="Polymarket event slug if search does not find it")
    p.add_argument("--kalshi-event", help="Kalshi event ticker, e.g. KXF1RACE-SINGP26")

    race_args(sub.add_parser("audio", help="download radio MP3s"))

    p = race_args(sub.add_parser("transcribe", help="faster-whisper transcripts (edit them after)"))
    p.add_argument("--model", default="small.en")

    p = race_args(sub.add_parser("align", help="find each clip in a broadcast recording"))
    p.add_argument("--recording", required=True, help="video/audio file of the race broadcast")
    p.add_argument("--wallclock-start", help="UTC at second 0 of a live recording, e.g. 2026-10-11T12:01:07.4Z")
    p.add_argument("--anchor-video", help="replay time of the anchor moment, e.g. 0:14:31.2")
    p.add_argument("--anchor-event", default="lap:2", help="lap:N (on-screen lap counter flips) or session_started")
    p.add_argument("--viewer-latency", type=float, default=0.0, help="seconds your viewers lag the world feed")
    p.add_argument("--max-delay", type=float, default=900.0)

    p = race_args(sub.add_parser("analyze", help="event study + backtest + report"))
    p.add_argument("--t-avail", default="feed_utc", choices=["feed_utc", "capture_utc"],
                   help="when a fast trader is assumed to know: feed publication, or the moment it was spoken")
    p.add_argument("--live", help="use a recorder.py directory instead of historical prices")
    for f in ("reaction_s", "hold_s", "default_delay_s", "stake", "participation", "poly_half_spread",
              "poly_fee_rate"):
        p.add_argument("--" + f.replace("_", "-"), type=float, default=getattr(backtest.Params, f))

    p = race_args(sub.add_parser("all", help="fetch + audio + analyze"))
    p.add_argument("--poly-slug")
    p.add_argument("--kalshi-event")

    p = sub.add_parser("record", help="record live order books during a race")
    p.add_argument("--out", required=True)
    p.add_argument("--poly-slug")
    p.add_argument("--kalshi-event")
    p.add_argument("--interval", type=float, default=1.0)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if a.cmd == "fetch":
        pipeline.fetch(a.year, a.race, a.poly_slug, a.kalshi_event)
    elif a.cmd == "audio":
        pipeline.audio(a.year, a.race)
    elif a.cmd == "transcribe":
        pipeline.transcribe(a.year, a.race, a.model)
    elif a.cmd == "align":
        pipeline.align(a.year, a.race, a.recording, a.wallclock_start, a.anchor_video, a.anchor_event,
                       a.viewer_latency, a.max_delay)
    elif a.cmd == "analyze":
        prm = backtest.Params(**{f: getattr(a, f) for f in ("reaction_s", "hold_s", "default_delay_s", "stake",
                                                             "participation", "poly_half_spread",
                                                             "poly_fee_rate")})
        pipeline.analyze(a.year, a.race, a.t_avail, a.live, prm)
    elif a.cmd == "all":
        pipeline.fetch(a.year, a.race, a.poly_slug, a.kalshi_event)
        pipeline.audio(a.year, a.race)
        pipeline.analyze(a.year, a.race, "feed_utc", None, backtest.Params())
    elif a.cmd == "record":
        from .recorder import record

        record(Path(a.out), a.poly_slug, a.kalshi_event, a.interval)


if __name__ == "__main__":
    main()
