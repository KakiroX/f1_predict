"""python -m f1radio <command> --year 2026 --race singapore"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import backtest, bookmakers, pipeline


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="f1radio", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def race_args(p):
        p.add_argument("--year", type=int, default=2026)
        p.add_argument("--race", default="singapore", help="singapore, baku, or any meeting name")
        return p

    p = race_args(sub.add_parser("fetch", help="F1 archive + Polymarket/Kalshi history"))
    p.add_argument("--poly-slug", help="Polymarket event slug if search does not find it")
    p.add_argument("--kalshi-event", help="Kalshi event ticker, e.g. KXF1RACE-SIN26")
    p.add_argument("--source", default="auto", choices=["auto", "f1", "openf1"],
                   help="race data: F1 archive, OpenF1, or archive with OpenF1 fallback (default)")

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
    p.add_argument("--exchange-commission", type=float, default=backtest.Params.exchange_commission)
    p.add_argument("--acceptance-delay", type=float, default=bookmakers.BookieParams.acceptance_delay_s,
                   help="bookmaker in-play bet delay in seconds")
    p.add_argument("--bookie-stake", type=float, default=bookmakers.BookieParams.stake)
    p.add_argument("--max-stake", type=float, default=bookmakers.BookieParams.max_stake,
                   help="largest in-play bet the bookmaker would accept")
    p.add_argument("--winner", type=int, help="winning car number, for settling bookmaker bets")

    p = race_args(sub.add_parser("import-odds", help="odds you captured from any bookmaker (CSV)"))
    p.add_argument("--file", required=True)
    p.add_argument("--bookmaker", help="name, if the CSV has no bookmaker column")
    p.add_argument("--tz", help="time zone of naive timestamps, e.g. Europe/Moscow (default UTC)")
    p.add_argument("--kind", choices=["bookmaker", "exchange"],
                   help="default: exchange if the CSV has lay_odds, else bookmaker")

    p = race_args(sub.add_parser("import-betfair", help="Betfair historical stream file (.bz2)"))
    p.add_argument("--file", required=True)
    p.add_argument("--market-id", help="needed if the file holds several markets")

    p = sub.add_parser("record-betfair", help="poll a Betfair market live into an odds CSV")
    p.add_argument("--market-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--api", default=bookmakers.BETFAIR_API, help="regional API base URL if not .com")

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
        pipeline.fetch(a.year, a.race, a.poly_slug, a.kalshi_event, source=a.source)
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
                                                             "poly_fee_rate", "exchange_commission")})
        bprm = bookmakers.BookieParams(reaction_s=a.reaction_s, acceptance_delay_s=a.acceptance_delay,
                                       hold_s=a.hold_s, stake=a.bookie_stake, max_stake=a.max_stake)
        pipeline.analyze(a.year, a.race, a.t_avail, a.live, prm, bprm, a.winner)
    elif a.cmd == "import-odds":
        pipeline.import_odds(a.year, a.race, a.file, a.bookmaker, a.tz, a.kind)
    elif a.cmd == "import-betfair":
        pipeline.import_betfair(a.year, a.race, a.file, a.market_id)
    elif a.cmd == "record-betfair":
        import os

        key, token = os.environ.get("BETFAIR_APP_KEY"), os.environ.get("BETFAIR_SESSION_TOKEN")
        if not (key and token):
            raise SystemExit("set BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN")
        bookmakers.record_betfair(a.market_id, Path(a.out), key, token, a.interval, a.api)
    elif a.cmd == "all":
        pipeline.fetch(a.year, a.race, a.poly_slug, a.kalshi_event)
        pipeline.audio(a.year, a.race)
        pipeline.analyze(a.year, a.race, "feed_utc", None, backtest.Params())
    elif a.cmd == "record":
        from .recorder import record

        record(Path(a.out), a.poly_slug, a.kalshi_event, a.interval)


if __name__ == "__main__":
    main()
