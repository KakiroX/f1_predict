"""OpenF1 (api.openf1.org) as a fallback when the F1 archive refuses requests.

Produces the same tables as f1feed.fetch_all, with one gap: OpenF1 gives each
radio clip's recording time (`date`, the same value as the archive's
capture `Utc`), but not when F1 published it to the live feed. `feed_utc` is
therefore empty, and the spoken -> feed delay cannot be measured from this
source. The analysis then uses `capture_utc` as the availability time, which
is an upper bound on what a public-data trader could have done.

OpenF1 has no track-status or session-status topic. Safety car, VSC and red
flag periods come from race-control messages, and the race window comes from
the session schedule and the chequered flag.

Radio `recording_url`s point at livetiming.formula1.com, so they fail
wherever the archive does.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .f1feed import _norm, to_utc
from .http import get_json

log = logging.getLogger(__name__)

BASE = "https://api.openf1.org/v1"


@dataclass
class Session:
    year: int
    meeting: str
    name: str
    session_key: int
    date_start: str
    date_end: str

    @property
    def path(self) -> str:
        return f"openf1:session_key={self.session_key}"

    @property
    def scheduled_start_utc(self) -> pd.Timestamp:
        return to_utc(self.date_start)


def _get(endpoint: str, **params) -> list[dict]:
    return get_json(f"{BASE}/{endpoint}", params)


def find_session(year: int, query: str, session_name: str = "Race") -> Session:
    q = _norm(query)
    for s in _get("sessions", year=year, session_name=session_name):
        hay = _norm(" ".join(str(s.get(k) or "") for k in
                             ("country_name", "location", "circuit_short_name", "meeting_name")))
        if q in hay:
            return Session(year, s.get("meeting_name") or f"{s.get('country_name')} Grand Prix",
                           s["session_name"], s["session_key"], s["date_start"], s["date_end"])
    raise LookupError(f"No OpenF1 {session_name!r} session matching {query!r} in {year}")


def drivers(key: int) -> pd.DataFrame:
    rows = {int(d["driver_number"]): {
        "driver_number": int(d["driver_number"]), "tla": d.get("name_acronym"),
        "first_name": d.get("first_name"), "last_name": d.get("last_name"),
        "full_name": d.get("full_name"), "team": d.get("team_name"),
    } for d in _get("drivers", session_key=key)}
    return pd.DataFrame(list(rows.values())).sort_values("driver_number").reset_index(drop=True)


def team_radio(key: int) -> pd.DataFrame:
    rows = [{"driver_number": int(r["driver_number"]), "capture_utc": to_utc(r["date"]), "feed_utc": pd.NaT,
             "audio_url": r["recording_url"], "audio_file": r["recording_url"].rsplit("/", 1)[-1]}
            for r in _get("team_radio", session_key=key) if r.get("recording_url")]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.drop_duplicates("audio_file").sort_values("capture_utc").reset_index(drop=True)
    df["feed_utc"] = pd.to_datetime(df["feed_utc"], utc=True)
    df["feed_lag_s"] = float("nan")
    return df


def race_control(key: int) -> pd.DataFrame:
    rows = [{"feed_utc": to_utc(m["date"]), "utc": to_utc(m["date"]), "category": m.get("category"),
             "flag": m.get("flag"), "lap": m.get("lap_number"), "driver_number": m.get("driver_number"),
             "message": m.get("message", "")} for m in _get("race_control", session_key=key)]
    return pd.DataFrame(rows)


def track_status(rc: pd.DataFrame) -> pd.DataFrame:
    """Safety car / VSC / red flag / track clear messages, standing in for the TrackStatus topic."""
    if rc.empty:
        return pd.DataFrame(columns=["feed_utc", "status", "message"])
    msg = rc["message"].fillna("")
    keep = (rc["category"].eq("SafetyCar") | rc["flag"].eq("RED")
            | msg.str.contains(r"SAFETY CAR|\bVSC\b|\bRED FLAG|TRACK CLEAR", case=False, regex=True))
    ts = rc[keep]
    return pd.DataFrame({"feed_utc": ts["feed_utc"], "status": ts["flag"].fillna(ts["category"]),
                         "message": ts["message"]}).reset_index(drop=True)


def lap_count(key: int) -> pd.DataFrame:
    """When the leader started each lap: the earliest lap start across all drivers."""
    laps = pd.DataFrame(_get("laps", session_key=key))
    if laps.empty:
        return pd.DataFrame(columns=["feed_utc", "lap", "total"])
    laps = laps.dropna(subset=["date_start"])
    laps["feed_utc"] = laps["date_start"].map(to_utc)
    first = laps.groupby("lap_number")["feed_utc"].min().reset_index()
    first = first.rename(columns={"lap_number": "lap"})
    first["total"] = int(laps["lap_number"].max())
    return first[["feed_utc", "lap", "total"]]


def pit_stops(key: int) -> pd.DataFrame:
    rows = [{"feed_utc": to_utc(p["date"]), "driver_number": int(p["driver_number"]),
             "duration": p.get("pit_duration", p.get("lane_duration")), "lap": p.get("lap_number")}
            for p in _get("pit", session_key=key)]
    return pd.DataFrame(rows)


def session_status(s: Session, rc: pd.DataFrame) -> pd.DataFrame:
    start = s.scheduled_start_utc
    chequered = rc[rc["flag"].eq("CHEQUERED")]["feed_utc"] if not rc.empty else pd.Series(dtype=object)
    end = chequered.iloc[0] if len(chequered) else to_utc(s.date_end)
    return pd.DataFrame({"feed_utc": [start, end], "status": ["Started", "Finished"]})


def fetch_all(year: int, query: str) -> dict:
    s = find_session(year, query)
    rc = race_control(s.session_key)
    log.info("OpenF1 %s %s: session_key %s", s.meeting, s.name, s.session_key)
    return {
        "session": s,
        "t0": None,
        "source": "openf1",
        "drivers": drivers(s.session_key),
        "radio": team_radio(s.session_key),
        "track_status": track_status(rc),
        "session_status": session_status(s, rc),
        "race_control": rc,
        "laps": lap_count(s.session_key),
        "pits": pit_stops(s.session_key),
    }
