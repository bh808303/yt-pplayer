"""Terminal UI."""

from __future__ import annotations

import asyncio
import os
import random
import signal

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from .offline import RESERVE_BYTES, Offline, estimate, fmt_size
from .player import Mpv
from .theme import load_omarchy_theme, theme_stamp
from .youtube import NotLoggedIn, Playlist, Track, YouTube


def loudness_target() -> float | None:
    """YT_PPLAYER_LOUDNESS: target in LUFS (default -14, like YouTube), or "off"."""
    value = os.environ.get("YT_PPLAYER_LOUDNESS", "-14").strip().lower()
    if value == "off":
        return None
    try:
        return min(-5.0, max(-70.0, float(value)))  # the range loudnorm accepts
    except ValueError:
        return -14.0


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


class TrackTable(DataTable):
    """DataTable that asks the app to re-layout its columns once its own size changed."""

    def on_resize(self) -> None:
        self.app.fill_table()


class Choice(ModalScreen[str | None]):
    """A question answered with one key: a key in choices returns its value, any other
    key cancels (None), so a stray or repeated key press never does anything."""

    DEFAULT_CSS = """
    Choice { align: center middle; }
    Choice > Static { width: 72; max-width: 90%; height: auto; padding: 1 2;
                      border: round $accent; background: $surface; }
    """

    def __init__(self, message: str, choices: dict[str, str], hint: str) -> None:
        super().__init__()
        self.message, self.choices, self.hint = message, choices, hint

    def compose(self) -> ComposeResult:
        text = Text(self.message + "\n\n")
        text.append(self.hint, style="dim")
        yield Static(text)

    def on_key(self, event) -> None:
        event.stop()
        event.prevent_default()
        self.dismiss(self.choices.get(event.key))


