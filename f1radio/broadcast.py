"""Find when each team-radio clip was aired on the TV broadcast.

The F1 archive gives the radio MP3s. The broadcast plays the same audio
(under commentary and engine noise), so a normalised cross-correlation of the
band-limited clip against the broadcast audio finds the airing to within a
few milliseconds. Video time is converted to UTC with one of:

* ``--wallclock-start``: UTC when you started recording a *live* stream.
  This includes your stream's own latency, i.e. it measures when a viewer
  on that stream actually heard the message. Preferred.
* ``--anchor-video`` + ``--anchor-utc``/feed event: a moment visible in the
  replay (lights out) whose UTC is known from the feed. This measures delay
  relative to the world feed; add your viewers' stream latency on top
  (``--viewer-latency``), since most bettors watch 5-60 s behind real time.

Needs ffmpeg on PATH.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

SR = 8000
_BAND = "highpass=f=300,lowpass=f=3400"  # radio voice band; drops most engine noise


def load_audio(path: str | Path, start_s: float | None = None, duration_s: float | None = None,
               sr: int = SR) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start_s is not None:
        cmd += ["-ss", f"{max(start_s, 0):.3f}"]
    if duration_s is not None:
        cmd += ["-t", f"{duration_s:.3f}"]
    cmd += ["-i", str(path), "-vn", "-ac", "1", "-ar", str(sr), "-af", _BAND, "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def trim_template(clip: np.ndarray, sr: int = SR, max_s: float = 8.0) -> tuple[np.ndarray, int]:
    """Cut leading silence and cap the length; returns (template, samples cut from the start).

    A short template is more robust to the broadcast editing or bleeping part of a clip.
    """
    win = sr // 50
    if len(clip) < win:
        return clip, 0
    energy = np.convolve(clip ** 2, np.ones(win) / win, mode="same")
    loud = np.flatnonzero(energy > 0.05 * energy.max())
    lead = int(loud[0]) if loud.size else 0
    return clip[lead: lead + int(max_s * sr)], lead


def ncc(signal: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation in 'valid' mode, values in [-1, 1]."""
    n = len(template)
    if len(signal) < n or n == 0:
        return np.zeros(0)
    t = template - template.mean()
    t_norm = np.sqrt((t ** 2).sum())
    if t_norm == 0:
        return np.zeros(len(signal) - n + 1)
    size = 1 << int(np.ceil(np.log2(len(signal) + n)))
    corr = np.fft.irfft(np.fft.rfft(signal, size) * np.conj(np.fft.rfft(t, size)), size)[: len(signal) - n + 1]
    c1 = np.concatenate([[0.0], np.cumsum(signal)])
    c2 = np.concatenate([[0.0], np.cumsum(signal ** 2)])
    s1 = c1[n:] - c1[:-n]
    s2 = c2[n:] - c2[:-n]
    local = np.sqrt(np.maximum(s2 - s1 ** 2 / n, 1e-12))
    return corr / (local * t_norm)


@dataclass
class Match:
    offset_s: float  # where the clip starts in the searched audio
    score: float     # NCC at the chosen airing
    airings: int     # how many distinct airings scored as matches (replays)


def locate(signal: np.ndarray, clip: np.ndarray, sr: int = SR, min_score: float = 0.35) -> Match | None:
    """Earliest airing of `clip` in `signal` (the broadcast often replays radio later)."""
    template, lead = trim_template(clip, sr)
    scores = ncc(signal, template)
    if scores.size == 0:
        return None
    best = scores.max()
    if best < min_score:
        return None
    thresh = max(min_score, 0.6 * best)
    above = np.flatnonzero(scores >= thresh)
    # Group hits closer than one template length into one airing; keep each group's peak.
    groups = np.split(above, np.flatnonzero(np.diff(above) > len(template)) + 1)
    peaks = [g[np.argmax(scores[g])] for g in groups if g.size]
    first = min(peaks)
    return Match(offset_s=(first - lead) / sr, score=float(scores[first]), airings=len(peaks))


def align(radio: pd.DataFrame, broadcast: str | Path, audio_dir: Path, video_to_utc,
          utc_to_video, max_delay_s: float = 900.0, pre_s: float = 30.0) -> pd.DataFrame:
    """Add broadcast_utc for every clip found in the broadcast recording.

    Each clip is searched only from its capture time (minus `pre_s`) to
    `max_delay_s` after it, which keeps the FFTs small and avoids matching a
    later replay.
    """
    out = []
    for _, r in radio.iterrows():
        clip_path = audio_dir / r["audio_file"]
        row = r.to_dict() | {"broadcast_utc": pd.NaT, "match_score": np.nan, "airings": 0}
        if not clip_path.exists():
            log.warning("missing clip %s (run download-audio)", clip_path)
            out.append(row)
            continue
        start_v = utc_to_video(r["capture_utc"]) - pre_s
        clip = load_audio(clip_path)
        sig = load_audio(broadcast, start_s=start_v, duration_s=pre_s + max_delay_s + len(clip) / SR)
        m = locate(sig, clip)
        if m:
            row |= {"broadcast_utc": video_to_utc(max(start_v, 0) + m.offset_s),
                    "match_score": m.score, "airings": m.airings}
        out.append(row)
    df = pd.DataFrame(out)
    df["broadcast_delay_s"] = (df["broadcast_utc"] - df["capture_utc"]).dt.total_seconds()
    df["feed_to_broadcast_s"] = (df["broadcast_utc"] - df["feed_utc"]).dt.total_seconds()
    return df


def linear_clock(anchor_video_s: float, anchor_utc: pd.Timestamp, viewer_latency_s: float = 0.0):
    """(video_to_utc, utc_to_video) for a recording with a known anchor point."""
    def video_to_utc(v: float) -> pd.Timestamp:
        return anchor_utc + pd.Timedelta(seconds=v - anchor_video_s + viewer_latency_s)

    def utc_to_video(t: pd.Timestamp) -> float:
        return (t - anchor_utc).total_seconds() + anchor_video_s - viewer_latency_s

    return video_to_utc, utc_to_video


def parse_clock(s: str) -> float:
    """'1:02:03.5' / '62:03.5' / '3723.5' -> seconds."""
    parts = [float(p) for p in str(s).split(":")]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs
