"""Tests on synthetic fixtures. They check the code paths, not any market finding."""
from __future__ import annotations

import json
import shutil
import subprocess

import numpy as np
import pandas as pd
import pytest

from f1radio import backtest, broadcast, classify, f1feed, markets, pipeline, study

T0 = pd.Timestamp("2026-10-11T11:00:00Z")
PATH = "2026/2026-10-11_Singapore_Grand_Prix/2026-10-11_Race/"


def off(s: float) -> str:
    td = pd.Timedelta(seconds=s)
    h, rem = divmod(td.total_seconds(), 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def jsonstream(rows) -> str:
    return "﻿" + "\r\n".join(off(s) + json.dumps(m) for s, m in rows)


def utc(s: float) -> str:
    return (T0 + pd.Timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


# A race from t=3600s to t=3600+7200s. Car 1 leads at 0.80 until a "lost power"
# radio is spoken at 5000s, hits the feed at 5010s and airs on TV at 5130s.
# The market starts falling at 5040s and bottoms at 0.30 by 5150s.
SPOKEN, FEED, AIR = 5000, 5010, 5130


def fake_archive() -> dict[str, str]:
    base = f1feed.BASE + PATH
    files = {
        f"{f1feed.BASE}2026/Index.json": json.dumps({"Meetings": [
            {"Name": "Singapore Grand Prix", "Location": "Marina Bay", "Country": {"Name": "Singapore"},
             "Sessions": [{"Name": "Race", "Path": PATH, "StartDate": "2026-10-11T20:00:00",
                           "GmtOffset": "08:00:00"}]}]}),
        base + "Heartbeat.jsonStream": jsonstream([(s, {"Utc": utc(s)}) for s in range(0, 11000, 600)]),
        base + "TeamRadio.jsonStream": jsonstream([
            (2000, {"Captures": [{"Utc": utc(1990), "RacingNumber": "16", "Path": "TeamRadio/LEC_a.mp3"}]}),
            (FEED, {"Captures": {"1": {"Utc": utc(SPOKEN), "RacingNumber": "1", "Path": "TeamRadio/VER_b.mp3"}}}),
        ]),
        base + "SessionStatus.jsonStream": jsonstream([(3600, {"Status": "Started"}),
                                                       (10800, {"Status": "Finished"})]),
        base + "TrackStatus.jsonStream": jsonstream([(4000, {"Status": "4", "Message": "SCDeployed"})]),
        base + "RaceControlMessages.jsonStream": jsonstream([
            (3500, {"Messages": [{"Utc": utc(3500)[:19], "Category": "Flag", "Message": "GREEN LIGHT"}]})]),
        base + "LapCount.jsonStream": jsonstream([(3600, {"CurrentLap": 1, "TotalLaps": 62}),
                                                  (3700, {"CurrentLap": 2})]),
        base + "PitLaneTimeCollection.jsonStream": jsonstream([]),
        base + "DriverList.json": json.dumps({
            "1": {"RacingNumber": "1", "Tla": "VER", "LastName": "Verstappen", "FirstName": "Max"},
            "16": {"RacingNumber": "16", "Tla": "LEC", "LastName": "Leclerc", "FirstName": "Charles"}}),
    }
    return files


def fake_api(url, params=None, cache=True):
    params = params or {}
    if url.endswith("/public-search"):
        return {"events": [{"title": "Singapore Grand Prix Winner", "slug": "sg-gp", "endDate": "2026-10-11T12:00:00Z"}]}
    if url.endswith("/events") and "slug" in params:
        return [{"markets": [
            {"groupItemTitle": "Max Verstappen", "conditionId": "c1", "clobTokenIds": '["y1","n1"]',
             "outcomes": '["Yes","No"]'},
            {"groupItemTitle": "Charles Leclerc", "conditionId": "c16", "clobTokenIds": '["y16","n16"]',
             "outcomes": '["Yes","No"]'}]}]
    if url.endswith("/trades") and "market" in params:
        if params["offset"] > 0 or params["market"] != "c1":
            return []
        ts = list(range(int(T0.timestamp()) + 3600, int(T0.timestamp()) + 10800, 10))
        px = [0.80 if t < T0.timestamp() + 5040 else max(0.30, 0.80 - (t - T0.timestamp() - 5040) / 220)
              for t in ts]
        rows = [{"timestamp": t, "price": p, "size": 400, "side": "BUY", "outcome": "Yes"} for t, p in zip(ts, px)]
        return rows[::-1]  # newest first, like the API
    if url.endswith("/prices-history"):
        return {"history": []}
    if url.endswith("/trade-api/v2/events"):
        return {"events": [], "cursor": None}
    raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def fake_net(monkeypatch, tmp_path):
    files = fake_archive()
    monkeypatch.setattr(f1feed, "get_text", lambda url, params=None, cache=True: files[url])
    monkeypatch.setattr(f1feed, "get_json", lambda url, params=None, cache=True: json.loads(files[url]))
    monkeypatch.setattr(markets, "get_json", fake_api)
    monkeypatch.setattr(pipeline, "DATA", tmp_path / "data")
    monkeypatch.setattr(pipeline, "ANNOTATIONS", tmp_path / "annotations")
    return tmp_path


def test_jsonstream_and_anchor():
    rows = f1feed.parse_jsonstream(jsonstream([(61.5, {"Utc": utc(61.5)}), (125.25, {"Utc": utc(125.25)})]))
    assert rows[0][0] == pd.Timedelta(seconds=61.5)
    assert f1feed.stream_t0(rows) == T0


def test_team_radio_feed_lag(fake_net):
    s = f1feed.find_session(2026, "singapore")
    assert s.scheduled_start_utc == pd.Timestamp("2026-10-11T12:00:00Z")
    radio = f1feed.team_radio(s, T0)
    assert list(radio["driver_number"]) == [16, 1]
    assert list(radio["feed_lag_s"]) == [10.0, 10.0]
    assert radio["audio_url"].iloc[1].endswith("TeamRadio/VER_b.mp3")


def test_classify():
    drv = pd.DataFrame({"driver_number": [1, 16], "last_name": ["Verstappen", "Leclerc"]})
    assert classify.classify_text("I've lost power, lost power!") == ("failure", -1)
    assert classify.classify_text("Box box, box this lap") == ("pit_call", 0)
    assert classify.classify_text("Copy, nice job") == ("other", 0)
    radio = pd.DataFrame({"driver_number": [1], "text": ["Leclerc has a problem, he's slowing"]})
    assert classify.tag(radio, drv)["target_driver"].iloc[0] == 16


def test_kalshi_fee():
    # 100 contracts at 50c: 0.07 * 100 * 0.25 = $1.75
    assert backtest.kalshi_fee(100, 0.5) == 1.75
    assert backtest.kalshi_fee(1, 0.5) == 0.02  # rounds up to the cent


def test_ncc_finds_clip():
    rng = np.random.default_rng(1)
    clip = np.sin(np.linspace(0, 900, 4 * broadcast.SR)) * rng.uniform(0.5, 1, 4 * broadcast.SR)
    sig = rng.normal(0, 1.0, 120 * broadcast.SR)
    at = 47.25
    i = int(at * broadcast.SR)
    sig[i: i + clip.size] += 0.8 * clip  # under "commentary" noise
    sig[i + 30 * broadcast.SR: i + 30 * broadcast.SR + clip.size] += 0.8 * clip  # a later replay
    m = broadcast.locate(sig, np.concatenate([np.zeros(800), clip]))
    assert m is not None and abs(m.offset_s - (at - 0.1)) < 0.01 and m.airings == 2


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_align_with_ffmpeg(tmp_path):
    sr = broadcast.SR
    rng = np.random.default_rng(2)
    clip = (np.sin(2 * np.pi * 700 * np.arange(3 * sr) / sr) * rng.uniform(0.3, 1, 3 * sr) * 0.4)
    bc = rng.normal(0, 0.15, 300 * sr)
    bc[100 * sr: 103 * sr] += clip
    for name, x in (("clip.wav", clip), ("bc.wav", bc)):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f64le", "-ar", str(sr), "-ac", "1", "-i", "-",
                        str(tmp_path / name)], input=x.astype("<f8").tobytes(), check=True)
    start = pd.Timestamp("2026-10-11T12:00:00Z")
    radio = pd.DataFrame({"audio_file": ["clip.wav"], "capture_utc": [start + pd.Timedelta(seconds=40)],
                          "feed_utc": [start + pd.Timedelta(seconds=45)]})
    out = broadcast.align(radio, tmp_path / "bc.wav", tmp_path, *broadcast.linear_clock(0.0, start))
    assert abs(out["broadcast_delay_s"].iloc[0] - 60.0) < 0.05


