"""Speech-to-text for radio clips with faster-whisper (optional dependency).

Machine transcripts of F1 radio are rough (engine noise, accents, jargon).
The output CSV is meant to be checked by hand: edit `text`, or set
`direction_override` (-1/0/1) and `target_override` (car number) directly.
Rows you have edited are never overwritten.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

PROMPT = ("Formula 1 team radio. Box box. Copy. Tyres, deg, delta, safety car, VSC, "
          "DRS, MGU-K, ERS, harvest, lift and coast, plan B, P1, gap, undercut.")


def transcribe(radio: pd.DataFrame, audio_dir: Path, out_csv: Path, model_size: str = "small.en") -> pd.DataFrame:
    existing = pd.read_csv(out_csv) if out_csv.exists() else pd.DataFrame(columns=["audio_file", "text"])
    done = set(existing.loc[existing["text"].notna(), "audio_file"])
    todo = [f for f in radio["audio_file"] if f not in done and (audio_dir / f).exists()]
    if todo:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device="auto", compute_type="int8")
        rows = []
        for f in todo:
            segs, _ = model.transcribe(str(audio_dir / f), language="en", initial_prompt=PROMPT, vad_filter=True)
            text = " ".join(s.text.strip() for s in segs)
            log.info("%s: %s", f, text)
            rows.append({"audio_file": f, "text": text})
        existing = pd.concat([existing[existing["audio_file"].isin(done)], pd.DataFrame(rows)], ignore_index=True)
    for col in ("direction_override", "target_override"):
        if col not in existing:
            existing[col] = pd.NA
    existing.to_csv(out_csv, index=False)
    return existing
