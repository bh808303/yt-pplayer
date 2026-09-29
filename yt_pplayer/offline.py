"""Offline favourites: playlists kept on disk, downloaded in the background.

Each offline playlist gets a folder named after it under ~/Music/yt-pplayer (the XDG
music folder), with tracks named "Artist - Title [<video id>].<ext>". The folder's
hidden .yt-pplayer.json lists its complete tracks (an entry is added once a download
has finished) with their info and SponsorBlock segments. A track in several offline
playlists is hard-linked, so it takes disk space once.
What to keep lives in $XDG_STATE_HOME/yt-pplayer/offline.json.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from yt_dlp.utils import DownloadCancelled

from .youtube import Playlist, Track, YouTube, sponsorblock

STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "yt-pplayer/offline.json"

INDEX = ".yt-pplayer.json"
FORMATS = ("original", "m4a", "webm", "mp3")
BYTES_PER_SECOND = 20_000        # ~1.2 MB/min: a little above YouTube's usual audio bitrates
MP3_BYTES_PER_SECOND = 24_000    # 192 kbps
UNKNOWN_TRACK_BYTES = 100_000_000
RESERVE_BYTES = 1_000_000_000    # never fill the disk beyond this
PAUSE_BETWEEN = 3                # seconds between downloads, so YouTube doesn't throttle us
MAX_ERRORS_IN_ROW = 3            # of other errors (network down, ...) before pausing
# yt-dlp messages for a track that's gone or blocked (removed, private, not in your
# country): skipped with a ✗, the rest carries on.
UNAVAILABLE = ("unavailable", "not available", "private video", "removed", "terminated",
               "copyright", "blocked", "account", "confirm your age")
# YouTube pushing back on the whole session: pause right away instead of hammering it.
THROTTLED = ("rate-limit", "rate limit", "not a bot", "too many requests", "http error 429")
# Only files named like this ("... [<video id>].<ext>") are ever deleted, so other files in
# the folders are safe. Includes yt-dlp's in-progress names: .part, .part-Frag<n>, .ytdl,
# and the .temp.<ext> of a fixup or conversion step.
OWN_FILE = re.compile(r".* \[([A-Za-z0-9_-]{11})\](\.temp)?\.[a-z0-9]{2,5}(\.part(-Frag\d+)?|\.ytdl)?")


def music_dir() -> Path:
    """The XDG music folder (~/.config/user-dirs.dirs), else ~/Music."""
    try:
        for line in (Path.home() / ".config/user-dirs.dirs").read_text().splitlines():
            if line.startswith("XDG_MUSIC_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                return Path(value.replace("$HOME", str(Path.home())))
    except OSError:
        pass
    return Path.home() / "Music"


def offline_dir() -> Path:
    custom = os.environ.get("YT_PPLAYER_OFFLINE_DIR")
    return Path(custom).expanduser() if custom else music_dir() / "yt-pplayer"


def offline_format() -> str:
    """YT_PPLAYER_OFFLINE_FORMAT: original (default), m4a, webm or mp3."""
    fmt = os.environ.get("YT_PPLAYER_OFFLINE_FORMAT", "original").strip().lower()
    return fmt if fmt in FORMATS else "original"


def safe_name(text: str, limit: int = 150) -> str:
    """Text made safe as a file or folder name, also on FAT/exFAT USB sticks."""
    name = re.sub(r'[*?"<>|]', "", text).replace(": ", " - ")
    name = re.sub(r'[/\\:\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(".")[:limit].strip()
    return "_" + name[1:] if name.startswith(".") else name


def folder_name(title: str) -> str:
    return safe_name(title) or "Playlist"


def file_name(track: Track) -> str:
    """ "Artist - Title [<id>]" (no extension). YouTube Music's generated tracks have a
    bare title and an "Artist - Topic" channel: put the artist in front."""
    title = track.title
    if " - " not in title and track.channel.endswith(" - Topic"):
        title = f"{track.channel.removesuffix(' - Topic')} - {title}"
    return f"{safe_name(title) or 'Track'} [{track.id}]"


def bytes_per_second() -> int:
    return MP3_BYTES_PER_SECOND if offline_format() == "mp3" else BYTES_PER_SECOND


def estimate(tracks: list[Track]) -> tuple[int, int]:
    """(estimated bytes, number of tracks of unknown length) for downloading tracks."""
    known = [t.duration for t in tracks if t.duration]
    return int(sum(known) * bytes_per_second()), len(tracks) - len(known)


def fmt_size(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _disk_mounted(path: Path) -> bool:
    """False for a folder on a removable disk that isn't mounted: writing there would
    silently fill the disk the mount point lives on instead."""
    for root in (Path("/run/media"), Path("/media"), Path("/mnt")):
        if path.is_relative_to(root):
            return any(p.exists() and os.path.ismount(p)
                       for p in [path, *path.parents] if p.is_relative_to(root) and p != root)
    return True


def _track(d: dict) -> Track:
    return Track(d["id"], d["title"], d.get("channel", ""), d.get("duration"))


class Offline:
    def __init__(self, yt: YouTube, on_event: Callable[[str, str], None]) -> None:
        """on_event(kind, message) is called from the download thread: kind is
        "done" (a track finished), "paused" or "error"."""
        self.yt = yt
        self.dir = offline_dir()
        self._on_event = on_event
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = False
        self._cancel: str | None = None       # track id whose download must abort
        self.playlists: dict[str, Playlist] = {}
        self.folders: dict[str, str] = {}     # playlist id -> its folder name
        self.kept: dict[str, dict[str, dict]] = {}  # folder -> tracks kept there ("keep")
        self.files: dict[str, dict[str, Path]] = {}  # track id -> folder -> audio file
        self.meta: dict[str, dict] = {}       # track id -> index entry (track info, segments)
        self.index: dict[str, dict[str, dict]] = {}  # folder -> its .yt-pplayer.json
        self.wanted: dict[str, Track] = {}    # everything to keep, in download order
        self.need: dict[str, list[str]] = {}  # track id -> folders it belongs in
        self.current: tuple[str, str] | None = None  # (track id, folder) being downloaded
        self.paused = ""                      # reason downloads stopped, until woken
        self.failed: dict[str, str] = {}      # id -> reason (this session only)
        self.version = 0                      # bumped whenever markers may have changed
        self._load()
        self._thread = threading.Thread(target=self._run, name="offline", daemon=True)
        self._thread.start()

    # -- state -----------------------------------------------------------

    def _load(self) -> None:
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            state = {}
        self.playlists = {p["id"]: Playlist(**p) for p in state.get("playlists", [])}
        self.folders = {pid: state.get("folders", {}).get(pid) or folder_name(p.title)
                        for pid, p in self.playlists.items()}
        self.kept = {folder: {t["id"]: t for t in tracks}
                     for folder, tracks in state.get("kept", {}).items()}
        self._scan()
        self._remove_partial()
        self._recompute()

    def _scan(self) -> None:
        files: dict[str, dict[str, Path]] = {}
        meta: dict[str, dict] = {}
        index: dict[str, dict[str, dict]] = {}
        if self.dir.is_dir():
            for path in self.dir.glob(f"*/{INDEX}"):
                folder = path.parent.name
                try:
                    entries = json.loads(path.read_text())
                    entries = {tid: e for tid, e in entries.items() if (path.parent / e["file"]).exists()}
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    continue
                index[folder] = entries
                for tid, e in entries.items():
                    files.setdefault(tid, {})[folder] = path.parent / e["file"]
                    meta[tid] = e
        with self._lock:
            self.files, self.meta, self.index = files, meta, index
            self.version += 1

    def _save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "playlists": [asdict(p) for p in self.playlists.values()],
            "folders": self.folders,
            "kept": {folder: list(tracks.values()) for folder, tracks in self.kept.items() if tracks},
        }, indent=1))
        tmp.replace(STATE_FILE)

    def _recompute(self) -> None:
        # Under the lock as a whole: otherwise the download thread could store a result
        # computed from the choices as they were just before a change.
        with self._lock:
            wanted: dict[str, Track] = {}
            need: dict[str, list[str]] = {}
            for pid, p in self.playlists.items():
                for t in self.yt.cached_tracks(p):
                    wanted.setdefault(t.id, t)
                    need.setdefault(t.id, []).append(self.folders[pid])
            for folder, tracks in self.kept.items():
                for tid, t in tracks.items():
                    wanted.setdefault(tid, _track(t))
                    need.setdefault(tid, []).append(folder)
            self.wanted, self.need = wanted, need
            self.version += 1

    def refresh(self) -> None:
        """A playlist's track list changed (e.g. it was re-fetched): re-evaluate."""
        self._recompute()
        self._wake.set()

    def rename(self, playlists: list[Playlist]) -> None:
        """Follow playlists renamed on YouTube: rename their folders too."""
        changed = False
        with self._lock:
            for p in playlists:
                old = self.folders.get(p.id)
                if old is None or self.playlists[p.id].title == p.title:
                    continue
                self.playlists[p.id] = p
                new = self._unique_folder(p)
                if new != old and not (self.dir / new).exists():
                    if (self.dir / old).is_dir():
                        (self.dir / old).rename(self.dir / new)
                    self.folders[p.id] = new
                changed = True
            if changed:
                self._save()
        if changed:
            self._scan()
            self._recompute()

    def _unique_folder(self, playlist: Playlist) -> str:
        name = folder_name(playlist.title)
        taken = {f for pid, f in self.folders.items() if pid != playlist.id}
        return f"{name} ({playlist.id[-6:]})" if name in taken else name

    def _remove_partial(self) -> None:
        """Delete unfinished downloads: our files the folder's index doesn't list."""
        if not self.dir.is_dir():
            return
        for p in self.dir.glob("*/*"):
            m = OWN_FILE.fullmatch(p.name)
            if m and self.files.get(m.group(1), {}).get(p.parent.name) != p:
                p.unlink(missing_ok=True)

    def _save_index(self, folder: str) -> None:
        path = self.dir / folder / INDEX
        entries = self.index.get(folder, {})
        if not entries:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_name(INDEX + ".tmp")
        tmp.write_text(json.dumps(entries, indent=1))
        tmp.replace(path)

    def _delete(self, track_id: str, folder: str) -> None:
        with self._lock:
            path = self.files.get(track_id, {}).pop(folder, None)
            if not self.files.get(track_id):
                self.files.pop(track_id, None)
            self.index.get(folder, {}).pop(track_id, None)
            self._save_index(folder)
            self.version += 1
        if path is not None and OWN_FILE.fullmatch(path.name):
            path.unlink(missing_ok=True)

    def _cleanup(self) -> None:
        """Delete downloads nothing wants any more (e.g. removed from a playlist on YouTube)."""
        # Without a cached track list we can't tell what an offline playlist holds: keep all.
        if not all(self.yt.has_cached_tracks(p) for p in self.playlists.values()):
            return
        emptied = set()
        for tid, where in list(self.files.items()):
            for folder in list(where):
                if folder not in self.need.get(tid, []):
                    self._delete(tid, folder)
                    emptied.add(folder)
        for folder in emptied:
            try:
                (self.dir / folder).rmdir()  # only if nothing else is left in it
            except OSError:
                pass

    # -- queries (UI thread) ---------------------------------------------

    def status(self, track_id: str) -> str:
        """"done", "failed" (this session), "wanted" or ""."""
        if track_id in self.files:
            return "done"
        if track_id not in self.wanted:
            return ""
        return "failed" if track_id in self.failed else "wanted"

    def local(self, track_id: str) -> tuple[Path, list[tuple[float, float]]] | None:
        for path in self.files.get(track_id, {}).values():
            if path.exists():
                return path, [tuple(s) for s in self.meta.get(track_id, {}).get("segments", [])]
        return None

    def playlist_progress(self, playlist: Playlist) -> tuple[int, int] | None:
        """(downloaded, total) for an offline playlist, else None."""
        folder = self.folders.get(playlist.id)
        if folder is None:
            return None
        ids = [t.id for t in self.yt.cached_tracks(playlist)]
        return sum(1 for i in ids if folder in self.files.get(i, {})), len(ids)

    def progress(self) -> tuple[int, int]:
        with self._lock:
            return sum(1 for t in self.wanted if t in self.files), len(self.wanted)

    def failed_count(self, playlist: Playlist | None = None) -> int:
        ids = [t.id for t in self.yt.cached_tracks(playlist)] if playlist else list(self.wanted)
        return sum(1 for i in ids if i in self.failed and i in self.wanted)

    def missing(self, tracks: list[Track]) -> list[Track]:
        """Tracks that need downloading (ones on disk elsewhere are just linked)."""
        return [t for t in tracks if t.id not in self.files]

    def downloaded_in(self, playlist: Playlist) -> list[str]:
        folder = self.folders.get(playlist.id)
        return [t.id for t in self.yt.cached_tracks(playlist) if folder in self.files.get(t.id, {})]

    def size_in(self, playlist: Playlist) -> int:
        folder = self.folders.get(playlist.id)
        return sum(self.files[tid][folder].stat().st_size for tid in self.downloaded_in(playlist))

    def free_space(self) -> int | None:
        """Free bytes on the disk the offline folder is (or will be) on; None if unreachable."""
        path = self.dir
        if not _disk_mounted(path):
            return None
        while not path.exists():
            path = path.parent
        return shutil.disk_usage(path).free

    # -- changes (UI thread) ---------------------------------------------

    def add_playlist(self, playlist: Playlist) -> None:
        with self._lock:
            folder = self._unique_folder(playlist)
            self.playlists[playlist.id] = playlist
            self.folders[playlist.id] = folder
            self.kept.pop(folder, None)  # tracks kept there are covered by the playlist again
            self._save()
        self.paused = ""
        self.failed.clear()  # retry tracks that failed (e.g. while the network was down)
        self._recompute()
        self._wake.set()

    def remove_playlist(self, playlist: Playlist, keep: bool) -> None:
        """Stop keeping a playlist offline; keep=True keeps its downloaded tracks."""
        with self._lock:
            ids = set(self.downloaded_in(playlist))
            self.playlists.pop(playlist.id, None)
            folder = self.folders.pop(playlist.id, None)
            if keep and folder and ids:
                self.kept[folder] = {tid: {**asdict(_track(self.meta[tid]["track"]))}
                                     for tid in ids}
            self._save()
        self._recompute()
        if self.current and folder not in self.need.get(self.current[0], []) \
                and self.current[1] == folder:
            self._cancel = self.current[0]
        self._cleanup()
        self._wake.set()

    def shutdown(self) -> None:
        self._stop = True
        if self.current:
            self._cancel = self.current[0]
        self._wake.set()
        self._thread.join(timeout=3)
        self._remove_partial()

    # -- download thread -------------------------------------------------

    def _todo(self) -> list[tuple[Track, str]]:
        with self._lock:
            return [(t, folder) for t in self.wanted.values() for folder in self.need.get(t.id, [])
                    if folder not in self.files.get(t.id, {}) and t.id not in self.failed]

    def _run(self) -> None:
        synced: set[str] = set()
        errors_in_row = 0
        while not self._stop:
            # Fetch each offline playlist once per session, so tracks added on YouTube
            # get downloaded (and removed ones deleted).
            for p in list(self.playlists.values()):
                if p.id not in synced and not self._stop:
                    try:
                        self.yt.fetch_tracks(p)
                        synced.add(p.id)
                    except Exception:
                        pass  # offline or logged out: work from the cached list
            self._recompute()
            self._cleanup()
            todo = self._todo()
            if not todo or self.paused:
                self._wake.wait()
                self._wake.clear()
                continue
            track, folder = todo[0]
            if track.id in self.files:
                self._link(track, folder)  # already on disk for another playlist
                continue
            free = self.free_space()
            need = (track.duration or 0) * bytes_per_second() or UNKNOWN_TRACK_BYTES
            if free is None or free - need < RESERVE_BYTES:
                self.paused = ("offline folder not reachable" if free is None
                               else "disk almost full")
                self._on_event("paused", f"Offline downloads paused: {self.paused}")
                continue
            try:
                self._download(track, folder)
                errors_in_row = 0
                self._on_event("done", track.title)
            except Exception as e:
                self._remove_partial()
                if isinstance(e, DownloadCancelled) or self._stop or self._cancel == track.id:
                    continue  # stopped on purpose (finally still runs)
                message = str(e).lower()
                reason = str(e).split(": ", 2)[-1]
                self.failed[track.id] = reason
                if any(s in message for s in THROTTLED):
                    self.paused = "YouTube is throttling"
                    self._on_event("paused", f"Offline downloads paused, YouTube is throttling: {reason}")
                elif any(s in message for s in UNAVAILABLE):
                    pass  # this video is gone or blocked; it gets a ✗, the rest carries on
                else:
                    errors_in_row += 1
                    if errors_in_row >= MAX_ERRORS_IN_ROW:
                        errors_in_row = 0
                        self.paused = "repeated errors"
                        self._on_event("paused", f"Offline downloads paused after repeated errors: {reason}")
                    else:
                        self._on_event("error", f"Could not download “{track.title}”: {reason}")
            finally:
                self.current = None
                self._cancel = None
                with self._lock:
                    self.version += 1
            self._wake.wait(PAUSE_BETWEEN)
            self._wake.clear()

    def _add_entry(self, track_id: str, folder: str, data: dict) -> None:
        """Record a finished track in the folder's index (this marks it complete)."""
        with self._lock:
            self.index.setdefault(folder, {})[track_id] = data
            self._save_index(folder)
            self.files.setdefault(track_id, {})[folder] = self.dir / folder / data["file"]
            self.meta[track_id] = data
            self.version += 1

    def _download(self, track: Track, folder: str) -> None:
        self.current = (track.id, folder)
        with self._lock:
            self.version += 1
        (self.dir / folder).mkdir(parents=True, exist_ok=True)

        def hook(_status: dict) -> None:
            if self._cancel == track.id or self._stop:
                raise DownloadCancelled()

        path = self.yt.download(track.id, self.dir / folder, file_name(track), offline_format(), hook)
        data = {"track": asdict(track), "file": path.name, "segments": sponsorblock(track.id)}
        self._add_entry(track.id, folder, data)

    def _link(self, track: Track, folder: str) -> None:
        src = next(iter(self.files[track.id].values()))
        dest = self.dir / folder / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.unlink(missing_ok=True)
            os.link(src, dest)  # same disk: no extra space
        except OSError:
            shutil.copy2(src, dest)
        self._add_entry(track.id, folder, {**self.meta[track.id], "file": src.name})