def test_end_to_end(fake_net):
    d = pipeline.fetch(2026, "singapore")
    (d / "transcripts.csv").write_text("audio_file,text\nVER_b.mp3,\"I've lost power, lost power\"\n"
                                       "LEC_a.mp3,Copy that\n")
    ann = pipeline.ANNOTATIONS / f"{d.name}_broadcast.csv"
    ann.parent.mkdir(parents=True)
    ann.write_text(f"audio_file,broadcast_utc\nVER_b.mp3,{utc(AIR)}\n")
    pipeline.analyze(2026, "singapore", "feed_utc", None, backtest.Params(reaction_s=5, hold_s=30))

    es = pipeline.load(d / "event_study.csv")
    e = es[(es["audio_file"] == "VER_b.mp3") & (es["venue"] == "polymarket")].iloc[0]
    assert e["direction"] == -1 and e["avail_to_broadcast_s"] == AIR - FEED
    assert e["p_avail"] == pytest.approx(0.80)
    assert e["move_window"] > 0.3  # fell a lot before the broadcast: signed positive
    assert "track" not in e["confounders"]

    bt = pipeline.load(d / "backtest.csv")
    t = bt.iloc[0]
    assert t["side"] == "NO" and t["px_in"] == pytest.approx(0.21)  # 1 - (0.80 + half spread)
    assert t["pnl"] > 0 and t["contracts"] > 0
    assert "Event study" in (d / "report.md").read_text()


