"""Visual identity: palettes, glyphs, the demon registry and how a theme is chosen.

Pure data and small helpers, no rich import, so any front end (inline, plain, a future full-screen app) can use it.
A background colour is never set: the terminal keeps its own.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Mapping

THEME_NAMES = ("dark", "light")


@dataclass(frozen=True)
class Palette:
    laplace: str
    brillouin: str
    maxwell: str
    loschmidt: str
    descartes: str
    success: str
    warning: str
    error: str
    text: str
    dim: str
    line: str


DARK = Palette(
    laplace="#7AA2F7", brillouin="#7DCFFF", maxwell="#FF9E64", loschmidt="#BB9AF7", descartes="#F38BA8",
    success="#9ECE6A", warning="#E5C07B", error="#FF6B6B", text="#E6E9F5", dim="#8A90B0", line="#3B4266",
)  # fmt: skip
LIGHT = Palette(
    laplace="#2E5EAA", brillouin="#0E7C86", maxwell="#B85C00", loschmidt="#7A4FC2", descartes="#B4235A",
    success="#3F7D20", warning="#9A6B00", error="#C62828", text="#1F2340", dim="#5E6385", line="#C9CCE0",
)  # fmt: skip
PALETTES: dict[str, Palette] = {"dark": DARK, "light": LIGHT}


@dataclass(frozen=True)
class Demon:
    role: str  # internal role key; config keys stay unchanged
    name: str
    glyph: str
    ascii: str
    color: str  # attribute name on Palette


DEMONS: dict[str, Demon] = {
    "planner": Demon("planner", "Laplace", "✦", "*", "laplace"),
    "scout": Demon("scout", "Brillouin", "◎", "o", "brillouin"),
    "builder": Demon("builder", "Maxwell", "◆", "#", "maxwell"),
    "reviewer": Demon("reviewer", "Loschmidt", "↺", "<", "loschmidt"),
    "repair": Demon("repair", "Descartes", "✚", "+", "descartes"),
}

# symbol -> (unicode, ascii, palette attribute)
SYMBOLS: dict[str, tuple[str, str, str]] = {
    "ok": ("✓", "[ok]", "success"),
    "warn": ("!", "[!]", "warning"),
    "err": ("✗", "[x]", "error"),
    "star_done": ("✦", "*", "success"),
    "star_run": ("✧", "@", "text"),
    "star_pending": ("·", ".", "dim"),
    "line": ("─", "-", "line"),
}


def all_glyphs() -> str:
    return "".join(d.glyph for d in DEMONS.values()) + "".join(u for u, _, _ in SYMBOLS.values())


def can_encode(stream=None) -> bool:
    """True when the stream's encoding can write every glyph we use."""
    stream = stream if stream is not None else sys.stdout
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        all_glyphs().encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Theme:
    """A resolved theme: palette plus the colour and ASCII switches."""

    def __init__(self, name: str = "dark", color: bool = True, ascii_: bool = False):
        if name not in PALETTES:
            raise ValueError(f"unknown theme {name!r}; valid: {', '.join(THEME_NAMES)}")
        self.name, self.color, self.ascii = name, color, ascii_
        self.palette = PALETTES[name]

    def hex(self, attr: str) -> str:
        return getattr(self.palette, attr)

    def style(self, attr: str, bold: bool = False) -> str:
        """A rich style string for a palette attribute; empty (no colour) under NO_COLOR."""
        if not self.color:
            return "dim" if attr == "dim" else ("bold" if bold else "")
        return ("bold " if bold else "") + self.hex(attr)

    # demons
    def demon(self, role: str) -> Demon:
        return DEMONS[role]

    def demon_glyph(self, role: str) -> str:
        d = DEMONS[role]
        return d.ascii if self.ascii else d.glyph

    def demon_style(self, role: str, bold: bool = False) -> str:
        return self.style(DEMONS[role].color, bold)

    # symbols
    def sym(self, key: str) -> str:
        u, a, _ = SYMBOLS[key]
        return a if self.ascii else u

    def sym_style(self, key: str) -> str:
        return self.style(SYMBOLS[key][2])


def demon_name(role: str) -> str:
    d = DEMONS.get(role)
    return d.name if d else role


def demon_label(role: str, first: bool = False) -> str:
    """'Maxwell', or 'Maxwell (builder)' on first mention. Unknown roles come back unchanged."""
    d = DEMONS.get(role)
    if d is None:
        return role
    return f"{d.name} ({role})" if first else d.name


def resolve_theme(
    cli_theme: str | None = None,
    cli_ascii: bool = False,
    cfg_theme: str | None = None,
    env: Mapping[str, str] | None = None,
    stream=None,
) -> Theme:
    """--theme > ORCH_THEME > config ui.theme > dark. NO_COLOR drops colour; ASCII with --ascii or an unfit encoding."""
    env = os.environ if env is None else env
    name = cli_theme or env.get("ORCH_THEME") or cfg_theme or "dark"
    name = name.strip().lower()
    if name not in PALETTES:
        raise ValueError(f"unknown theme {name!r}; valid: {', '.join(THEME_NAMES)}")
    color = not env.get("NO_COLOR")  # https://no-color.org: any non-empty value
    return Theme(name, color=color, ascii_=cli_ascii or not can_encode(stream))


_current: Theme | None = None


def set_theme(theme: Theme | None) -> None:
    global _current
    _current = theme


def current_theme() -> Theme:
    """The theme chosen by the CLI callback, or a default resolved from env when none was set (tests, library use)."""
    return _current if _current is not None else resolve_theme()