class YtPPlayer(App):
    TITLE = "yt-pplayer"
    CSS = """
    #main { height: 1fr; }
    #playlists { width: 32; height: 1fr; border: round $pp-muted; }
    #playlists:focus-within { border: round $accent; }
    #right { width: 1fr; }
    #tracks { height: 1fr; border: round $pp-muted; }
    #tracks:focus { border: round $accent; }
    #search { display: none; }
    #search.visible { display: block; }
    #nowplaying { height: 5; padding: 0 1; border: round $pp-muted; }
    #np-text { height: 2; }
    #progress Bar { width: 1fr; }
    #progress { width: 1fr; }
    """
    BINDINGS = [
        Binding("space", "pause", "Play/Pause"),
        Binding("n", "next", "Next"),
        Binding("b", "prev", "Prev"),
        Binding("r", "random", "Random"),
        Binding("s", "shuffle", "Shuffle"),
        Binding("v", "normalize", "Normalize"),
        Binding("left", "seek(-10)", "-10s", priority=True, show=False),
        Binding("right", "seek(10)", "+10s", priority=True, show=False),
        Binding("comma", "seek(-60)", "-1m", show=False),
        Binding("full_stop", "seek(60)", "+1m", show=False),
        Binding("plus,equals_sign", "volume(5)", "Vol+", show=False),
        Binding("minus", "volume(-5)", "Vol-", show=False),
        Binding("m", "mute", "Mute"),
        Binding("o", "offline", "Offline"),
        Binding("slash", "search", "Search"),
        Binding("escape", "close_search", "Close search", show=False),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("l", "login", "Log in"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.yt = YouTube()
        self.mpv = Mpv(self.on_mpv_event)
        self.playlists: list[Playlist] = []
        self.shown: Playlist | None = None     # playlist in the track table
        self.tracks: list[Track] = []           # tracks of the shown playlist
        self.queue: list[Track] = []            # tracks being played
        self.queue_playlist: Playlist | None = None
        self.order: list[int] = []              # play order (indices into queue)
        self.pos = 0                            # position in order
        self.history: list[int] = []
        self.current: int | None = None         # index into queue
        self.random_next: int | None = None     # pre-picked (and pre-fetched) random jump
        self.shuffle = False
        self.loudness = loudness_target()       # LUFS target when normalizing
        self.normalize = self.loudness is not None
        self.segments: list[tuple[float, float]] = []
        self.time_pos: float | None = None
        self.duration: float | None = None
        self.paused = False
        self.volume = 100.0
        self.muted = False
        self.loading = False
        self.errors_in_row = 0
        self.theme_stamp = 0.0
        self.theme_count = 0
        self.logged_out = False
        self.cookie_stamp = 0.0
        self.offline: Offline | None = None     # started in on_mount
        self.offline_version = -1
        self.tracks_note = ""                   # e.g. "refreshing…", in the tracks border

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield OptionList(id="playlists")
            with Vertical(id="right"):
                yield Input(placeholder="Filter tracks…", id="search")
                yield TrackTable(id="tracks", cursor_type="row", zebra_stripes=True)
        with Vertical(id="nowplaying"):
            yield Static("Nothing playing — pick a playlist, then a track (or press r)", id="np-text")
            yield ProgressBar(id="progress", show_eta=False, show_percentage=False)
        yield Footer()

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Used when the Omarchy theme can't be read (or YT_PPLAYER_THEME picks a built-in one).
        return {"pp-muted": "#555555"}

    def sync_theme(self) -> None:
        """Follow the active Omarchy theme, including live `omarchy theme set` switches."""
        if os.environ.get("YT_PPLAYER_THEME"):
            self.theme = os.environ["YT_PPLAYER_THEME"]
            return
        stamp = theme_stamp()
        if stamp == self.theme_stamp:
            return
        self.theme_stamp = stamp
        # A fresh name per load makes Textual re-apply the theme even if only colors changed.
        self.theme_count += 1
        theme = load_omarchy_theme(f"omarchy-{self.theme_count}")
        if theme is not None:
            self.register_theme(theme)
            self.theme = theme.name
            self.update_now_playing()

    async def on_mount(self) -> None:
        self.offline = Offline(self.yt, self.on_offline_event)
        # Closing the terminal window sends SIGHUP; quit properly instead of dying mid-playback.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.action_quit()))
        self.sync_theme()
        if not os.environ.get("YT_PPLAYER_THEME"):
            self.set_interval(2, self.sync_theme)
        self.query_one("#playlists", OptionList).border_title = "Playlists"
        table = self.query_one("#tracks", DataTable)
        table.border_title = "Tracks"
        try:
            await self.mpv.start()
            await self.mpv.set_loudness(self.loudness if self.normalize else None)
        except Exception as e:
            self.notify(f"Could not start mpv: {e}", severity="error", timeout=30)
        self.set_playlists(self.yt.cached_playlists())
        self.query_one("#playlists").focus()
        self.refresh_playlists()
        self.set_interval(2, self.watch_login)
        self.set_interval(1, self.update_offline)

    # -- loading ---------------------------------------------------------

    def set_playlists(self, playlists: list[Playlist]) -> None:
        if not playlists:
            return
        self.playlists = playlists
        if self.offline:
            self.offline.rename(playlists)  # renamed on YouTube: rename the folder too
        ol = self.query_one("#playlists", OptionList)
        highlighted = ol.highlighted
        ol.clear_options()
        ol.add_options([Option(self.playlist_label(p), id=p.id) for p in playlists])
        # Highlight something from the start, so enter and o work right away.
        ol.highlighted = highlighted if highlighted is not None and highlighted < len(playlists) else 0

    def playlist_label(self, playlist: Playlist) -> Text:
        progress = self.offline.playlist_progress(playlist) if self.offline else None
        if progress is None:
            return Text(playlist.title)
        done, total = progress
        return Text.assemble(("● " if done == total else "↓ ", "bold"), playlist.title)

    @work(exclusive=True, group="playlists")
    async def refresh_playlists(self, reload_cookies: bool = False) -> None:
        self.sub_title = "refreshing playlists…"
        try:
            if reload_cookies:
                await asyncio.to_thread(self.yt.reload_cookies)
            self.set_playlists(await asyncio.to_thread(self.yt.fetch_playlists))
        except NotLoggedIn:
            if not self.logged_out:
                self.notify("Not logged in to YouTube. Press l to log in in the browser; "
                            "yt-pplayer picks the login up by itself.", severity="warning", timeout=20)
            self.set_logged_out(True)
        except Exception as e:
            reason = str(e).removeprefix("ERROR: ")
            self.notify(f"Could not load playlists: {reason}", severity="error", timeout=20)
        else:
            if self.logged_out:
                self.notify("Logged in to YouTube")
            self.set_logged_out(False)
        finally:
            self.sub_title = ""

    def set_logged_out(self, logged_out: bool) -> None:
        self.logged_out = logged_out
        self.cookie_stamp = self.yt.cookie_stamp()
        self.refresh_bindings()  # show/hide "l Log in" in the footer
        self.update_now_playing()

    def watch_login(self) -> None:
        """While logged out, retry as soon as the browser writes new cookies."""
        if not self.logged_out:
            return
        stamp = self.yt.cookie_stamp()
        if stamp != self.cookie_stamp:
            self.cookie_stamp = stamp
            self.refresh_playlists(reload_cookies=True)

    def action_login(self) -> None:
        try:
            self.yt.open_login()
        except OSError as e:
            self.notify(f"Could not open the browser: {e}", severity="error")
            return
        self.notify("Log in to YouTube in the browser; yt-pplayer continues once you're in.", timeout=10)

    @on(OptionList.OptionSelected, "#playlists")
    def playlist_selected(self, event: OptionList.OptionSelected) -> None:
        playlist = next(p for p in self.playlists if p.id == event.option.id)
        self.open_playlist(playlist)

    @work(exclusive=True, group="tracks")
    async def open_playlist(self, playlist: Playlist, focus: bool = True) -> None:
        self.shown = playlist
        cached = self.yt.cached_tracks(playlist)
        self.show_tracks(cached)
        if focus:
            self.query_one("#tracks").focus()
        self.tracks_note = "refreshing…"
        self.update_offline(force=True)
        try:
            fresh = await asyncio.to_thread(self.yt.fetch_tracks, playlist)
        except Exception as e:
            self.notify(f"Could not load playlist: {e}", severity="error", timeout=10)
            fresh = cached
        if self.shown is playlist and [t.id for t in fresh] != [t.id for t in cached]:
            self.show_tracks(fresh)
            if self.offline and playlist.id in self.offline.playlists:
                self.offline.refresh()  # tracks added/removed on YouTube
        self.tracks_note = ""
        self.update_offline(force=True)

    def show_tracks(self, tracks: list[Track]) -> None:
        self.tracks = tracks
        self.query_one("#tracks", DataTable).border_title = self.shown.title if self.shown else "Tracks"
        self.fill_table()

    def fill_table(self, follow: bool = False) -> None:
        """Redraw the track table; with follow, move the cursor to the playing track."""
        table = self.query_one("#tracks", DataTable)
        needle = self.query_one("#search", Input).value.strip().lower()
        playing_id = self.queue[self.current].id if self.current is not None else None
        row = table.cursor_row
        same_playlist = (self.queue_playlist is not None and self.shown is not None
                         and self.queue_playlist.id == self.shown.id)
        # Room left after border (2), scrollbar (2), cell padding (4 x 2), mark (2), time (7).
        text_width = max(12, table.size.width - 21)
        channel_width = min(24, max(7, text_width * 3 // 10))  # 7 = "Channel" header
        title_width = text_width - channel_width
        # Recreate the columns: DataTable never shrinks a column once content widened it.
        table.clear(columns=True)
        table.add_column(" ", key="mark", width=2)
        table.add_column("Title", key="title")
        table.add_column("Channel", key="channel")
        table.add_column("Time", key="time", width=7)
        for i, t in enumerate(self.tracks):
            if needle and needle not in t.title.lower() and needle not in t.channel.lower():
                continue
            playing = same_playlist and t.id == playing_id
            if playing and follow:
                row = table.row_count
            offline = self.offline.status(t.id) if self.offline else ""
            if playing:
                mark = Text("▶")
            elif offline == "failed":
                mark = Text("✗", style=f"bold {self.current_theme.error}")
            else:
                mark = Text({"done": "●", "wanted": "↓"}.get(offline, ""))
            title = Text(t.title, no_wrap=True)
            title.truncate(title_width, overflow="ellipsis")
            channel = Text(t.channel, no_wrap=True)
            channel.truncate(channel_width, overflow="ellipsis")
            table.add_row(mark, title, channel, fmt_time(t.duration), key=str(i))
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    # -- search ----------------------------------------------------------

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.focus()

    def action_close_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.has_class("visible"):
            search.value = ""
            search.remove_class("visible")
            self.fill_table()
            self.query_one("#tracks").focus()

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        self.fill_table()

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self.query_one("#tracks").focus()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # While a question is open, only its own keys count.
        if isinstance(self.screen, Choice):
            return False
        # Let the arrow keys move the text cursor while typing a search.
        if action == "seek" and isinstance(self.focused, Input):
            return False
        if action == "login":
            return self.logged_out
        return True

    # -- playback control ------------------------------------------------

    @on(DataTable.RowSelected, "#tracks")
    def track_selected(self, event: DataTable.RowSelected) -> None:
        self.start_queue(int(event.row_key.value))

    def start_queue(self, index: int) -> None:
        self.queue = list(self.tracks)
        self.queue_playlist = self.shown
        self.history.clear()
        self.build_order(index)
        self.play(index)

    def build_order(self, current: int) -> None:
        self.order = list(range(len(self.queue)))
        if self.shuffle:
            random.shuffle(self.order)
            self.order.remove(current)
            self.order.insert(0, current)
        self.pos = self.order.index(current)

    def play(self, index: int, record: bool = True) -> None:
        if record and self.current is not None:
            self.history.append(self.current)
        self.current = index
        self.pos = self.order.index(index)
        self.pick_random_next()
        self.load_current()

    @work(exclusive=True, group="play")
    async def load_current(self) -> None:
        index = self.current
        track = self.queue[index]
        self.loading = True
        self.segments = []
        self.time_pos = self.duration = None
        self.update_now_playing()
        self.fill_table(follow=True)
        local = self.offline.local(track.id) if self.offline else None
        if local is not None:
            path, self.segments = local
            await self.mpv.play(str(path), {}, f"{track.title} — {track.channel}")
            self.errors_in_row = 0
            self.prefetch()
            return
        try:
            stream = await asyncio.wrap_future(self.yt.stream(track.id))
        except Exception as e:
            self.loading = False
            self.errors_in_row += 1
            # yt-dlp messages look like "ERROR: [youtube] <id>: Video unavailable".
            reason = str(e).rsplit(": ", 1)[-1]
            self.notify(f"Skipped “{track.title}”: {reason}", severity="warning", timeout=6)
            if self.errors_in_row < 5 and self.current == index:
                self.play(self.order[(self.pos + 1) % len(self.order)], record=False)
            return
        if self.current != index:
            return  # user skipped while we were resolving
        self.segments = stream.segments
        await self.mpv.play(stream.url, stream.headers, f"{track.title} — {track.channel}")
        self.errors_in_row = 0
        self.prefetch()

    def prefetch(self) -> None:
        """Resolve the likely next tracks in the background so skipping is instant."""
        if not self.order:
            return
        nexts = [self.order[(self.pos + 1) % len(self.order)], self.random_next]
        for index in (i for i in nexts if i is not None):
            track_id = self.queue[index].id
            if not (self.offline and self.offline.local(track_id)):
                self.yt.stream(track_id)

    def pick_random_next(self) -> None:
        choices = [i for i in range(len(self.queue)) if i != self.current]
        self.random_next = random.choice(choices) if choices else None

    def action_next(self) -> None:
        if self.order:
            self.play(self.order[(self.pos + 1) % len(self.order)])

    async def action_prev(self) -> None:
        if not self.order:
            return
        if (self.time_pos or 0) > 3:
            await self.mpv.command("seek", 0, "absolute")
        elif self.history:
            self.play(self.history.pop(), record=False)
        else:
            self.play(self.order[(self.pos - 1) % len(self.order)], record=False)

    def action_random(self) -> None:
        if not self.queue:
            if not self.tracks:
                self.notify("Open a playlist first")
                return
            self.queue = list(self.tracks)
            self.queue_playlist = self.shown
            self.current = None
            self.build_order(random.randrange(len(self.queue)))
            self.play(self.order[self.pos])
        elif self.random_next is not None:
            self.play(self.random_next)

    def action_shuffle(self) -> None:
        self.shuffle = not self.shuffle
        if self.current is not None:
            self.build_order(self.current)
            self.prefetch()
        self.update_now_playing()

    async def action_normalize(self) -> None:
        self.normalize = not self.normalize
        if self.loudness is None:
            self.loudness = -14.0  # turned on although YT_PPLAYER_LOUDNESS=off
        await self.mpv.set_loudness(self.loudness if self.normalize else None)
        self.notify(f"Volume normalization on ({self.loudness:g} LUFS)" if self.normalize
                    else "Volume normalization off", timeout=3)
        self.update_now_playing()

    async def action_pause(self) -> None:
        if self.current is not None:
            await self.mpv.command("cycle", "pause")
        else:
            self.action_random()

    async def action_seek(self, seconds: int) -> None:
        if self.current is not None:
            await self.mpv.command("seek", seconds, "relative")

    async def action_volume(self, delta: int) -> None:
        await self.mpv.command("add", "volume", delta)

    async def action_mute(self) -> None:
        await self.mpv.command("cycle", "mute")

    def action_refresh(self) -> None:
        self.refresh_playlists(reload_cookies=True)
        if self.shown:
            self.open_playlist(self.shown, focus=False)

    async def action_quit(self) -> None:
        if self.offline:
            await asyncio.to_thread(self.offline.shutdown)  # aborts and removes a partial download
        await self.mpv.stop()
        self.yt.shutdown()
        self.exit()

    # -- offline ---------------------------------------------------------

    def on_offline_event(self, kind: str, message: str) -> None:
        """Called from the download thread."""
        if kind in ("paused", "error"):
            severity = "warning" if kind == "paused" else "information"
            try:
                self.call_from_thread(self.notify, message, severity=severity, timeout=8)
            except RuntimeError:
                pass  # app not running (yet, or any more)

    def update_offline(self, force: bool = False) -> None:
        """Refresh offline markers and download progress (polled; cheap when unchanged)."""
        off = self.offline
        if off is None:
            return
        if off.version != self.offline_version or force:
            self.offline_version = off.version
            ol = self.query_one("#playlists", OptionList)
            for p in self.playlists:
                ol.replace_option_prompt(p.id, self.playlist_label(p))
            self.fill_table()
        done, total = off.progress()
        if off.paused and done < total:
            status = f"offline {done}/{total} · paused"
        elif off.current:
            status = f"↓ offline {done}/{total}"
        else:
            status = f"offline {done}/{total}" if total else ""
        if failed := off.failed_count():
            status += f" · {failed} failed"
        self.query_one("#playlists").border_subtitle = status
        parts = [self.tracks_note or f"{len(self.tracks)} tracks"]
        if self.shown and (progress := off.playlist_progress(self.shown)):
            parts.append(f"offline {progress[0]}/{progress[1]}")
            if failed := off.failed_count(self.shown):
                parts.append(f"✗ {failed} failed")
        self.query_one("#tracks").border_subtitle = " · ".join(parts)

    def ask(self, message: str, choices: dict[str, str], hint: str, then) -> None:
        self.push_screen(Choice(message, choices, hint), then)

    def space_problem(self, tracks: list[Track]) -> tuple[str, str]:
        """(summary, problem) for downloading tracks; problem is "" if they fit."""
        need, unknown = estimate(tracks)
        free = self.offline.free_space()
        summary = f"about {fmt_size(need)}"
        if unknown:
            summary += f" (+{unknown} of unknown size)"
        if free is None:
            return summary, (f"The offline folder {self.offline.dir} can't be reached "
                             "(external disk not connected?).")
        if free - need < RESERVE_BYTES:
            return summary, (f"Not enough space: needs {summary}, {fmt_size(free)} free "
                             f"(keeps {fmt_size(RESERVE_BYTES)} spare). Free up space or set "
                             "YT_PPLAYER_OFFLINE_DIR.")
        return f"{summary} — {fmt_size(free)} free", ""

    def action_offline(self) -> None:
        """o keeps a whole playlist offline: the highlighted one, or the open one."""
        if self.offline is None:
            return
        if isinstance(self.focused, OptionList):
            index = self.focused.highlighted
            if index is not None and index < len(self.playlists):
                self.offline_playlist(self.playlists[index])
        elif self.shown:
            self.offline_playlist(self.shown)

    @work(exclusive=True, group="offline")
    async def offline_playlist(self, playlist: Playlist) -> None:
        off = self.offline
        tracks = self.yt.cached_tracks(playlist)
        if playlist.id in off.playlists:
            done = off.downloaded_in(playlist)
            if not done:
                off.remove_playlist(playlist, keep=False)
                self.update_offline(force=True)
                self.notify(f"“{playlist.title}” is no longer kept offline")
                return
            def answer(choice: str | None) -> None:
                if choice:
                    off.remove_playlist(playlist, keep=choice == "keep")
                    self.update_offline(force=True)
                    self.notify(f"“{playlist.title}” is no longer kept offline"
                                + ("; downloaded tracks kept" if choice == "keep" else ""))
            self.ask(f"Stop keeping “{playlist.title}” offline? {len(done)} downloaded "
                     f"track{'s' if len(done) != 1 else ''} ({fmt_size(off.size_in(playlist))}).\n"
                     "Tracks that are also in another offline playlist stay.",
                     {"d": "delete", "k": "keep"}, "d delete them · k keep them · esc cancel", answer)
            return
        if not self.yt.has_cached_tracks(playlist):
            self.notify(f"Loading “{playlist.title}”…", timeout=3)
            try:
                tracks = await asyncio.to_thread(self.yt.fetch_tracks, playlist)
            except Exception as e:
                self.notify(f"Could not load playlist: {e}", severity="error", timeout=10)
                return
        missing = off.missing(tracks)
        summary, problem = self.space_problem(missing)
        if problem:
            self.ask(f"Can't keep “{playlist.title}” offline. {problem}", {}, "esc close", None)
            return
        def answer(choice: str | None) -> None:
            if choice:
                off.add_playlist(playlist)
                self.update_offline(force=True)
        count = f"{len(missing)} track{'s' if len(missing) != 1 else ''}"
        self.ask(f"Keep “{playlist.title}” offline? {count} to download, {summary}.",
                 {"enter": "yes"}, "enter download · esc cancel", answer)

    # -- mpv events ------------------------------------------------------

    def on_mpv_event(self, msg: dict) -> None:
        event = msg["event"]
        if event == "property-change":
            name, value = msg.get("name"), msg.get("data")
            if name == "time-pos":
                self.time_pos = value
                self.skip_segments()
            elif name == "duration":
                self.duration = value
            elif name == "pause":
                self.paused = bool(value)
            elif name == "volume" and value is not None:
                self.volume = value
            elif name == "mute":
                self.muted = bool(value)
            self.update_now_playing()
        elif event == "file-loaded":
            self.loading = False
            self.update_now_playing()
        elif event == "end-file":
            reason = msg.get("reason")
            if reason == "eof":
                self.action_next()
            elif reason == "error" and not self.loading:
                self.notify("Playback error, skipping", severity="warning")
                self.action_next()

    def skip_segments(self) -> None:
        pos = self.time_pos
        if pos is None:
            return
        for start, end in self.segments:
            if start <= pos < end - 1:
                asyncio.create_task(self.mpv.command("seek", end, "absolute"))
                self.notify("Skipped non-music segment (SponsorBlock)", timeout=3)
                break

    def update_now_playing(self) -> None:
        text = Text()
        if self.current is None and self.logged_out:
            text.append("Not logged in to YouTube — press l to log in in the browser", style="bold")
        elif self.current is None:
            text.append("Nothing playing — pick a playlist, then a track (or press r)", style="dim")
        else:
            track = self.queue[self.current]
            state = "…" if self.loading else ("⏸" if self.paused else "▶")
            text.append(f"{state} ", style="bold")
            text.append(track.title, style="bold")
            if track.channel:
                text.append(f"  {track.channel}", style="dim")
            text.append("\n")
            text.append(f"{fmt_time(self.time_pos)} / {fmt_time(self.duration or track.duration)}")
            if self.queue_playlist:
                text.append(f"   {self.queue_playlist.title} [{self.pos + 1}/{len(self.order)}]", style="dim")
            if self.muted:
                text.append("   🔇 muted", style=f"bold {self.current_theme.secondary}")
            else:
                text.append(f"   vol {int(self.volume)}", style="dim")
            if self.normalize:
                text.append(f"   norm {self.loudness:g} LUFS", style="dim")
        if self.shuffle:
            text.append("   🔀 shuffle", style=f"bold {self.current_theme.secondary}")
        self.query_one("#np-text", Static).update(text)
        bar = self.query_one("#progress", ProgressBar)
        total = self.duration or (self.queue[self.current].duration if self.current is not None else None)
        bar.update(total=total or 100, progress=self.time_pos or 0)


def main() -> None:
    YtPPlayer().run()