def test_pricebook_candles_and_asof():
    t = pd.Timestamp("2026-10-11T12:00:00Z")
    q = pd.DataFrame({"venue": "kalshi", "driver_number": 1, "utc": [t, t + pd.Timedelta(minutes=1)],
                      "bid": [0.4, 0.5], "ask": [0.45, 0.55], "yes_price": [0.42, 0.52]})
    candles = study.PriceBook(pd.DataFrame(), q)
    assert candles.quote(("kalshi", 1), t + pd.Timedelta(seconds=20)) == (0.5, 0.55)  # candle ending 12:01
    live = study.PriceBook(pd.DataFrame(), q, quotes_at="asof")
    assert live.quote(("kalshi", 1), t + pd.Timedelta(seconds=20)) == (0.4, 0.45)


def fake_openf1(url, params=None, cache=True):
    ep = url.rsplit("/", 1)[-1]
    if ep == "sessions":
        return [{"session_key": 9999, "session_name": "Race", "country_name": "Singapore",
                 "location": "Marina Bay", "date_start": utc(3600), "date_end": utc(10800)}]
    if ep == "drivers":
        return [{"driver_number": 1, "name_acronym": "VER", "first_name": "Max", "last_name": "Verstappen"},
                {"driver_number": 16, "name_acronym": "LEC", "first_name": "Charles", "last_name": "Leclerc"}]
    if ep == "team_radio":
        return [{"driver_number": 1, "date": utc(SPOKEN), "recording_url": f1feed.BASE + PATH + "TeamRadio/VER_b.mp3"}]
    if ep == "race_control":
        return [{"date": utc(4000), "category": "SafetyCar", "flag": None, "message": "SAFETY CAR DEPLOYED"},
                {"date": utc(4100), "category": "Flag", "flag": "CLEAR", "message": "CLEAR IN TRACK SECTOR 4"},
                {"date": utc(10700), "category": "Flag", "flag": "CHEQUERED", "message": "CHEQUERED FLAG"}]
    if ep == "laps":
        return [{"driver_number": 1, "lap_number": 1, "date_start": None},
                {"driver_number": 1, "lap_number": 2, "date_start": utc(3700)},
                {"driver_number": 16, "lap_number": 2, "date_start": utc(3702)}]
    if ep == "pit":
        return []
    raise AssertionError(url)


