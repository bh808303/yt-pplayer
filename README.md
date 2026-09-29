# yt-pplayer — YouTube Playlist Player

![yt-pplayer on Omarchy: Super+M opens the player, picks a track, then hides and shows it while the music keeps playing](docs/demo.gif)

*Super+M opens the player; pressing it again hides it while the music keeps
playing. ([Full-quality video](docs/demo.mp4))*

Terminal audio player for your YouTube playlists. Uses your Chromium login
(cookies read from the GNOME keyring), yt-dlp to list playlists and resolve
audio streams, and a headless mpv for playback — so no YouTube ads. Non-music
segments are skipped via SponsorBlock. mpv's MPRIS plugin makes media keys work.

## Install

On Arch / Omarchy, build the package from the PKGBUILD in this repo
(an AUR package is coming):

    git clone https://github.com/bh808303/yt-pplayer
    cd yt-pplayer/aur && makepkg -si

yt-pplayer reuses your YouTube login from Chromium. If it finds you logged out
(or the session went stale), press `l` to open YouTube in Chromium; once you're
logged in, yt-pplayer picks up the new login by itself.

From source, for development:

    ./setup.sh                          # venv on top of the system yt-dlp
    .venv/bin/yt-pplayer

Rerun `./setup.sh` after a Python upgrade breaks the venv.

## Keys

| Key            | Action                              |
|----------------|-------------------------------------|
| enter          | open playlist / play track          |
| space          | play/pause                          |
| n / b          | next / previous                     |
| r              | jump to a random track              |
| s              | toggle shuffle                      |
| v              | toggle volume normalization         |
| ← / →          | seek ±10s  (`,` / `.` = ±1 min)     |
| + / -          | volume                              |
| m              | mute / unmute                       |
| o              | keep the playlist offline           |
| /              | filter tracks (esc to close)        |
| tab            | switch pane                         |
| ctrl+r         | refresh playlists (re-reads cookies)|
| l              | log in to YouTube (when logged out) |
| q              | quit                                |

## Config (env vars)

- `YT_PPLAYER_BROWSER` — default `chromium`
- `YT_PPLAYER_KEYRING` — default `GNOMEKEYRING`
- `YT_PPLAYER_LOUDNESS` — volume normalization target in LUFS, default `-14`
  (YouTube's own level); `off` starts with normalization off
- `YT_PPLAYER_OFFLINE_DIR` — where offline playlists go, default
  `~/Music/yt-pplayer` (your XDG music folder)
- `YT_PPLAYER_OFFLINE_FORMAT` — format of offline tracks: `original` (default; as
  YouTube serves it, mostly webm/Opus), `m4a`, `webm`, or `mp3` (converted at
  192 kbps: plays everywhere, but slightly lower quality and ~20% larger).
  Tracks already downloaded keep their format
- `YT_PPLAYER_THEME` — a built-in Textual theme (e.g. `nord`) instead of following
  the Omarchy theme

By default the colors come from the active Omarchy theme
(`~/.local/state/omarchy/current/theme/colors.toml`) and follow
`omarchy theme set` live.

Playlist data is cached in `~/.cache/yt-pplayer/`. yt-dlp comes from pacman,
so if playback breaks after a YouTube change, run `omarchy update` (or
`sudo pacman -Syu yt-dlp`).

## Offline favourites

Press `o` to keep a playlist offline: the highlighted one in the playlist pane,
or the open one in the track list. yt-pplayer asks first, showing the estimated
size and the free space; it won't start if that would leave less than 1 GB free.
Tracks then download in the background, one at a time, while yt-pplayer runs:
`↓` marks what's still to come, `●` what's on disk, `✗` a track that failed
(retried when you next start yt-pplayer). Downloaded tracks play from disk, so
they start instantly and work without internet. An offline playlist stays in
sync: tracks you add on YouTube are downloaded, tracks you remove are deleted.

Press `o` again to stop keeping it offline; yt-pplayer asks whether to delete
the downloaded tracks or keep them (kept tracks are no longer synced; press `o`
on the playlist again to resume, then again to delete).

Each offline playlist gets its own folder named after it, e.g.
`~/Music/yt-pplayer/Oldschool Elektro/`, renamed along when you rename the
playlist on YouTube. Tracks are named `Artist - Title [video id].webm` (or
`.m4a`/`.mp3`, see `YT_PPLAYER_OFFLINE_FORMAT`) and tagged with title and
artist, so other players show them properly. A hidden `.yt-pplayer.json` in each
folder lists the finished tracks and their SponsorBlock skips. A track that's in
two offline playlists is hard-linked, so it takes disk space only once.
yt-pplayer only ever deletes files named like its own (ending in `[video id]`),
so other files in those folders are safe. Which playlists are kept offline is
stored in `~/.local/state/yt-pplayer/offline.json`.

## Omarchy: Super+M to show/hide

`yt-pplayer-toggle` (installed by the package, or `omarchy/yt-pplayer-toggle`
in this repo) starts the player on a hidden special workspace, and on later
presses shows or hides it — the music keeps playing while it's hidden.
Closing the window (Super+W) or pressing `q` stops it.

`~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + M", "YouTube Playlist Player", "yt-pplayer-toggle")
```

`~/.config/hypr/hyprland.lua`:

```lua
o.window("org.omarchy.yt-pplayer", { float = true })
o.window("org.omarchy.yt-pplayer", { center = true })
o.window("org.omarchy.yt-pplayer", { size = { 1100, 700 } })
o.window("org.omarchy.yt-pplayer", { workspace = "special:music" })
```

## Releasing

    ./release.sh 0.2.0 --dry-run   # rehearse: builds everything, publishes nothing
    ./release.sh 0.2.0             # bump, tag, pin PKGBUILD checksum, GitHub release
    ./release.sh 0.2.0 --aur       # ...and update the AUR package

## License

MIT — see [LICENSE](LICENSE).
