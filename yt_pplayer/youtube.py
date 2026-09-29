"""YouTube access via yt-dlp: playlists, tracks, stream resolution, SponsorBlock."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.cookies import _get_chromium_based_browser_settings, extract_cookies_from_browser
from yt_dlp.postprocessor.metadataparser import MetadataParserPP

BROWSER = os.environ.get("YT_PPLAYER_BROWSER", "chromium")
KEYRING = os.environ.get("YT_PPLAYER_KEYRING", "GNOMEKEYRING")
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "yt-pplayer"

# Segments skipped inside a track. Intro/outro are left out on purpose: in DJ mixes
# those are usually music.
SPONSORBLOCK_CATEGORIES = ["sponsor", "selfpromo", "interaction", "music_offtopic"]

# Commands that open each yt-dlp browser name, tried in order (xdg-open as a last resort).
BROWSER_COMMANDS = {
    "brave": ["brave", "brave-browser"],
    "chrome": ["google-chrome-stable", "google-chrome"],
    "chromium": ["chromium"],
    "edge": ["microsoft-edge-stable", "microsoft-edge"],
    "firefox": ["firefox"],
    "opera": ["opera"],
    "vivaldi": ["vivaldi-stable", "vivaldi"],
}


class NotLoggedIn(Exception):
    """YouTube rejected the browser cookies (logged out, or the session went stale)."""


def is_auth_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(s in msg for s in ("401", "unauthorized", "sign in", "login required", "authentication"))


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

    def reload_cookies(self) -> None:
        """Re-read the browser's cookies, e.g. after logging in again."""
        fresh = extract_cookies_from_browser(BROWSER, None, keyring=KEYRING)
        with self._cookie_lock:
            if self._cookies is None:
                self._cookies = fresh
                return
            # Refill the shared jar in place: yt-dlp's request handlers keep a reference to it.
            self._cookies.clear()
            for cookie in fresh:
                self._cookies.set_cookie(cookie)

    def cookie_stamp(self) -> float:
        """Last change of the browser's cookie database (0 if unknown), to notice a login."""
        try:
            root = Path(_get_chromium_based_browser_settings(BROWSER)["browser_dir"])
        except Exception:
            return 0.0  # not Chromium-based; logging in then needs a manual refresh
        stamps = [p.stat().st_mtime for p in (*root.glob("*/Cookies"), *root.glob("*/Network/Cookies"))]
        return max(stamps, default=0.0)

    def open_login(self) -> None:
        """Open YouTube in the browser whose cookies we read, so the user can log in."""
        cmd = next((c for c in BROWSER_COMMANDS.get(BROWSER, []) if shutil.which(c)), "xdg-open")
        subprocess.Popen([cmd, "https://www.youtube.com/"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

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
        """Fetch the user's playlists; on an auth error, re-read the cookies and retry once."""
        url = "https://www.youtube.com/feed/playlists"
        try:
            info = self._ydl(flat=True).extract_info(url, download=False)
        except Exception as e:
            if not is_auth_error(e):
                raise
            # The browser may have refreshed its session since we read the cookies.
            self.reload_cookies()
            try:
                info = self._ydl(flat=True).extract_info(url, download=False)
            except Exception as e:
                if is_auth_error(e):
                    raise NotLoggedIn(str(e)) from e
                raise
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
            segments=sponsorblock(track_id),
        )

    def download(self, track_id: str, dest: Path, name: str, fmt: str, progress) -> Path:
        """Download a track's audio to dest/<name>.<ext>, with title/artist tags.

        fmt: "original" (as YouTube serves it), "m4a", "webm" or "mp3" (re-encoded).
        progress is a yt-dlp progress hook; raising DownloadCancelled in it aborts.
        """
        formats = {"m4a": "bestaudio[ext=m4a]/bestaudio", "webm": "bestaudio[ext=webm]/bestaudio"}
        postprocessors = []
        if fmt in ("m4a", "mp3"):
            # Converts only when needed: an m4a download is kept as it is.
            postprocessors.append({"key": "FFmpegExtractAudio", "preferredcodec": fmt,
                                   "preferredquality": "192"})
        postprocessors += [
            # "Artist - Title" uploads: tag artist and title separately instead of the
            # channel as artist (YouTube Music tracks already carry proper tags).
            {"key": "MetadataParser", "when": "pre_process",
             "actions": [(MetadataParserPP.interpretter, "title", r"(?P<artist>.+?) - (?P<title>.+)")]},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]
        opts = {
            "quiet": True, "no_warnings": True, "noprogress": True, "logger": _SilentLogger(),
            "format": formats.get(fmt, "bestaudio/best"),
            "outtmpl": str(dest / (name.replace("%", "%%") + ".%(ext)s")),
            "continuedl": False, "progress_hooks": [progress], "postprocessors": postprocessors,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.__dict__["cookiejar"] = self._cookiejar()
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={track_id}", download=True)
        return Path(info["requested_downloads"][0]["filepath"])

    def has_cached_tracks(self, playlist: Playlist) -> bool:
        return (CACHE_DIR / f"pl_{playlist.id}.json").exists()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def sponsorblock(track_id: str) -> list[tuple[float, float]]:
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
