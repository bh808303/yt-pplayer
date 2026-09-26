"""Build a Textual theme from the active Omarchy theme's colors.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from textual.theme import Theme

OMARCHY_CURRENT = Path.home() / ".local/state/omarchy/current"
COLORS = OMARCHY_CURRENT / "theme" / "colors.toml"
# omarchy-theme-set rewrites this file on every switch, so its mtime is a cheap change signal.
THEME_NAME = OMARCHY_CURRENT / "theme.name"


def theme_stamp() -> float:
    try:
        return THEME_NAME.stat().st_mtime
    except OSError:
        return 0.0


def load_omarchy_theme(name: str) -> Theme | None:
    try:
        c = tomllib.loads(COLORS.read_text())
    except (OSError, ValueError):
        return None
    try:
        return Theme(
            name=name,
            dark=c.get("mode", "dark") != "light",
            primary=c["accent"],
            secondary=c.get("magenta", c["accent"]),
            accent=c["accent"],
            warning=c.get("yellow", c["accent"]),
            error=c.get("red", c["accent"]),
            success=c.get("green", c["accent"]),
            foreground=c["foreground"],
            background=c["background"],
            surface=c.get("lighter_background", c["background"]),
            panel=c.get("dark_background", c["background"]),
            variables={
                "pp-muted": c.get("muted", c.get("dark_foreground", c["foreground"])),
                "block-cursor-background": c["accent"],
                "block-cursor-foreground": c["background"],
                "block-cursor-blurred-background": c.get("selection", c["accent"]),
                "block-cursor-blurred-foreground": c["foreground"],
                "footer-key-foreground": c["accent"],
                "input-selection-background": c.get("selection", c["accent"]),
            },
        )
    except (KeyError, ValueError):
        return None