def test_openf1_fallback_on_401(fake_net, monkeypatch):
    import requests

    def refuse(*a, **k):
        raise requests.HTTPError("401", response=type("R", (), {"status_code": 401})())

    monkeypatch.setattr(f1feed, "get_text", refuse)
    monkeypatch.setattr(f1feed, "get_json", refuse)
    from f1radio import openf1
    monkeypatch.setattr(openf1, "get_json", fake_openf1)

    d = pipeline.fetch(2026, "singapore")
    meta = json.loads((d / "meta.json").read_text())
    assert meta["source"] == "openf1"
    assert pd.Timestamp(meta["race_end_utc"]) == pd.Timestamp(utc(10700))
    ts = pipeline.load(d / "track_status.csv")
    assert list(ts["message"]) == ["SAFETY CAR DEPLOYED"]  # sector clears are not track status
    assert pipeline.load(d / "laps.csv")["feed_utc"].iloc[0] == pd.Timestamp(utc(3700))

    (d / "transcripts.csv").write_text("audio_file,text\nVER_b.mp3,\"lost power\"\n")
    pipeline.analyze(2026, "singapore", "feed_utc", None, backtest.Params())
    es = pipeline.load(d / "event_study.csv")
    assert es["t_avail"].iloc[0] == pd.Timestamp(utc(SPOKEN))  # fell back to capture time
    assert "openf1" in (d / "report.md").read_text()

    with pytest.raises(requests.HTTPError):
        pipeline.fetch(2026, "singapore", source="f1")


def test_polymarket_slug_candidates_pick_year(monkeypatch):
    events = {"old-slug": [{"slug": "old-slug", "endDate": "2025-09-21T12:00:00Z", "markets": []}],
              "new-slug": [{"slug": "new-slug", "endDate": "2026-09-26T12:00:00Z", "markets": []}]}
    monkeypatch.setattr(markets, "get_json", lambda url, params=None, cache=True: events.get(params["slug"], []))
    assert markets.polymarket_event(year=2026, slug=["old-slug", "new-slug"])["slug"] == "new-slug"
    with pytest.raises(LookupError):
        markets.polymarket_event(year=2024, slug=["old-slug", "new-slug"])


# --------------------------------------------------------------------------- bookmakers

DRIVERS = pd.DataFrame({"driver_number": [1, 16], "tla": ["VER", "LEC"], "last_name": ["Verstappen", "Leclerc"]})


def test_parse_odds_formats():
    from f1radio.bookmakers import parse_odds
    assert parse_odds("3.5") == 3.5
    assert parse_odds("5/2") == 3.5
    assert parse_odds("+250") == 3.5
    assert parse_odds("-200") == 1.5
    assert parse_odds("151") == 151.0  # big decimal longshot, not American
    assert np.isnan(parse_odds(""))


def test_normalise_removes_margin_and_carries_forward():
    from f1radio.bookmakers import normalise
    t = pd.Timestamp("2026-10-11T12:00:00Z")
    raw = pd.DataFrame({"utc": [t, t, t + pd.Timedelta(seconds=5)], "selection": ["Verstappen", "LEC", "VER"],
                        "odds": ["1.25", "4.0", "2.0"]})
    out = normalise(raw, "winline", "bookmaker", DRIVERS)
    first = out[out["utc"] == t].set_index("driver_number")["yes_price"]
    assert first.sum() == pytest.approx(1.0)
    assert first[1] == pytest.approx(0.8 / 1.05)
    later = out[out["utc"] == t + pd.Timedelta(seconds=5)].set_index("driver_number")
    assert later.loc[16, "odds"] == 4.0  # Leclerc carried forward
    assert later["implied"].sum() < 1 and later.loc[1, "yes_price"] == pytest.approx(0.5)  # under-round: raw


def test_betfair_stream(tmp_path):
    import bz2
    from f1radio.bookmakers import normalise, read_betfair_stream
    pt = int(pd.Timestamp("2026-10-11T12:00:00Z").timestamp() * 1000)
    md = {"name": "Winner", "status": "OPEN", "inPlay": True,
          "runners": [{"id": 11, "name": "Max Verstappen", "status": "ACTIVE"},
                      {"id": 22, "name": "Charles Leclerc", "status": "ACTIVE"}]}
    msgs = [
        {"op": "mcm", "pt": pt, "mc": [{"id": "1.5", "marketDefinition": md, "rc": [
            {"id": 11, "batb": [[0, 1.30, 500]], "batl": [[0, 1.32, 400]]},
            {"id": 22, "batb": [[0, 4.0, 100]], "batl": [[0, 4.2, 80]]}]}]},
        {"op": "mcm", "pt": pt + 1000, "mc": [{"id": "1.5", "rc": [{"id": 11, "batb": [[0, 1.30, 0]]}]}]},
        {"op": "mcm", "pt": pt + 2000, "mc": [{"id": "1.5", "marketDefinition": md | {"status": "SUSPENDED"}}]},
        {"op": "mcm", "pt": pt + 9000, "mc": [{"id": "1.5", "marketDefinition": md | {
            "status": "CLOSED", "runners": [{"id": 11, "name": "Max Verstappen", "status": "LOSER"},
                                            {"id": 22, "name": "Charles Leclerc", "status": "WINNER"}]}}]},
    ]
    f = tmp_path / "1.5.bz2"
    with bz2.open(f, "wt") as fh:
        fh.write("\n".join(json.dumps(m) for m in msgs))
    raw = read_betfair_stream(f)
    assert raw.attrs["winner"] == "Charles Leclerc"
    out = normalise(raw, "betfair", "exchange", DRIVERS)
    v0 = out[(out["driver_number"] == 1)].iloc[0]
    assert v0["ask"] == pytest.approx(1 / 1.30) and v0["bid"] == pytest.approx(1 / 1.32)
    v1 = out[(out["driver_number"] == 1)].iloc[1]
    assert np.isnan(v1["odds"]) and v1["lay_odds"] == 1.32  # back side emptied: no stale 1.30
    susp = out[out["utc"] == pd.Timestamp(pt + 2000, unit="ms", tz="UTC")]
    assert len(susp) == 2 and susp["suspended"].eq(1).all()  # suspension kept for both runners


