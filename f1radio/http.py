"""Small HTTP helper: retries with backoff and an on-disk cache.

Historical race data never changes, so every GET is cached under
``data/cache`` keyed by URL + params. Delete the folder to force a refetch.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "f1radio-research/0.1"


def _cache_path(url: str, params: dict | None, suffix: str) -> Path:
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + suffix)


def _get(url: str, params: dict | None = None, retries: int = 4) -> requests.Response:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = SESSION.get(url, params=params, timeout=60)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code} for {resp.url}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                raise
            if attempt == retries:
                raise
            log.warning("GET %s failed (%s); retrying in %.0fs", url, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def get_text(url: str, params: dict | None = None, cache: bool = True) -> str:
    path = _cache_path(url, params, ".txt")
    if cache and path.exists():
        return path.read_text(encoding="utf-8")
    resp = _get(url, params)
    # F1 static files are UTF-8 with a BOM; requests guesses ISO-8859-1 otherwise.
    text = resp.content.decode("utf-8-sig")
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return text


def get_json(url: str, params: dict | None = None, cache: bool = True):
    return json.loads(get_text(url, params, cache))


def download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_get(url).content)
    return dest
