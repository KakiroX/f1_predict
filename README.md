# f1_predict: team radio vs. race-winner markets

Can you make money on Polymarket or Kalshi race-winner markets by acting on a
team-radio message before the TV broadcast airs it?

This repo matches every radio clip from a race (Singapore and Baku 2026 by default)
to the winner-market prices on Polymarket and Kalshi on one UTC clock. It then
measures how much the price moved between "a fast trader could know" and
"everyone watching TV knows", and backtests trading that gap after spread and fees.

> **Status: the pipeline has not been run on real data yet.** The tests use
> synthetic fixtures to check the code; they say nothing about the markets.
>
> **The F1 archive is closed.** Since October 2026, `livetiming.formula1.com`
> answers `401 Unauthorized` for every season (2024-2026), including the radio
> MP3s. `fetch` now falls back to [OpenF1](https://openf1.org) automatically.
> See [What changes on OpenF1](#what-changes-on-openf1). Polymarket and Kalshi
> both answer normally.

## Before you run: network access

The pipeline needs HTTPS access to these hosts:

| Host | Used for |
|---|---|
| `livetiming.formula1.com` | F1 archive: radio clips, publish times, track status, laps. **Currently answers 401.** |
| `api.openf1.org` | race data when the archive refuses (radio times, race control, laps, pits, drivers) |
| `gamma-api.polymarket.com` | Polymarket event and market lookup |
| `data-api.polymarket.com` | Polymarket trades |
| `clob.polymarket.com` | Polymarket 1-minute price history |
| `ws-subscriptions-clob.polymarket.com` | live Polymarket order book (`record` only) |
| `api.elections.kalshi.com` | Kalshi events, trades and candles |

On your own machine this usually just works. In a Claude Code cloud
environment, open the environment's settings (cloud environment menu in the
session title bar, then Edit), go to Network access, and add these hosts under
Allowed domains (or pick a broader access level). Leave "Allow package
managers" ticked. Steps:
<https://code.claude.com/docs/en/cloud-environments#network-access>.

## The timing chain

```
 spoken            on live-timing feed         on world feed            on your screen
 capture_utc  -->  feed_utc               -->  broadcast_utc       -->  + stream latency
 (driver talks)    (F1 TeamRadio topic)        (FOM airs the clip)      (5-60 s, OTT/TV)
```

| Instant | Source | How it is measured |
|---|---|---|
| `capture_utc` | `Utc` field of each TeamRadio capture in the F1 archive (OpenF1: `date`, the same value) | read directly |
| `feed_utc` | publish offset of that capture in `TeamRadio.jsonStream`, converted to UTC with `Heartbeat` | read directly; **not available from OpenF1** |
| `broadcast_utc` | the TV broadcast | cross-correlate each radio MP3 against the broadcast audio (`align`), or enter it by hand in `annotations/` |
| market prices | Polymarket trades (to the second) + 1-min history; Kalshi trades + 1-min bid/ask candles | APIs, see `f1radio/markets.py` |

There is an edge only if prices move in the direction the message implies
inside the window that starts when you can know and ends when the clip airs.
`move_window` in the output is that move, signed so that positive means "moved
the way the message predicted".

## What changes on OpenF1

`fetch --source auto` (the default) tries the F1 archive and switches to
OpenF1 when it answers 401 or 403. Use `--source openf1` to skip the archive,
or `--source f1` to fail instead of falling back. `meta.json` and `report.md`
record which source was used.

| | F1 archive | OpenF1 |
|---|---|---|
| when the clip was recorded (`capture_utc`) | yes | yes, same timestamp |
| when F1 published it to the live feed (`feed_utc`) | yes | **no** |
| safety car / VSC / red flag | TrackStatus topic | derived from race-control messages |
| race start / end | SessionStatus topic | scheduled start, chequered-flag message |
| lap counter (for the `lap:N` anchor) | LapCount topic | earliest lap start across drivers |
| radio MP3s | yes | links point to the archive, so they **fail with 401 too** |

What that means for the analysis:

- **No feed publish time.** `analyze` uses the recording time as the
  availability time and says so in the report. That is the best case for a
  trader (hearing the clip as it is spoken), not what someone watching
  public data could do. The spoken -> feed delay, hypothesis 1 below, cannot
  be measured from OpenF1. The spoken -> TV delay, the gap your question is
  about, still can.
- **No audio, as long as the archive refuses.** `audio` reports how many clips
  failed and why. Without the MP3s, `transcribe` and `align` have nothing to
  work on. Instead:
  - type what each message says into `data/<year>_<race>/transcripts.csv`
    (columns `audio_file,text`, one row per clip in `radio.csv`), and
  - note when each clip aired in a replay and enter it in
    `annotations/<year>_<race>_broadcast.csv` (see `TEMPLATE_broadcast.csv`).

  Only the clips that matter for the winner market need this: radio from the
  leader and whoever is close behind in the last part of the race.

## Run it

On Windows, run these in PowerShell from the repo folder (put the `align`
command on one line, without the trailing `\`).

```bash
pip install -r requirements.txt          # ffmpeg is also needed for `align`
python -m pytest                         # 10 tests, no network

# 1. Race data (F1 archive, or OpenF1 when the archive refuses) + both markets.
#    Market ids for Baku and Singapore 2026 are built in (see below).
python -m f1radio fetch --year 2026 --race baku
python -m f1radio fetch --year 2026 --race singapore
#    For other races, or to override: --kalshi-event KXF1RACE-<code> --poly-slug <slug>

# 2. Radio audio + transcripts (check and correct the transcripts by hand)
python -m f1radio audio --year 2026 --race baku
python -m f1radio transcribe --year 2026 --race baku     # optional; writes data/2026_baku/transcripts.csv
#    Machine transcripts of radio are rough. Fix them by hand before trusting the tags,
#    or skip `transcribe` and type transcripts into that CSV yourself.

# 3. When did each clip air? Either from a recording of the broadcast...
python -m f1radio align --year 2026 --race baku --recording baku_replay.mp4 \
       --anchor-video 0:21:14.0 --anchor-event lap:2 --viewer-latency 20
#    (anchor = video time when the on-screen lap counter flips to lap 2), or with
#    --wallclock-start <UTC> for a recording you made of the live stream.
#    ...or by hand: annotations/2026_baku_broadcast.csv (see TEMPLATE_broadcast.csv)

# 4. Event study, backtest, report
python -m f1radio analyze --year 2026 --race baku
python -m f1radio analyze --year 2026 --race baku --t-avail capture_utc   # upper bound: you hear it as it is spoken
```

Output goes to `data/<year>_<race>/`: `event_study.csv`, `event_summary.csv`,
`backtest.csv`, `report.md` (with charts if matplotlib is installed).
`--race` also takes any meeting name ("Monaco", "Las Vegas"), so the same
commands run on other races. Two races is too few to conclude much, so run it
on more.

### Market identifiers

Found by querying both venues; built into `MARKET_IDS` in `f1radio/pipeline.py`:

| Race | Kalshi event | Polymarket slug |
|---|---|---|
| Baku 2026 | `KXF1RACE-AZEGP26` | `f1-azerbaijan-grand-prix-winner` or `azerbijan-grand-prix-winner` (sic) |
| Singapore 2026 | `KXF1RACE-SIN26` | not known; `fetch` searches for it |

Polymarket has two Baku winner listings, and it is not yet confirmed which one
is the 2026 race. `fetch` checks both and uses the one dated 2026; the log line
`Polymarket event: ...` says which. If neither is, it stops with the dates it found.

Kalshi's race-winner series is `KXF1RACE`. Event codes do not follow one
pattern (`AZEGP26`, `SIN26`, `MIAGP26`), so for other races look the ticker up
on Kalshi and pass `--kalshi-event`. For Polymarket, pass `--poly-slug` with the
slug from the event URL. `fetch` logs every market it could not match to a driver.

The backtest assumes 0 fees on Polymarket. Check the fee schedule of the
market you trade and pass `--poly-fee-rate`.

### Tagging messages

`f1radio/classify.py` tags each transcript with a category (`failure`,
`damage`, `retirement`, `problem`, `tyres_gone`, `penalty`, `team_order`,
`pit_call`, `other`) and a direction for the target driver's chances (-1, 0,
+1). The target is the speaker, unless the message names one other driver
("Leclerc has stopped"). Only directional messages are traded. Override any
tag with `direction_override` / `target_override` in `transcripts.csv`.

### Backtest assumptions (all flags on `analyze`)

- Entry at `t_avail + 5 s` (reaction time), exit 30 s after the broadcast.
- Kalshi fills at the 1-minute candle's bid/ask. Polymarket keeps no historical
  order book, so fills there are last trade ± 1 c (`--poly-half-spread`).
- Fees: Kalshi taker fee `ceil(0.07 × C × P × (1−P))` per order. The Polymarket
  fee defaults to 0 (`--poly-fee-rate`); check the fee schedule of the specific
  market, because Polymarket has been adding taker fees to market categories.
- Size is capped at 25 % of the contracts that actually traded in the window
  (`--participation`). For in-race F1 markets that cap usually binds before
  your stake does.
- Each event lists **confounders**: safety car or VSC changes, a pit stop by
  the target, and race-control penalty or retirement messages inside the
  window. A move that coincides with a safety car is not evidence about radio.
  The report shows P&L with and without these events.

### Record the next races live

Historical data is weakest exactly where it matters: second-level order books.
During a race, run

```bash
python -m f1radio record --out data/live/austin26 --poly-slug <slug> --kalshi-event <ticker>
```

and record the stream you watch, noting the UTC wall-clock time when the
recording starts. Then `align --wallclock-start ...` gives the moment a viewer
heard each clip, stream latency included, and `analyze --live data/live/austin26`
backtests against real bid/ask.

## Is the edge real? What to expect before running it

These are hypotheses for the pipeline to confirm or reject, not results.

1. **The live-timing feed probably does not give you the radio minutes early.**
   The F1 TeamRadio topic only contains clips that FOM selects for release.
   If FOM publishes a capture to the feed at about the time it airs, then
   `feed_utc ≈ broadcast_utc`, and the "couple of minutes" exists only between
   `capture_utc` and `feed_utc`. Nobody outside the teams and FOM can hear the
   radio in that period. The first thing to check in `report.md` is the
   *spoken → feed* and *feed → TV* distributions. If spoken → feed is the
   large one, a public-data trader has no radio edge. While the archive
   refuses requests, history cannot answer this: OpenF1 has no publish times.
2. **What a public-data trader can still have is stream latency.** Most
   bettors watch an OTT stream 20–60 s behind the world feed, and the
   live-timing feed usually runs ahead of OTT. That edge is smaller, and
   anyone with a live-timing dashboard and a bot can compete for it.
   Recording your own stream (above) measures it.
3. **Radio rarely tells the market something new.** A car losing power shows
   up in sector times, speed traps and position data within seconds. A pit
   stop shows up in pit-lane timing. Watch `move_capture_to_avail` and
   `move_pre_60s`: if the price already moved before the clip was available,
   faster public data got there first.
4. **Only a few messages per race matter for the winner market.** They are
   problems for the leader or for a close second.
   [Singapore 2026](https://www.the-race.com/formula-1/f1-2026-singapore-grand-prix-race-result/)
   was pole to win for Verstappen by 7.3 s, so expect almost no
   winner-relevant radio.
   [Baku 2026](https://www.silverstone.co.uk/news/f1-2026-azerbaijan-grand-prix-results-russell-wins-baku)
   is the better test case: Russell beat Verstappen by 0.196 s after two
   safety cars. Late-race radio about tyres, battery or lift-and-coast for
   either of them is what to look at first.
5. **Liquidity, not signal, sets the ceiling.** In-race winner markets on the
   front-runners can carry real volume, but each news event moves a thin book
   in seconds, and market makers pull quotes the moment the timing screens
   change. Kalshi's taker fee is up to 1.75 c per contract at 50 c, so an
   edge has to beat that on both legs.

**Winline** is not in the pipeline. It publishes no historical odds, live bets
are accepted after a delay of several seconds, markets suspend on incidents,
and bookmakers limit accounts that win on latency. The only way to test it
would be to log its live odds yourself during a race.

**Rules and law.** Trading on public data (live timing, your own TV feed) is
ordinary fast trading. Getting team radio any other way, such as intercepting
team frequencies or using a leak from inside a team or FOM, would be using
misappropriated information. On a CFTC-regulated venue like Kalshi that can
be a legal problem, not only a terms-of-service problem. Check that each venue
is legal where you live: Polymarket's international exchange excludes US
persons, and Winline is licensed only in Russia.

## Layout

```
f1radio/
  f1feed.py      F1 static archive: index, .jsonStream parsing, Heartbeat clock, radio/track/RC/laps/pits
  openf1.py      the same tables from api.openf1.org, used when the archive refuses
  markets.py     Polymarket (gamma, data-api, clob) and Kalshi (KXF1RACE) history, driver matching
  transcribe.py  faster-whisper transcripts (optional)
  classify.py    rule-based message tags
  broadcast.py   find each clip in broadcast audio (FFT normalised cross-correlation), video clock -> UTC
  study.py       price book, event study, confounders, placebo windows
  backtest.py    execution and fee model, P&L
  recorder.py    live order-book recorder (Polymarket websocket, Kalshi polling)
  report.py      report.md and charts
  pipeline.py    the steps above, wired to data/<year>_<race>/
tests/           synthetic fixtures, no network
annotations/     hand-entered broadcast times
```

Run the tests with `python -m pytest`.
