"""Theme constants and reusable styling values for the GUI."""

from typing import Dict

BACKGROUND = "#0F0F0F"
PANEL = "#1A1A1A"
GOLD = "#D4AF37"
GOLD_ALT = "#FFD700"
GOLD_SECONDARY = "#B8860B"
TEXT = "#FFFFFF"
TEXT_SOFT = "#E8D5A3"
HOVER = "#FFDF4D"
SHADOW = "#0A0A0A"

FONT_HEADING = ("Segoe UI", 18, "bold")
FONT_SUBTITLE = ("Segoe UI", 12)
FONT_BODY = ("Segoe UI", 11)

BUTTON_STYLE: Dict[str, str] = {
    "fg_color": GOLD,
    "hover_color": GOLD_ALT,
    "text_color": BACKGROUND,
    "corner_radius": 14,
}