def winline_csv(path):
    """Moscow local time, no tz. Verstappen drifts and Leclerc shortens after the radio; suspended 5100-5140."""
    lines = ["timestamp,selection,odds,suspended"]
    for sec in range(3600, 10800, 20):
        if sec < 5040:
            ver, lec = "1.25", "4.00"
        else:
            ver, lec = "3.00", "1.40"
        susp = int(5100 <= sec < 5140)
        local = (T0 + pd.Timedelta(seconds=sec)).tz_convert("Europe/Moscow").tz_localize(None).isoformat()
        lines += [f"{local},Max Verstappen,{ver},{susp}", f"{local},Charles Leclerc,{lec},{susp}"]
    path.write_text("\n".join(lines) + "\n")


def test_bookmaker_end_to_end(fake_net):
    d = pipeline.fetch(2026, "singapore")
    (d / "transcripts.csv").write_text("audio_file,text\nVER_b.mp3,\"I've lost power\"\n")
    ann = pipeline.ANNOTATIONS / f"{d.name}_broadcast.csv"
    ann.parent.mkdir(parents=True)
    ann.write_text(f"audio_file,broadcast_utc\nVER_b.mp3,{utc(AIR)}\n")
    winline_csv(fake_net / "winline.csv")
    pipeline.import_odds(2026, "singapore", str(fake_net / "winline.csv"), "Winline", "Europe/Moscow", None)
    odds = pipeline.load(d / "odds_winline.csv")
    assert odds["kind"].eq("bookmaker").all()
    assert odds["utc"].min() == T0 + pd.Timedelta(seconds=3600)  # Moscow time converted to UTC

    pipeline.analyze(2026, "singapore", "feed_utc", None, backtest.Params(), winner=16)
    bk = pipeline.load(d / "backtest_bookmakers.csv")
    bet = bk.iloc[0]
    assert bet["backed"] == 16 and bet["status"] == "accepted"  # bad news for VER: back Leclerc
    assert bet["odds_taken"] == 4.0
    assert bet["settled_pnl"] == pytest.approx(300.0)  # 100 at 4.0 on the winner
    assert bet["ev_after_broadcast"] > 100
    # winline is not traded in and out like a market
    assert "winline" not in set(pipeline.load(d / "backtest.csv")["venue"])
    es = pipeline.load(d / "event_study.csv")
    assert "winline" in set(es["venue"])
    assert "Fixed-odds bookmakers" in (d / "report.md").read_text()


def test_bookmaker_rejects_when_suspended():
    from f1radio.bookmakers import BookieParams, backtest as bk_backtest
    t = pd.Timestamp("2026-10-11T12:00:00Z")
    odds = pd.DataFrame({"utc": [t, t], "venue": "winline", "kind": "bookmaker", "driver_number": [1, 16],
                         "odds": [1.25, 4.0], "suspended": [1, 1], "yes_price": [0.76, 0.24]})
    es = pd.DataFrame({"audio_file": ["a"], "direction": [-1], "target_driver": [1], "category": ["failure"],
                       "t_avail": [t + pd.Timedelta(seconds=1)], "broadcast_utc": [t + pd.Timedelta(seconds=90)],
                       "confounders": [""]})
    out = bk_backtest(es, odds, winner=16, prm=BookieParams())
    assert out["status"].iloc[0] == "suspended"
