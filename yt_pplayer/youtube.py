"""YouTube access via yt-dlp: playlists, tracks, stream resolution, SponsorBlock."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.cookies import extract_cookies_from_browser

BROWSER = os.environ.get("YT_PPLAYER_BROWSER", "chromium")
KEYRING = os.environ.get("YT_PPLAYER_KEYRING", "GNOMEKEYRING")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yt-pplayer"

# Segments skipped inside a track. Intro/outro are left out on purpose: in DJ mixes
# those are usually music.
SPONSORBLOCK_CATEGORIES = ["sponsor", "selfpromo", "interaction", "music_offtopic"]


@dataclass
class Playlist:
    id: str
    title: str
    url: str


@dataclass
class Track:
    id: str
    title: str
    channel: str = ""
    duration: float | None = None


@dataclass
class Stream:
    url: str
    headers: dict[str, str]
    expires: float
    segments: list[tuple[float, float]] = field(default_factory=list)


class YouTube:
    def __init__(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._cookies = None
        self._cookie_lock = threading.Lock()
        self._local = threading.local()
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ytdl")
        self._streams: dict[str, Future[Stream]] = {}

    # -- yt-dlp plumbing -------------------------------------------------

    def _cookiejar(self):
        # Decrypting browser cookies is slow; do it once and share the jar in memory
        # (nothing is written to disk).
        with self._cookie_lock:
            if self._cookies is None:
                self._cookies = extract_cookies_from_browser(BROWSER, None, keyring=KEYRING)
            return self._cookies

    def _ydl(self, flat: bool) -> yt_dlp.YoutubeDL:
        key = "flat" if flat else "full"
        ydl = getattr(self._local, key, None)
        if ydl is None:
            opts = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _SilentLogger()}
            if flat:
                opts["extract_flat"] = "in_playlist"
            else:
                opts["format"] = "bestaudio/best"
            ydl = yt_dlp.YoutubeDL(opts)
            ydl.__dict__["cookiejar"] = self._cookiejar()
            setattr(self._local, key, ydl)
        return ydl

    # -- playlists -------------------------------------------------------

    def cached_playlists(self) -> list[Playlist]:
        return [Playlist(**p) for p in _read_json(CACHE_DIR / "playlists.json", [])]

    def fetch_playlists(self) -> list[Playlist]:
        info = self._ydl(flat=True).extract_info("https://www.youtube.com/feed/playlists", download=False)
        result = []
        for e in info.get("entries") or []:
            url = e.get("url") or ""
            pid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("list", [e.get("id")])[0]
            result.append(Playlist(id=pid, title=e.get("title") or pid, url=url))
        _write_json(CACHE_DIR / "playlists.json", [asdict(p) for p in result])
        return result

    def cached_tracks(self, playlist: Playlist) -> list[Track]:
        return [Track(**t) for t in _read_json(CACHE_DIR / f"pl_{playlist.id}.json", [])]

    def fetch_tracks(self, playlist: Playlist) -> list[Track]:
        info = self._ydl(flat=True).extract_info(playlist.url, download=False)
        result = []
        for e in info.get("entries") or []:
            if not e or not e.get("id"):
                continue
            title = e.get("title") or e["id"]
            if title in ("[Private video]", "[Deleted video]"):
                continue
            result.append(Track(
                id=e["id"],
                title=title,
                channel=e.get("channel") or e.get("uploader") or "",
                duration=e.get("duration"),
            ))
        _write_json(CACHE_DIR / f"pl_{playlist.id}.json", [asdict(t) for t in result])
        return result

    # -- streams ---------------------------------------------------------

    def stream(self, track_id: str) -> Future[Stream]:
        """Resolve (or return the in-flight/cached resolution of) a track's audio stream."""
        fut = self._streams.get(track_id)
        if fut is not None:
            if not fut.done():
                return fut
            if fut.exception() is None and fut.result().expires > time.time() + 60:
                return fut
        fut = self._pool.submit(self._resolve, track_id)
        self._streams[track_id] = fut
        return fut

    def _resolve(self, track_id: str) -> Stream:
        info = self._ydl(flat=False).extract_info(f"https://www.youtube.com/watch?v={track_id}", download=False)
        url = info["url"]
        expire = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("expire", [0])[0]
        return Stream(
            url=url,
            headers=info.get("http_headers") or {},
            expires=float(expire) or time.time() + 3600,
            segments=_sponsorblock(track_id),
        )

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _sponsorblock(track_id: str) -> list[tuple[float, float]]:
    query = urllib.parse.urlencode({"videoID": track_id, "categories": json.dumps(SPONSORBLOCK_CATEGORIES)})
    try:
        with urllib.request.urlopen(f"https://sponsor.ajay.app/api/skipSegments?{query}", timeout=4) as r:
            data = json.load(r)
    except Exception:
        return []  # 404 = no segments; network errors are not worth failing playback over
    return sorted((float(s["segment"][0]), float(s["segment"][1])) for s in data if s.get("actionType") == "skip")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


class _SilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
