"""Formula 1 live-timing archive (livetiming.formula1.com/static).

Every topic is archived as a ``.jsonStream`` file whose lines look like::

    00:41:07.312{"Captures":{"7":{"Utc":"...","RacingNumber":"16","Path":"TeamRadio/..."}}}

The leading offset is when the message was *published on the live feed*,
relative to the start of the stream. That is the earliest moment anyone
watching live timing (F1 TV, F1 app, third-party dashboards) could have seen
it. The ``Utc`` inside a TeamRadio capture is when the clip was recorded.
The two differ, and the difference is part of what this project measures.

Offsets are converted to UTC with the Heartbeat topic, which publishes its
own ``Utc`` every few seconds.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

import pandas as pd

from .http import get_json, get_text

log = logging.getLogger(__name__)

BASE = "https://livetiming.formula1.com/static/"
_LINE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d+)(.*)$")


@dataclass
class Session:
    year: int
    meeting: str
    name: str
    path: str  # e.g. 2026/2026-10-11_Singapore_Grand_Prix/2026-10-11_Race/
    start_local: str
    gmt_offset: str

    @property
    def url(self) -> str:
        return BASE + self.path

    @property
    def scheduled_start_utc(self) -> pd.Timestamp:
        h, m, s = (int(x) for x in self.gmt_offset.lstrip("-").split(":"))
        sign = -1 if self.gmt_offset.startswith("-") else 1
        local = pd.Timestamp(self.start_local)
        return (local - sign * pd.Timedelta(hours=h, minutes=m, seconds=s)).tz_localize("UTC")


def to_utc(s) -> pd.Timestamp:
    ts = pd.Timestamp(s)
    return ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def find_session(year: int, query: str, session_name: str = "Race") -> Session:
    """Find e.g. (2026, "Singapore", "Race") in the season index."""
    index = get_json(f"{BASE}{year}/Index.json")
    q = _norm(query)
    for meeting in index["Meetings"]:
        hay = _norm(" ".join([
            meeting.get("Name", ""), meeting.get("Location", ""),
            meeting.get("Country", {}).get("Name", ""), meeting.get("OfficialName", ""),
        ]))
        if q not in hay:
            continue
        for s in meeting["Sessions"]:
            if s.get("Name") == session_name and s.get("Path"):
                return Session(year, meeting["Name"], s["Name"], s["Path"],
                               s["StartDate"], s.get("GmtOffset", "00:00:00"))
    raise LookupError(f"No {session_name!r} session matching {query!r} in {year}")


def parse_jsonstream(text: str) -> list[tuple[pd.Timedelta, dict]]:
    import json

    out = []
    for line in text.lstrip("﻿").splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        h, mi, s, frac, payload = m.groups()
        offset = pd.Timedelta(hours=int(h), minutes=int(mi), seconds=int(s),
                              microseconds=int(frac.ljust(6, "0")[:6]))
        try:
            out.append((offset, json.loads(payload)))
        except json.JSONDecodeError:
            log.debug("skipping unparsable line: %s", line[:80])
    return out


def stream(session: Session, topic: str) -> list[tuple[pd.Timedelta, dict]]:
    return parse_jsonstream(get_text(f"{session.url}{topic}.jsonStream"))


def stream_t0(heartbeats: list[tuple[pd.Timedelta, dict]]) -> pd.Timestamp:
    """UTC instant of offset 00:00:00, estimated from Heartbeat messages."""
    est = [to_utc(m["Utc"]) - off for off, m in heartbeats if "Utc" in m]
    if not est:
        raise ValueError("no Heartbeat.Utc messages to anchor the stream")
    est = pd.Series(est)
    return est.sort_values().iloc[len(est) // 2]  # median, robust to stragglers


def _items(container):
    """Feed collections arrive as a list in the first keyframe, then as {"idx": item} patches."""
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        return list(container.values())
    return []


def team_radio(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for off, msg in stream(session, "TeamRadio"):
        for cap in _items(msg.get("Captures")):
            if not isinstance(cap, dict) or "Path" not in cap:
                continue
            rows.append({
                "driver_number": int(cap["RacingNumber"]),
                "capture_utc": to_utc(cap["Utc"]),
                "feed_utc": t0 + off,
                "audio_url": session.url + cap["Path"],
                "audio_file": cap["Path"].rsplit("/", 1)[-1],
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates("audio_file").sort_values("feed_utc").reset_index(drop=True)
    df["feed_lag_s"] = (df["feed_utc"] - df["capture_utc"]).dt.total_seconds()
    return df


def track_status(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = [{"feed_utc": t0 + off, "status": m.get("Status"), "message": m.get("Message")}
            for off, m in stream(session, "TrackStatus") if "Status" in m]
    return pd.DataFrame(rows)


def session_status(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = [{"feed_utc": t0 + off, "status": m.get("Status")}
            for off, m in stream(session, "SessionStatus") if "Status" in m]
    return pd.DataFrame(rows)


def race_control(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for off, msg in stream(session, "RaceControlMessages"):
        for m in _items(msg.get("Messages")):
            if isinstance(m, dict) and "Message" in m:
                rows.append({"feed_utc": t0 + off, "utc": to_utc(m["Utc"]) if "Utc" in m else pd.NaT,
                             "category": m.get("Category"), "flag": m.get("Flag"), "lap": m.get("Lap"),
                             "driver_number": m.get("RacingNumber"), "message": m["Message"]})
    return pd.DataFrame(rows)


def lap_count(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = [{"feed_utc": t0 + off, "lap": m["CurrentLap"], "total": m.get("TotalLaps")}
            for off, m in stream(session, "LapCount") if "CurrentLap" in m]
    return pd.DataFrame(rows)


def pit_stops(session: Session, t0: pd.Timestamp) -> pd.DataFrame:
    rows = []
    for off, msg in stream(session, "PitLaneTimeCollection"):
        for num, p in (msg.get("PitTimes") or {}).items():
            if isinstance(p, dict) and p.get("Duration"):
                rows.append({"feed_utc": t0 + off, "driver_number": int(num),
                             "duration": p.get("Duration"), "lap": p.get("Lap")})
    return pd.DataFrame(rows)


def drivers(session: Session) -> pd.DataFrame:
    data = get_json(f"{session.url}DriverList.json")
    rows = [{"driver_number": int(num), "tla": d.get("Tla"), "first_name": d.get("FirstName"),
             "last_name": d.get("LastName"), "full_name": d.get("FullName"), "team": d.get("TeamName")}
            for num, d in data.items() if isinstance(d, dict) and "Tla" in d]
    return pd.DataFrame(rows).sort_values("driver_number").reset_index(drop=True)


def fetch_all(year: int, query: str) -> dict[str, pd.DataFrame | Session]:
    session = find_session(year, query)
    t0 = stream_t0(stream(session, "Heartbeat"))
    log.info("%s %s: stream t0 = %s", session.meeting, session.name, t0)
    return {
        "session": session,
        "t0": t0,
        "drivers": drivers(session),
        "radio": team_radio(session, t0),
        "track_status": track_status(session, t0),
        "session_status": session_status(session, t0),
        "race_control": race_control(session, t0),
        "laps": lap_count(session, t0),
        "pits": pit_stops(session, t0),
    }
