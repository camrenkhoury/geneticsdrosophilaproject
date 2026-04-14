from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    background: str = "#f8faf9"
    surface: str = "#f8faf9"
    surface_low: str = "#f2f4f3"
    surface_high: str = "#e6e9e8"
    surface_highest: str = "#e1e3e2"
    surface_lowest: str = "#ffffff"
    primary: str = "#3c6751"
    primary_container: str = "#7ba88f"
    primary_fixed_dim: str = "#a3d1b6"
    secondary_container: str = "#d0e5d7"
    secondary_fixed: str = "#d3e7da"
    text: str = "#191c1c"
    text_muted: str = "#414943"
    outline: str = "#c1c8c0"
    error: str = "#ba1a1a"
    error_container: str = "#ffdad6"
    success: str = "#3c6751"
    success_container: str = "#d3e7da"
    warning_container: str = "#f4e8cf"
    warning_text: str = "#6b4f1d"
    black: str = "#000000"


PALETTE = Palette()

TITLE_FONT = ("Inter", 28, "bold")
HEADLINE_FONT = ("Inter", 20, "bold")
SECTION_FONT = ("Inter", 16, "bold")
BODY_FONT = ("Inter", 11)
LABEL_FONT = ("Inter", 9, "bold")
MONO_FONT = ("Courier New", 10)
BIG_COUNT_FONT = ("Inter", 34, "bold")
CHIP_FONT = ("Inter", 9, "bold")
BUTTON_FONT = ("Inter", 12, "bold")
