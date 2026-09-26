"""Terminal UI."""

from __future__ import annotations

import asyncio
import os
import random

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Input, OptionList, ProgressBar, Static
from textual.widgets.option_list import Option

from .player import Mpv
from .theme import load_omarchy_theme, theme_stamp
from .youtube import Playlist, Track, YouTube


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


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
        Binding("left", "seek(-10)", "-10s", priority=True, show=False),
        Binding("right", "seek(10)", "+10s", priority=True, show=False),
        Binding("comma", "seek(-60)", "-1m", show=False),
        Binding("full_stop", "seek(60)", "+1m", show=False),
        Binding("plus,equals_sign", "volume(5)", "Vol+", show=False),
        Binding("minus", "volume(-5)", "Vol-", show=False),
        Binding("slash", "search", "Search"),
        Binding("escape", "close_search", "Close search", show=False),
        Binding("ctrl+r", "refresh", "Refresh"),
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
        self.segments: list[tuple[float, float]] = []
        self.time_pos: float | None = None
        self.duration: float | None = None
        self.paused = False
        self.volume = 100.0
        self.loading = False
        self.errors_in_row = 0
        self.theme_stamp = 0.0
        self.theme_count = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield OptionList(id="playlists")
            with Vertical(id="right"):
                yield Input(placeholder="Filter tracks…", id="search")
                yield DataTable(id="tracks", cursor_type="row", zebra_stripes=True)
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
        self.sync_theme()
        if not os.environ.get("YT_PPLAYER_THEME"):
            self.set_interval(2, self.sync_theme)
        self.query_one("#playlists", OptionList).border_title = "Playlists"
        table = self.query_one("#tracks", DataTable)
        table.border_title = "Tracks"
        table.add_column(" ", key="mark", width=2)
        table.add_column("Title", key="title")  # width follows the terminal, see fill_table
        table.add_column("Channel", key="channel", width=24)
        table.add_column("Time", key="time", width=8)
        try:
            await self.mpv.start()
        except Exception as e:
            self.notify(f"Could not start mpv: {e}", severity="error", timeout=30)
        self.set_playlists(self.yt.cached_playlists())
        self.query_one("#playlists").focus()
        self.refresh_playlists()

    # -- loading ---------------------------------------------------------

    def set_playlists(self, playlists: list[Playlist]) -> None:
        if not playlists:
            return
        self.playlists = playlists
        ol = self.query_one("#playlists", OptionList)
        highlighted = ol.highlighted
        ol.clear_options()
        ol.add_options([Option(p.title, id=p.id) for p in playlists])
        if highlighted is not None and highlighted < len(playlists):
            ol.highlighted = highlighted

    @work(exclusive=True, group="playlists")
    async def refresh_playlists(self) -> None:
        self.sub_title = "refreshing playlists…"
        try:
            self.set_playlists(await asyncio.to_thread(self.yt.fetch_playlists))
        except Exception as e:
            self.notify(f"Could not load playlists (logged in to YouTube in Chromium?)\n{e}",
                        severity="error", timeout=20)
        finally:
            self.sub_title = ""

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
        self.query_one("#tracks").border_subtitle = "refreshing…"
        try:
            fresh = await asyncio.to_thread(self.yt.fetch_tracks, playlist)
        except Exception as e:
            self.notify(f"Could not load playlist: {e}", severity="error", timeout=10)
            fresh = cached
        if self.shown is playlist and [t.id for t in fresh] != [t.id for t in cached]:
            self.show_tracks(fresh)
        self.query_one("#tracks").border_subtitle = f"{len(self.tracks)} tracks"

    def show_tracks(self, tracks: list[Track]) -> None:
        self.tracks = tracks
        self.query_one("#tracks", DataTable).border_title = self.shown.title if self.shown else "Tracks"
        self.fill_table()

    def fill_table(self) -> None:
        table = self.query_one("#tracks", DataTable)
        needle = self.query_one("#search", Input).value.strip().lower()
        playing_id = self.queue[self.current].id if self.current is not None else None
        row = table.cursor_row
        # mark + channel + time columns, cell padding, border and scrollbar
        title_width = max(20, table.size.width - 2 - 24 - 8 - 8 - 4)
        table.clear()
        for i, t in enumerate(self.tracks):
            if needle and needle not in t.title.lower() and needle not in t.channel.lower():
                continue
            mark = "▶" if t.id == playing_id and self.queue_playlist is self.shown else ""
            title = t.title if len(t.title) <= title_width else t.title[: title_width - 1] + "…"
            channel = t.channel if len(t.channel) <= 24 else t.channel[:23] + "…"
            table.add_row(mark, title, channel, fmt_time(t.duration), key=str(i))
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))

    def on_resize(self) -> None:
        self.call_after_refresh(self.fill_table)

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
        # Let the arrow keys move the text cursor while typing a search.
        if action == "seek" and isinstance(self.focused, Input):
            return False
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
        self.fill_table()
        try:
            stream = await asyncio.wrap_future(self.yt.stream(track.id))
        except Exception as e:
            self.loading = False
            self.errors_in_row += 1
            self.notify(f"Can't play “{track.title}”: {e}", severity="warning", timeout=6)
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
        self.yt.stream(self.queue[self.order[(self.pos + 1) % len(self.order)]].id)
        if self.random_next is not None:
            self.yt.stream(self.queue[self.random_next].id)

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

    def action_refresh(self) -> None:
        self.refresh_playlists()
        if self.shown:
            self.open_playlist(self.shown, focus=False)

    async def action_quit(self) -> None:
        await self.mpv.stop()
        self.yt.shutdown()
        self.exit()

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
        if self.current is None:
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
            text.append(f"   vol {int(self.volume)}", style="dim")
        if self.shuffle:
            text.append("   🔀 shuffle", style=f"bold {self.current_theme.secondary}")
        self.query_one("#np-text", Static).update(text)
        bar = self.query_one("#progress", ProgressBar)
        total = self.duration or (self.queue[self.current].duration if self.current is not None else None)
        bar.update(total=total or 100, progress=self.time_pos or 0)


def main() -> None:
    YtPPlayer().run()
